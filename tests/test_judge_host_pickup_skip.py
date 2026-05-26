"""Verify host-pickup-pending envelopes bypass the judge plane."""
from __future__ import annotations

from pathlib import Path

import pytest

from hydra_core.state import HydraState


HYDRA_ROOT = Path(__file__).resolve().parents[1]


class _HostPickupDispatcher:
    """Dispatcher whose emit_claude_prompt always returns host_pickup_required."""
    def call_mcp(self, server, tool, args, **_kw):
        return {"status": "done", "tool": tool, "result": {"ok": True}}

    def spawn_subprocess(self, cmd, env=None):
        return {"status": "done", "stdout": "", "stderr": ""}

    def emit_claude_prompt(self, prompt, agent=None):
        return {"status": "host_pickup_required", "agent": agent, "summary": "placeholder"}

    def invoke_claude_skill(self, skill, args):
        return {"status": "host_pickup_required", "skill": skill, "summary": "placeholder"}


class _FailIfCalledClient:
    """Critique client that fails the test if called. Used to prove the judge
    didn't run on a host-pickup placeholder."""
    def critique(self, *, vendor, artifact_text, rubric_md):
        rubric_head = (rubric_md or "").split("\n", 1)[0]
        # The synthesis-coherence judge IS allowed to run (it judges the
        # post-synthesis DecisionRecord which Hydra authors). Only the
        # per-squad path should be skipped for host-pickup envelopes.
        if "Synthesis Coherence" in rubric_head or "Constitution Alignment" in rubric_head:
            return {
                "outcome": "pass",
                "critique_md": "ok " * 40,
                "score_json": {"x": 5},
            }
        pytest.fail(
            f"judge should not fire on host-pickup placeholder; rubric={rubric_head!r}"
        )


def test_executive_host_pickup_skipped_by_per_squad_judge():
    """Executive squad → boardroom impersonation → host_pickup_required.
    Per-squad judge must skip the placeholder DecisionRecord; only the
    synthesis judge runs (and it gets the cathedral output, not the
    placeholder)."""
    from hydra_core.supervisor import build_supervisor

    sup = build_supervisor(
        project_root=HYDRA_ROOT,
        dispatcher=_HostPickupDispatcher(),
        critique_client=_FailIfCalledClient(),
        force_pure_python=True,
    )
    state = HydraState(root_goal="quick strategic refresh")
    final = sup.invoke(state)

    # No per-squad verdict against board-decision-quality (it was skipped).
    rubric_ids = {v["rubric_id"] for v in final.verdicts}
    assert "board-decision-quality@1" not in rubric_ids
    # Synthesis-coherence still ran (the synthesis DecisionRecord is real).
    assert "synthesis-coherence@1" in rubric_ids
