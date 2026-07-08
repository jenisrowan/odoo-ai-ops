"""Tests for the Claude integration: risk-tiered model selection and the
LangGraph fraud / reconciliation flows with `ChatAnthropic` mocked (no real
Anthropic calls, no cost)."""

from unittest.mock import AsyncMock

import pytest
from langgraph.checkpoint.memory import MemorySaver

import app.graph.fraud_graph as fraud_mod
import app.graph.reconciliation_graph as recon_mod
from app.config import Settings, get_settings
from app.graph.state import FraudVerdict, ReconciliationVerdict
from app.llm import model_for_risk
from app.runtime import AgentRuntime
from app.schemas import FraudTaskRequest, ReconciliationTaskRequest


# --- fake Claude ----------------------------------------------------------
class _FakeStructured:
    def __init__(self, verdict):
        self._v = verdict

    async def ainvoke(self, messages, config=None):
        return self._v


class _FakeChat:
    def __init__(self, verdict):
        self._v = verdict

    def with_structured_output(self, model):
        return _FakeStructured(self._v)


def _runtime():
    rt = AgentRuntime(Settings(ai_ops_shared_token="t", valkey_url=""))
    rt.odoo_client = AsyncMock()
    rt.slack_client = None
    rt.langfuse_handler = None
    rt.checkpointer = MemorySaver()
    return rt


# --- model tiering --------------------------------------------------------
def test_model_for_risk_selects_tier():
    s = get_settings()
    assert model_for_risk("high") == s.model_high
    assert model_for_risk("medium") == s.model_medium
    assert model_for_risk("low") == s.model_medium  # anything not high -> cheap tier


# --- fraud workflow -------------------------------------------------------
@pytest.mark.asyncio
async def test_fraud_graph_analyzes_and_pauses_then_finalizes(monkeypatch):
    rt = _runtime()
    verdict = FraudVerdict(
        recommendation="reject",
        confidence=0.9,
        reasoning="billing/shipping mismatch",
        signals=["ip_mismatch"],
    )
    monkeypatch.setattr(fraud_mod, "get_chat_model", lambda name: _FakeChat(verdict))
    rt.fraud_graph = fraud_mod.build_fraud_graph(rt)

    req = FraudTaskRequest(
        odoo_task_ref="AIOPS/1", odoo_task_id=5, risk_level="high", order={"total": 250}
    )
    await rt.start_fraud(req, run_id="fr-test")

    # notify node marked the task pending_approval with Claude's reasoning
    rt.odoo_client.register_agent_run.assert_awaited()
    reg = rt.odoo_client.register_agent_run.await_args.kwargs
    assert reg["state"] == "pending_approval"
    assert "mismatch" in reg["analysis"]

    # a manager decision resumes the paused graph and writes it back to Odoo
    await rt.resume("fr-test", decision="approve", manager_name="Dana")
    rt.odoo_client.set_approval.assert_awaited()
    assert rt.odoo_client.set_approval.await_args.kwargs["decision"] == "approve"


# --- reconciliation root-cause workflow -----------------------------------
@pytest.mark.asyncio
async def test_reconciliation_diagnoses_and_pushes_to_shopify(monkeypatch):
    rt = _runtime()
    rt.odoo_client.discrepancy_context.return_value = {
        "odoo_on_hand": 10,
        "shopify_available": 3,
        "discrepancy_odoo_minus_shopify": 7,
        "pending_outgoing_moves": [],
        "recent_sale_orders": [],
    }
    verdict = ReconciliationVerdict(
        direction="odoo_higher",
        root_cause="Shopify undercount (human error)",
        recommended_action="update_shopify",
        shopify_target_qty=10.0,
        corrected_odoo_qty=None,
        suspect_move_ids=[],
        reasoning="Odoo is authoritative; no stuck moves found.",
        confidence=0.8,
    )
    monkeypatch.setattr(recon_mod, "get_chat_model", lambda name: _FakeChat(verdict))
    rt.reconciliation_graph = recon_mod.build_reconciliation_graph(rt)

    req = ReconciliationTaskRequest(
        odoo_task_ref="AIOPS/2", odoo_task_id=7, product_id=42, context={}
    )
    await rt.start_reconciliation(req, run_id="rc-test")

    rt.odoo_client.discrepancy_context.assert_awaited()
    rt.odoo_client.register_agent_run.assert_awaited()

    # approving an "update_shopify" diagnosis pushes Odoo's on-hand to Shopify
    await rt.resume("rc-test", decision="approve", manager_name="Dana")
    rt.odoo_client.push_inventory_to_shopify.assert_awaited()
    push_kwargs = rt.odoo_client.push_inventory_to_shopify.await_args.kwargs
    assert push_kwargs["qty"] == 10.0
    # The write must reference the task so Odoo's approval gate can verify it,
    # and the decision must have been persisted BEFORE the write (the gate
    # checks the recorded decision).
    assert push_kwargs["task_id"] == 7
    rt.odoo_client.set_approval.assert_awaited()
    assert rt.odoo_client.set_approval.await_args.kwargs["decision"] == "approve"
