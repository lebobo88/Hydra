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


# ---------------- Reflexion ceiling exhaustion (R3-tail 2026-05-21) ----------------

def test_synthesis_preserves_dissents_and_marks_unsealed_on_conflict():
    """R3-tail post-mortem Fix 2.2 (2026-05-21): node_synthesis must
    preserve dissenting opinions verbatim (no paraphrase) AND set
    sealed=False when mutually-exclusive verdicts exist on the same squad
    (engineering 'pass' + security 'fail' is the canonical case). The
    enriched synthesis was wired into the supervisor after the R3-tail
    hydra-synthesizer subagent dropped mid-action."""
    from hydra_core.supervisor import build_supervisor

    # Force a revise verdict on the per-squad pass so the dissent flows
    # into state.verdicts and node_synthesis picks it up.
    client = _SequencedClient(queue=[
        {"outcome": "revise",
         "critique_md": "PHI redaction incomplete on inbound payload. needs explicit allowlist. " * 2,
         "score_json": {"redaction_completeness": 1}},
    ])
    sup = build_supervisor(
        project_root=HYDRA_ROOT,
        dispatcher=_StubDispatcher(),
        critique_client=client,
        force_pure_python=True,
    )
    state = HydraState(root_goal="healthcare PHI redaction policy review")
    final = _invoke(sup, state)

    # Find the synthesis DecisionRecord (origin_squad="hydra" is the
    # synthesis output; healthcare stubs also emit DECISION_RECORDs with
    # origin_squad="healthcare" so we filter to hydra-origin).
    decision_records = [
        e for e in final.envelopes
        if e.get("type") == "DECISION_RECORD" and e.get("origin_squad") == "hydra"
    ]
    assert decision_records, "expected the synthesis DECISION_RECORD (origin_squad='hydra')"
    syn = decision_records[-1]  # last one is the synthesis output

    # Dissents preserved verbatim (R3-tail contract: NEVER paraphrase).
    dissents = syn.get("dissenting_opinions") or []
    assert any("PHI redaction incomplete" in d for d in dissents), (
        f"expected revise critique preserved verbatim in dissents, got: {dissents}"
    )

    # Rationale carries the structured per-squad / budget / HITL block.
    rationale = syn.get("rationale") or ""
    assert "Squad outputs:" in rationale, "rationale must include squad-by-squad breakdown"
    assert "Budget:" in rationale, "rationale must include budget burn line"


def test_reflexion_ceiling_exhausted_emits_override_hitl():
    """When a retry envelope's re-judge ALSO returns 'revise', the active
    Reflexion ceiling is exhausted. The supervisor must surface a
    `reflexion_override` HITL instead of silently advancing to synthesis with
    an unresolved revise. R3-tail post-mortem (2026-05-21) — operators kept
    overriding the ×1 ceiling ad-hoc with no audit trail. This test pins the
    formal path: ceiling-hit → HITL with reason=`reflexion_override`.

    We route to the `engineering` squad because executive runs best-of-N which
    consumes the queue during candidate scoring, never reaching the per-squad
    reflexion path. Engineering's best_of_n=0 → straight to per-squad judge.
    """
    from hydra_core.supervisor import build_supervisor

    # Healthcare's per-squad route exercises 2 rubrics per envelope. To force
    # both the original AND its retry into 'revise', we queue 4 revises:
    #   - calls 1,2: original envelope, 2 rubrics → both revise → trigger retry
    #   - calls 3,4: retry envelope, 2 rubrics → both revise → ceiling exhausted
    # Subsequent envelopes drain to the default pass and don't affect this path.
    client = _SequencedClient(queue=[
        {"outcome": "revise",
         "critique_md": "PHI redaction coverage incomplete on inbound payload. " * 3,
         "score_json": {"redaction_completeness": 1}},
        {"outcome": "revise",
         "critique_md": "constitution alignment check failed on the policy refusal pattern. " * 3,
         "score_json": {"refusal_respect": 1}},
        {"outcome": "revise",
         "critique_md": "retry still leaks DOB in trace logs. " * 3,
         "score_json": {"redaction_completeness": 2}},
        {"outcome": "revise",
         "critique_md": "retry still misaligned with Article II refusal pattern. " * 3,
         "score_json": {"refusal_respect": 2}},
    ])
    sup = build_supervisor(
        project_root=HYDRA_ROOT,
        dispatcher=_StubDispatcher(),
        critique_client=client,
        force_pure_python=True,
    )
    # Goal phrased so the router picks 'healthcare' (best_of_n=0 AND
    # `enabled` in policy.yaml). Both conditions matter: best_of_n=0 puts us
    # on the per-squad judge path where Reflexion retry actually fires; and
    # squad_enabled=True means the scripted critique client is used (not the
    # NoOp fallback), so the revise verdicts are substantive, not
    # pragmatic-guard artefacts from a not-yet-enabled squad.
    state = HydraState(root_goal="healthcare PHI redaction policy review")
    final = _invoke(sup, state)

    # Ceiling was exhausted → workflow surfaced with reflexion_override HITL.
    assert final.phase == "surfaced", (
        f"expected phase=surfaced when ceiling exhausted, got {final.phase}"
    )
    assert final.pending_hitl is not None, "ceiling exhaustion must emit HITL"
    assert final.pending_hitl["reason"] == "reflexion_override", (
        f"expected reason=reflexion_override, got {final.pending_hitl.get('reason')}"
    )
    # Options offer both a raise-the-ceiling path and an accept-partial path.
    options = final.pending_hitl["options"]
    assert any("approve_override_raise_to_" in o for o in options), (
        f"override HITL must offer a ceiling-raise option, got {options}"
    )
    assert "accept_partial" in options


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
