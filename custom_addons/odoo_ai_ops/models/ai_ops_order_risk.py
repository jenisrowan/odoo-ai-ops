# -*- coding: utf-8 -*-
"""Order-risk gatekeeper.

This is the decision point described in the architecture's "Path 2: Fraud
Detection" flow. The FastAPI agent polls the Shopify
``orders/risk_assessment_changed`` webhook off SQS and forwards the payload here.
That assessment arrives *after* the order was placed (Shopify's fraud analysis is
asynchronous) and carries no order total, so we correlate it back to the
``sale.order`` imported on ``orders/create`` to recover the total. Odoo then
decides:

* **Cheap + risky -> reject for free.** If the order is below the configured
  threshold (default < $10) *and* Shopify flagged it medium/high risk, cancel it
  directly in Shopify. No LLM tokens are spent.
* **Otherwise -> escalate to the agent.** Open an ``ai.ops.task`` and dispatch the
  LangGraph fraud workflow over REST.

Implemented as an ``AbstractModel`` service so it carries no table of its own and
is easy to call from the controller (``env['ai.ops.order.risk'].process_webhook``)
and to unit-test.
"""

import json
import logging

from odoo import api, models

_logger = logging.getLogger(__name__)

# Risk levels (normalised) that the cheap-order rule treats as "risky".
RISKY_LEVELS = {"medium", "high"}

# The lowercase JSON form of Shopify's RiskAssessmentResult enum
# (NONE/LOW/MEDIUM/HIGH/PENDING) - the only values orders/risk_assessment_changed
# delivers. "pending" means the assessment is still running, so it maps to
# "none": act only on a delivered verdict. The dedup guard explicitly lets a
# later risky assessment escalate a recorded benign one, so nothing is lost by
# waiting. Unknown values also normalise to "none" (escalate-on-next-verdict
# beats guessing).
_RISK_ALIASES = {
    "none": "none",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "pending": "none",
}


