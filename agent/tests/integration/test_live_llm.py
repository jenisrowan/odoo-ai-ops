"""Live-LLM integration: real Claude calls, end to end, with the trace chain.

``test_fullstack.py`` fakes Anthropic's network round-trip so it can run for
free. That proves every component *around* the model, and nothing about the
model itself. This suite is the other half: real calls to real Claude, against
the real compose stack, with the full telemetry chain asserted at every hop::

    agent ─► Valkey (checkpoint) ─► Langfuse (trace + usage + cost) ─► ClickHouse

Two scenarios, chosen because they exercise opposite shapes of LLM work:

* **High-risk fraud order** - a single structured judgement over a payload the
  model is handed. Deterministic-ish, cheap, and gradeable: we plant specific
  red flags and check the verdict reflects them, then run a *clean* order
  through the same path to prove the model is reading the payload rather than
  reflexively rejecting everything.
* **Stock reconciliation** - a genuine investigation loop where the model
  chooses its own next question. This is the part the fake could never
  exercise: it short-circuits after one turn by design, so until now nothing
  has ever run the loop. We plant a root cause in Odoo (a reasoned inventory
  adjustment) and check the model finds *that* cause using its read-only tools.

Cost
----
Every test here spends money. Reconciliation is the expensive one - it is a
multi-turn loop on the strong model. Gated behind its own flag, separate from
``RUN_INTEGRATION``, so it can never be picked up by an ordinary run.

Run
---
    RUN_LIVE_LLM=1 ./agent/tests/integration/run.sh

(``run.sh`` passes ``.env`` through, which is where ``ANTHROPIC_API_KEY`` lives.)
"""

from __future__ import annotations

import os
import time
import uuid

import httpx
import pytest
import pytest_asyncio

from app.config import Settings
from app.graph.state import FraudVerdict, ReconciliationVerdict
from app.runtime import AgentRuntime
from app.schemas import FraudTaskRequest, ReconciliationTaskRequest

pytestmark = [
    pytest.mark.skipif(
        os.environ.get("RUN_LIVE_LLM") != "1",
        reason="live-LLM test: costs real money; set RUN_LIVE_LLM=1 to run",
    ),
    pytest.mark.skipif(
        not os.environ.get("ANTHROPIC_API_KEY"),
        reason="ANTHROPIC_API_KEY not set",
    ),
]

_CLICKHOUSE_HTTP = os.environ.get("CLICKHOUSE_HTTP", "http://clickhouse:8123")
_CLICKHOUSE_PW = os.environ.get("CLICKHOUSE_PASSWORD", "clickhouse")

# Telemetry is asynchronous: the SDK batches, Langfuse ingests, the worker then
# writes through to ClickHouse. Poll rather than sleep a fixed amount.
_POLL_ATTEMPTS = 20
_POLL_INTERVAL = 3


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest_asyncio.fixture
async def runtime():
    settings = Settings()
    assert settings.anthropic_api_key, "ANTHROPIC_API_KEY must reach the agent's Settings"
    assert settings.langfuse_enabled, "LANGFUSE_HOST + keys must be set (see README)"
    assert settings.valkey_url, "VALKEY_URL must point at the compose Valkey"
    rt = await AgentRuntime.create(settings)
    try:
        yield rt
    finally:
        await rt.aclose()


# ---------------------------------------------------------------------------
# Telemetry chain: agent -> Valkey -> Langfuse -> ClickHouse
# ---------------------------------------------------------------------------
async def _langfuse_traces(settings, session_id: str) -> list[dict]:
    auth = (settings.langfuse_public_key, settings.langfuse_secret_key)
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.get(
            f"{settings.langfuse_host}/api/public/traces",
            params={"sessionId": session_id},
            auth=auth,
        )
        r.raise_for_status()
        return r.json().get("data", [])


async def _langfuse_observations(settings, trace_id: str) -> list[dict]:
    """Every span/generation on a trace, with usage and cost as Langfuse scored it."""
    auth = (settings.langfuse_public_key, settings.langfuse_secret_key)
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.get(
            f"{settings.langfuse_host}/api/public/observations",
            params={"traceId": trace_id, "limit": 100},
            auth=auth,
        )
        r.raise_for_status()
        return r.json().get("data", [])


async def _clickhouse_trace_count(session_id: str) -> int:
    q = f"SELECT count() FROM traces WHERE session_id = '{session_id}'"
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post(
            _CLICKHOUSE_HTTP, params={"user": "default", "password": _CLICKHOUSE_PW}, content=q
        )
        r.raise_for_status()
        return int((r.text or "0").strip() or "0")


