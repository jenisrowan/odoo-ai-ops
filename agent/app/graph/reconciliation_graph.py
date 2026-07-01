"""Inventory-reconciliation LangGraph workflow (Path 1 in the architecture).

Flow::

    gather ─► analyze ─► notify ─► await_decision ─(interrupt)─► apply ─► END

* **gather** - pull the catalog record and historical warehouse moves for the
  product from Odoo over JSON-RPC.
* **analyze** - Claude reconciles the moves and proposes a corrected on-hand
  quantity (a structured :class:`ReconciliationVerdict`).
* **notify** - surface the proposal to a manager via Slack and mark the task
  ``pending_approval``.
* **await_decision** - ``interrupt()`` until a human approves/rejects.
* **apply** - on approval, write the inventory adjustment patch back to Odoo.
"""

from __future__ import annotations

import logging

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from ..llm import get_chat_model, model_for_risk
from .state import ReconciliationState, ReconciliationVerdict

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "You are an inventory-reconciliation assistant. Given a product's catalog "
    "record and its recent warehouse moves, determine the physically correct "
    "on-hand quantity, accounting for unfulfilled orders, returns, and transfer "
    "moves. Return the corrected count with concise reasoning."
)


def build_reconciliation_graph(runtime):
    """Compile the reconciliation graph, closing over the shared runtime."""

    async def gather(state: ReconciliationState) -> dict:
        product_id = state["product_id"]
        catalog = await runtime.odoo_client.query_catalog(domain=[["id", "=", product_id]], limit=1)
        moves = await runtime.odoo_client.warehouse_moves(product_id=product_id, limit=100)
        logger.info(
            "[%s] gathered %s moves for product %s",
            state.get("odoo_task_ref"),
            len(moves or []),
            product_id,
        )
        return {"catalog": catalog or [], "moves": moves or []}

    async def analyze(state: ReconciliationState) -> dict:
        # Reconciliation is a high-value write path -> use the strong model.
        chat = get_chat_model(model_for_risk("high")).with_structured_output(ReconciliationVerdict)
        prompt = (
            f"Catalog record:\n{state.get('catalog')}\n\n"
            f"Recent warehouse moves:\n{state.get('moves')}\n\n"
            f"Extra context:\n{state.get('context', {})}\n\n"
            "Propose the corrected on-hand quantity."
        )
        config = {}
        if runtime.langfuse_handler is not None:
            config["callbacks"] = [runtime.langfuse_handler]
            config["metadata"] = {"odoo_task_ref": state.get("odoo_task_ref")}
        verdict: ReconciliationVerdict = await chat.ainvoke(
            [("system", _SYSTEM_PROMPT), ("human", prompt)], config=config or None
        )
        return {"proposal": verdict.model_dump()}

    async def notify(state: ReconciliationState) -> dict:
        task_id = state.get("odoo_task_id")
        proposal = state.get("proposal", {})
        if task_id:
            try:
                await runtime.odoo_client.register_agent_run(
                    task_id=task_id,
                    run_id=state.get("run_id"),
                    state="pending_approval",
                    analysis=proposal.get("reasoning"),
                )
            except Exception:  # noqa: BLE001
                logger.exception("[%s] failed to update Odoo task", state.get("odoo_task_ref"))
        if runtime.slack_client is not None:
            try:
                await runtime.slack_client.post_text(
                    f":package: *Inventory reconciliation* for product "
                    f"{state['product_id']} ({state.get('odoo_task_ref')})\n"
                    f"Proposed on-hand: *{proposal.get('counted_qty')}*\n"
                    f"{proposal.get('reasoning')}"
                )
            except Exception:  # noqa: BLE001
                logger.exception("[%s] failed to post Slack message", state.get("odoo_task_ref"))
        return {}

    async def await_decision(state: ReconciliationState) -> dict:
        decision_payload = interrupt(
            {
                "kind": "reconciliation_approval",
                "odoo_task_ref": state.get("odoo_task_ref"),
                "proposal": state.get("proposal"),
            }
        )
        return {
            "decision": (decision_payload or {}).get("decision", "review"),
            "manager_name": (decision_payload or {}).get("manager_name"),
        }

    async def apply(state: ReconciliationState) -> dict:
        decision = state.get("decision")
        task_id = state.get("odoo_task_id")
        if decision == "approve":
            proposal = state.get("proposal", {})
            try:
                await runtime.odoo_client.apply_inventory_patch(
                    product_id=state["product_id"],
                    counted_qty=proposal.get("counted_qty"),
                    reason=f"AI reconciliation {state.get('odoo_task_ref')}",
                )
            except Exception:  # noqa: BLE001
                logger.exception("[%s] failed to apply inventory patch", state.get("odoo_task_ref"))
        if task_id and decision in ("approve", "reject"):
            try:
                await runtime.odoo_client.set_approval(
                    task_id=task_id,
                    decision=decision,
                    manager_name=state.get("manager_name"),
                    run_id=state.get("run_id"),
                )
            except Exception:  # noqa: BLE001
                logger.exception("[%s] failed to persist decision", state.get("odoo_task_ref"))
        return {}

    builder = StateGraph(ReconciliationState)
    builder.add_node("gather", gather)
    builder.add_node("analyze", analyze)
    builder.add_node("notify", notify)
    builder.add_node("await_decision", await_decision)
    builder.add_node("apply", apply)

    builder.add_edge(START, "gather")
    builder.add_edge("gather", "analyze")
    builder.add_edge("analyze", "notify")
    builder.add_edge("notify", "await_decision")
    builder.add_edge("await_decision", "apply")
    builder.add_edge("apply", END)

    return builder.compile(checkpointer=runtime.checkpointer)
