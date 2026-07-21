"""Slack integration: Block Kit approval cards + request signature verification.

The agent posts an interactive card with **Approve** / **Reject** buttons. The
button ``value`` carries the routing context (odoo task id + LangGraph thread id)
so that when a manager clicks, Slack's interactive callback - delivered through
API Gateway -> SQS - can be matched back to the paused workflow and resume it.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

SLACK_POST_MESSAGE = "https://slack.com/api/chat.postMessage"
SLACK_UPDATE_MESSAGE = "https://slack.com/api/chat.update"


def verify_slack_signature(
    signing_secret: str, timestamp: str, raw_body: str, signature: str, max_skew: int = 300
) -> bool:
    """Validate Slack's ``v0`` request signature (and reject stale requests)."""
    if not signing_secret or not signature or not timestamp:
        return False
    try:
        if abs(time.time() - int(timestamp)) > max_skew:
            return False
    except (TypeError, ValueError):
        return False
    basestring = f"v0:{timestamp}:{raw_body}".encode()
    digest = hmac.new(signing_secret.encode(), basestring, hashlib.sha256).hexdigest()
    expected = f"v0={digest}"
    return hmac.compare_digest(expected, signature)


class SlackClient:
    def __init__(self, bot_token: str, default_channel: str, timeout: float = 15.0):
        self.bot_token = bot_token
        self.default_channel = default_channel
        self._client = httpx.AsyncClient(timeout=timeout)

    async def aclose(self) -> None:
        await self._client.aclose()

    @staticmethod
    def build_fraud_blocks(
        task_ref: str,
        odoo_task_id: int,
        thread_id: str,
        order: dict,
        risk_level: str,
        verdict: dict,
        decision: str | None = None,
        manager_name: str | None = None,
    ) -> list[dict]:
        """Compose the Block Kit card for a fraud review.

        Without ``decision`` the card is interactive (Approve/Reject buttons).
        With a ``decision`` the actions block is replaced by the outcome, so the
        posted card can be updated in place once a manager has decided - stale
        buttons must not stay clickable forever.
        """
        # The producer contract is Odoo's ai.ops.task._fraud_order_context():
        # {"order_name", "total", "currency", "risk_assessment", "order"}.
        order_name = order.get("order_name") or task_ref
        total = order.get("total") or "?"
        currency = order.get("currency") or ""
        recommendation = verdict.get("recommendation", "review")
        reasoning = verdict.get("reasoning", "No analysis provided.")
        confidence = verdict.get("confidence", "n/a")
        signals = verdict.get("signals") or []

        blocks = [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": f"Fraud Review - {order_name}"},
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Risk level:*\n{risk_level.title()}"},
                    {"type": "mrkdwn", "text": f"*Order total:*\n{total} {currency}"},
                    {"type": "mrkdwn", "text": f"*AI recommendation:*\n{recommendation.title()}"},
                    {"type": "mrkdwn", "text": f"*Confidence:*\n{confidence}"},
                ],
            },
            {"type": "section", "text": {"type": "mrkdwn", "text": f"*Analysis*\n{reasoning}"}},
        ]
        # Surface the concrete red flags behind the recommendation. Added before
        # the decision/actions branch so the outcome card keeps them on record.
        if signals:
            bullets = "\n".join(f"• {s}" for s in signals)
            blocks.append(
                {"type": "section", "text": {"type": "mrkdwn", "text": f"*Red flags*\n{bullets}"}}
            )
        if decision:
            by = f" by *{manager_name}*" if manager_name else ""
            outcome = (
                f":white_check_mark: *Approved*{by} - order kept."
                if decision == "approve"
                else f":no_entry: *Rejected*{by} - order cancelled in Shopify and Odoo."
            )
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": outcome}})
            return blocks

        # Encoded in both buttons so the resume worker can route the decision.
        ctx = json.dumps(
            {"odoo_task_id": odoo_task_id, "thread_id": thread_id, "task_ref": task_ref}
        )
        blocks.append(
            {
                "type": "actions",
                "block_id": "ai_ops_decision",
                "elements": [
                    {
                        "type": "button",
                        "style": "primary",
                        "action_id": "ai_ops_approve",
                        "text": {"type": "plain_text", "text": "Approve Order"},
                        "value": ctx,
                    },
                    {
                        "type": "button",
                        "style": "danger",
                        "action_id": "ai_ops_reject",
                        "text": {"type": "plain_text", "text": "Reject & Cancel"},
                        "value": ctx,
                        "confirm": {
                            "title": {"type": "plain_text", "text": "Cancel this order?"},
                            "text": {
                                "type": "mrkdwn",
                                "text": "This cancels the order in Shopify.",
                            },
                            "confirm": {"type": "plain_text", "text": "Reject"},
                            "deny": {"type": "plain_text", "text": "Keep"},
                        },
                    },
                ],
            }
        )
        return blocks

    async def post_fraud_card(
        self,
        *,
        task_ref: str,
        odoo_task_id: int,
        thread_id: str,
        order: dict,
        risk_level: str,
        verdict: dict,
        channel: str | None = None,
    ) -> dict:
        blocks = self.build_fraud_blocks(
            task_ref, odoo_task_id, thread_id, order, risk_level, verdict
        )
        return await self._post_message(
            channel or self.default_channel,
            text=f"Fraud review required for {task_ref}",
            blocks=blocks,
        )

    async def update_fraud_card(
        self,
        *,
        channel: str,
        ts: str,
        task_ref: str,
        odoo_task_id: int,
        thread_id: str,
        order: dict,
        risk_level: str,
        verdict: dict,
        decision: str,
        manager_name: str | None = None,
    ) -> dict:
        """Replace the posted card's buttons with the decision outcome (chat.update)."""
        blocks = self.build_fraud_blocks(
            task_ref,
            odoo_task_id,
            thread_id,
            order,
            risk_level,
            verdict,
            decision=decision,
            manager_name=manager_name,
        )
        payload: dict[str, Any] = {
            "channel": channel,
            "ts": ts,
            "text": f"Fraud review for {task_ref}: "
            + ("approved" if decision == "approve" else "rejected"),
            "blocks": blocks,
        }
        return await self._call(SLACK_UPDATE_MESSAGE, payload)

    async def post_text(
        self, text: str, channel: str | None = None, thread_ts: str | None = None
    ) -> dict:
        return await self._post_message(
            channel or self.default_channel, text=text, thread_ts=thread_ts
        )

    async def _post_message(
        self,
        channel: str,
        text: str,
        blocks: list | None = None,
        thread_ts: str | None = None,
    ) -> dict:
        payload: dict[str, Any] = {"channel": channel, "text": text}
        if blocks:
            payload["blocks"] = blocks
        if thread_ts:
            payload["thread_ts"] = thread_ts
        return await self._call(SLACK_POST_MESSAGE, payload)

    async def _call(self, api_url: str, payload: dict) -> dict:
        resp = await self._client.post(
            api_url,
            json=payload,
            headers={"Authorization": f"Bearer {self.bot_token}"},
        )
        resp.raise_for_status()
        body = resp.json()
        if not body.get("ok"):
            logger.error("Slack call %s failed: %s", api_url, body.get("error"))
        return body
