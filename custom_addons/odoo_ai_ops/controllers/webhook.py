# -*- coding: utf-8 -*-
"""HTTP endpoints consumed by the FastAPI agent cluster.

These are private, server-to-server endpoints (the agent reaches Odoo over ECS
Service Connect). They are *not* exposed to the public internet flow - the
public Shopify/Slack webhooks land on API Gateway, not here. Authentication is a
shared bearer token (``odoo_ai_ops.shared_token``) compared in constant time.

Endpoints
---------
* ``POST /ai_ops/webhook/order_risk`` - forwarded Shopify order-risk payload.
* ``POST /ai_ops/task/<id>/callback``  - manager approval/rejection relayed
  from Slack by the agent.
* ``GET  /ai_ops/health``              - lightweight liveness probe.
"""

import hmac
import json
import logging

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)


def _json_response(payload, status=200):
    """Return a JSON HTTP response (works for type='http' routes)."""
    return request.make_json_response(payload, status=status)


def _authenticate():
    """Validate the shared token. Returns ``None`` on success, else a Response.

    Accepts the token via either ``X-AI-Ops-Token`` or
    ``Authorization: Bearer <token>``.
    """
    settings = request.env["res.config.settings"].sudo()
    expected = settings._ai_ops_get_param("odoo_ai_ops.shared_token") or ""
    if not expected:
        _logger.error("AI Ops: shared token is not configured; rejecting request.")
        return _json_response({"error": "integration_not_configured"}, status=503)

    headers = request.httprequest.headers
    provided = headers.get("X-AI-Ops-Token")
    if not provided:
        auth = headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            provided = auth[len("Bearer ") :]
    provided = provided or ""

    # Constant-time comparison to avoid leaking the token via timing.
    if not hmac.compare_digest(provided, expected):
        _logger.warning("AI Ops: rejected request with invalid shared token.")
        return _json_response({"error": "unauthorized"}, status=401)
    return None


def _parse_body():
    """Parse the raw request body as JSON, tolerating empty bodies."""
    raw = request.httprequest.get_data(as_text=True) or "{}"
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None


class AiOpsWebhookController(http.Controller):
    @http.route("/ai_ops/health", type="http", auth="public", methods=["GET"], csrf=False)
    def health(self, **kwargs):
        return _json_response({"status": "ok", "service": "odoo_ai_ops"})

    @http.route(
        "/ai_ops/webhook/order_risk", type="http", auth="public", methods=["POST"], csrf=False, save_session=False
    )
    def order_risk(self, **kwargs):
        """Receive a forwarded Shopify order-risk payload and evaluate it."""
        denied = _authenticate()
        if denied:
            return denied

        payload = _parse_body()
        if payload is None:
            return _json_response({"error": "invalid_json"}, status=400)

        try:
            result = request.env["ai.ops.order.risk"].sudo().process_webhook(payload)
        except Exception as exc:  # noqa: BLE001 - surface a clean 500 to the agent
            _logger.exception("AI Ops: order_risk processing failed.")
            return _json_response({"error": "processing_failed", "detail": str(exc)}, status=500)
        return _json_response(result)

    @http.route(
        "/ai_ops/task/<int:task_id>/callback",
        type="http",
        auth="public",
        methods=["POST"],
        csrf=False,
        save_session=False,
    )
    def task_callback(self, task_id, **kwargs):
        """Persist a manager's decision relayed by the agent from Slack."""
        denied = _authenticate()
        if denied:
            return denied

        payload = _parse_body()
        if payload is None:
            return _json_response({"error": "invalid_json"}, status=400)

        task = request.env["ai.ops.task"].sudo().browse(task_id)
        if not task.exists():
            return _json_response({"error": "task_not_found"}, status=404)

        decision = payload.get("decision")
        try:
            result = task.ai_ops_set_approval(
                decision,
                manager_name=payload.get("manager_name"),
                note=payload.get("note"),
                run_id=payload.get("run_id"),
            )
        except Exception as exc:  # noqa: BLE001
            _logger.exception("AI Ops: task callback failed for task %s.", task_id)
            return _json_response({"error": "callback_failed", "detail": str(exc)}, status=400)
        return _json_response(result)