async def _poll(fn, *, what: str):
    """Poll an async telemetry probe until it returns something truthy."""
    result = None
    for _ in range(_POLL_ATTEMPTS):
        result = await fn()
        if result:
            return result
        time.sleep(_POLL_INTERVAL)
    raise AssertionError(f"{what} never arrived after {_POLL_ATTEMPTS * _POLL_INTERVAL}s")


async def assert_trace_chain(runtime, session_id: str, run_id: str, *, expect_model: str) -> dict:
    """Assert the run is visible at every hop, and return the usage totals.

    This is the whole point of running against the real stack rather than
    mocking the SDK: each hop is a different process, and a break between any
    two of them loses observability silently in production.
    """
    # 1. Valkey - the workflow's own state survived the process.
    cfg = {"configurable": {"thread_id": run_id}}
    assert await runtime.checkpointer.aget_tuple(cfg) is not None, (
        f"no checkpoint in Valkey for {run_id}"
    )

    from langfuse import get_client

    get_client().flush()

    # 2. Langfuse - the trace left the agent.
    traces = await _poll(
        lambda: _langfuse_traces(runtime.settings, session_id),
        what=f"Langfuse trace for session {session_id}",
    )
    trace_id = traces[0]["id"]

    # 3. The trace carries a real generation: right model, non-zero tokens.
    #    A stubbed call cannot produce these, so this is what distinguishes a
    #    genuine Anthropic round-trip from a fake one.
    observations = await _poll(
        lambda: _langfuse_observations(runtime.settings, trace_id),
        what=f"Langfuse observations for trace {trace_id}",
    )
    generations = [o for o in observations if (o.get("type") or "").upper() == "GENERATION"]
    assert generations, f"no GENERATION observation on trace {trace_id}: {observations!r}"

    models = {(g.get("model") or "") for g in generations}
    assert any(expect_model in m for m in models), (
        f"expected a generation on {expect_model}, saw {models}"
    )

    def _usage(gen, key):
        usage = gen.get("usage") or {}
        details = gen.get("usageDetails") or {}
        return usage.get(key) or details.get(key) or 0

    input_tokens = sum(_usage(g, "input") for g in generations)
    output_tokens = sum(_usage(g, "output") for g in generations)
    assert input_tokens > 0 and output_tokens > 0, (
        f"token usage never reached Langfuse (in={input_tokens} out={output_tokens}); "
        "the callback is attached but usage metadata is being dropped"
    )

    # 4. ClickHouse - the worker persisted it (Langfuse's own store).
    count = await _poll(
        lambda: _clickhouse_trace_count(session_id),
        what=f"ClickHouse row for session {session_id}",
    )
    assert count >= 1

    cost = sum(float(g.get("calculatedTotalCost") or g.get("totalCost") or 0) for g in generations)
    return {
        "generations": len(generations),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost_usd": cost,
    }


# ---------------------------------------------------------------------------
# Odoo helpers
# ---------------------------------------------------------------------------
async def _new_task(rt, task_type: str, **vals) -> tuple[int, str]:
    task_id = await rt.odoo_client.execute_kw(
        "ai.ops.task", "create", [{"task_type": task_type, **vals}]
    )
    rec = await rt.odoo_client.execute_kw("ai.ops.task", "read", [[task_id], ["name"]])
    return task_id, rec[0]["name"]


async def _read_task(rt, task_id: int, fields: list[str]) -> dict:
    rec = await rt.odoo_client.execute_kw("ai.ops.task", "read", [[task_id], fields])
    return rec[0]


async def _set_on_hand(rt, product_id: int, location_id: int, qty: float, reason: str) -> None:
    """Force a product's on-hand count, exactly as a human would in the UI.

    Odoo journals this as a ``stock.move`` flagged ``is_inventory``, carrying the
    reason in ``inventory_name`` and the author in the move's user - which is
    precisely the evidence ``recent_inventory_adjustments`` surfaces to the
    model. The reason travels in the context, the same way Odoo's own
    adjustment-reason wizard passes it.
    """
    # Reuse the product's existing quant at this location. Creating a second one
    # does not *set* the count - Odoo applies it as a further adjustment, so the
    # quantities add up instead of replacing each other.
    existing = await rt.odoo_client.execute_kw(
        "stock.quant",
        "search",
        [[["product_id", "=", product_id], ["location_id", "=", location_id]]],
        {"limit": 1},
    )
    if existing:
        quant_id = existing[0]
        await rt.odoo_client.execute_kw(
            "stock.quant", "write", [[quant_id], {"inventory_quantity": qty}]
        )
    else:
        quant_id = await rt.odoo_client.execute_kw(
            "stock.quant",
            "create",
            [{"product_id": product_id, "location_id": location_id, "inventory_quantity": qty}],
        )
    await rt.odoo_client.execute_kw(
        "stock.quant",
        "action_apply_inventory",
        [[quant_id]],
        {"context": {"inventory_name": reason}},
    )


