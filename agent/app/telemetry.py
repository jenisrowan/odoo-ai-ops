"""Langfuse telemetry wiring.

Returns a LangChain callback handler that streams trace metrics to the
self-hosted Langfuse server. The architecture buffers telemetry through Valkey
and flushes it asynchronously; the Langfuse SDK already batches/queues events in
a background thread and tolerates the Fargate-Spot server being briefly
unavailable, which satisfies that requirement without bespoke buffering code.
"""

from __future__ import annotations

import logging
import os

from .config import Settings

logger = logging.getLogger(__name__)


def build_langfuse_handler(settings: Settings) -> object | None:
    """Return a Langfuse CallbackHandler, or ``None`` if disabled/unavailable."""
    if not settings.langfuse_enabled:
        logger.info("Langfuse disabled (missing host/keys) - telemetry not exported.")
        return None

    # The Langfuse SDK reads these from the environment.
    os.environ.setdefault("LANGFUSE_HOST", settings.langfuse_host)
    os.environ.setdefault("LANGFUSE_PUBLIC_KEY", settings.langfuse_public_key)
    os.environ.setdefault("LANGFUSE_SECRET_KEY", settings.langfuse_secret_key)

    # Telemetry is non-critical: any problem wiring up the handler must disable
    # it gracefully, never crash the agent at startup. Hence the broad excepts.
    try:
        # Langfuse v3/v4 layout
        from langfuse.langchain import CallbackHandler

        return CallbackHandler()
    except Exception:  # noqa: BLE001
        try:
            # Langfuse v2 layout
            from langfuse.callback import CallbackHandler  # type: ignore

            return CallbackHandler(
                host=settings.langfuse_host,
                public_key=settings.langfuse_public_key,
                secret_key=settings.langfuse_secret_key,
            )
        except Exception:  # noqa: BLE001
            logger.warning("Langfuse handler unavailable - telemetry disabled.")
            return None