class AiOpsOrderRisk(models.AbstractModel):
    _name = "ai.ops.order.risk"
    _description = "AI Ops Order Risk Gatekeeper"

    # ------------------------------------------------------------------
    # Payload normalisation
    # ------------------------------------------------------------------
    @api.model
    def _normalize_risk(self, value):
        if value is None:
            return "none"
        return _RISK_ALIASES.get(str(value).strip().lower(), "none")

    @api.model
    def _extract(self, payload):
        """Pull the order identity + verdict out of the risk webhook body.

        The real ``orders/risk_assessment_changed`` payload is small and flat
        (verified against captured production deliveries - Shopify publishes no
        sample for this topic)::

            {"provider_id": ..., "provider_title": "...", "risk_level": "high",
             "created_at": "...", "order_id": 123,
             "admin_graphql_api_order_id": "gid://shopify/Order/123"}

        It carries no order name, total, or currency; those are recovered from
        the ``sale.order`` imported on ``orders/create``.
        """
        order_id = payload.get("order_id")
        return {
            "order_id": str(order_id) if order_id is not None else False,
            "risk_level": self._normalize_risk(payload.get("risk_level")),
        }

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------
    @api.model
    def process_webhook(self, payload):
        """Evaluate a forwarded Shopify order-risk payload.

        Returns a JSON-serialisable decision dict for the agent/caller.
        """
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except (ValueError, TypeError):
                payload = {}
        if not isinstance(payload, dict):
            payload = {}

        info = self._extract(payload)

        # Correlate the assessment back to the order imported on orders/create.
        # The risk-assessment webhook carries no total, so we recover it (and the
        # order to cancel) from the sale.order.
        sale_order = self.env["sale.order"]
        if info["order_id"]:
            sale_order = sale_order.search([("shopify_order_id", "=", info["order_id"])], limit=1)

        total = sale_order.amount_total if sale_order else 0.0
        total_known = bool(sale_order)

        # Surface the latest risk level on the order for at-a-glance visibility.
        if sale_order:
            sale_order.shopify_risk_level = info["risk_level"]

        is_risky = info["risk_level"] in RISKY_LEVELS

        # Idempotency / escalation guard. SQS is at-least-once, Shopify retries,
        # and orders/risk_assessment_changed can legitimately fire several times
        # per order. We must not cancel/dispatch twice, but we MUST still act if a
        # later assessment escalates a previously benign order. So: a task that is
        # in-flight, already auto-cancelled, or already decided is terminal (skip);
        # a prior low/no-risk "ignored" task does NOT block a new risky verdict.
        if info["order_id"]:
            prior = self.env["ai.ops.task"].search(
                [
                    ("task_type", "=", "fraud"),
                    ("shopify_order_id", "=", info["order_id"]),
                    ("state", "!=", "failed"),
                ],
                order="id desc",
            )
            blocking = prior.filtered(
                lambda t: (
                    t.state in ("queued", "running", "pending_approval", "bypassed")
                    or t.decision in ("approve", "reject")
                )
            )
            # Repeated benign assessments should not pile up duplicate log tasks.
            handled = blocking or (prior and not is_risky)
            if handled:
                existing = (blocking or prior)[0]
                _logger.info(
                    "AI Ops: order-risk webhook for order %s already handled (task %s, state %s) - skipping.",
                    info["order_id"],
                    existing.name,
                    existing.state,
                )
                return {
                    "action": "duplicate",
                    "task": existing.name,
                    "state": existing.state,
                    "risk_level": info["risk_level"],
                    "order_total": total,
                }

        settings = self.env["res.config.settings"]
        threshold = float(settings._ai_ops_get_param("odoo_ai_ops.bypass_threshold", 10.0) or 10.0)
        auto_reject = settings._ai_ops_get_param("odoo_ai_ops.auto_reject_enabled", True)
        # ir.config_parameter stores booleans as the strings "True"/"False".
        auto_reject = str(auto_reject).strip().lower() not in ("false", "0", "")

        base_vals = {
            "task_type": "fraud",
            "risk_level": info["risk_level"],
            "shopify_order_id": info["order_id"],
            "shopify_order_name": sale_order.shopify_order_name if sale_order else False,
            "order_total": total,
            "currency_id": (sale_order.currency_id.id if sale_order else False) or self.env.company.currency_id.id,
            "sale_order_id": sale_order.id or False,
            "payload": json.dumps(payload),
        }

        # Only trust the cheap-order bypass when we actually know the total; a
        # missing total must not be read as "cheap" and auto-cancel everything.
        is_cheap = total_known and total < threshold

        # -------- Path A: cheap + risky -> immediate auto-rejection --------
        if auto_reject and is_cheap and is_risky:
            task = self.env["ai.ops.task"].create(
                dict(
                    base_vals,
                    state="bypassed",
                    bypass_reason="Cheap order (%.2f < %.2f) flagged %s risk" % (total, threshold, info["risk_level"]),
                )
            )
            cancelled = task._cancel_in_shopify(
                reason="FRAUD",
                staff_note="Auto-rejected: cheap order with %s risk" % info["risk_level"],
            )
            order_cancelled = task._cancel_sale_order()
            _logger.info(
                "AI Ops: auto-rejected order %s (total=%.2f, risk=%s) without LLM spend.",
                info["order_id"],
                total,
                info["risk_level"],
            )
            return {
                "action": "auto_reject",
                "task": task.name,
                "risk_level": info["risk_level"],
                "order_total": total,
                "shopify_cancelled": cancelled,
                "order_cancelled": order_cancelled,
            }

        # -------- Path B: not risky enough to act on -> record only --------
        if not is_risky:
            task = self.env["ai.ops.task"].create(dict(base_vals, state="done"))
            _logger.info("AI Ops: order %s low/no risk - no AI task dispatched.", info["order_id"])
            return {
                "action": "ignored",
                "task": task.name,
                "risk_level": info["risk_level"],
                "order_total": total,
            }

        # -------- Path C: escalate to the LangGraph agent --------
        task = self.env["ai.ops.task"].create(dict(base_vals, state="queued"))
        task.dispatch_fraud_workflow()
        return {
            "action": "dispatched",
            "task": task.name,
            "agent_run_id": task.agent_run_id,
            "risk_level": info["risk_level"],
            "order_total": total,
        }
