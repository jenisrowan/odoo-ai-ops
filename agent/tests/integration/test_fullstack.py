"""Full-stack integration tests against the running docker-compose stack.

Agent-direct: signed Shopify webhooks are HMAC-verified by a local edge shim
(standing in for the production Lambda) and handed to the agent's *real* forward
path -> Odoo. Workflows run with a **fake LLM** (no Claude, no cost), and state is
asserted across the real backends: Odoo, Valkey, Langfuse and ClickHouse.

Covers the happy path *and* the failure/edge cases: bad signature, duplicate
delivery, unknown topic, each gatekeeper decision branch (ignore / auto-reject /
escalate), checkpoint durability across a fresh runtime, and telemetry export.

Gated: RUN_INTEGRATION=1, run on the compose network. See README.md.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
import urllib.parse
import uuid

import httpx
import pytest
import pytest_asyncio
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from app.config import Settings
from app.graph.state import FraudVerdict, ReconciliationVerdict
from app.runtime import AgentRuntime
from app.schemas import FraudTaskRequest, ReconciliationTaskRequest

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_INTEGRATION") != "1",
    reason="integration test: set RUN_INTEGRATION=1 and run against the docker-compose stack",
)

_WEBHOOK_SECRET = os.environ.get("SHOPIFY_WEBHOOK_SECRET", "").encode()
_CLICKHOUSE_HTTP = os.environ.get("CLICKHOUSE_HTTP", "http://clickhouse:8123")
_CLICKHOUSE_PW = os.environ.get("CLICKHOUSE_PASSWORD", "clickhouse")
# The edge shim, as seen from inside the compose network (run_edge_shim.sh).
_SHIM_URL = os.environ.get("SHIM_URL", "http://aiops-edge-shim:9000")


# --------------------------------------------------------------------------
# Edge shim: exactly what the production Lambda does before SQS.
# --------------------------------------------------------------------------
def _sign(body: bytes, secret: bytes = _WEBHOOK_SECRET) -> str:
    return base64.b64encode(hmac.new(secret, body, hashlib.sha256).digest()).decode()


def _edge_verify(body: bytes, provided_sig: str) -> bool:
    return hmac.compare_digest(_sign(body), provided_sig)


def _edge_envelope(topic: str, payload: dict, secret: bytes = _WEBHOOK_SECRET) -> dict:
    """Sign + verify like the Lambda, then build the SQS envelope it enqueues."""
    body = json.dumps(payload).encode()
    sig = _sign(body, secret)
    if not _edge_verify(body, sig):
        raise AssertionError("edge rejected the signature")
    return {"source": "shopify", "topic": topic, "payload": payload}


def _order(order_id: str, total: str = "250.00") -> dict:
    return {
        "id": order_id,
        "name": f"#{order_id}",
        "currency": "USD",
        "total_price": total,
        "email": "itest@example.com",
        "customer": {"first_name": "Iggy", "last_name": "Test", "email": "itest@example.com"},
        "line_items": [
            {"title": "Widget", "sku": "SKU-ITEST", "quantity": 1, "price": total}
        ],
    }


# --------------------------------------------------------------------------
# Simulated Claude
#
# Only Anthropic's *network round-trip* is stubbed, with the shape Anthropic
# really returns for a structured call: an assistant message whose content is a
# `tool_use` block carrying the verdict as the tool's `input`, stop_reason
# "tool_use", plus usage metadata.
#
# Everything we own stays real - the actual ChatAnthropic client, the real
# `with_structured_output()` tool binding, and the real Pydantic tool parser that
# turns the tool_call args into a FraudVerdict / ReconciliationVerdict. If the
# verdict model and what the chain can actually parse ever drift apart, these
# tests fail; a fake that simply hands back a finished verdict object could never
# catch that. It also keeps the Langfuse callback path real (model name + token
# usage flow through as they would in production).
# --------------------------------------------------------------------------
_VERDICT_ARGS = {
    "recommendation": "reject",
    "confidence": 0.91,
    "reasoning": "integration-test synthetic verdict",
    "signals": ["itest"],
}

_RECON_ARGS = {
    "direction": "odoo_lower",
    "root_cause": "integration test: restock never recorded in Odoo",
    "recommended_action": "adjust_odoo",
    "shopify_target_qty": None,
    "corrected_odoo_qty": 7.0,
    "suspect_move_ids": [],
    "reasoning": "integration-test synthetic diagnosis",
    "confidence": 0.8,
}


def _simulate_claude(monkeypatch, schema, args: dict) -> None:
    """Replace Anthropic's HTTP call with a realistic `tool_use` response."""

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        tool_id = "toolu_" + uuid.uuid4().hex[:20]
        message = AIMessage(
            content=[{"type": "tool_use", "id": tool_id, "name": schema.__name__, "input": args}],
            tool_calls=[
                {"name": schema.__name__, "args": args, "id": tool_id, "type": "tool_call"}
            ],
            response_metadata={
                "model_name": getattr(self, "model", "claude-sonnet-5"),
                "stop_reason": "tool_use",
            },
            usage_metadata={"input_tokens": 812, "output_tokens": 96, "total_tokens": 908},
        )
        return ChatResult(generations=[ChatGeneration(message=message)])

    monkeypatch.setattr(ChatAnthropic, "_agenerate", _agenerate)


