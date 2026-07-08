"""Inventory-reconciliation LangGraph workflow (Path 1 in the architecture).

Goal: explain *why* Odoo and Shopify stock disagree for a product, and propose
the right fix — not just guess a number.

Flow::

    gather ─► diagnose ─► notify ─► await_decision ─(interrupt)─► apply ─► END

* **gather** — pull the discrepancy context from Odoo (on-hand vs Shopify
  available, open/aged outgoing & incoming moves, recent sales orders).
* **diagnose** — Claude performs root-cause analysis over that evidence and
  returns a structured :class:`ReconciliationVerdict` (direction, root cause,
  recommended action).
* **notify** — surface the diagnosis to a manager via Slack; mark the task
  ``pending_approval``.
* **await_decision** — ``interrupt()`` until a human approves/rejects.
* **apply** — on approval, execute the recommended action (push Odoo's on-hand
  to Shopify, adjust Odoo, or leave it for a human to investigate a stuck move).
"""

from __future__ import annotations

import logging

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from ..llm import get_chat_model, model_for_risk
from .state import ReconciliationState, ReconciliationVerdict

logger = logging.getLogger(__name__)

# The analytical framework encodes the operator's guidance for interpreting the
# direction of the discrepancy and choosing a resolution.
_SYSTEM_PROMPT = (
    "You are an inventory-reconciliation analyst. A product's on-hand quantity "
    "in Odoo disagrees with the 'available' quantity in Shopify. Using the "
    "supplied evidence, determine the SINGLE most likely root cause and the "
    "right corrective action. Reason carefully about the data — do not guess.\n\n"
    "Framework:\n"
    "- If Odoo has MORE stock than Shopify: the most common cause is a Shopify "
    "undercount from human error → recommend 'update_shopify' to set Shopify to "
    "Odoo's on-hand. BUT first check the evidence for alternatives: a Shopify "
    "sale with no matching Odoo sales order (a 'missing_sale_order' — Odoo would "
    "then be overstating) → recommend 'create_missing_sale_order'; or an "
    "outgoing delivery already shipped but still open/aged in Odoo (it never "
    "decremented Odoo) → recommend 'validate_or_investigate_move' and list the "
    "suspect move ids.\n"
    "- If Odoo has LESS stock than Shopify: something is wrong on the Odoo side. "
    "Look for an incoming receipt stuck in a draft/intermediate state (a restock "
    "not recorded), a duplicated/over-applied outgoing move, or an erroneous "
    "adjustment → usually 'validate_or_investigate_move' (list the suspect move "
    "ids) or 'adjust_odoo' with the corrected on-hand if you can determine it.\n"
    "- If they already match: 'no_action'.\n\n"
    "Only choose 'update_shopify' when Odoo is clearly the source of truth. "
    "Only choose 'adjust_odoo' when you can justify the corrected number from the "
    "moves. Populate corrected_odoo_qty / shopify_target_qty accordingly, and "
    "always fill 'reasoning' with the specific evidence you relied on."
)


