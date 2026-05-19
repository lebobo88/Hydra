"""Judge-plane schemas.

`JudgeVerdict` is the result envelope every judge call produces. It is registered
in `hydra_core.schemas.SCHEMA_REGISTRY` so it travels through the standard
envelope validation path.

Replay determinism: `rubric_id` is pinned with a `@<version>` suffix so a past
verdict can be reapplied against the exact rubric body that produced it.
"""
from __future__ import annotations

from typing import Literal, Optional
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from ..schemas import HydraEnvelope


JudgeOutcome = Literal["pass", "revise", "fail", "skip"]
JudgeVendor = Literal["codex", "gemini", "claude"]
JudgeTier = Literal["cross_vendor", "same_vendor", "skip"]


class RubricRef(BaseModel):
    """Pin a rubric by versioned ID. The body is fetched from the registry."""
    rubric_id: str  # e.g., "board-decision-quality@1"

    @field_validator("rubric_id")
    @classmethod
    def _must_be_versioned(cls, v: str) -> str:
        if "@" not in v:
            raise ValueError(
                f"rubric_id must include @<version> for replay determinism, got {v!r}"
            )
        return v


class JudgeVerdict(HydraEnvelope):
    type: Literal["JUDGE_VERDICT"] = "JUDGE_VERDICT"
    target_envelope_id: UUID
    outcome: JudgeOutcome
    rubric_id: str
    judge_vendor: JudgeVendor
    generator_vendor: str = "unknown"
    critique_md: str = ""
    score_json: dict = Field(default_factory=dict)
    retry_index: int = 0
    parent_verdict_id: Optional[UUID] = None
    # Set True when the verdict was inherited from PP (we skipped re-judging).
    judged_externally: bool = False
