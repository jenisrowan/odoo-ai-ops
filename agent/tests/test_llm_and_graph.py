"""Tests for the Claude integration: risk-tiered model selection and the
LangGraph fraud / reconciliation flows with `ChatAnthropic` mocked (no real
Anthropic calls, no cost)."""

import copy
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage
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
    """Stands in for ChatAnthropic in both graphs.

    ``tool_script`` drives the reconciliation investigation loop: each entry is
    the list of tool calls to emit on that turn. Once the script is exhausted
    the fake answers in prose, which is what ends the loop. Like the real
    client, ``bind_tools`` returns a *bound copy* and the unbound original can
    make no tool calls - that is what lets the budget-exhausted turn be tested.
    """

    def __init__(self, verdict, tool_script=None):
        self._v = verdict
        self._script = list(tool_script or [])  # shared with bound copies
        self.tools = None
        self.calls_seen = []

    def with_structured_output(self, model):
        return _FakeStructured(self._v)

    def bind_tools(self, tools):
        bound = copy.copy(self)
        bound.tools = tools
        return bound

    async def ainvoke(self, messages, config=None):
        self.calls_seen.append(messages)
        if self.tools and self._script:
            return AIMessage(content="", tool_calls=self._script.pop(0))
        return AIMessage(content="Investigation complete.")


def _tool_call(name, args, call_id="c1"):
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}


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
    assert await rt.resume("fr-test", decision="approve", manager_name="Dana") is True
    rt.odoo_client.set_approval.assert_awaited_once()
    assert rt.odoo_client.set_approval.await_args.kwargs["decision"] == "approve"

    # a second decision (double click / SQS redelivery) must be a no-op
    assert await rt.resume("fr-test", decision="reject", manager_name="Eve") is False
    rt.odoo_client.set_approval.assert_awaited_once()


@pytest.mark.asyncio
async def test_resume_unknown_run_is_refused(monkeypatch):
    rt = _runtime()
    verdict = FraudVerdict(recommendation="review", confidence=0.5, reasoning="x", signals=[])
    monkeypatch.setattr(fraud_mod, "get_chat_model", lambda name: _FakeChat(verdict))
    rt.fraud_graph = fraud_mod.build_fraud_graph(rt)
    assert await rt.resume("fr-never-started", decision="approve") is False
    rt.odoo_client.set_approval.assert_not_awaited()


@pytest.mark.asyncio
async def test_fraud_decision_updates_the_slack_card(monkeypatch):
    """The posted approval card is updated in place with the outcome."""
    rt = _runtime()
    rt.slack_client = AsyncMock()
    rt.slack_client.post_fraud_card.return_value = {"ok": True, "channel": "C1", "ts": "42.1"}
    verdict = FraudVerdict(
        recommendation="approve", confidence=0.7, reasoning="looks fine", signals=[]
    )
    monkeypatch.setattr(fraud_mod, "get_chat_model", lambda name: _FakeChat(verdict))
    rt.fraud_graph = fraud_mod.build_fraud_graph(rt)

    req = FraudTaskRequest(
        odoo_task_ref="AIOPS/9", odoo_task_id=9, risk_level="medium", order={"total": 40}
    )
    await rt.start_fraud(req, run_id="fr-slack")
    rt.slack_client.post_fraud_card.assert_awaited_once()

    await rt.resume("fr-slack", decision="reject", manager_name="Dana")
    rt.slack_client.update_fraud_card.assert_awaited_once()
    kwargs = rt.slack_client.update_fraud_card.await_args.kwargs
    assert kwargs["channel"] == "C1"
    assert kwargs["ts"] == "42.1"
    assert kwargs["decision"] == "reject"
    assert kwargs["manager_name"] == "Dana"


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
    chat = _FakeChat(verdict)
    monkeypatch.setattr(recon_mod, "get_chat_model", lambda name: chat)
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


