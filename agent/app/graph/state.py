"""Typed state objects shared across the LangGraph workflows."""

from __future__ import annotations

from typing import Annotated, Any, Literal, TypedDict

from langgraph.graph.message import add_messages
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
    # Where the approval card was posted, so finalize can update it in place.
    slack_channel: str | None
    slack_ts: str | None
    decision: str
    manager_name: str | None
    note: str | None


class ReconciliationVerdict(BaseModel):
    """Structured root-cause analysis of an Odoo vs Shopify stock discrepancy."""

    direction: Literal["odoo_higher", "odoo_lower", "match", "unknown"] = Field(
        ..., description="How Odoo on-hand compares to Shopify available."
    )
    root_cause: str = Field(..., description="The single most likely reason the stock diverged.")
    recommended_action: Literal[
        "update_shopify",
        "adjust_odoo",
        "validate_or_investigate_move",
        "create_missing_sale_order",
        "no_action",
    ] = Field(..., description="What should be done to resolve the discrepancy.")
    corrected_odoo_qty: float | None = Field(
        None, description="Corrected Odoo on-hand, when recommended_action='adjust_odoo'."
    )
    shopify_target_qty: float | None = Field(
        None, description="Quantity to set in Shopify, when recommended_action='update_shopify'."
    )
    suspect_move_ids: list[int] = Field(
        default_factory=list,
        description="stock.move ids implicated (e.g. an aged/stuck delivery).",
    )
    reasoning: str = Field(..., description="Evidence-based explanation for the conclusion.")
    confidence: float = Field(..., ge=0.0, le=1.0)


class ReconciliationState(TypedDict, total=False):
    run_id: str
    odoo_task_ref: str
    odoo_task_id: int | None
    product_id: int
    context: dict[str, Any]
    discrepancy: dict[str, Any]
    proposal: dict[str, Any]
    # Investigation transcript: the model's tool calls and their results. This
    # is the evidence the verdict is drawn from, and it is checkpointed with
    # the rest of the state so a resumed run keeps its reasoning.
    messages: Annotated[list, add_messages]
    # Number of investigate->tools round trips taken, against MAX_TOOL_LOOPS.
    tool_loops: int
    # Where the diagnosis was posted, so apply can confirm the outcome in-thread.
    slack_channel: str | None
    slack_ts: str | None
    decision: str
    manager_name: str | None