async def _langfuse_trace_count(settings, session_id: str) -> int:
    auth = (settings.langfuse_public_key, settings.langfuse_secret_key)
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(
            f"{settings.langfuse_host}/api/public/traces",
            params={"sessionId": session_id},
            auth=auth,
        )
        r.raise_for_status()
        return len(r.json().get("data", []))


async def _clickhouse_trace_count(session_id: str) -> int:
    q = f"SELECT count() FROM traces WHERE session_id = '{session_id}'"
    params = {"user": "default", "password": _CLICKHOUSE_PW}
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post(_CLICKHOUSE_HTTP, params=params, content=q)
        r.raise_for_status()
        return int((r.text or "0").strip() or "0")


@pytest_asyncio.fixture
async def runtime():
    settings = Settings()
    assert settings.langfuse_enabled, "LANGFUSE_HOST + keys must be set (see README)"
    assert settings.valkey_url, "VALKEY_URL must point at the compose Valkey"
    rt = await AgentRuntime.create(settings)
    try:
        yield rt
    finally:
        await rt.aclose()


async def _count_orders(rt, oid: str) -> int:
    return await rt.odoo_client.execute_kw(
        "sale.order", "search_count", [[["shopify_order_id", "=", oid]]]
    )


async def _read_task(rt, task_id: int, fields: list[str]) -> dict:
    rec = await rt.odoo_client.execute_kw("ai.ops.task", "read", [[task_id], fields])
    return rec[0]


async def _new_task(rt, task_type: str, **vals) -> tuple[int, str]:
    """Create a real ai.ops.task directly (no gatekeeper dispatch to race us)."""
    task_id = await rt.odoo_client.execute_kw(
        "ai.ops.task", "create", [{"task_type": task_type, **vals}]
    )
    return task_id, (await _read_task(rt, task_id, ["name"]))["name"]


async def _new_storable_product(rt, label: str) -> int:
    sku = f"SKU-RECON-{uuid.uuid4().hex[:8]}"
    return await rt.odoo_client.execute_kw(
        "product.product",
        "create",
        [{"name": f"{label} {sku}", "default_code": sku, "is_storable": True}],
    )


# --------------------------------------------------------------------------
# Ingress: webhook -> agent -> Odoo
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_orders_create_ingests_into_odoo(runtime):
    oid = f"ITEST-{uuid.uuid4().hex[:10]}"
    await runtime.handle_sqs_message(_edge_envelope("orders/create", _order(oid)))
    assert await _count_orders(runtime, oid) == 1


@pytest.mark.asyncio
async def test_orders_create_is_idempotent_on_redelivery(runtime):
    """SQS is at-least-once and Shopify retries: a redelivery must not duplicate."""
    oid = f"ITEST-{uuid.uuid4().hex[:10]}"
    env = _edge_envelope("orders/create", _order(oid))
    await runtime.handle_sqs_message(env)
    await runtime.handle_sqs_message(env)
    assert await _count_orders(runtime, oid) == 1


@pytest.mark.asyncio
async def test_edge_rejects_bad_signature_so_nothing_reaches_odoo(runtime):
    """A payload signed with the wrong secret must never get past the edge."""
    oid = f"ITEST-{uuid.uuid4().hex[:10]}"
    payload = _order(oid)
    body = json.dumps(payload).encode()
    forged = _sign(body, b"wrong-secret")
    assert not _edge_verify(body, forged)
    with pytest.raises(AssertionError):
        _edge_envelope("orders/create", payload, secret=b"wrong-secret-2")
    # nothing was forwarded, so Odoo has no such order
    assert await _count_orders(runtime, oid) == 0


