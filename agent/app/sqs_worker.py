"""SQS poller.

Continuously long-polls the webhook queue. Each message is a JSON envelope
produced by the API Gateway Lambda; it is dispatched through the
:class:`AgentRuntime` (Shopify order-risk -> forwarded to Odoo; Slack interaction
-> resume the paused workflow). Messages are deleted only after successful
handling, so transient failures are retried and poison messages eventually land
in the configured dead-letter queue.

``boto3`` is synchronous, so its calls are offloaded to a worker thread to keep
the event loop responsive.
"""

from __future__ import annotations

import asyncio
import json
import logging

import boto3

from .config import Settings
from .runtime import AgentRuntime

logger = logging.getLogger(__name__)


class SqsWorker:
    def __init__(self, settings: Settings, runtime: AgentRuntime):
        self.settings = settings
        self.runtime = runtime
        self._stop = asyncio.Event()
        self._client = boto3.client("sqs", region_name=settings.aws_region)

    def stop(self) -> None:
        self._stop.set()

    async def _receive(self) -> list[dict]:
        resp = await asyncio.to_thread(
            self._client.receive_message,
            QueueUrl=self.settings.sqs_queue_url,
            MaxNumberOfMessages=self.settings.sqs_max_messages,
            WaitTimeSeconds=self.settings.sqs_wait_time_seconds,
            VisibilityTimeout=self.settings.sqs_visibility_timeout,
            MessageAttributeNames=["All"],
        )
        return resp.get("Messages", [])

    async def _delete(self, receipt_handle: str) -> None:
        await asyncio.to_thread(
            self._client.delete_message,
            QueueUrl=self.settings.sqs_queue_url,
            ReceiptHandle=receipt_handle,
        )

    async def _handle(self, message: dict) -> None:
        try:
            body = json.loads(message.get("Body") or "{}")
        except (ValueError, TypeError):
            logger.error("SQS message %s has a non-JSON body; deleting.", message.get("MessageId"))
            await self._delete(message["ReceiptHandle"])
            return

        try:
            await self.runtime.handle_sqs_message(body)
        except Exception:  # noqa: BLE001 - keep the message for retry/DLQ
            logger.exception(
                "Failed to handle SQS message %s; leaving for retry.", message.get("MessageId")
            )
            return
        await self._delete(message["ReceiptHandle"])

    async def run(self) -> None:
        if not self.settings.sqs_queue_url:
            logger.warning("SQS_QUEUE_URL not set - SQS worker idle.")
            return
        logger.info("SQS worker polling %s", self.settings.sqs_queue_url)
        while not self._stop.is_set():
            try:
                messages = await self._receive()
            except Exception:  # noqa: BLE001 - network/throttling; back off and retry
                logger.exception("SQS receive failed; backing off 5s.")
                await asyncio.sleep(5)
                continue
            if not messages:
                continue
            # Process the batch concurrently; each deletes itself on success.
            await asyncio.gather(*(self._handle(m) for m in messages))
        logger.info("SQS worker stopped.")
