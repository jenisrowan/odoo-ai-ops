# -*- coding: utf-8 -*-
"""HTTP client used by Odoo to dispatch AI tasks to the FastAPI agent cluster.

Odoo is the *gatekeeper*: it only calls the agent for orders that survive the
cheap-order auto-rejection rule. The agent answers asynchronously (it posts a
Slack card and later calls Odoo back), so these calls only need to reliably
hand off the task and surface transport errors to the caller.
"""

import logging

import requests

_logger = logging.getLogger(__name__)

# Connect/read timeouts (seconds). The agent acknowledges quickly (202) and
# does the heavy lifting in the background, so a short read timeout is fine.
DEFAULT_TIMEOUT = (5, 15)


class AgentError(Exception):
    """Raised when the agent cluster cannot be reached or rejects the request."""


class AgentClient:
    def __init__(self, base_url, token, timeout=DEFAULT_TIMEOUT):
        if not base_url:
            raise AgentError("AI Ops agent base URL is not configured.")
        self.base_url = base_url.rstrip("/")
        self.token = token or ""
        self.timeout = timeout

    def _headers(self):
        return {
            "Authorization": "Bearer %s" % self.token,
            "Content-Type": "application/json",
        }

    def _post(self, path, payload):
        url = "%s%s" % (self.base_url, path)
        try:
            response = requests.post(url, json=payload, headers=self._headers(), timeout=self.timeout)
        except requests.RequestException as exc:
            raise AgentError("Could not reach AI agent at %s: %s" % (url, exc)) from exc

        if response.status_code >= 400:
            raise AgentError("AI agent returned HTTP %s for %s: %s" % (response.status_code, path, response.text[:500]))
        try:
            return response.json()
        except ValueError:
            return {}

    def start_fraud_workflow(self, task_ref, order, risk_level, task_id=None):
        """Kick off the LangGraph fraud-validation workflow for an order.

        ``task_id`` is the numeric ``ai.ops.task`` DB id; the agent uses it to
        call back into Odoo over JSON-RPC once a manager decides.
        """
        return self._post(
            "/v1/tasks/fraud",
            {
                "odoo_task_ref": task_ref,
                "odoo_task_id": task_id,
                "risk_level": risk_level,
                "order": order,
            },
        )

    def start_reconciliation_workflow(self, task_ref, product_id, context=None, task_id=None):
        """Kick off the inventory reconciliation workflow for a product."""
        return self._post(
            "/v1/tasks/reconciliation",
            {
                "odoo_task_ref": task_ref,
                "odoo_task_id": task_id,
                "product_id": product_id,
                "context": context or {},
            },
        )