@pytest.mark.asyncio
async def test_reconciliation_confirms_outcome_in_slack_thread(monkeypatch):
    """The decision is confirmed as a threaded reply on the diagnosis message."""
    rt = _runtime()
    rt.slack_client = AsyncMock()
    rt.slack_client.post_text.return_value = {"ok": True, "channel": "C9", "ts": "9.9"}
    rt.odoo_client.discrepancy_context.return_value = {
        "odoo_on_hand": 5,
        "shopify_available": 5,
        "discrepancy_odoo_minus_shopify": 0,
    }
    verdict = ReconciliationVerdict(
        direction="match",
        root_cause="No divergence",
        recommended_action="no_action",
        corrected_odoo_qty=None,
        shopify_target_qty=None,
        suspect_move_ids=[],
        reasoning="Quantities already agree.",
        confidence=0.9,
    )
    chat = _FakeChat(verdict)
    monkeypatch.setattr(recon_mod, "get_chat_model", lambda name: chat)
    rt.reconciliation_graph = recon_mod.build_reconciliation_graph(rt)

    req = ReconciliationTaskRequest(
        odoo_task_ref="AIOPS/3", odoo_task_id=3, product_id=1, context={}
    )
    await rt.start_reconciliation(req, run_id="rc-slack")
    await rt.resume("rc-slack", decision="approve", manager_name="Dana")

    assert rt.slack_client.post_text.await_count == 2
    confirm = rt.slack_client.post_text.await_args
    assert confirm.kwargs["thread_ts"] == "9.9"
    assert confirm.kwargs["channel"] == "C9"
    assert "Approved" in confirm.args[0]
    assert "no_action" in confirm.args[0]


# --- the investigation loop -----------------------------------------------
def _recon_verdict(**overrides):
    base = {
        "direction": "odoo_higher",
        "root_cause": "Delivery shipped but never validated",
        "recommended_action": "validate_or_investigate_move",
        "corrected_odoo_qty": None,
        "shopify_target_qty": None,
        "suspect_move_ids": [55],
        "reasoning": "Move 55 is aged and still open.",
        "confidence": 0.8,
    }
    base.update(overrides)
    return ReconciliationVerdict(**base)


@pytest.mark.asyncio
async def test_investigation_follows_up_with_tools_before_diagnosing(monkeypatch):
    """The model can ask follow-up questions; their answers reach the verdict.

    This is the non-linear part: `gather` only supplies the snapshot, and the
    model decides for itself to drill into the suspect move.
    """
    rt = _runtime()
    rt.odoo_client.discrepancy_context.return_value = {
        "odoo_on_hand": 10,
        "shopify_available": 3,
        "discrepancy_odoo_minus_shopify": 7,
        "pending_outgoing_moves": [{"id": 55, "state": "assigned", "aged": True}],
    }
    rt.odoo_client.move_details.return_value = [
        {"id": 55, "state": "assigned", "picking": {"name": "WH/OUT/007", "state": "assigned"}}
    ]
    chat = _FakeChat(
        _recon_verdict(),
        tool_script=[[_tool_call("get_move_details", {"move_ids": [55]})]],
    )
    monkeypatch.setattr(recon_mod, "get_chat_model", lambda name: chat)
    rt.reconciliation_graph = recon_mod.build_reconciliation_graph(rt)

    req = ReconciliationTaskRequest(
        odoo_task_ref="AIOPS/4", odoo_task_id=4, product_id=42, context={}
    )
    await rt.start_reconciliation(req, run_id="rc-tools")

    # The model's chosen follow-up actually hit Odoo...
    rt.odoo_client.move_details.assert_awaited_once_with([55])
    # ...and its result was in the transcript the verdict was drawn from.
    cfg = {"configurable": {"thread_id": "rc-tools"}}
    snap = await rt.reconciliation_graph.aget_state(cfg)
    transcript = str(snap.values["messages"])
    assert "WH/OUT/007" in transcript
    assert snap.values["proposal"]["recommended_action"] == "validate_or_investigate_move"