# --------------------------------------------------------------------------
# Gatekeeper decision branches (real Odoo logic, via the agent)
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_low_risk_order_is_ignored(runtime):
    oid = f"ITEST-{uuid.uuid4().hex[:10]}"
    await runtime.handle_sqs_message(_edge_envelope("orders/create", _order(oid, "5.00")))
    res = await runtime.forward_webhook(
        {"id": oid, "risk_level": "low"}, topic="orders/risk_assessment_changed"
    )
    assert res["action"] == "ignored", res


@pytest.mark.asyncio
async def test_cheap_high_risk_order_is_auto_rejected(runtime):
    """Cheap + risky => cancelled with zero LLM spend (the bypass rule)."""
    oid = f"ITEST-{uuid.uuid4().hex[:10]}"
    await runtime.handle_sqs_message(_edge_envelope("orders/create", _order(oid, "5.00")))
    res = await runtime.forward_webhook(
        {"id": oid, "risk_level": "high"}, topic="orders/risk_assessment_changed"
    )
    assert res["action"] == "auto_reject", res
    assert res["order_total"] == 5.0, res


@pytest.mark.asyncio
async def test_expensive_high_risk_order_escalates_not_auto_rejected(runtime):
    """Above the threshold the order must NOT be auto-cancelled; it escalates."""
    oid = f"ITEST-{uuid.uuid4().hex[:10]}"
    await runtime.handle_sqs_message(_edge_envelope("orders/create", _order(oid, "250.00")))
    res = await runtime.forward_webhook(
        {"id": oid, "risk_level": "high"}, topic="orders/risk_assessment_changed"
    )
    assert res["action"] != "auto_reject", res
    assert res["order_total"] == 250.0, res


@pytest.mark.asyncio
async def test_risk_webhook_without_total_recovers_it_from_the_order(runtime):
    """The risk payload carries no total; it must be recovered from the sale.order."""
    oid = f"ITEST-{uuid.uuid4().hex[:10]}"
    await runtime.handle_sqs_message(_edge_envelope("orders/create", _order(oid, "5.00")))
    res = await runtime.forward_webhook(
        {"id": oid, "risk_level": "high"}, topic="orders/risk_assessment_changed"
    )
    assert res["order_total"] == 5.0, res


@pytest.mark.asyncio
async def test_unknown_order_with_unknown_total_is_escalated_never_cancelled(runtime):
    """An assessment for an order Odoo never imported must not be auto-cancelled."""
    oid = f"ITEST-UNKNOWN-{uuid.uuid4().hex[:8]}"
    res = await runtime.forward_webhook(
        {"id": oid, "risk_level": "high"}, topic="orders/risk_assessment_changed"
    )
    assert res["action"] != "auto_reject", res


# --------------------------------------------------------------------------
# Workflow state: Valkey durability + resume
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_fraud_workflow_persists_to_valkey_and_resumes(runtime, monkeypatch):
    _simulate_claude(monkeypatch, FraudVerdict, _VERDICT_ARGS)

    run_id = f"fr-itest-{uuid.uuid4().hex[:10]}"
    ref = f"AIOPS-ITEST-{uuid.uuid4().hex[:10]}"
    req = FraudTaskRequest(
        odoo_task_ref=ref, odoo_task_id=None, risk_level="high", order={"total": 250}
    )
    await runtime.start_fraud(req, run_id=run_id)

    cfg = {"configurable": {"thread_id": run_id}}
    snap = await runtime.fraud_graph.aget_state(cfg)
    assert snap.next, f"workflow did not pause at the interrupt (next={snap.next})"
    assert await runtime.checkpointer.aget_tuple(cfg) is not None, "no checkpoint in Valkey"

    await runtime.resume(run_id, decision="approve", manager_name="itest")
    snap2 = await runtime.fraud_graph.aget_state(cfg)
    assert snap2.next == (), f"workflow not complete after resume (next={snap2.next})"


