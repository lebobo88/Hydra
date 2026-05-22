"""Reflexion ×1 bridge.

Port of pair-programmer's invariant (`daemon/src/orchestrator/loop-ceiling.ts:43-74`):
exactly one retry per generator attempt, with the judge's critique fed back as
input. Hydra reuses its existing loop ceiling (`governance.py:42`) rather than
maintaining a separate counter.

This module is pure data: it packages a critique into a `ReflexionPacket` that
the dispatcher (`squad_node.execute_squad`) can interpret as "rerun the squad
with the prior output + this critique as additional context."

## Override path (R3-tail post-mortem, 2026-05-21)

The R3-tail recovery workflow required four operator-authorized overrides of
the ×1 ceiling on a single envelope (δ feature). Those overrides were
LLM-mediated text routing with no programmatic capture. To formalize the
override boundary while preserving the invariant as the default:

  - The ceiling can be raised PER-CALL by passing ``max_retry_override``
    to ``package_retry``. The supervisor sets this from
    ``state.reflexion_override_granted_until`` after an operator-approved
    HITL request with ``reason="reflexion_override"``.
  - For tests / local diagnostics only, the env var
    ``HYDRA_REFLEXION_MAX_RETRY_INDEX_OVERRIDE`` is honored as a
    last-resort raise. The supervisor does NOT read this env directly —
    the formal override path is the HITL gate, and this env is a debug
    seam, not a production knob.

The default remains MAX_RETRY_INDEX = 1. The constitutional invariant
"Reflexion ×1" is upheld; the override is a HITL-gated exception, not an
amendment of the rule.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from .schemas import JudgeVerdict


MAX_RETRY_INDEX = 1  # Reflexion ×1 invariant (default ceiling).
ENV_OVERRIDE_KEY = "HYDRA_REFLEXION_MAX_RETRY_INDEX_OVERRIDE"


def effective_max_retry_index(*, max_retry_override: int | None = None) -> int:
    """Return the active ceiling for this call.

    Precedence (highest wins):
      1. ``max_retry_override`` argument (set by the supervisor from the
         per-workflow ``state.reflexion_override_granted_until`` field after
         operator HITL approval).
      2. ``HYDRA_REFLEXION_MAX_RETRY_INDEX_OVERRIDE`` env var (debug seam;
         do not rely on this in production).
      3. ``MAX_RETRY_INDEX`` (the invariant default of 1).
    """
    if max_retry_override is not None and max_retry_override > MAX_RETRY_INDEX:
        return max_retry_override
    env_val = os.environ.get(ENV_OVERRIDE_KEY)
    if env_val:
        try:
            parsed = int(env_val)
            if parsed > MAX_RETRY_INDEX:
                return parsed
        except ValueError:
            pass
    return MAX_RETRY_INDEX


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
    max_retry_override: int | None = None,
) -> ReflexionPacket | None:
    """Build a retry packet IFF the verdict warrants a retry and we are within
    the active ceiling. Returns None when:
      - outcome is 'pass' or 'skip' (no retry needed),
      - outcome is 'fail' (must escalate to HITL, not retry),
      - prior_retry_index already at the active ceiling.

    The active ceiling is the larger of ``MAX_RETRY_INDEX``, the
    ``HYDRA_REFLEXION_MAX_RETRY_INDEX_OVERRIDE`` env var (debug seam), and
    ``max_retry_override`` (the per-workflow HITL-approved raise). See
    ``effective_max_retry_index`` for the precedence rules.
    """
    if verdict.outcome != "revise":
        return None
    ceiling = effective_max_retry_index(max_retry_override=max_retry_override)
    if prior_retry_index >= ceiling:
        return None
    return ReflexionPacket(
        original_envelope=original_envelope,
        verdict=verdict,
        retry_index=prior_retry_index + 1,
    )
