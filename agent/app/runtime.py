"""AgentRuntime - the orchestration core shared by the REST API and SQS worker.

Owns the long-lived clients (Odoo, Slack), the Valkey checkpointer, the
Langfuse handler and the two compiled LangGraph workflows, and exposes the
high-level operations both entry points need:

* ``start_fraud`` / ``start_reconciliation`` - launch a workflow (runs up to the
  human-approval interrupt, then persists to Valkey and returns the run id).
* ``resume`` - rehydrate a paused workflow from Valkey with a manager decision.
* ``forward_webhook`` - relay a Shopify order-risk payload to the Odoo gatekeeper.
* ``handle_slack_interaction`` / ``handle_sqs_message`` - route inbound events.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid

from langgraph.types import Command

from .checkpointer import build_checkpointer, close_checkpointer
from .config import Settings
from .graph.fraud_graph import build_fraud_graph
from .graph.reconciliation_graph import build_reconciliation_graph
from .odoo_client import OdooClient
from .schemas import FraudTaskRequest, ReconciliationTaskRequest
from .slack_client import SlackClient
from .telemetry import build_langfuse_handler

logger = logging.getLogger(__name__)


class AgentRuntime:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.odoo_client: OdooClient | None = None
        self.slack_client: SlackClient | None = None
        self.langfuse_handler = None
        self.checkpointer = None
        self._checkpointer_cm = None
        self.fraud_graph = None
        self.reconciliation_graph = None
        # Strong refs to in-flight background workflow tasks (prevent GC).
        self._bg_tasks: set[asyncio.Task] = set()

    def spawn(self, coro) -> None:
        """Run a coroutine as a tracked, fire-and-forget background task."""
        task = asyncio.create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    def _log_task_error(self, awaitable_name: str):
        async def _wrap(coro):
            try:
                await coro
            except Exception:  # noqa: BLE001
                logger.exception("Background workflow %s failed.", awaitable_name)

        return _wrap

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    @classmethod
    async def create(cls, settings: Settings) -> AgentRuntime:
        self = cls(settings)
        self.odoo_client = OdooClient(
            base_url=settings.odoo_base_url,
            db=settings.odoo_db,
            username=settings.odoo_username,
            password=settings.odoo_password,
            shared_token=settings.ai_ops_shared_token,
        )
        if settings.slack_enabled:
            self.slack_client = SlackClient(settings.slack_bot_token, settings.slack_channel)
        self.langfuse_handler = build_langfuse_handler(settings)
        self.checkpointer, self._checkpointer_cm = await build_checkpointer(settings.valkey_url)
        self.fraud_graph = build_fraud_graph(self)
        self.reconciliation_graph = build_reconciliation_graph(self)
        logger.info("AgentRuntime initialized.")
        return self

    async def aclose(self) -> None:
        if self.odoo_client:
            await self.odoo_client.aclose()
        if self.slack_client:
            await self.slack_client.aclose()
        await close_checkpointer(self._checkpointer_cm)

    # ------------------------------------------------------------------
    # Workflow entry points
    # ------------------------------------------------------------------
    async def start_fraud(self, req: FraudTaskRequest, run_id: str | None = None) -> str:
        run_id = run_id or f"fr-{uuid.uuid4()}"
        state = {
            "run_id": run_id,
            "odoo_task_ref": req.odoo_task_ref,
            "odoo_task_id": req.odoo_task_id,
            "risk_level": req.risk_level,
            "order": req.order,
        }
        config = {"configurable": {"thread_id": run_id}}
        # Runs through to the approval interrupt, then pauses (persisted to Valkey).
        await self.fraud_graph.ainvoke(state, config=config)
        logger.info("Fraud workflow %s paused for approval (%s).", run_id, req.odoo_task_ref)
        return run_id

    async def start_reconciliation(
        self, req: ReconciliationTaskRequest, run_id: str | None = None
    ) -> str:
        run_id = run_id or f"rc-{uuid.uuid4()}"
        state = {
            "run_id": run_id,
            "odoo_task_ref": req.odoo_task_ref,
            "odoo_task_id": req.odoo_task_id,
            "product_id": req.product_id,
            "context": req.context,
        }
        config = {"configurable": {"thread_id": run_id}}
        await self.reconciliation_graph.ainvoke(state, config=config)
        logger.info("Reconciliation workflow %s paused for approval.", run_id)
        return run_id

    def _graph_for(self, run_id: str):
        if run_id.startswith("rc-"):
            return self.reconciliation_graph
        return self.fraud_graph

    async def resume(
        self, run_id: str, decision: str, manager_name: str | None = None, note: str | None = None
    ) -> None:
        """Resume a paused workflow with a manager's decision."""
        graph = self._graph_for(run_id)
        config = {"configurable": {"thread_id": run_id}}
        resume_value = {"decision": decision, "manager_name": manager_name, "note": note}
        await graph.ainvoke(Command(resume=resume_value), config=config)
        logger.info("Resumed workflow %s with decision=%s.", run_id, decision)

    # ------------------------------------------------------------------
    # Event routing
    # ------------------------------------------------------------------
    async def forward_webhook(self, payload: dict, topic: str = "") -> dict:
        """Relay a Shopify webhook to the right Odoo endpoint by topic.

        ``orders/create`` builds the sale.order; the risk-assessment topic
        (``orders/risk_assessment_changed``, or the legacy ``orders/risk``) drives
        the fraud gatekeeper. Unknown topics default to the risk path for
        backward compatibility.
        """
        if topic == "orders/create":
            result = await self.odoo_client.forward_order_create(payload)
            logger.info("Forwarded orders/create webhook to Odoo -> %s", result.get("action"))
            return result
        result = await self.odoo_client.forward_order_risk(payload)
        logger.info(
            "Forwarded order-risk webhook (%s) to Odoo -> %s", topic or "n/a", result.get("action")
        )
        return result

    async def handle_slack_interaction(self, payload: dict) -> None:
        """Resume a workflow from a Slack interactive button click."""
        actions = payload.get("actions") or []
        if not actions:
            logger.warning("Slack interaction with no actions; ignoring.")
            return
        action = actions[0]
        action_id = action.get("action_id", "")
        decision = "approve" if action_id.endswith("approve") else "reject"

        try:
            ctx = json.loads(action.get("value") or "{}")
        except (ValueError, TypeError):
            ctx = {}
        run_id = ctx.get("thread_id")
        if not run_id:
            logger.error("Slack interaction missing thread_id in button value; cannot route.")
            return

        user = payload.get("user") or {}
        manager_name = user.get("name") or user.get("username")
        await self.resume(
            run_id, decision=decision, manager_name=manager_name, note="Decision via Slack"
        )

    async def handle_sqs_message(self, body: dict) -> None:
        """Route a single SQS message body to the right handler.

        Expected envelope (produced by the API Gateway Lambda)::

            {"source": "shopify"|"slack", "topic": "...", "payload": {...}}
        """
        source = (body.get("source") or "").lower()
        topic = (body.get("topic") or "").lower()
        raw_payload = body.get("payload")
        payload = raw_payload if isinstance(raw_payload, dict | list) else body

        if source == "slack":
            await self.handle_slack_interaction(payload)
        elif source == "shopify":
            await self.forward_webhook(payload, topic=topic)
        else:
            # Fall back to a best-effort guess so malformed envelopes are still handled.
            if isinstance(payload, dict) and payload.get("type") == "block_actions":
                await self.handle_slack_interaction(payload)
            else:
                await self.forward_webhook(payload, topic=topic)
