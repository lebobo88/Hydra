"""`hydra finalize`'s response must be machine-actionable when a workflow
surfaces: `surfaced_reason` names the source (a judge rubric_id@N + failing
criteria, a governance gate id, or a HITL reason/summary) instead of leaving
the caller to string-parse `pending_hitl`/`last_event` themselves.

Mirrors the hermetic real-LangGraph-checkpoint pattern in
`tests/test_attended_finalize_e2_30.py`.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
from pathlib import Path
from uuid import uuid4

import pytest

from hydra_core import cli
from hydra_core.state import HydraState, TaskState

HYDRA_ROOT = Path(__file__).resolve().parents[1]


class _StubDispatcher:
    """Non-live dispatcher: build_supervisor wires no critique client for it,
    so judge_synthesis produces skeleton (always-pass) verdicts without any
    MCP call — the surfacing in these tests comes from `node_postcheck`'s
    governance pass (a failed task), not the judge plane."""

    live_execution = False

    def call_mcp(self, server, tool, args, *, squad_id=None):
        return {"status": "done", "result": {"ok": True}}

    def spawn_subprocess(self, *_a, **_kw):
        return {"status": "done"}

    def emit_claude_prompt(self, prompt, agent=None):
        return {"status": "host_pickup_required"}

    def invoke_claude_skill(self, skill, args):
        return {"status": "host_pickup_required"}

    def set_squad_packs(self, packs):
        pass


@pytest.fixture()
def hermetic(tmp_path, monkeypatch):
    monkeypatch.setenv("HYDRA_CHECKPOINT_DB", str(tmp_path / "checkpoints.db"))
    monkeypatch.setenv("HYDRA_EIGHTS_SPOOL", str(tmp_path / "spool"))
    monkeypatch.setenv("HYDRA_EIGHTS_DEAD_LETTER", str(tmp_path / "dead"))
    monkeypatch.setattr(cli, "_attended_live_dispatcher", lambda *_a, **_kw: _StubDispatcher())
    return tmp_path


def _seed_failed_task_workflow():
    """One engineering task, already `status="failed"` (excluded from
    `_attended_pending_task_ids`) -- `node_postcheck`'s governance pass
    surfaces on `"N task(s) failed"` with no HITLRequest raised, so the only
    record of why is `state.last_event`."""
    from hydra_core.supervisor import build_supervisor, _PurePythonRunner

    wf = uuid4()
    task = TaskState(owner_squad="engineering", description="ship it", status="failed")
    state = HydraState(
        workflow_id=wf,
        root_goal="finalize surfaced_reason regression (failed task)",
        selected_squads=["engineering"],
        phase="synthesis",
        tasks=[task],
        attended_completed_task_ids=[],
        attended_done_task_ids=[],
        attended_results=[],
    )
    sup = build_supervisor(project_root=HYDRA_ROOT, dispatcher=_StubDispatcher())
    assert not isinstance(sup, _PurePythonRunner), "langgraph required for this test"
    config = {"configurable": {"thread_id": str(wf)}}
    sup.update_state(config, state.model_dump(mode="json"), as_node="judge_per_squad")
    return str(wf)


def _finalize(wf: str) -> tuple[int, dict]:
    args = argparse.Namespace(project=str(HYDRA_ROOT), workflow_id=wf, verbose=False)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli._cmd_finalize(args)
    out = buf.getvalue()
    start = out.index("{")
    return rc, json.loads(out[start:])


def test_surfaced_finalize_response_carries_surfaced_reason(hermetic):
    wf = _seed_failed_task_workflow()
    rc, payload = _finalize(wf)

    # `_cmd_finalize` reports success on whether a DECISION_RECORD was
    # produced, independent of whether the workflow ended "done" or
    # "surfaced" -- rc==0/ok==True even though this run surfaces.
    assert rc == 0, payload
    assert payload["status"] == "finalized", payload
    assert payload["phase"] == "surfaced", payload
    # Every pre-existing key is still present (additive-only contract).
    for key in ("ok", "status", "workflow_id", "decision_record_id", "phase",
                "artifact_refs", "dissent_count", "sealed", "pending_hitl", "trace"):
        assert key in payload, f"missing pre-existing key {key!r}: {payload}"
    assert "surfaced_reason" in payload, payload
    reason = payload["surfaced_reason"]
    assert isinstance(reason, dict) and reason, reason
    # No HITLRequest was raised for a governance failed-task surface, so the
    # only source available is the postcheck gate id (state.last_event).
    assert "gate_id" in reason, reason
    assert "failed" in reason["gate_id"], reason


def test_non_surfaced_finalize_response_has_no_surfaced_reason(hermetic):
    """Control: a clean finalize never carries `surfaced_reason` (additive,
    not a key that always appears)."""
    from hydra_core.supervisor import build_supervisor, _PurePythonRunner

    wf = uuid4()
    task = TaskState(owner_squad="executive", description="frame it")
    rec = {
        "task_id": str(task.task_id), "owner_squad": "executive",
        "run_id": "run-executive", "status": "complete", "final_status": "complete",
        "summary": "pass", "cost_usd": 0.1,
        "artifact_ref": {"tier": "episodic", "key": "native:executive:attended",
                          "summary": "attended artifact"},
    }
    state = HydraState(
        workflow_id=wf, root_goal="finalize surfaced_reason control (clean run)",
        selected_squads=["executive"], phase="synthesis", tasks=[task],
        attended_completed_task_ids=[str(task.task_id)],
        attended_done_task_ids=[str(task.task_id)], attended_results=[rec],
    )
    sup = build_supervisor(project_root=HYDRA_ROOT, dispatcher=_StubDispatcher())
    assert not isinstance(sup, _PurePythonRunner)
    config = {"configurable": {"thread_id": str(wf)}}
    sup.update_state(config, state.model_dump(mode="json"), as_node="judge_per_squad")

    rc, payload = _finalize(str(wf))
    assert rc == 0, payload
    assert payload["status"] == "finalized", payload
    assert payload["phase"] == "done", payload
    assert "surfaced_reason" not in payload, payload


# --- Unit coverage for `_build_surfaced_reason`'s branches --------------
#
# The two tests above exercise `_build_surfaced_reason` only through the
# governance-gate (`last_event`) branch, because a real LangGraph run never
# leaves both a `fail` verdict on `state.verdicts` AND `pending_hitl`
# populated in a way that's cheap to seed hermetically. Drive the helper
# directly for the HITL-reason and judge-verdict branches instead.


def _base_state(**overrides) -> HydraState:
    wf = uuid4()
    kwargs = dict(
        workflow_id=wf,
        root_goal="surfaced_reason unit coverage",
        selected_squads=["engineering"],
        phase="surfaced",
    )
    kwargs.update(overrides)
    return HydraState(**kwargs)


def test_surfaced_reason_hitl_branch_and_last_event_fallback():
    # HITL populated with an explicit gate_node -> last_event is NOT used.
    state = _base_state(
        pending_hitl={
            "reason": "policy_breach",
            "gate_node": "node_postcheck",
            "summary": "operator review required",
        },
        last_event="unrelated last event",
    )
    reason = cli._build_surfaced_reason(state)
    assert reason == {
        "hitl_reason": "policy_breach",
        "gate_id": "node_postcheck",
        "summary": "operator review required",
    }

    # No gate_node in pending_hitl -> falls back to last_event as the
    # governance gate id.
    state2 = _base_state(
        pending_hitl={"reason": "policy_breach"},
        last_event="N task(s) failed",
    )
    reason2 = cli._build_surfaced_reason(state2)
    assert reason2["hitl_reason"] == "policy_breach"
    assert reason2["gate_id"] == "N task(s) failed"


def test_surfaced_reason_none_when_not_surfaced():
    state = _base_state(phase="done")
    assert cli._build_surfaced_reason(state) is None


def test_surfaced_reason_judge_branch_zero_to_five_scale():
    """A rubric-scored (0-5) `fail` verdict: failing criteria are those
    scoring below the raw 3-of-5 pass bar, excluding `_`-prefixed and bool
    entries."""
    state = _base_state(
        verdicts=[
            {
                "outcome": "pass",
                "rubric_id": "old-rubric@1",
                "score_json": {"clarity": 5},
            },
            {
                "outcome": "fail",
                "rubric_id": "constitution-alignment@1",
                "score_json": {
                    "clarity": 2,          # failing (< 3)
                    "correctness": 4,      # passing
                    "_meta_weight": 1,     # excluded (leading underscore)
                    "is_valid": False,     # excluded (bool, even though < 3)
                    "coverage": 2.9,       # failing (< 3)
                },
            },
        ],
    )
    reason = cli._build_surfaced_reason(state)
    assert reason["rubric_id"] == "constitution-alignment@1"
    assert set(reason["criteria"]) == {"clarity", "coverage"}


def test_surfaced_reason_judge_branch_zero_to_one_scale():
    """A codex-style (0-1) `fail` verdict: the pass bar normalises to 0.6,
    not the raw 3-of-5 rubric bar (which would wrongly fail everything on
    this scale)."""
    state = _base_state(
        verdicts=[
            {
                "outcome": "fail",
                "rubric_id": "constitution-alignment@1",
                "score_json": {
                    "clarity": 0.4,        # failing (< 0.6)
                    "correctness": 0.95,   # passing
                    "_meta_weight": 0.1,   # excluded (leading underscore)
                    "is_valid": False,     # excluded (bool)
                    "coverage": 0.59,      # failing (< 0.6)
                },
            },
        ],
    )
    reason = cli._build_surfaced_reason(state)
    assert reason["rubric_id"] == "constitution-alignment@1"
    assert set(reason["criteria"]) == {"clarity", "coverage"}


def test_surfaced_reason_judge_branch_uses_most_recent_fail_verdict():
    state = _base_state(
        verdicts=[
            {
                "outcome": "fail",
                "rubric_id": "stale-rubric@1",
                "score_json": {"stale_criterion": 1},
            },
            {
                "outcome": "pass",
                "rubric_id": "intermediate-rubric@1",
                "score_json": {"ok": 5},
            },
            {
                "outcome": "fail",
                "rubric_id": "constitution-alignment@1",
                "score_json": {"fresh_criterion": 1, "other_dim": 4},
            },
        ],
    )
    reason = cli._build_surfaced_reason(state)
    assert reason["rubric_id"] == "constitution-alignment@1"
    assert reason["criteria"] == ["fresh_criterion"]


def test_surfaced_reason_mixed_scale_is_unknown_not_guessed():
    """A verdict mixing a plausible 0-1-scale value with a value outside
    both the [0,1] and [0,5] bounds (e.g. a negative score alongside a
    normalised one) is ambiguous -- the helper must not guess a pass bar
    for it. Neither the [0,1] rule (violated by -0.5) nor the [0,5] rule
    (also violated by -0.5) apply, so it falls through to "unknown"."""
    state = _base_state(
        verdicts=[
            {
                "outcome": "fail",
                "rubric_id": "constitution-alignment@1",
                "score_json": {"clarity": 0.4, "correctness": -0.5},
            },
        ],
    )
    reason = cli._build_surfaced_reason(state)
    assert reason["rubric_id"] == "constitution-alignment@1"
    assert reason.get("criteria_scale") == "unknown"
    assert not reason.get("criteria")


def test_surfaced_reason_out_of_range_scale_is_unknown_not_guessed():
    """Scores outside [0, 5] (e.g. a 0-10 scale) are not misread as 0-5."""
    state = _base_state(
        verdicts=[
            {
                "outcome": "fail",
                "rubric_id": "constitution-alignment@1",
                "score_json": {"clarity": 2, "correctness": 10},
            },
        ],
    )
    reason = cli._build_surfaced_reason(state)
    assert reason["rubric_id"] == "constitution-alignment@1"
    assert reason.get("criteria_scale") == "unknown"
    assert not reason.get("criteria")


# --- Total-function guards: malformed/untrusted checkpoint data must -----
# never raise into `_cmd_finalize`'s response.
#
# `HydraState.verdicts` is typed `list[dict[str, Any]]` and `pending_hitl`
# is typed `Optional[dict[str, Any]]`, so pydantic itself would reject a
# non-dict verdict entry or a non-dict `pending_hitl` at construction time.
# `_build_surfaced_reason` is duck-typed (it only reads `.phase`,
# `.pending_hitl`, `.last_event`, `.verdicts`), so a corrupted/partial
# checkpoint that bypasses that validation (e.g. sanitized/replayed state,
# or a future schema drift) is modeled here with a minimal stand-in rather
# than fighting pydantic's own type enforcement.
class _RawState:
    def __init__(self, *, phase="surfaced", pending_hitl=None, last_event=None,
                 verdicts=None):
        self.phase = phase
        self.pending_hitl = pending_hitl
        self.last_event = last_event
        self.verdicts = verdicts


def test_surfaced_reason_never_raises_on_non_mapping_score_json():
    state = _base_state(
        verdicts=[
            {"outcome": "fail", "rubric_id": "constitution-alignment@1",
             "score_json": "bad"},
        ],
    )
    reason = cli._build_surfaced_reason(state)  # must not raise
    assert reason["rubric_id"] == "constitution-alignment@1"
    assert "criteria" not in reason


def test_surfaced_reason_never_raises_on_non_string_score_keys():
    state = _base_state(
        verdicts=[
            {"outcome": "fail", "rubric_id": "constitution-alignment@1",
             "score_json": {1: 0.2, "clarity": 0.4}},
        ],
    )
    reason = cli._build_surfaced_reason(state)  # must not raise
    assert reason["rubric_id"] == "constitution-alignment@1"
    assert reason["criteria"] == ["clarity"]


def test_surfaced_reason_never_raises_on_non_dict_verdict_entry():
    state = _RawState(
        verdicts=[
            "not-a-verdict",
            {"outcome": "fail", "rubric_id": "constitution-alignment@1",
             "score_json": {"clarity": 1}},
        ],
    )
    reason = cli._build_surfaced_reason(state)  # must not raise
    assert reason["rubric_id"] == "constitution-alignment@1"


def test_surfaced_reason_never_raises_on_non_dict_pending_hitl():
    state = _RawState(pending_hitl="not-a-dict", last_event="N task(s) failed")
    reason = cli._build_surfaced_reason(state)  # must not raise
    assert reason == {"gate_id": "N task(s) failed"}


def test_surfaced_reason_never_raises_on_truthy_non_iterable_verdicts():
    """`HydraState.verdicts` is typed `list[dict[str, Any]]`, so a truthy
    non-iterable (an int, a bare `object()`) is only reachable via
    `model_construct`, which bypasses pydantic validation -- e.g. a
    corrupted/partial checkpoint or a future schema drift. The old
    `raw_verdicts = final_state.verdicts or []` guard only catches falsy
    values (`0`, `None`, `[]`); a truthy non-iterable like `5` or
    `object()` sailed straight into `for v in raw_verdicts:` and raised
    `TypeError: 'int' object is not iterable`.

    A HITL reason is present so we can also assert the fix doesn't regress
    the HITL branch: the result must still surface the HITL-reason fields
    even though the malformed `verdicts` value is discarded, and it must
    carry no verdict-derived `rubric_id`/`criteria`.
    """
    wf = uuid4()
    for bad_verdicts in (5, object()):
        state = HydraState.model_construct(
            workflow_id=wf,
            root_goal="surfaced_reason unit coverage",
            selected_squads=["engineering"],
            phase="surfaced",
            pending_hitl={
                "reason": "policy_breach",
                "gate_node": "node_postcheck",
                "summary": "operator review required",
            },
            last_event=None,
            verdicts=bad_verdicts,
        )
        reason = cli._build_surfaced_reason(state)  # must not raise
        assert reason == {
            "hitl_reason": "policy_breach",
            "gate_id": "node_postcheck",
            "summary": "operator review required",
        }
        assert "rubric_id" not in reason
        assert "criteria" not in reason