async def _internal_location(rt) -> int:
    ids = await rt.odoo_client.execute_kw(
        "stock.location", "search", [[["usage", "=", "internal"]]], {"limit": 1}
    )
    assert ids, "no internal stock location in Odoo"
    return ids[0]


# ---------------------------------------------------------------------------
# Scenario payloads
# ---------------------------------------------------------------------------
def _high_risk_order(order_id: str) -> dict:
    """An order carrying the red flags the fraud prompt is told to weigh.

    Every signal here is one the system prompt names explicitly: NEGATIVE
    Shopify facts, a brand-new account, failed AVS/CVV, and a billing/shipping
    country mismatch on a high-value order. If the model cannot reject this, the
    prompt is not doing its job.
    """
    return {
        "id": order_id,
        "order_name": f"#{order_id}",
        "total": 1899.00,
        "currency": "USD",
        "email": "newbuyer9931@mail.example",
        "created_at": "2026-07-22T02:14:00Z",
        "line_items": [
            {"title": "Flagship Phone 512GB", "sku": "PHONE-512", "quantity": 3, "price": "633.00"}
        ],
        "billing_address": {"name": "A Buyer", "country": "United States", "zip": "10001"},
        "shipping_address": {"name": "Other Name", "country": "Nigeria", "zip": "100001"},
        "payment_verification": {"avs_result_code": "N", "cvv_result_code": "N"},
        "customer_history": {"orders_count": 0, "account_age_days": 0, "total_spent": "0.00"},
        "shopify_risk": {
            "risk_level": "high",
            "facts": [
                {
                    "description": "Card billing address does not match shipping",
                    "sentiment": "NEGATIVE",
                },
                {
                    "description": "Payment attempted with 3 cards before succeeding",
                    "sentiment": "NEGATIVE",
                },
                {
                    "description": "Connecting via a known proxy/VPN endpoint",
                    "sentiment": "NEGATIVE",
                },
                {"description": "First order from this customer", "sentiment": "NEGATIVE"},
            ],
        },
    }


def _clean_order(order_id: str) -> dict:
    """The control case: a long-standing customer, everything verified.

    Without this, "always reject" would score full marks on the test above.
    """
    return {
        "id": order_id,
        "order_name": f"#{order_id}",
        "total": 64.00,
        "currency": "USD",
        "email": "regular@example.com",
        "created_at": "2026-07-22T11:02:00Z",
        "line_items": [
            {"title": "Coffee Beans 1kg", "sku": "BEANS-1K", "quantity": 1, "price": "64.00"}
        ],
        "billing_address": {"name": "Sam Regular", "country": "United Kingdom", "zip": "SW1A 1AA"},
        "shipping_address": {"name": "Sam Regular", "country": "United Kingdom", "zip": "SW1A 1AA"},
        "payment_verification": {"avs_result_code": "Y", "cvv_result_code": "M"},
        "customer_history": {
            "orders_count": 27,
            "account_age_days": 1290,
            "total_spent": "1710.00",
        },
        "shopify_risk": {
            "risk_level": "medium",
            "facts": [
                {"description": "Billing and shipping addresses match", "sentiment": "POSITIVE"},
                {"description": "Customer has a consistent order history", "sentiment": "POSITIVE"},
                {"description": "Card verification succeeded", "sentiment": "POSITIVE"},
            ],
        },
    }


