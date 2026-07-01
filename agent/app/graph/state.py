"""Typed state objects shared across the LangGraph workflows."""

from __future__ import annotations

from typing import Any, Literal, TypedDict

from pydantic import BaseModel, Field


class FraudVerdict(BaseModel):
    """Structured output Claude must return for a fraud assessment."""

    recommendation: Literal["approve", "reject", "review"] = Field(
        ..., description="Suggested action for the order."
    )
    confidence: float = Field(
        ..., ge=0.0, le=1.0, description="Confidence in the recommendation (0-1)."
    )
    reasoning: str = Field(..., description="Concise human-readable justification.")
    signals: list[str] = Field(
        default_factory=list, description="Specific risk signals considered."
    )


class FraudState(TypedDict, total=False):
    run_id: str
    odoo_task_ref: str
    odoo_task_id: int | None
    risk_level: str
    order: dict[str, Any]
    model: str
    verdict: dict[str, Any]
    decision: str
    manager_name: str | None
    note: str | None


class ReconciliationVerdict(BaseModel):
    counted_qty: float = Field(..., description="Corrected on-hand quantity.")
    reasoning: str = Field(..., description="Why this is the correct level.")
    confidence: float = Field(..., ge=0.0, le=1.0)


class ReconciliationState(TypedDict, total=False):
    run_id: str
    odoo_task_ref: str
    odoo_task_id: int | None
    product_id: int
    context: dict[str, Any]
    catalog: list[dict[str, Any]]
    moves: list[dict[str, Any]]
    proposal: dict[str, Any]
    decision: str
    manager_name: str | None
