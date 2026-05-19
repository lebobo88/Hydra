"""Typed state object for the Hydra supervisor graph.

LangGraph reduces state with the `Annotated[..., reducer]` pattern. We use that
for collections (tasks, messages, artifacts) and replace-by-default for scalars.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any, Literal, Optional
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


def _append(left: list, right: list) -> list:
    """LangGraph reducer: append-only for lists."""
    return [*left, *right]


def _merge_dict(left: dict, right: dict) -> dict:
    return {**left, **right}


class BudgetLedger(BaseModel):
    budget_usd: float = 50.0
    token_limit: int = 200_000
    spent_usd: float = 0.0
    spent_tokens: int = 0

    @property
    def usd_remaining(self) -> float:
        return max(self.budget_usd - self.spent_usd, 0.0)

    @property
    def percent_consumed(self) -> float:
        return (self.spent_usd / self.budget_usd) if self.budget_usd else 0.0


class TaskState(BaseModel):
    task_id: UUID = Field(default_factory=uuid4)
    owner_squad: str
    description: str
    status: Literal["pending", "running", "blocked", "done", "failed", "surfaced"] = "pending"
    envelope_id: Optional[UUID] = None      # the message that triggered it
    result_envelope_id: Optional[UUID] = None
    retries: int = 0
    priority: Literal["P0", "P1", "P2", "P3"] = "P2"


class HydraState(BaseModel):
    """Persistent supervisor-graph state. Checkpointed by LangGraph per workflow_id."""

    workflow_id: UUID = Field(default_factory=uuid4)
    tenant_id: str = "default"
    root_goal: str = ""
    phase: Literal[
        "intake", "planning", "approval", "dispatch",
        "executing", "synthesis", "postcheck", "done", "surfaced"
    ] = "intake"

    # Routing
    selected_squads: list[str] = Field(default_factory=list)
    current_node: Optional[str] = None

    # Work
    tasks: Annotated[list[TaskState], _append] = Field(default_factory=list)
    envelopes: Annotated[list[dict[str, Any]], _append] = Field(default_factory=list)
    artifacts: Annotated[list[dict[str, Any]], _append] = Field(default_factory=list)

    # Memory handles
    episodic_refs: Annotated[list[str], _append] = Field(default_factory=list)
    semantic_queries: Annotated[list[dict[str, Any]], _append] = Field(default_factory=list)

    # Governance
    budget: BudgetLedger = Field(default_factory=BudgetLedger)
    iteration_count: int = 0
    depth: int = 0
    loop_ceiling: int = 25
    depth_ceiling: int = 5
    error_counters: Annotated[dict[str, int], _merge_dict] = Field(default_factory=dict)

    requires_human_approval: bool = False
    pending_hitl: Optional[dict[str, Any]] = None
    hitl_history: Annotated[list[dict[str, Any]], _append] = Field(default_factory=list)

    # Trace
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: Optional[datetime] = None
    last_event: Optional[str] = None

    def bump_iteration(self) -> None:
        self.iteration_count += 1

    def is_over_budget(self) -> bool:
        return self.budget.spent_usd > self.budget.budget_usd

    def is_looping(self) -> bool:
        return self.iteration_count >= self.loop_ceiling or self.depth >= self.depth_ceiling