@pytest.mark.asyncio
async def test_paused_workflow_is_resumable_by_a_fresh_runtime(monkeypatch):
    """The whole point of Valkey: a *different* process must be able to resume.

    Start the workflow on one runtime, tear it down, then resume on a brand-new
    runtime - proving the state lives in Valkey, not in process memory.
    """
    _simulate_claude(monkeypatch, FraudVerdict, _VERDICT_ARGS)
    settings = Settings()
    run_id = f"fr-itest-{uuid.uuid4().hex[:10]}"
    ref = f"AIOPS-ITEST-{uuid.uuid4().hex[:10]}"

    rt1 = await AgentRuntime.create(settings)
    try:
        req = FraudTaskRequest(
            odoo_task_ref=ref, odoo_task_id=None, risk_level="high", order={"total": 250}
        )
        await rt1.start_fraud(req, run_id=run_id)
    finally:
        await rt1.aclose()  # process 1 is gone

    rt2 = await AgentRuntime.create(settings)
    try:
        cfg = {"configurable": {"thread_id": run_id}}
        snap = await rt2.fraud_graph.aget_state(cfg)
        assert snap.next, "fresh runtime could not see the paused state in Valkey"
        await rt2.resume(run_id, decision="reject", manager_name="itest")
        snap2 = await rt2.fraud_graph.aget_state(cfg)
        assert snap2.next == (), "fresh runtime could not complete the workflow"
    finally:
        await rt2.aclose()


# --------------------------------------------------------------------------
# Agent -> Odoo writeback (JSON-RPC): the half that persists HITL decisions.
# This is the path that was silently broken by the wrong ODOO_DB.
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_workflow_writes_back_to_the_odoo_task(runtime, monkeypatch):
    """notify -> register_agent_run and finalize -> set_approval must really
    land on the ai.ops.task in Odoo (not just be 'called')."""
    _simulate_claude(monkeypatch, FraudVerdict, _VERDICT_ARGS)

    # A real task, created directly so the gatekeeper's dispatch can't race us.
    task_id, ref = await _new_task(runtime, "fraud", risk_level="high")
    assert (await _read_task(runtime, task_id, ["state"]))["state"] == "draft"

    run_id = f"fr-itest-{uuid.uuid4().hex[:10]}"
    req = FraudTaskRequest(
        odoo_task_ref=ref, odoo_task_id=task_id, risk_level="high", order={"total": 250}
    )
    await runtime.start_fraud(req, run_id=run_id)

    # notify -> register_agent_run wrote the pending state + the AI analysis.
    rec = await _read_task(runtime, task_id, ["state", "analysis", "agent_run_id"])
    assert rec["state"] == "pending_approval", rec
    assert "synthetic verdict" in (rec["analysis"] or ""), rec
    assert rec["agent_run_id"] == run_id, rec

    # finalize -> set_approval persisted the manager's decision. Note the state
    # machine: ai_ops_set_approval writes approved/rejected and then immediately
    # writes 'done' ("the workflow is now resolved"), so BOTH outcomes terminate
    # at 'done' and the outcome itself lives in `decision`.
    await runtime.resume(run_id, decision="approve", manager_name="itest-manager")
    rec = await _read_task(runtime, task_id, ["state", "decision", "approver_name"])
    assert rec["decision"] == "approve", rec
    assert rec["state"] == "done", rec
    assert rec["approver_name"] == "itest-manager", rec


# --------------------------------------------------------------------------
# Path 1: inventory reconciliation - the OTHER AI workflow.
# gather (real Odoo evidence) -> diagnose (fake AI) -> notify -> interrupt
# -> apply (real Odoo stock write)
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_reconciliation_workflow_gathers_pauses_and_applies_to_odoo(runtime, monkeypatch):
    _simulate_claude(monkeypatch, ReconciliationVerdict, _RECON_ARGS)

    product_id = await _new_storable_product(runtime, "Recon Test")
    task_id, ref = await _new_task(runtime, "reconciliation", product_id=product_id)

    run_id = f"rc-itest-{uuid.uuid4().hex[:10]}"
    req = ReconciliationTaskRequest(
        odoo_task_ref=ref, odoo_task_id=task_id, product_id=product_id, context={}
    )
    await runtime.start_reconciliation(req, run_id=run_id)

    # gather pulled real evidence from Odoo; notify wrote the diagnosis back.
    cfg = {"configurable": {"thread_id": run_id}}
    snap = await runtime.reconciliation_graph.aget_state(cfg)
    assert snap.next, f"reconciliation did not pause at the interrupt (next={snap.next})"
    assert snap.values.get("discrepancy") is not None, "gather produced no discrepancy context"
    assert await runtime.checkpointer.aget_tuple(cfg) is not None, "no checkpoint in Valkey"
    rec = await _read_task(runtime, task_id, ["state", "analysis"])
    assert rec["state"] == "pending_approval", rec
    assert "restock never recorded" in (rec["analysis"] or ""), rec

    # Approving must actually move stock in Odoo (the write path).
    await runtime.resume(run_id, decision="approve", manager_name="itest-manager")
    snap2 = await runtime.reconciliation_graph.aget_state(cfg)
    assert snap2.next == (), f"reconciliation not complete after resume (next={snap2.next})"
    prod = await runtime.odoo_client.execute_kw(
        "product.product", "read", [[product_id], ["qty_available"]]
    )
    assert prod[0]["qty_available"] == 7.0, f"approved adjust_odoo never moved stock: {prod}"
    assert (await _read_task(runtime, task_id, ["decision"]))["decision"] == "approve"


