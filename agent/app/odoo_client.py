"""Async client for talking to Odoo.

Two transport paths, matching the architecture:

* **JSON-RPC** (``/jsonrpc`` -> ``object.execute_kw``) for model method calls:
  reporting workflow progress, persisting the manager decision, and the
  reconciliation catalog/move/patch operations.
* **Plain HTTP** to the custom ``/ai_ops/...`` controllers for forwarding the
  Shopify order-risk webhook (guarded by the shared token).

The client lazily authenticates once and caches the uid for the process
lifetime, re-authenticating on demand if the session is rejected.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class OdooError(Exception):
    pass


class OdooClient:
    def __init__(
        self,
        base_url: str,
        db: str,
        username: str,
        password: str,
        shared_token: str,
        timeout: float = 20.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.db = db
        self.username = username
        self.password = password
        self.shared_token = shared_token
        self._uid: int | None = None
        self._client = httpx.AsyncClient(timeout=timeout)

    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------
    # Low-level JSON-RPC
    # ------------------------------------------------------------------
    async def _jsonrpc(self, service: str, method: str, args: list) -> Any:
        payload = {
            "jsonrpc": "2.0",
            "method": "call",
            "params": {"service": service, "method": method, "args": args},
            "id": None,
        }
        resp = await self._client.post(f"{self.base_url}/jsonrpc", json=payload)
        resp.raise_for_status()
        body = resp.json()
        if body.get("error"):
            raise OdooError(f"Odoo JSON-RPC error: {body['error']}")
        return body.get("result")

    async def authenticate(self, force: bool = False) -> int:
        if self._uid is not None and not force:
            return self._uid
        uid = await self._jsonrpc(
            "common", "authenticate", [self.db, self.username, self.password, {}]
        )
        if not uid:
            raise OdooError("Odoo authentication failed (check credentials/db).")
        self._uid = int(uid)
        logger.info("Authenticated with Odoo as uid=%s", self._uid)
        return self._uid

    async def execute_kw(
        self, model: str, method: str, args: list | None = None, kwargs: dict | None = None
    ) -> Any:
        uid = await self.authenticate()
        try:
            return await self._jsonrpc(
                "object",
                "execute_kw",
                [self.db, uid, self.password, model, method, args or [], kwargs or {}],
            )
        except OdooError:
            # Session may have expired - re-auth once and retry.
            uid = await self.authenticate(force=True)
            return await self._jsonrpc(
                "object",
                "execute_kw",
                [self.db, uid, self.password, model, method, args or [], kwargs or {}],
            )

    # ------------------------------------------------------------------
    # Webhook forwarding (HTTP controller)
    # ------------------------------------------------------------------
    async def forward_order_risk(self, payload: dict) -> dict:
        """Forward a Shopify order-risk payload to the Odoo gatekeeper."""
        resp = await self._client.post(
            f"{self.base_url}/ai_ops/webhook/order_risk",
            json=payload,
            headers={"X-AI-Ops-Token": self.shared_token},
        )
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # High-level model helpers
    # ------------------------------------------------------------------
    async def register_agent_run(
        self,
        task_id: int,
        run_id: str,
        state: str = "pending_approval",
        analysis: str | None = None,
    ) -> Any:
        return await self.execute_kw(
            "ai.ops.task",
            "register_agent_run",
            [[task_id]],
            {"run_id": run_id, "state": state, "analysis": analysis},
        )

    async def set_approval(
        self,
        task_id: int,
        decision: str,
        manager_name: str | None = None,
        note: str | None = None,
        run_id: str | None = None,
    ) -> Any:
        return await self.execute_kw(
            "ai.ops.task",
            "ai_ops_set_approval",
            [[task_id], decision],
            {"manager_name": manager_name, "note": note, "run_id": run_id},
        )

    async def query_catalog(self, domain=None, fields=None, limit=100) -> Any:
        return await self.execute_kw(
            "ai.ops.inventory",
            "query_catalog",
            [domain, fields, limit],
        )

    async def warehouse_moves(self, product_id: int, limit=100) -> Any:
        return await self.execute_kw(
            "ai.ops.inventory",
            "warehouse_moves",
            [product_id, limit],
        )

    async def apply_inventory_patch(
        self, product_id: int, counted_qty: float, location_id=None, reason=None, task_id=None
    ) -> Any:
        """Adjust Odoo's on-hand quantity.

        ``task_id`` must reference the approved ``ai.ops.task``: Odoo enforces a
        server-side approval gate on the agent's write paths.
        """
        return await self.execute_kw(
            "ai.ops.inventory",
            "apply_inventory_patch",
            [product_id, counted_qty, location_id, reason],
            {"task_id": task_id},
        )

    async def discrepancy_context(self, product_id: int, fetch_shopify: bool = True) -> Any:
        """Odoo vs Shopify stock + the evidence to explain a divergence."""
        return await self.execute_kw(
            "ai.ops.inventory",
            "discrepancy_context",
            [product_id, fetch_shopify],
        )

    async def push_inventory_to_shopify(
        self, product_id: int, qty: float, reason=None, task_id=None
    ) -> Any:
        """Correct Shopify's available quantity from Odoo (Odoo is source of truth).

        ``task_id`` must reference the approved ``ai.ops.task`` (server-side
        approval gate, same as :meth:`apply_inventory_patch`).
        """
        return await self.execute_kw(
            "ai.ops.inventory",
            "push_inventory_to_shopify",
            [product_id, qty, reason],
            {"task_id": task_id},
        )
