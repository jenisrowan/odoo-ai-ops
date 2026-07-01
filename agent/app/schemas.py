"""Pydantic request/response models for the agent's REST API."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

RiskLevel = Literal["none", "low", "medium", "high"]


class FraudTaskRequest(BaseModel):
    """Body of ``POST /v1/tasks/fraud`` (sent by the Odoo gatekeeper)."""

    odoo_task_ref: str = Field(..., description="Human reference, e.g. AIOPS/2026/00001")
    odoo_task_id: int | None = Field(None, description="Numeric ai.ops.task id for callbacks")
    risk_level: RiskLevel = "high"
    order: dict[str, Any] = Field(default_factory=dict)


class ReconciliationTaskRequest(BaseModel):
    """Body of ``POST /v1/tasks/reconciliation``."""

    odoo_task_ref: str
    odoo_task_id: int | None = None
    product_id: int
    context: dict[str, Any] = Field(default_factory=dict)


class TaskAccepted(BaseModel):
    """202 response acknowledging an accepted async workflow."""

    run_id: str
    status: str = "accepted"
    odoo_task_ref: str


class HealthResponse(BaseModel):
    status: str = "ok"
    version: str
    sqs_worker: bool