@pytest.mark.asyncio
async def test_reconciliation_rejection_does_not_touch_stock(runtime, monkeypatch):
    """A rejected diagnosis must never write inventory."""
    _simulate_claude(
        monkeypatch, ReconciliationVerdict, {**_RECON_ARGS, "corrected_odoo_qty": 99.0}
    )

    product_id = await _new_storable_product(runtime, "Recon Rej")
    task_id, ref = await _new_task(runtime, "reconciliation", product_id=product_id)

    run_id = f"rc-itest-{uuid.uuid4().hex[:10]}"
    req = ReconciliationTaskRequest(
        odoo_task_ref=ref, odoo_task_id=task_id, product_id=product_id, context={}
    )
    await runtime.start_reconciliation(req, run_id=run_id)
    await runtime.resume(run_id, decision="reject", manager_name="itest-manager")

    prod = await runtime.odoo_client.execute_kw(
        "product.product", "read", [[product_id], ["qty_available"]]
    )
    assert prod[0]["qty_available"] == 0.0, f"rejected diagnosis still moved stock: {prod}"


# --------------------------------------------------------------------------
# Slack HITL loop (needs SLACK_BOT_TOKEN + SLACK_CHANNEL in .env)
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_slack_approval_card_is_really_posted(runtime, monkeypatch):
    """Posts a real Block Kit card: proves the token, the channel and the block
    payload are all valid (Slack silently 200s with ok=false otherwise).

    The card is deliberately backed by a REAL paused workflow and a REAL
    ai.ops.task, so the buttons in Slack are actually clickable end-to-end
    (click -> /webhooks/slack -> resume from Valkey -> Odoo writeback). A card
    posted with a dangling thread_id would 'look' fine but fail on click.
    """
    if not runtime.settings.slack_enabled:
        pytest.skip("SLACK_BOT_TOKEN + SLACK_CHANNEL not configured")

    _simulate_claude(monkeypatch, FraudVerdict, _VERDICT_ARGS)

    task_id, ref = await _new_task(runtime, "fraud", risk_level="high")
    run_id = f"fr-itest-{uuid.uuid4().hex[:10]}"
    req = FraudTaskRequest(
        odoo_task_ref=ref, odoo_task_id=task_id, risk_level="high", order={"total": 250}
    )
    await runtime.start_fraud(req, run_id=run_id)  # paused in Valkey

    body = await runtime.slack_client.post_fraud_card(
        task_ref=ref,
        odoo_task_id=task_id,
        thread_id=run_id,  # a real paused thread -> the buttons really work
        order={"total": 250, "id": "ITEST"},
        risk_level="high",
        verdict={"recommendation": "reject", "confidence": 0.91, "reasoning": "integration test"},
    )
    assert body.get("ok") is True, f"Slack rejected the card: {body.get('error')} ({body})"


