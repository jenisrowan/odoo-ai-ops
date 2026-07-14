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

# Severity ordering, used to pick the most severe when several assessments arrive.
_RISK_SEVERITY = {"none": 0, "low": 1, "medium": 2, "high": 3}

# Shopify expresses risk in a few shapes across API versions. Normalise them all
# down to none/low/medium/high.
_RISK_ALIASES = {
    "low": "low",
    "medium": "medium",
    "high": "high",
    "none": "none",
    # recommendation-style values
    "accept": "low",
    "investigate": "medium",
    "cancel": "high",
    # score buckets occasionally seen in assessments
    "pending": "medium",
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
        """Pull the fields we care about out of a tolerant payload shape.

        Accepts either a normalised envelope (``{order_id, order_name, total,
        currency, risk_level}``) or a raw Shopify order/risk webhook body.
        """
        order = payload.get("order") if isinstance(payload.get("order"), dict) else payload

        order_id = (
            payload.get("order_id") or order.get("id") or order.get("order_id") or order.get("admin_graphql_api_id")
        )
        order_name = payload.get("order_name") or order.get("name") or order.get("order_number")

        raw_total = (
            payload.get("total")
            or order.get("current_total_price")
            or order.get("total_price")
            or order.get("total")
            or 0.0
        )
        try:
            total = float(raw_total)
        except (TypeError, ValueError):
            total = 0.0

        currency = payload.get("currency") or order.get("currency") or order.get("presentment_currency")

        # Risk can arrive as a flat level (``risk_level`` / ``riskLevel``), a
        # recommendation, or a nested assessment object/list (the
        # ``orders/risk_assessment_changed`` payload). Normalise them all.
        risk_value = (
            payload.get("risk_level") or payload.get("riskLevel") or order.get("risk_level") or order.get("riskLevel")
        )
        if not isinstance(risk_value, str):
            assessment = (
                order.get("risk_assessment")
                or order.get("risk")
                or payload.get("risk_assessment")
                or payload.get("risk")
                or {}
            )
            if isinstance(assessment, list):
                # Several assessments (e.g. Shopify + a fraud app): take the worst.
                levels = [
                    self._normalize_risk(a.get("risk_level") or a.get("riskLevel") or a.get("recommendation"))
                    for a in assessment
                    if isinstance(a, dict)
                ]
                risk_value = max(levels, key=lambda lvl: _RISK_SEVERITY.get(lvl, 0)) if levels else None
            elif isinstance(assessment, dict):
                risk_value = (
                    assessment.get("risk_level") or assessment.get("riskLevel") or assessment.get("recommendation")
                )
        risk_level = self._normalize_risk(risk_value)

        return {
            "order_id": str(order_id) if order_id is not None else False,
            "order_name": order_name and str(order_name) or False,
            "total": total,
            "currency": currency,
            "risk_level": risk_level,
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

        total = info["total"]
        total_known = bool(total)
        if not total_known and sale_order:
            total = sale_order.amount_total
            total_known = True

        # Surface the latest risk level on the order for at-a-glance visibility.
        if sale_order and info["risk_level"]:
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

        currency = self.env["res.currency"].search([("name", "=", info["currency"])], limit=1)
        if not currency and sale_order:
            currency = sale_order.currency_id

        base_vals = {
            "task_type": "fraud",
            "risk_level": info["risk_level"],
            "shopify_order_id": info["order_id"],
            "shopify_order_name": info["order_name"] or (sale_order.shopify_order_name if sale_order else False),
            "order_total": total,
            "currency_id": currency.id or self.env.company.currency_id.id,
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