@pytest.mark.asyncio
async def test_investigation_stops_at_the_tool_budget(monkeypatch):
    """A model that keeps asking questions is cut off and made to conclude."""
    rt = _runtime()
    rt.odoo_client.discrepancy_context.return_value = {"odoo_on_hand": 1}
    rt.odoo_client.move_details.return_value = [{"id": 1}]
    # Far more tool turns than the budget allows.
    chat = _FakeChat(
        _recon_verdict(),
        tool_script=[[_tool_call("get_move_details", {"move_ids": [1]})]] * 50,
    )
    monkeypatch.setattr(recon_mod, "get_chat_model", lambda name: chat)
    rt.reconciliation_graph = recon_mod.build_reconciliation_graph(rt)

    req = ReconciliationTaskRequest(
        odoo_task_ref="AIOPS/5", odoo_task_id=5, product_id=9, context={}
    )
    await rt.start_reconciliation(req, run_id="rc-budget")

    cfg = {"configurable": {"thread_id": "rc-budget"}}
    snap = await rt.reconciliation_graph.aget_state(cfg)
    # It ran the full budget and no further, then still produced a verdict.
    assert rt.odoo_client.move_details.await_count == recon_mod.MAX_TOOL_LOOPS
    assert snap.values["tool_loops"] == recon_mod.MAX_TOOL_LOOPS + 1
    assert snap.values["proposal"]["recommended_action"] == "validate_or_investigate_move"
    assert snap.next, "budget-capped run should still reach the approval interrupt"


@pytest.mark.asyncio
async def test_a_failing_tool_does_not_kill_the_run(monkeypatch):
    """Odoo errors come back as data so the model can adapt, not as crashes."""
    rt = _runtime()
    rt.odoo_client.discrepancy_context.return_value = {"odoo_on_hand": 4}
    rt.odoo_client.move_details.side_effect = RuntimeError("Odoo said no")
    chat = _FakeChat(
        _recon_verdict(),
        tool_script=[[_tool_call("get_move_details", {"move_ids": [1]})]],
    )
    monkeypatch.setattr(recon_mod, "get_chat_model", lambda name: chat)
    rt.reconciliation_graph = recon_mod.build_reconciliation_graph(rt)

    req = ReconciliationTaskRequest(
        odoo_task_ref="AIOPS/6", odoo_task_id=6, product_id=9, context={}
    )
    await rt.start_reconciliation(req, run_id="rc-toolfail")

    cfg = {"configurable": {"thread_id": "rc-toolfail"}}
    snap = await rt.reconciliation_graph.aget_state(cfg)
    assert "Odoo said no" in str(snap.values["messages"])
    assert snap.values.get("proposal"), "run should still reach a verdict"


def test_the_toolbelt_covers_every_root_cause():
    """Each cause a stock divergence actually has needs a tool behind it.

    Without one, the model can name the cause and never gather evidence for it —
    which is how `create_missing_sale_order` sat in the verdict enum
    unsupportable for so long.
    """
    rt = _runtime()
    names = {t.name for t in recon_mod._build_tools(rt)}
    for cause, tool_name in {
        "stuck/aged move": "get_move_details",
        "open moves by type": "list_stock_moves",
        "forced on-hand count": "list_stock_moves",  # kinds=['inventory_adjustment']
        "moved to another warehouse": "get_stock_by_location",
        "untracked direct quant write": "check_stock_ledger",
        "undelivered Odoo order": "list_sale_order_lines",
        "Shopify sale Odoo never recorded": "list_shopify_orders",
        "SKU duplicated across variants": "search_products",
    }.items():
        assert tool_name in names, f"no tool backs the '{cause}' root cause"


def test_the_toolbelt_is_read_only():
    """The safety property: the model is never handed a tool that writes.

    Writes stay in `apply`, which dispatches on the verdict's action enum after
    a human approves. If a write ever appears here, a prompt injection in a
    product name or order note is one convincing sentence away from moving
    stock, and the Slack card becomes the only thing standing in its way.
    """
    rt = _runtime()
    names = {t.name for t in recon_mod._build_tools(rt)}
    forbidden = {
        "apply_inventory_patch",
        "push_inventory_to_shopify",
        "set_approval",
        "register_agent_run",
    }
    assert not (names & forbidden)
    # Nothing that merely *looks* like a mutation either.
    mutating = ("apply", "push", "set_", "write", "update", "create", "delete")
    assert not [n for n in names if any(verb in n for verb in mutating)]
