"""Liveness/readiness endpoints (used by ALB/ECS health checks if exposed)."""

from __future__ import annotations

from fastapi import APIRouter, Request

from .. import __version__
from ..config import get_settings
from ..schemas import HealthResponse

router = APIRouter(tags=["health"])


@router.get("/healthz", response_model=HealthResponse)
async def healthz(request: Request) -> HealthResponse:
    settings = get_settings()
    return HealthResponse(
        status="ok" if getattr(request.app.state, "runtime", None) else "starting",
        version=__version__,
        sqs_worker=settings.enable_sqs_worker,
    )
