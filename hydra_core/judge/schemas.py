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


JudgeOutcome = Literal["pass", "revise", "fail", "skip", "unjudgeable"]
# "unjudgeable" (cross-vendor judge finding, item 1/6 CRITICAL) is DISTINCT
# from a legitimately routed "skip" (`route.tier == "skip"` or no rubric ids,
# e.g. `supervisor.py`'s `_judge_envelope` at ~line 1543 -- an ordinary,
# intentional non-decision that stays excluded from Borda/HITL exactly as
# before). "unjudgeable" is fabricated ONLY by `judge.dispatcher` when an
# envelope fails strict JSON serialization (a genuine data defect -- e.g. a
# legacy non-finite field a pre-strict-JSON checkpoint carried) and could
# therefore never actually be evaluated by ANY vendor. A real MCP critique
# client is never expected to emit it (see `mcp_client.py`'s response
# validation, which still only accepts pass/revise/fail/skip). Every
# verdict-consuming call site MUST treat "unjudgeable" as a hard block:
# never advance to synthesis, never mark the workflow done, never offer the
# ordinary approve gate as if real judgment occurred.
JudgeVendor = Literal["codex", "agy", "claude"]
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
