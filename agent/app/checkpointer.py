"""LangGraph checkpointer backed by ElastiCache Serverless (Valkey).

The fraud workflow pauses at a human-approval interrupt and *terminates the
thread* to save compute (per the architecture). Its full state graph is
serialized to Valkey so that - possibly minutes or hours later, in a different
ECS task - the SQS resume worker can rehydrate and continue it.

We use the official ``langgraph-checkpoint-redis`` saver (Valkey is wire
compatible with Redis OSS). If no Valkey URL is configured (e.g. local unit
tests) we fall back to an in-memory saver and log a clear warning.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


async def build_checkpointer(valkey_url: str):
    """Return ``(saver, context_manager_or_none)``.

    When the Redis saver is used it must stay open for the process lifetime; the
    caller keeps the returned async context manager and passes it to
    :func:`close_checkpointer` on shutdown.
    """
    if not valkey_url:
        from langgraph.checkpoint.memory import MemorySaver

        logger.warning(
            "VALKEY_URL not set - using in-memory checkpointer "
            "(state will NOT survive restarts). Do not use in production."
        )
        return MemorySaver(), None

    try:
        from langgraph.checkpoint.redis.aio import AsyncRedisSaver
    except ImportError:  # pragma: no cover - dependency guard
        from langgraph.checkpoint.memory import MemorySaver

        logger.error(
            "langgraph-checkpoint-redis not installed - falling back to in-memory checkpointer."
        )
        return MemorySaver(), None

    cm = AsyncRedisSaver.from_conn_string(valkey_url)
    saver = await cm.__aenter__()
    # Create the required Redis indices/keys once.
    await saver.asetup()
    logger.info("Initialized Valkey-backed LangGraph checkpointer.")
    return saver, cm


async def close_checkpointer(cm) -> None:
    """Close the checkpointer context manager opened by :func:`build_checkpointer`."""
    if cm is not None:
        try:
            await cm.__aexit__(None, None, None)
        except Exception:  # pragma: no cover - best-effort cleanup
            logger.exception("Error while closing the Valkey checkpointer.")
