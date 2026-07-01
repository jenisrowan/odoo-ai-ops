"""Fraud-validation LangGraph workflow (Path 2 in the architecture).

Flow::

    triage ─► analyze ─► notify ─► await_decision ─(interrupt)─► finalize ─► END

* **triage** - pick the model tier from the Shopify risk level
  (medium -> Haiku, high -> Sonnet).
* **analyze** - Claude returns a structured :class:`FraudVerdict`.
* **notify** - post the Slack Block Kit card and mark the Odoo task
  ``pending_approval``. (Runs exactly once; kept separate from the interrupt so
  re-execution on resume never double-posts.)
* **await_decision** - ``interrupt()``; the thread freezes and its state is
  persisted to Valkey until a manager clicks Approve/Reject in Slack.
* **finalize** - write the decision back to Odoo over JSON-RPC.
"""

from __future__ import annotations

import logging

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from ..llm import get_chat_model, model_for_risk
from .state import FraudState, FraudVerdict

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "You are a fraud-analysis assistant for an e-commerce operations team. "
    "Given a Shopify order flagged by the OrderRisk system, assess the "
    "likelihood of fraud. Weigh signals such as billing/shipping mismatch, "
    "high-value vs. account age, mismatched IP geolocation, unusual quantities, "
    "and prior chargebacks. Be decisive but conservative: only recommend "
    "'reject' when signals strongly indicate fraud, 'approve' when the order "
    "looks legitimate, and 'review' when genuinely ambiguous."
)


def build_fraud_graph(runtime):
    """Compile the fraud graph, closing over the shared :class:`AgentRuntime`."""

    async def triage(state: FraudState) -> dict:
        risk = state.get("risk_level", "high")
        model = model_for_risk(risk)
        logger.info("[%s] triage: risk=%s -> model=%s", state.get("odoo_task_ref"), risk, model)
        return {"model": model}

    async def analyze(state: FraudState) -> dict:
        model_name = state["model"]
        chat = get_chat_model(model_name).with_structured_output(FraudVerdict)
        order = state.get("order", {})
        prompt = (
            f"Shopify risk level: {state.get('risk_level')}\n"
            f"Order context (JSON):\n{order}\n\n"
            "Return your structured fraud assessment."
        )
        config = {}
        if runtime.langfuse_handler is not None:
            config["callbacks"] = [runtime.langfuse_handler]
            config["metadata"] = {
                "odoo_task_ref": state.get("odoo_task_ref"),
                "langfuse_session_id": state.get("odoo_task_ref"),
                "risk_level": state.get("risk_level"),
            }
        verdict: FraudVerdict = await chat.ainvoke(
            [("system", _SYSTEM_PROMPT), ("human", prompt)], config=config or None
        )
        logger.info(
            "[%s] analyze: recommendation=%s confidence=%.2f",
            state.get("odoo_task_ref"),
            verdict.recommendation,
            verdict.confidence,
        )
        return {"verdict": verdict.model_dump()}

    async def notify(state: FraudState) -> dict:
        thread_id = state.get("odoo_task_ref")
        task_id = state.get("odoo_task_id")
        verdict = state.get("verdict", {})

        # Mark the Odoo task as awaiting a human decision.
        if task_id:
            try:
                await runtime.odoo_client.register_agent_run(
                    task_id=task_id,
                    run_id=state.get("run_id"),
                    state="pending_approval",
                    analysis=verdict.get("reasoning"),
                )
            except Exception:  # noqa: BLE001 - don't lose the workflow on a transient error
                logger.exception("[%s] failed to update Odoo task", thread_id)

        # Post the interactive approval card.
        if runtime.slack_client is not None:
            try:
                await runtime.slack_client.post_fraud_card(
                    task_ref=state.get("odoo_task_ref"),
                    odoo_task_id=task_id,
                    thread_id=state.get("run_id"),
                    order=state.get("order", {}),
                    risk_level=state.get("risk_level", "high"),
                    verdict=verdict,
                )
            except Exception:  # noqa: BLE001
                logger.exception("[%s] failed to post Slack card", thread_id)
        return {}

    async def await_decision(state: FraudState) -> dict:
        # Freeze here. The resume worker supplies the manager's decision dict.
        decision_payload = interrupt(
            {
                "kind": "fraud_approval",
                "odoo_task_ref": state.get("odoo_task_ref"),
                "verdict": state.get("verdict"),
            }
        )
        decision = (decision_payload or {}).get("decision", "review")
        return {
            "decision": decision,
            "manager_name": (decision_payload or {}).get("manager_name"),
            "note": (decision_payload or {}).get("note"),
        }

    async def finalize(state: FraudState) -> dict:
        task_id = state.get("odoo_task_id")
        decision = state.get("decision")
        if task_id and decision in ("approve", "reject"):
            try:
                await runtime.odoo_client.set_approval(
                    task_id=task_id,
                    decision=decision,
                    manager_name=state.get("manager_name"),
                    note=state.get("note"),
                    run_id=state.get("run_id"),
                )
            except Exception:  # noqa: BLE001
                logger.exception(
                    "[%s] failed to persist decision to Odoo", state.get("odoo_task_ref")
                )
        logger.info("[%s] finalized with decision=%s", state.get("odoo_task_ref"), decision)
        return {}

    builder = StateGraph(FraudState)
    builder.add_node("triage", triage)
    builder.add_node("analyze", analyze)
    builder.add_node("notify", notify)
    builder.add_node("await_decision", await_decision)
    builder.add_node("finalize", finalize)

    builder.add_edge(START, "triage")
    builder.add_edge("triage", "analyze")
    builder.add_edge("analyze", "notify")
    builder.add_edge("notify", "await_decision")
    builder.add_edge("await_decision", "finalize")
    builder.add_edge("finalize", END)

    return builder.compile(checkpointer=runtime.checkpointer)
