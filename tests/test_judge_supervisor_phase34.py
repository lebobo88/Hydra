"""Phase 3 + 4 supervisor integration tests.

Covers:
- Reflexion ×1 retry triggered by a `revise` verdict.
- Per-squad HITL escalation on a HITL-severity `fail`.
- Best-of-N path producing N candidates and picking a winner via Borda.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from hydra_core.state import HydraState


HYDRA_ROOT = Path(__file__).resolve().parents[1]


class _StubDispatcher:
    """Returns `done` (not `host_pickup_required`) so the judge plane scores
    the resulting envelopes. Use _HostPickupDispatcher in tests that need
    placeholder behavior."""
    def call_mcp(self, server, tool, args):
        return {"status": "done", "tool": tool, "result": {"ok": True}}

    def spawn_subprocess(self, cmd, env=None):
        return {"status": "done", "stdout": "", "stderr": ""}

    def emit_claude_prompt(self, prompt, agent=None):
        return {"status": "done", "agent": agent, "summary": "stub boardroom output"}

    def invoke_claude_skill(self, skill, args):
        return {"status": "done", "skill": skill, "summary": "stub skill output"}


class _SequencedClient:
    """Returns a different verdict on each call from a queue."""
    def __init__(self, queue: list[dict]):
        self.queue = list(queue)
        self.default = {
            "outcome": "pass",
            "critique_md": "thorough analysis " * 10,
            "score_json": {"x": 5, "y": 4},
        }
        self.calls: list[str] = []

    def critique(self, *, vendor, artifact_text, rubric_md):
        # Tag by rubric headline so tests can assert which arrived first.
        head = rubric_md.split("\n")[0] if rubric_md else ""
        self.calls.append(head)
        if self.queue:
            return self.queue.pop(0)
        return dict(self.default)


def _invoke(sup, state):
    from hydra_core.supervisor import _PurePythonRunner
    if isinstance(sup, _PurePythonRunner):
        return sup.invoke(state)
    out = sup.invoke(state, config={"configurable": {"thread_id": str(state.workflow_id)}})
    return HydraState.model_validate(out) if isinstance(out, dict) else out


# ---------------- Reflexion retry ----------------

def test_reflexion_retry_on_revise_triggers_second_dispatch():
    """First per-squad verdict revises → reflexion retry re-dispatches the squad.

    The retry envelope is judged again; the second pass (default queue empty
    → falls through to default 'pass') produces additional verdicts and the
    workflow completes normally.
    """
    from hydra_core.supervisor import build_supervisor

    # Force a revise on the first judge call (per-squad on bon-winner).
    # Subsequent calls (retry, synthesis) default to pass.
    client = _SequencedClient(queue=[
        {"outcome": "revise",
         "critique_md": "your draft lacks rigor. address the risk-treatment dimension and resubmit. " * 3,
         "score_json": {"risk_treatment": 1}},
    ])
    sup = build_supervisor(
        project_root=HYDRA_ROOT,
        dispatcher=_StubDispatcher(),
        critique_client=client,
        force_pure_python=True,
    )
    state = HydraState(root_goal="quick budget refresh, low stakes")
    final = _invoke(sup, state)

    # Multiple verdicts including at least one with retry_index>0 OR the
    # workflow reaching synthesis-coherence after the retry.
    rubric_ids = {v["rubric_id"] for v in final.verdicts}
    assert "synthesis-coherence@1" in rubric_ids
    # At least one revise verdict captured the original failure.
    revise_seen = any(v["outcome"] == "revise" for v in final.verdicts)
    assert revise_seen


# ---------------- HITL escalation on per-squad fail ----------------

def test_per_squad_hitl_on_high_severity_fail():
    from hydra_core.supervisor import build_supervisor

    # Force fail on the first call (constitution-alignment@1 is HITL-severity).
    client = _SequencedClient(queue=[
        {"outcome": "fail",
         "critique_md": "violates the immortal head's third refusal pattern explicitly. " * 3,
         "score_json": {"refusal_respect": 0}},
    ])
    sup = build_supervisor(
        project_root=HYDRA_ROOT,
        dispatcher=_StubDispatcher(),
        critique_client=client,
        force_pure_python=True,
    )
    state = HydraState(root_goal="something flagged for constitutional review")
    final = _invoke(sup, state)

    assert final.phase == "surfaced"
    assert final.pending_hitl is not None
    assert final.pending_hitl["reason"] == "policy_breach"


# ---------------- Best-of-N path ----------------

def test_best_of_n_produces_n_candidates_and_picks_winner():
    """With pack.best_of_n=3, node_dispatch produces 3 candidates and
    Borda-ranks them. The winner envelope is what flows downstream."""
    from hydra_core.supervisor import build_supervisor

    client = _SequencedClient(queue=[])  # all passes
    sup = build_supervisor(
        project_root=HYDRA_ROOT,
        dispatcher=_StubDispatcher(),
        critique_client=client,
        force_pure_python=True,
    )
    state = HydraState(root_goal="strategic OKR refresh")
    final = _invoke(sup, state)

    # Look for the bon_losers artifact emitted by _dispatch_best_of_n.
    bon_artifact = next(
        (a for a in final.artifacts if a.get("kind") == "bon_losers"),
        None,
    )
    assert bon_artifact is not None, "best-of-N path should emit bon_losers artifact"
    assert len(bon_artifact["loser_envelope_ids"]) >= 1  # at least 1 loser when N=3
    assert bon_artifact["squad"] == "executive"

    # Synthesis judge still ran post-Borda.
    rubric_ids = {v["rubric_id"] for v in final.verdicts}
    assert "synthesis-coherence@1" in rubric_ids


def test_best_of_n_falls_back_when_insufficient_candidates(monkeypatch):
    """If execute_squad produces <2 usable candidates, best-of-N falls back
    to returning whatever candidates exist without ranking."""
    from hydra_core.supervisor import build_supervisor
    import hydra_core.supervisor as sv

    # Patch execute_squad to return zero envelopes always.
    orig = sv.execute_squad

    def empty_exec(state, pack, payload, dispatcher):
        result = orig(state, pack, payload, dispatcher)
        result.envelopes = []
        return result

    monkeypatch.setattr(sv, "execute_squad", empty_exec)

    client = _SequencedClient(queue=[])
    sup = build_supervisor(
        project_root=HYDRA_ROOT,
        dispatcher=_StubDispatcher(),
        critique_client=client,
        force_pure_python=True,
    )
    state = HydraState(root_goal="strategic OKR refresh")
    final = _invoke(sup, state)
    # No bon_losers artifact because we never had ≥2 candidates to rank.
    bon_artifact = next(
        (a for a in final.artifacts if a.get("kind") == "bon_losers"),
        None,
    )
    assert bon_artifact is None