def build_reconciliation_graph(runtime):
    """Compile the reconciliation graph, closing over the shared runtime."""

    async def gather(state: ReconciliationState) -> dict:
        product_id = state["product_id"]
        try:
            discrepancy = await runtime.odoo_client.discrepancy_context(product_id)
        except Exception:  # noqa: BLE001 - fall back to an empty context, diagnose will flag it
            logger.exception(
                "[%s] failed to gather discrepancy context", state.get("odoo_task_ref")
            )
            discrepancy = {}
        logger.info(
            "[%s] discrepancy for product %s: odoo=%s shopify=%s delta=%s",
            state.get("odoo_task_ref"),
            product_id,
            discrepancy.get("odoo_on_hand"),
            discrepancy.get("shopify_available"),
            discrepancy.get("discrepancy_odoo_minus_shopify"),
        )
        return {"discrepancy": discrepancy}

    async def diagnose(state: ReconciliationState) -> dict:
        # High-value write path -> use the strong model.
        chat = get_chat_model(model_for_risk("high")).with_structured_output(ReconciliationVerdict)
        prompt = (
            f"Product & stock discrepancy evidence (JSON):\n{state.get('discrepancy', {})}\n\n"
            f"Extra context from Odoo:\n{state.get('context', {})}\n\n"
            "Diagnose the root cause and recommend the corrective action."
        )
        config = {}
        if runtime.langfuse_handler is not None:
            config["callbacks"] = [runtime.langfuse_handler]
            config["metadata"] = {"odoo_task_ref": state.get("odoo_task_ref")}
        verdict: ReconciliationVerdict = await chat.ainvoke(
            [("system", _SYSTEM_PROMPT), ("human", prompt)], config=config or None
        )
        logger.info(
            "[%s] diagnosis: direction=%s action=%s confidence=%.2f",
            state.get("odoo_task_ref"),
            verdict.direction,
            verdict.recommended_action,
            verdict.confidence,
        )
        return {"proposal": verdict.model_dump()}

    async def notify(state: ReconciliationState) -> dict:
        task_id = state.get("odoo_task_id")
        proposal = state.get("proposal", {})
        disc = state.get("discrepancy", {})
        summary = (
            f"Root cause: {proposal.get('root_cause')}\n"
            f"Recommended action: {proposal.get('recommended_action')}\n"
            f"{proposal.get('reasoning')}"
        )
        if task_id:
            try:
                await runtime.odoo_client.register_agent_run(
                    task_id=task_id,
                    run_id=state.get("run_id"),
                    state="pending_approval",
                    analysis=summary,
                )
            except Exception:  # noqa: BLE001
                logger.exception("[%s] failed to update Odoo task", state.get("odoo_task_ref"))
        if runtime.slack_client is not None:
            try:
                await runtime.slack_client.post_text(
                    f"*Inventory reconciliation* - {state.get('odoo_task_ref')}\n"
                    f"*Odoo on-hand:* {disc.get('odoo_on_hand')}  "
                    f"*Shopify available:* {disc.get('shopify_available')}  "
                    f"(*Δ* {disc.get('discrepancy_odoo_minus_shopify')})\n"
                    f"*Direction:* {proposal.get('direction')}\n{summary}"
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
                "discrepancy": state.get("discrepancy"),
            }
        )
        return {
            "decision": (decision_payload or {}).get("decision", "review"),
            "manager_name": (decision_payload or {}).get("manager_name"),
        }

    async def apply(state: ReconciliationState) -> dict:
        decision = state.get("decision")
        task_id = state.get("odoo_task_id")
        product_id = state.get("product_id")
        proposal = state.get("proposal", {})
        action = proposal.get("recommended_action")
        ref = state.get("odoo_task_ref")

        # Persist the manager decision FIRST: Odoo's server-side approval gate
        # only lets the agent's inventory writes through once the task carries
        # a recorded 'approve' decision.
        if task_id and decision in ("approve", "reject"):
            try:
                await runtime.odoo_client.set_approval(
                    task_id=task_id,
                    decision=decision,
                    manager_name=state.get("manager_name"),
                    note=f"Reconciliation action: {action}",
                    run_id=state.get("run_id"),
                )
            except Exception:  # noqa: BLE001
                logger.exception("[%s] failed to persist decision", ref)
                # Without the persisted decision Odoo will refuse the write;
                # don't attempt it against a closed gate.
                return {}

        if decision == "approve":
            try:
                if action == "update_shopify" and proposal.get("shopify_target_qty") is not None:
                    # Odoo is source of truth -> correct Shopify (human-error case).
                    await runtime.odoo_client.push_inventory_to_shopify(
                        product_id=product_id,
                        qty=proposal["shopify_target_qty"],
                        reason=f"AI reconciliation {ref}",
                        task_id=task_id,
                    )
                elif action == "adjust_odoo" and proposal.get("corrected_odoo_qty") is not None:
                    await runtime.odoo_client.apply_inventory_patch(
                        product_id=product_id,
                        counted_qty=proposal["corrected_odoo_qty"],
                        reason=f"AI reconciliation {ref}",
                        task_id=task_id,
                    )
                else:
                    # validate_or_investigate_move / create_missing_sale_order /
                    # no_action -> no automated write; the diagnosis is recorded on
                    # the Odoo task for a human to act on.
                    logger.info("[%s] action '%s' recorded for human follow-up.", ref, action)
            except Exception:  # noqa: BLE001
                logger.exception("[%s] failed to apply reconciliation action '%s'", ref, action)
                # Surface the failed write on the task so it doesn't sit
                # closed-but-unapplied.
                if task_id:
                    try:
                        await runtime.odoo_client.register_agent_run(
                            task_id=task_id,
                            run_id=state.get("run_id"),
                            state="failed",
                            analysis=f"Approved action '{action}' could not be applied; "
                            "see agent logs.",
                        )
                    except Exception:  # noqa: BLE001
                        logger.exception("[%s] failed to flag the task as failed", ref)
        return {}

    builder = StateGraph(ReconciliationState)
    builder.add_node("gather", gather)
    builder.add_node("diagnose", diagnose)
    builder.add_node("notify", notify)
    builder.add_node("await_decision", await_decision)
    builder.add_node("apply", apply)

    builder.add_edge(START, "gather")
    builder.add_edge("gather", "diagnose")
    builder.add_edge("diagnose", "notify")
    builder.add_edge("notify", "await_decision")
    builder.add_edge("await_decision", "apply")
    builder.add_edge("apply", END)

    return builder.compile(checkpointer=runtime.checkpointer)
