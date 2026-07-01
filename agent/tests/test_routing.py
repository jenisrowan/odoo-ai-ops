"""Tests for event routing and Slack helpers (no external services)."""

import json
from unittest.mock import AsyncMock

import pytest

from app.config import Settings
from app.runtime import AgentRuntime
from app.slack_client import SlackClient, verify_slack_signature


def _runtime() -> AgentRuntime:
    # Bare runtime (no graphs) - we only exercise the routing methods.
    return AgentRuntime(Settings(ai_ops_shared_token="t", valkey_url=""))


@pytest.mark.asyncio
async def test_slack_interaction_routes_to_resume():
    rt = _runtime()
    rt.resume = AsyncMock()
    ctx = json.dumps({"odoo_task_id": 5, "thread_id": "fr-abc", "task_ref": "AIOPS/1"})
    payload = {
        "type": "block_actions",
        "user": {"name": "dana", "username": "dana"},
        "actions": [{"action_id": "ai_ops_reject", "value": ctx}],
    }
    await rt.handle_slack_interaction(payload)
    rt.resume.assert_awaited_once()
    _, kwargs = rt.resume.await_args
    assert kwargs["decision"] == "reject"
    assert kwargs["manager_name"] == "dana"


@pytest.mark.asyncio
async def test_sqs_shopify_message_forwards_to_odoo():
    rt = _runtime()
    rt.forward_webhook = AsyncMock(return_value={"action": "dispatched"})
    await rt.handle_sqs_message({"source": "shopify", "payload": {"order_id": "1"}})
    rt.forward_webhook.assert_awaited_once_with({"order_id": "1"})


@pytest.mark.asyncio
async def test_sqs_slack_message_routes_to_interaction():
    rt = _runtime()
    rt.handle_slack_interaction = AsyncMock()
    await rt.handle_sqs_message({"source": "slack", "payload": {"type": "block_actions"}})
    rt.handle_slack_interaction.assert_awaited_once()


def test_verify_slack_signature_roundtrip():
    import hashlib
    import hmac
    import time

    secret = "shhh"
    ts = str(int(time.time()))
    body = "payload=%7B%7D"
    digest = hmac.new(secret.encode(), f"v0:{ts}:{body}".encode(), hashlib.sha256).hexdigest()
    sig = f"v0={digest}"
    assert verify_slack_signature(secret, ts, body, sig) is True
    assert verify_slack_signature(secret, ts, body, "v0=deadbeef") is False


def test_build_fraud_blocks_has_decision_buttons():
    blocks = SlackClient.build_fraud_blocks(
        task_ref="AIOPS/1",
        odoo_task_id=7,
        thread_id="fr-xyz",
        order={"order_name": "#1001", "total": 250, "currency": "USD"},
        risk_level="high",
        verdict={"recommendation": "reject", "reasoning": "mismatch", "confidence": 0.9},
    )
    actions = [b for b in blocks if b["type"] == "actions"][0]
    action_ids = {e["action_id"] for e in actions["elements"]}
    assert action_ids == {"ai_ops_approve", "ai_ops_reject"}
    # Routing context must be embedded so the resume worker can match it.
    value = json.loads(actions["elements"][0]["value"])
    assert value["thread_id"] == "fr-xyz"
    assert value["odoo_task_id"] == 7