# ---------------------------------------------------------------------------
# Scenario 1: high-risk fraud order
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_high_risk_order_gets_a_real_claude_verdict(runtime):
    """Real Claude, real verdict, whole trace chain - and the verdict is graded."""
    task_id, ref = await _new_task(runtime, "fraud", risk_level="high")
    run_id = f"fr-live-{uuid.uuid4().hex[:10]}"
    order = _high_risk_order(f"LIVE-{uuid.uuid4().hex[:8]}")

    await runtime.start_fraud(
        FraudTaskRequest(odoo_task_ref=ref, odoo_task_id=task_id, risk_level="high", order=order),
        run_id=run_id,
    )

    cfg = {"configurable": {"thread_id": run_id}}
    snap = await runtime.fraud_graph.aget_state(cfg)
    assert snap.next, f"workflow did not pause at the interrupt (next={snap.next})"

    # High risk must route to the strong model, not the cheap pre-screen.
    assert snap.values.get("model") == runtime.settings.model_high, snap.values.get("model")

    # --- the verdict is real and well-formed ---------------------------------
    raw = snap.values.get("verdict")
    assert raw, "no verdict in state - the model call produced nothing"
    verdict = FraudVerdict(**raw)  # re-validating proves the live output fits the schema

    # --- evaluation: did the model actually read the payload? ----------------
    assert verdict.recommendation != "approve", (
        f"Claude approved a blatantly fraudulent order: {verdict.model_dump()}"
    )
    assert verdict.signals, "no risk signals listed for an order full of them"

    haystack = f"{verdict.reasoning} {' '.join(verdict.signals)}".lower()
    planted = ("avs", "cvv", "billing", "shipping", "proxy", "vpn", "new", "first", "card")
    assert any(flag in haystack for flag in planted), (
        f"verdict cites none of the planted red flags: {verdict.model_dump()}"
    )

    # The prompt asks for ~50 words, 60 max. Prompt compliance is part of the
    # contract: the Slack card has to stay readable.
    words = len(verdict.reasoning.split())
    assert 5 <= words <= 90, (
        f"reasoning is {words} words, well outside the brief: {verdict.reasoning}"
    )

    # --- Odoo received the analysis -----------------------------------------
    rec = await _read_task(runtime, task_id, ["state", "analysis", "agent_run_id"])
    assert rec["state"] == "pending_approval", rec
    assert rec["analysis"], "the AI analysis was never written back to the Odoo task"
    assert rec["agent_run_id"] == run_id, rec

    # --- the chain ------------------------------------------------------------
    usage = await assert_trace_chain(runtime, ref, run_id, expect_model=runtime.settings.model_high)
    print(f"\n[fraud/high-risk] {verdict.recommendation} @ {verdict.confidence:.2f} | {usage}")

    # --- and it still resumes to completion ----------------------------------
    await runtime.resume(run_id, decision="reject", manager_name="live-itest")
    assert (await runtime.fraud_graph.aget_state(cfg)).next == ()
    assert (await _read_task(runtime, task_id, ["decision"]))["decision"] == "reject"


@pytest.mark.asyncio
async def test_clean_order_is_not_rejected(runtime):
    """The discriminating half: a good order must not be rejected.

    Paired with the test above, this is what makes either of them meaningful -
    a model that always says 'reject' passes one and fails this.
    """
    task_id, ref = await _new_task(runtime, "fraud", risk_level="medium")
    run_id = f"fr-live-{uuid.uuid4().hex[:10]}"

    await runtime.start_fraud(
        FraudTaskRequest(
            odoo_task_ref=ref,
            odoo_task_id=task_id,
            risk_level="medium",
            order=_clean_order(f"LIVE-{uuid.uuid4().hex[:8]}"),
        ),
        run_id=run_id,
    )

    snap = await runtime.fraud_graph.aget_state({"configurable": {"thread_id": run_id}})
    # Medium risk takes the cheap tier - the cost model depends on this holding.
    assert snap.values.get("model") == runtime.settings.model_medium, snap.values.get("model")

    verdict = FraudVerdict(**snap.values["verdict"])
    assert verdict.recommendation != "reject", (
        f"Claude rejected a clean order - the prompt is over-triggering: {verdict.model_dump()}"
    )

    usage = await assert_trace_chain(
        runtime, ref, run_id, expect_model=runtime.settings.model_medium
    )
    print(f"\n[fraud/clean] {verdict.recommendation} @ {verdict.confidence:.2f} | {usage}")


