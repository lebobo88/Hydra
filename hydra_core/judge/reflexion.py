"""Reflexion ×1 bridge.

Port of pair-programmer's invariant (`daemon/src/orchestrator/loop-ceiling.ts:43-74`):
exactly one retry per generator attempt, with the judge's critique fed back as
input. Hydra reuses its existing loop ceiling (`governance.py:42`) rather than
maintaining a separate counter.

This module is pure data: it packages a critique into a `ReflexionPacket` that
the dispatcher (`squad_node.execute_squad`) can interpret as "rerun the squad
with the prior output + this critique as additional context."
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from .schemas import JudgeVerdict


MAX_RETRY_INDEX = 1  # Reflexion ×1 invariant.


@dataclass
class ReflexionPacket:
    """Carries a critique back to the source squad for a single retry."""
    original_envelope: dict[str, Any]
    verdict: JudgeVerdict
    retry_index: int

    def to_squad_payload(self) -> dict[str, Any]:
        """Render as the structured prompt addendum a squad dispatcher consumes."""
        return {
            "kind": "reflexion_retry",
            "retry_index": self.retry_index,
            "rubric_id": self.verdict.rubric_id,
            "judge_vendor": self.verdict.judge_vendor,
            "critique_md": self.verdict.critique_md,
            "score_json": dict(self.verdict.score_json),
            "prior_envelope_id": str(self.original_envelope.get("id")),
            "instruction": (
                "Your prior output did not pass the rubric below. Revise it to "
                "address the critique directly. Do not restate accepted parts; "
                "focus the revision on the failing dimensions."
            ),
        }


def package_retry(
    original_envelope: dict[str, Any],
    verdict: JudgeVerdict,
    *,
    prior_retry_index: int = 0,
) -> ReflexionPacket | None:
    """Build a retry packet IFF the verdict warrants a retry and we are within
    the ×1 invariant. Returns None when:
      - outcome is 'pass' or 'skip' (no retry needed),
      - outcome is 'fail' (must escalate to HITL, not retry),
      - prior_retry_index already at the ceiling.
    """
    if verdict.outcome != "revise":
        return None
    if prior_retry_index >= MAX_RETRY_INDEX:
        return None
    return ReflexionPacket(
        original_envelope=original_envelope,
        verdict=verdict,
        retry_index=prior_retry_index + 1,
    )
