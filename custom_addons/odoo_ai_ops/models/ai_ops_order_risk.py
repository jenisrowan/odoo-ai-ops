# -*- coding: utf-8 -*-
"""Order-risk gatekeeper.

This is the decision point described in the architecture's "Path 2: Fraud
Detection" flow. The FastAPI agent polls the Shopify ``orders/risk`` webhook off
SQS and forwards the payload here. Odoo then decides:

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

        # Risk can arrive as a flat level, a recommendation, or a nested
        # assessment object.
        risk_value = payload.get("risk_level") or payload.get("risk") or order.get("risk_level")
        if risk_value is None:
            assessment = order.get("risk") or order.get("risk_assessment") or {}
            if isinstance(assessment, dict):
                risk_value = assessment.get("risk_level") or assessment.get("recommendation")
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
        settings = self.env["res.config.settings"]
        threshold = float(settings._ai_ops_get_param("odoo_ai_ops.bypass_threshold", 10.0) or 10.0)
        auto_reject = settings._ai_ops_get_param("odoo_ai_ops.auto_reject_enabled", True)
        # ir.config_parameter stores booleans as the strings "True"/"False".
        auto_reject = str(auto_reject).strip().lower() not in ("false", "0", "")

        currency = self.env["res.currency"].search([("name", "=", info["currency"])], limit=1)

        base_vals = {
            "task_type": "fraud",
            "risk_level": info["risk_level"],
            "shopify_order_id": info["order_id"],
            "shopify_order_name": info["order_name"],
            "order_total": info["total"],
            "currency_id": currency.id or self.env.company.currency_id.id,
            "payload": json.dumps(payload),
        }

        is_cheap = info["total"] < threshold
        is_risky = info["risk_level"] in RISKY_LEVELS

        # -------- Path A: cheap + risky -> immediate auto-rejection --------
        if auto_reject and is_cheap and is_risky:
            task = self.env["ai.ops.task"].create(
                dict(
                    base_vals,
                    state="bypassed",
                    bypass_reason="Cheap order (%.2f < %.2f) flagged %s risk"
                    % (info["total"], threshold, info["risk_level"]),
                )
            )
            cancelled = task._cancel_in_shopify(
                reason="FRAUD",
                staff_note="Auto-rejected: cheap order with %s risk" % info["risk_level"],
            )
            _logger.info(
                "AI Ops: auto-rejected order %s (total=%.2f, risk=%s) without LLM spend.",
                info["order_id"],
                info["total"],
                info["risk_level"],
            )
            return {
                "action": "auto_reject",
                "task": task.name,
                "risk_level": info["risk_level"],
                "order_total": info["total"],
                "shopify_cancelled": cancelled,
            }

        # -------- Path B: not risky enough to act on -> record only --------
        if info["risk_level"] in ("none", "low"):
            task = self.env["ai.ops.task"].create(dict(base_vals, state="done"))
            _logger.info("AI Ops: order %s low/no risk - no AI task dispatched.", info["order_id"])
            return {
                "action": "ignored",
                "task": task.name,
                "risk_level": info["risk_level"],
                "order_total": info["total"],
            }

        # -------- Path C: escalate to the LangGraph agent --------
        task = self.env["ai.ops.task"].create(dict(base_vals, state="queued"))
        task.dispatch_fraud_workflow()
        return {
            "action": "dispatched",
            "task": task.name,
            "agent_run_id": task.agent_run_id,
            "risk_level": info["risk_level"],
            "order_total": info["total"],
        }
