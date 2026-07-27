"""LangGraph checkpointer backed by ElastiCache Serverless (Valkey).

The fraud workflow pauses at a human-approval interrupt and *terminates the
thread* to save compute (per the architecture). Its full state graph is
serialized to Valkey so that - possibly minutes or hours later, in a different
ECS task - the SQS resume worker can rehydrate and continue it.

We use the official ``langgraph-checkpoint-redis`` saver. It is NOT a plain
key-value client: ``asetup()`` and checkpoint writes require the search
(``FT.*``) and json (``JSON.*``) modules. These are built into ElastiCache
version 9+ for Valkey (not 8). If no Valkey URL is configured (e.g. local unit
tests) we fall back to an in-memory saver and log a clear warning.
"""

from __future__ import annotations

import logging

from langgraph.checkpoint.memory import MemorySaver

logger = logging.getLogger(__name__)


async def build_checkpointer(settings):
    """Return ``(saver, context_manager_or_none)``.

    When the Redis saver is used it must stay open for the process lifetime; the
    caller keeps the returned async context manager and passes it to
    :func:`close_checkpointer` on shutdown.

    Two things are deliberately configured rather than left at their defaults:

    *Socket timeouts.* By default the client waits forever for a reply. A
    connection that stalls mid-command - the server holding a partial frame
    while the client waits on a response that never comes - parks the awaiting
    coroutine indefinitely, so the workflow neither completes nor fails and the
    worker is wedged with nothing in the logs. A timeout turns that into a
    ``TimeoutError``: the run fails, SQS redelivers, and the worker stays usable.

    *Checkpoint TTL.* Paused workflows would otherwise live in Valkey forever,
    so a run nobody ever decided keeps its state indefinitely and stays
    resumable long after the decision stopped being meaningful. The TTL bounds
    that at ``checkpoint_ttl_minutes`` (5 days by default) from the moment the
    workflow pauses.
    """
    valkey_url = settings.valkey_url
    if not valkey_url:
        logger.warning(
            "VALKEY_URL not set - using in-memory checkpointer "
            "(state will NOT survive restarts). Do not use in production."
        )
        return MemorySaver(), None

    from langgraph.checkpoint.redis.aio import AsyncRedisSaver

    connection_args = {
        "socket_timeout": settings.valkey_socket_timeout,
        "socket_connect_timeout": settings.valkey_connect_timeout,
        # Keepalive plus periodic health pings so a connection broken by an idle
        # NAT/ELB timeout is discovered and replaced, not handed out dead.
        "socket_keepalive": True,
        "health_check_interval": settings.valkey_health_check_interval,
        # Checkpoint writes are idempotent (JSON.SET on a deterministic key), so
        # transparently retrying a timed-out command cannot double-apply.
        "retry_on_timeout": True,
    }
    # TTL is expressed in minutes by langgraph-checkpoint-redis. refresh_on_read
    # stays off: the deadline should run from when the workflow paused, not be
    # extended every time something inspects the state.
    ttl = {"default_ttl": settings.checkpoint_ttl_minutes, "refresh_on_read": False}

    cm = AsyncRedisSaver.from_conn_string(valkey_url, connection_args=connection_args, ttl=ttl)
    saver = await cm.__aenter__()
    # Create the required Redis indices/keys once.
    await saver.asetup()
    logger.info(
        "Initialized Valkey-backed LangGraph checkpointer "
        "(socket_timeout=%ss, checkpoint TTL=%s minutes).",
        settings.valkey_socket_timeout,
        settings.checkpoint_ttl_minutes,
    )
    return saver, cm


async def close_checkpointer(cm) -> None:
    """Close the checkpointer context manager opened by :func:`build_checkpointer`."""
    if cm is not None:
        try:
            await cm.__aexit__(None, None, None)
        except Exception:  # pragma: no cover - best-effort cleanup
            logger.exception("Error while closing the Valkey checkpointer.")