# ---------------------------------------------------------------------------
# Scenario 2: stock reconciliation across Shopify and Odoo
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_reconciliation_investigates_and_finds_the_planted_cause(runtime):
    """The investigation loop, for real - the part the fake can never reach.

    We plant a cause in Odoo that is discoverable only by using the tools: an
    inventory adjustment that knocked the on-hand count down, carrying a
    distinctive reason string. A model that guesses instead of investigating
    cannot name it.

    When ``SHOPIFY_LIVE_TEST_SKU`` is set the product is given that SKU, so the
    Shopify side of the comparison is a genuine live quantity and the run really
    does span both systems. Without it the SKU exists only in Odoo, Shopify
    reports nothing, and the direction check is skipped - the investigation
    still runs, but it is no longer a cross-system reconciliation.
    """
    location_id = await _internal_location(runtime)
    marker = uuid.uuid4().hex[:6].upper()
    live_sku = os.environ.get("SHOPIFY_LIVE_TEST_SKU")
    sku = live_sku or f"SKU-LIVE-{marker}"

    product_id = await runtime.odoo_client.execute_kw(
        "product.product",
        "create",
        [{"name": f"Live Recon {marker}", "default_code": sku, "is_storable": True}],
    )

    # Baseline the Odoo count against whatever Shopify reports, so the only gap
    # is the one we plant. The plant moves the count UP: a surplus works from any
    # starting figure, whereas a shortfall would drive a low-stock SKU negative.
    probe = await runtime.odoo_client.discrepancy_context(product_id)
    shopify_available = probe.get("shopify_available")
    baseline = float(shopify_available) if shopify_available is not None else 40.0
    surplus = 7.0
    reason = f"Stocktake miscount {marker} - recount pending"

    await _set_on_hand(runtime, product_id, location_id, baseline, f"Opening count {marker}")
    await _set_on_hand(runtime, product_id, location_id, baseline + surplus, reason)

    task_id, ref = await _new_task(runtime, "reconciliation", product_id=product_id)
    run_id = f"rc-live-{uuid.uuid4().hex[:10]}"

    await runtime.start_reconciliation(
        ReconciliationTaskRequest(
            odoo_task_ref=ref, odoo_task_id=task_id, product_id=product_id, context={}
        ),
        run_id=run_id,
    )

    cfg = {"configurable": {"thread_id": run_id}}
    snap = await runtime.reconciliation_graph.aget_state(cfg)
    assert snap.next, f"reconciliation did not pause at the interrupt (next={snap.next})"

    # --- the loop actually ran ------------------------------------------------
    loops = snap.values.get("tool_loops", 0)
    assert loops >= 1, "the investigation never took a turn"
    tool_calls = [
        call
        for message in snap.values.get("messages", [])
        for call in (getattr(message, "tool_calls", None) or [])
    ]
    assert tool_calls, "the model concluded without calling a single read-only tool"
    used = {c.get("name") for c in tool_calls}
    print(f"\n[recon] {loops} loops, tools used: {sorted(used)}")

    # --- the verdict is well-formed and inside the closed vocabulary ---------
    proposal = snap.values.get("proposal")
    assert proposal, "no proposal - the structured call produced nothing"
    verdict = ReconciliationVerdict(**proposal)
    assert verdict.recommended_action in {
        "update_shopify",
        "adjust_odoo",
        "validate_or_investigate_move",
        "create_missing_sale_order",
        "no_action",
    }
    assert all(isinstance(i, int) for i in verdict.suspect_move_ids)

    # --- evaluation: the direction must match the arithmetic ----------------
    discrepancy = snap.values.get("discrepancy") or {}
    delta = discrepancy.get("discrepancy_odoo_minus_shopify")
    if delta is not None:
        expected = "odoo_lower" if delta < 0 else "odoo_higher" if delta > 0 else "match"
        assert verdict.direction == expected, (
            f"direction {verdict.direction!r} contradicts the numbers "
            f"(odoo-shopify={delta}): {verdict.model_dump()}"
        )

    # --- evaluation: did it find what we planted? ---------------------------
    haystack = f"{verdict.root_cause} {verdict.reasoning}".lower()
    assert marker.lower() in haystack or "adjust" in haystack or "count" in haystack, (
        f"the investigation missed the planted inventory adjustment ({reason!r}): "
        f"{verdict.model_dump()}"
    )

    # --- Odoo has the diagnosis ----------------------------------------------
    rec = await _read_task(runtime, task_id, ["state", "analysis"])
    assert rec["state"] == "pending_approval", rec
    assert rec["analysis"], "the diagnosis was never written back to Odoo"

    # --- the chain ------------------------------------------------------------
    usage = await assert_trace_chain(runtime, ref, run_id, expect_model=runtime.settings.model_high)
    # The loop means several generations, not one - that is the shape we expect.
    assert usage["generations"] >= 2, (
        f"expected the investigation loop plus the structured verdict, got {usage}"
    )
    print(f"[recon] {verdict.direction} / {verdict.recommended_action} | {usage}")

    # --- rejection must leave stock untouched --------------------------------
    before = await runtime.odoo_client.execute_kw(
        "product.product", "read", [[product_id], ["qty_available"]]
    )
    await runtime.resume(run_id, decision="reject", manager_name="live-itest")
    after = await runtime.odoo_client.execute_kw(
        "product.product", "read", [[product_id], ["qty_available"]]
    )
    assert after[0]["qty_available"] == before[0]["qty_available"], (
        f"a rejected diagnosis moved stock: {before} -> {after}"
    )