@pytest.mark.asyncio
async def test_real_slack_signed_click_drives_the_shim_to_odoo(runtime, monkeypatch):
    """The real Slack path over HTTP: a v0-signed interaction POSTed to the edge
    shim must verify, resume the workflow **in the shim's own process** (state
    comes from Valkey, not memory), and write the decision back to Odoo.

    Skips unless the shim is up (run_edge_shim.sh) and SLACK_SIGNING_SECRET is set.
    """
    secret = os.environ.get("SLACK_SIGNING_SECRET", "")
    if not secret:
        pytest.skip("SLACK_SIGNING_SECRET not set")
    async with httpx.AsyncClient(timeout=10) as c:
        try:
            await c.get(f"{_SHIM_URL}/healthz")
        except Exception:
            pytest.skip(f"edge shim not reachable at {_SHIM_URL}")

    _simulate_claude(monkeypatch, FraudVerdict, _VERDICT_ARGS)

    # Pause a real workflow against a real task (this process).
    task_id, ref = await _new_task(runtime, "fraud", risk_level="high")
    run_id = f"fr-itest-{uuid.uuid4().hex[:10]}"
    req = FraudTaskRequest(
        odoo_task_ref=ref, odoo_task_id=task_id, risk_level="high", order={"total": 250}
    )
    await runtime.start_fraud(req, run_id=run_id)

    # Exactly what Slack POSTs on a Reject click, signed the way Slack signs it.
    interaction = {
        "type": "block_actions",
        "user": {"name": "itest-manager"},
        "actions": [
            {
                "action_id": "ai_ops_reject",
                "value": json.dumps({"thread_id": run_id, "odoo_task_id": task_id}),
            }
        ],
    }
    body = "payload=" + urllib.parse.quote(json.dumps(interaction))
    ts = str(int(time.time()))
    sig = "v0=" + hmac.new(secret.encode(), f"v0:{ts}:{body}".encode(), hashlib.sha256).hexdigest()

    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(
            f"{_SHIM_URL}/webhooks/slack",
            content=body,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "X-Slack-Request-Timestamp": ts,
                "X-Slack-Signature": sig,
            },
        )
    assert r.status_code == 200, f"shim rejected the signed Slack click: {r.status_code} {r.text}"

    # The SHIM's process resumed it from Valkey and wrote the decision to Odoo.
    rec = await _read_task(runtime, task_id, ["state", "decision", "approver_name"])
    assert rec["decision"] == "reject", rec
    assert rec["state"] == "done", rec
    assert rec["approver_name"] == "itest-manager", rec


@pytest.mark.asyncio
async def test_slack_button_click_resumes_the_paused_workflow(runtime, monkeypatch):
    """The other half of HITL: an interactive button payload must route back to
    the right paused thread and drive it to completion."""
    _simulate_claude(monkeypatch, FraudVerdict, _VERDICT_ARGS)

    run_id = f"fr-itest-{uuid.uuid4().hex[:10]}"
    ref = f"AIOPS-ITEST-{uuid.uuid4().hex[:10]}"
    req = FraudTaskRequest(
        odoo_task_ref=ref, odoo_task_id=None, risk_level="high", order={"total": 250}
    )
    await runtime.start_fraud(req, run_id=run_id)

    cfg = {"configurable": {"thread_id": run_id}}
    assert (await runtime.fraud_graph.aget_state(cfg)).next, "workflow should be paused"

    # Exactly what Slack sends when a manager clicks Reject on the card.
    interaction = {
        "type": "block_actions",
        "user": {"name": "itest-manager"},
        "actions": [
            {
                "action_id": "ai_ops_reject",
                "value": json.dumps({"thread_id": run_id, "odoo_task_id": None}),
            }
        ],
    }
    await runtime.handle_slack_interaction(interaction)
    snap = await runtime.fraud_graph.aget_state(cfg)
    assert snap.next == (), f"button click did not complete the workflow (next={snap.next})"
    assert snap.values.get("decision") == "reject", snap.values


# --------------------------------------------------------------------------
# Telemetry: Langfuse + ClickHouse
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_workflow_trace_reaches_langfuse_and_clickhouse(runtime, monkeypatch):
    _simulate_claude(monkeypatch, FraudVerdict, _VERDICT_ARGS)

    ref = f"AIOPS-ITEST-{uuid.uuid4().hex[:10]}"
    run_id = f"fr-itest-{uuid.uuid4().hex[:10]}"
    req = FraudTaskRequest(
        odoo_task_ref=ref, odoo_task_id=None, risk_level="high", order={"total": 250}
    )
    await runtime.start_fraud(req, run_id=run_id)

    from langfuse import get_client

    get_client().flush()

    lf = 0
    for _ in range(20):
        lf = await _langfuse_trace_count(runtime.settings, ref)
        if lf:
            break
        time.sleep(3)
    assert lf >= 1, f"no Langfuse trace for session {ref} (telemetry never left the agent)"

    ch = 0
    for _ in range(20):
        ch = await _clickhouse_trace_count(ref)
        if ch:
            break
        time.sleep(3)
    assert ch >= 1, f"trace {ref} reached Langfuse but never landed in ClickHouse"
