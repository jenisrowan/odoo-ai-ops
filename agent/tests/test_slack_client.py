"""Unit tests for the Slack Block Kit fraud card (``build_fraud_blocks``).

Pure block-assembly tests - no Slack HTTP is involved, so ``build_fraud_blocks``
is exercised as the static method it is.
"""

from app.slack_client import SlackClient


def _blocks(**over):
    kwargs = dict(
        task_ref="AIOPS/1",
        odoo_task_id=5,
        thread_id="fr-1",
        order={"order_name": "#1001", "total": 250, "currency": "USD"},
        risk_level="high",
        verdict={
            "recommendation": "reject",
            "confidence": 0.9,
            # The ~50-word "what's wrong" report the manager reads.
            "reasoning": "New unverified account; AVS and CVV both failed on a $250 order.",
            "signals": ["avs_failed", "cvv_failed", "new_account"],
        },
    )
    kwargs.update(over)
    return SlackClient.build_fraud_blocks(**kwargs)


def _text(blocks):
    """Flatten the mrkdwn/plain_text of every section-style block."""
    return "\n".join(b["text"]["text"] for b in blocks if isinstance(b.get("text"), dict))


def test_card_renders_reasoning_and_signals():
    blocks = _blocks()
    text = _text(blocks)
    # The concise reasoning report is shown under Analysis.
    assert "AVS and CVV both failed" in text
    # Each concrete red flag is surfaced - signals were previously dropped.
    assert "avs_failed" in text
    assert "cvv_failed" in text
    assert "new_account" in text
    # An interactive card still carries the Approve/Reject actions.
    actions = [b for b in blocks if b.get("type") == "actions"]
    assert actions and len(actions[0]["elements"]) == 2


def test_card_keeps_signals_on_the_decided_card():
    # Once decided, the buttons are replaced by the outcome, but the red flags
    # stay on record for the channel.
    blocks = _blocks(decision="reject", manager_name="Dana")
    text = _text(blocks)
    assert "avs_failed" in text
    assert "Rejected" in text
    assert not [b for b in blocks if b.get("type") == "actions"]


def test_card_without_signals_omits_red_flags_section():
    verdict = {
        "recommendation": "approve",
        "confidence": 0.7,
        "reasoning": "Looks fine.",
        "signals": [],
    }
    blocks = _blocks(verdict=verdict)
    assert "Red flags" not in _text(blocks)
