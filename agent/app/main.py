"""FastAPI application entrypoint for the AI Ops agent.

Lifespan wiring:
  * build the :class:`AgentRuntime` (clients, Valkey checkpointer, graphs);
  * start the SQS poller as a background task (unless disabled);
  * tear both down cleanly on shutdown so in-flight Valkey state is flushed.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from . import __version__
from .config import get_settings
from .routers import health, tasks
from .runtime import AgentRuntime
from .sqs_worker import SqsWorker

logging.basicConfig(
    level=get_settings().log_level.upper(),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("ai_ops.agent")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    runtime = await AgentRuntime.create(settings)
    app.state.runtime = runtime

    worker = None
    worker_task = None
    if settings.enable_sqs_worker:
        worker = SqsWorker(settings, runtime)
        worker_task = asyncio.create_task(worker.run())
        app.state.sqs_worker = worker

    logger.info("AI Ops agent %s started.", __version__)
    try:
        yield
    finally:
        if worker is not None:
            worker.stop()
        if worker_task is not None:
            try:
                await asyncio.wait_for(worker_task, timeout=30)
            except TimeoutError:
                worker_task.cancel()
        await runtime.aclose()
        logger.info("AI Ops agent stopped.")


def create_app() -> FastAPI:
    app = FastAPI(title="Odoo AI Ops Agent", version=__version__, lifespan=lifespan)
    app.include_router(health.router)
    app.include_router(tasks.router)
    return app


app = create_app()
