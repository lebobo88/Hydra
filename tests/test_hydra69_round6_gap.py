"""Hydra#69 round 6 remaining gap: no path may continue or re-open a
workflow whose ``terminal_resolution`` is set.

`_cmd_attended_step` and `_cmd_finalize` loaded workflow state and could
dispatch a new attended task, write ``phase="synthesis"``, or call
``sup.invoke(...)`` with no check of ``state.terminal_resolution`` at all.
Fixed by a single shared predicate, ``hydra_core.state.
workflow_terminal_resolution`` (durable field, or -- for a legacy checkpoint
-- the same ``hitl_history`` scan the resume path already used), consulted
by ``_cmd_resume_locked``, ``_cmd_attended_step`` and ``_cmd_finalize``
before any of them can advance a workflow.

Every test here is proven as a property: reverting the corresponding fix
makes the test fail (see the paired revert/restore runs in the PR notes).
Uses REAL LangGraph checkpoints (hermetic SQLite, `HYDRA_CHECKPOINT_DB` in
tmp_path) -- the same `hermetic` fixture pattern as
`tests/test_hydra69_round6.py` / `tests/test_attended_finalize_e2_30.py`.
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
from hydra_core.state import HydraState, TaskState, workflow_terminal_resolution

HYDRA_ROOT = Path(__file__).resolve().parents[1]


class _StubDispatcher:
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


def _seed_durable_terminal_workflow(*, action, option=None, tasks=None):
    """Seed a REAL LangGraph checkpoint whose durable `terminal_resolution`
    field is already set (an operator abort/reject), still parked at the
    `approval` bare interrupt (mirrors round 6's `_seed_bare_interrupt_
    workflow`, plus the durable field this round's fix actually reads)."""
    from hydra_core.supervisor import build_supervisor, _PurePythonRunner

    wf = uuid4()
    state = HydraState(
        workflow_id=wf, root_goal="round 6 remaining-gap terminal repro",
        phase="surfaced", pending_hitl=None, tasks=tasks or [],
        requires_human_approval=True,
        terminal_resolution={
            "gate_node": "approval", "hitl_request_id": None,
            "action": action, "option": option, "plan_revision": None,
            "resolved_at": "2026-09-23T00:00:00+00:00",
        },
    )
    sup = build_supervisor(project_root=HYDRA_ROOT, dispatcher=_StubDispatcher())
    assert not isinstance(sup, _PurePythonRunner), "langgraph required for this test"
    config = {"configurable": {"thread_id": str(wf)}}
    sup.update_state(config, state.model_dump(mode="json"), as_node="planner")
    return str(wf), sup, config


def _seed_legacy_terminal_workflow():
    """Seed a REAL LangGraph checkpoint with NO durable `terminal_resolution`
    field at all (a checkpoint written before that field existed) but whose
    `hitl_history` records a terminal reject for the parked `plan_gate`
    occurrence -- the legacy fallback path."""
    from hydra_core.supervisor import build_supervisor, _PurePythonRunner

    wf = uuid4()
    plan_ref = {
        "steps": [{
            "step_id": "step-1", "target_squad": "engineering",
            "description": "do the thing", "priority": "P2",
            "acceptance_criteria": ["it works"], "envelope_type": "DEV_TASK",
        }],
    }
    state = HydraState(
        workflow_id=wf, root_goal="round 6 remaining-gap legacy terminal repro",
        phase="approval", plan_status="judged", plan_ref=plan_ref,
        plan_revision=1, pending_hitl=None, tasks=[],
        hitl_history=[{
            "gate_node": "plan_gate", "resolution": "reject",
            "option": None, "plan_revision": 1,
        }],
    )
    sup = build_supervisor(project_root=HYDRA_ROOT, dispatcher=_StubDispatcher())
    assert not isinstance(sup, _PurePythonRunner), "langgraph required for this test"
    config = {"configurable": {"thread_id": str(wf)}}
    sup.update_state(config, state.model_dump(mode="json"), as_node="plan_judge")
    snap = sup.get_state(config)
    assert tuple(snap.next) == ("plan_gate",), (
        f"fixture must park at plan_gate; got next={snap.next!r}"
    )
    assert snap.values.get("terminal_resolution") is None, (
        "this fixture must exercise the LEGACY fallback, not the durable field"
    )
    return str(wf), sup, config


def _seed_non_terminal_workflow(*, tasks):
    """Seed a REAL, genuinely non-terminal checkpoint (no pending gate, no
    terminal_resolution, one open attended task) -- the control fixture."""
    from hydra_core.supervisor import build_supervisor, _PurePythonRunner

    wf = uuid4()
    state = HydraState(
        workflow_id=wf, root_goal="round 6 remaining-gap control (non-terminal)",
        phase="dispatch", pending_hitl=None, tasks=tasks,
        requires_human_approval=False,
    )
    sup = build_supervisor(project_root=HYDRA_ROOT, dispatcher=_StubDispatcher())
    assert not isinstance(sup, _PurePythonRunner), "langgraph required for this test"
    config = {"configurable": {"thread_id": str(wf)}}
    sup.update_state(config, state.model_dump(mode="json"), as_node="dispatch")
    return str(wf), sup, config


def _step(wf_id):
    args = argparse.Namespace(project=str(HYDRA_ROOT), workflow_id=wf_id, verbose=False)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        rc = cli._cmd_attended_step(args)
    out = buf.getvalue()
    start = out.index("{")
    return rc, json.loads(out[start:])


def _finalize(wf_id):
    args = argparse.Namespace(project=str(HYDRA_ROOT), workflow_id=wf_id, verbose=False)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        rc = cli._cmd_finalize(args)
    out = buf.getvalue()
    start = out.index("{")
    return rc, json.loads(out[start:])


def _cursor_dir(wf_id: str) -> Path:
    return HYDRA_ROOT / ".hydra" / wf_id / "attended"


class TestRemainingGapStepRefusesTerminalWorkflow:
    def test_step_refuses_durable_terminal_abort_and_opens_no_cursor(self, hermetic):
        task = TaskState(owner_squad="healthcare", description="assess readiness")
        wf, sup, config = _seed_durable_terminal_workflow(
            action="approve", option="abort", tasks=[task])

        rc, body = _step(wf)
        assert rc == 0, body
        assert body["ok"] is False
        assert body["status"] == "workflow_terminal"
        assert body["terminal_resolution"]["option"] == "abort"

        values = sup.get_state(config).values
        assert not values.get("attended_completed_task_ids"), (
            "a terminal workflow must never have a task marked attended-complete"
        )
        assert not values.get("open_pp_runs"), (
            "a terminal workflow must never have a pp run opened"
        )
        cdir = _cursor_dir(wf)
        assert not cdir.exists() or not any(cdir.iterdir()), (
            f"a terminal workflow must never have an attended cursor opened: "
            f"{list(cdir.iterdir()) if cdir.exists() else []}"
        )

    def test_step_refuses_durable_terminal_reject(self, hermetic):
        task = TaskState(owner_squad="healthcare", description="assess readiness")
        wf, sup, config = _seed_durable_terminal_workflow(
            action="reject", option=None, tasks=[task])

        rc, body = _step(wf)
        assert rc == 0, body
        assert body["ok"] is False
        assert body["status"] == "workflow_terminal"
        assert body["terminal_resolution"]["action"] == "reject"

        cdir = _cursor_dir(wf)
        assert not cdir.exists() or not any(cdir.iterdir())

    def test_step_refuses_legacy_terminal_checkpoint(self, hermetic):
        wf, sup, config = _seed_legacy_terminal_workflow()

        rc, body = _step(wf)
        assert rc == 0, body
        assert body["ok"] is False
        assert body["status"] == "workflow_terminal"
        assert body["terminal_resolution"]["resolution"] == "reject"

        cdir = _cursor_dir(wf)
        assert not cdir.exists() or not any(cdir.iterdir())

    def test_step_unaffected_for_a_non_terminal_workflow(self, hermetic):
        """Control: a merely-open (never aborted/rejected) workflow must
        keep dispatching exactly as before."""
        task = TaskState(owner_squad="healthcare", description="assess readiness")
        wf, sup, config = _seed_non_terminal_workflow(tasks=[task])

        rc, body = _step(wf)
        assert rc == 0, body
        assert body.get("status") != "workflow_terminal"


class TestRemainingGapFinalizeRefusesTerminalWorkflow:
    def test_finalize_refuses_durable_terminal_abort_and_leaves_phase_unchanged(
        self, hermetic,
    ):
        task = TaskState(owner_squad="healthcare", description="assess readiness")
        wf, sup, config = _seed_durable_terminal_workflow(
            action="approve", option="abort", tasks=[task])
        before_phase = sup.get_state(config).values.get("phase")

        rc, body = _finalize(wf)
        assert rc == 0, body
        assert body["ok"] is False
        assert body["status"] == "workflow_terminal"

        after = sup.get_state(config).values
        assert after.get("phase") == before_phase, (
            f"finalize must never advance phase on a terminal workflow: "
            f"before={before_phase!r} after={after.get('phase')!r}"
        )
        assert after.get("phase") != "synthesis"
        assert not after.get("attended_finalized_record_id")

    def test_finalize_refuses_legacy_terminal_checkpoint(self, hermetic):
        wf, sup, config = _seed_legacy_terminal_workflow()
        before_phase = sup.get_state(config).values.get("phase")

        rc, body = _finalize(wf)
        assert rc == 0, body
        assert body["ok"] is False
        assert body["status"] == "workflow_terminal"

        after = sup.get_state(config).values
        assert after.get("phase") == before_phase
        assert after.get("phase") != "synthesis"

    def test_finalize_already_finalized_still_wins_over_terminal_check(self, hermetic):
        """Check-order requirement: a workflow finalized BEFORE it became
        terminal (impossible in practice -- a finalized workflow has no
        pending gate left to abort/reject -- but the ORDER of the two checks
        must still put `already_finalized` first) keeps returning
        `already_finalized`, never `workflow_terminal`."""
        from hydra_core.supervisor import build_supervisor, _PurePythonRunner

        wf = uuid4()
        record_id = str(uuid4())
        state = HydraState(
            workflow_id=wf, root_goal="round 6 remaining-gap already-finalized repro",
            phase="done", pending_hitl=None, tasks=[],
            attended_finalized_record_id=record_id,
            terminal_resolution={
                "gate_node": "approval", "hitl_request_id": None,
                "action": "reject", "option": None, "plan_revision": None,
                "resolved_at": "2026-09-23T00:00:00+00:00",
            },
        )
        sup = build_supervisor(project_root=HYDRA_ROOT, dispatcher=_StubDispatcher())
        assert not isinstance(sup, _PurePythonRunner)
        config = {"configurable": {"thread_id": str(wf)}}
        sup.update_state(config, state.model_dump(mode="json"), as_node="postcheck")

        rc, body = _finalize(str(wf))
        assert rc == 0, body
        assert body["ok"] is True
        assert body["status"] == "already_finalized"
        assert body["decision_record_id"] == record_id

    def test_finalize_unaffected_for_a_non_terminal_completed_workflow(self, hermetic):
        """Control: a genuinely non-terminal, all-tasks-done workflow keeps
        finalizing exactly as before (existing E2-30 behaviour)."""
        from hydra_core.supervisor import build_supervisor, _PurePythonRunner

        wf = uuid4()
        task = TaskState(owner_squad="executive", description="frame the decision")
        result = {
            "task_id": str(task.task_id), "owner_squad": "executive",
            "run_id": "run-executive", "status": "complete",
            "final_status": "complete", "summary": "pass", "cost_usd": 0.1,
            "artifact_ref": {"tier": "episodic", "key": "native:executive:attended",
                              "summary": "attended artifact"},
        }
        state = HydraState(
            workflow_id=wf, root_goal="round 6 remaining-gap non-terminal finalize control",
            selected_squads=["executive"], phase="synthesis", tasks=[task],
            attended_completed_task_ids=[str(task.task_id)],
            attended_done_task_ids=[str(task.task_id)],
            attended_results=[result],
        )
        sup = build_supervisor(project_root=HYDRA_ROOT, dispatcher=_StubDispatcher())
        assert not isinstance(sup, _PurePythonRunner)
        config = {"configurable": {"thread_id": str(wf)}}
        sup.update_state(config, state.model_dump(mode="json"), as_node="judge_per_squad")

        rc, body = _finalize(str(wf))
        assert rc == 0, body
        assert body.get("status") != "workflow_terminal"


class TestWorkflowTerminalResolutionHelperUnit:
    """Unit coverage for the shared predicate itself (state.py)."""

    def test_durable_field_wins_regardless_of_snap_next(self):
        term = {"gate_node": "approval", "action": "reject", "option": None}
        values = {"terminal_resolution": term, "hitl_history": []}
        assert workflow_terminal_resolution(values, ()) == term
        assert workflow_terminal_resolution(values, ("approval",)) == term

    def test_legacy_fallback_requires_snap_next(self):
        values = {
            "hitl_history": [{"gate_node": "plan_gate", "resolution": "reject",
                              "option": None, "plan_revision": 1}],
            "plan_revision": 1,
        }
        assert workflow_terminal_resolution(values, ()) is None, (
            "the legacy scan must never fire with no parked gate to bind to"
        )
        assert workflow_terminal_resolution(values, ("plan_gate",)) is not None

    def test_bare_interrupt_with_no_terminal_history_returns_none(self):
        """MU7 control: a genuine bare interrupt (paused before synthesis,
        never resolved) must return None, not be misread as terminal."""
        values = {"hitl_history": [], "plan_revision": 0}
        assert workflow_terminal_resolution(values, ("judge_synthesis",)) is None


class TestLegacyStaleRejectAtDifferentGateOccurrenceIsNotRefused:
    """Cross-vendor follow-up (b): a legacy checkpoint (no durable
    `terminal_resolution` field) whose latest `hitl_history` resolution is an
    OLD reject of `plan_gate` at revision 1 must not block a FRESH,
    never-resolved occurrence of `plan_gate` at revision 2 -- proven already
    at the unit level (`workflow_terminal_resolution`) and through
    `_cmd_resume`, but never through `_cmd_attended_step`/`_cmd_finalize`
    directly. Both consult the SAME shared helper, so this closes the gap in
    coverage rather than testing new production behaviour."""

    def _seed(self):
        from hydra_core.supervisor import build_supervisor, _PurePythonRunner

        wf = uuid4()
        plan_ref = {
            "steps": [{
                "step_id": "step-1", "target_squad": "engineering",
                "description": "do the thing", "priority": "P2",
                "acceptance_criteria": ["it works"], "envelope_type": "DEV_TASK",
            }],
        }
        state = HydraState(
            workflow_id=wf, root_goal="round 6 gap legacy stale-reject-new-revision repro",
            phase="approval", plan_status="judged", plan_ref=plan_ref,
            plan_revision=2, pending_hitl=None, tasks=[],
            hitl_history=[{
                "gate_node": "plan_gate", "resolution": "reject",
                "option": None, "plan_revision": 1,
            }],
        )
        sup = build_supervisor(project_root=HYDRA_ROOT, dispatcher=_StubDispatcher())
        assert not isinstance(sup, _PurePythonRunner), "langgraph required for this test"
        config = {"configurable": {"thread_id": str(wf)}}
        sup.update_state(config, state.model_dump(mode="json"), as_node="plan_judge")
        snap = sup.get_state(config)
        assert tuple(snap.next) == ("plan_gate",), (
            f"fixture must park at plan_gate; got next={snap.next!r}"
        )
        assert snap.values.get("terminal_resolution") is None, (
            "this fixture must exercise the LEGACY fallback, not the durable field"
        )
        return str(wf), sup, config

    def test_step_not_refused_for_stale_reject_at_different_plan_revision(self, hermetic):
        wf, sup, config = self._seed()

        rc, body = _step(wf)
        assert rc == 0, body
        assert body.get("status") != "workflow_terminal", (
            f"a stale reject bound to an OLDER plan_revision must never "
            f"refuse a fresh, never-resolved occurrence: {body}"
        )

    def test_finalize_not_refused_for_stale_reject_at_different_plan_revision(self, hermetic):
        wf, sup, config = self._seed()

        rc, body = _finalize(wf)
        assert rc == 0, body
        assert body.get("status") != "workflow_terminal", (
            f"a stale reject bound to an OLDER plan_revision must never "
            f"refuse `finalize` for a fresh, never-resolved occurrence: {body}"
        )


# =========================================================================== #
# Remaining gap (authoritative, this round): `recover-stalled-stage` could
# still continue/re-open a terminal workflow -- `_cmd_recover_stalled_stage`
# never consulted `workflow_terminal_resolution` before dispatching to
# `host_bridge.recover_stalled_stage`, which can call `_finalize(...,
# passed=passed)` (merging a worktree on a pass) or, for a surfaced cursor,
# `_merge_branch_back` directly. These tests prove the CLI wiring: the
# checkpoint's terminal status is read FIRST and threaded into
# `host_bridge.recover_stalled_stage` as `workflow_terminal=`, and the
# already-incurred cost is still charged exactly once even when refused.
# The actual merge-refusal behaviour inside `host_bridge.recover_stalled_
# stage` itself is proven separately in `tests/test_host_bridge.py`
# (`test_recover_stalled_stage_workflow_terminal_stalled_infra_refuses_merge`
# / `test_recover_stalled_stage_workflow_terminal_surfaced_never_calls_
# merge_branch_back`).
# =========================================================================== #

class TestRemainingGapRecoverStalledStageRefusesTerminalWorkflow:
    def _seed_cursor(self, wf, run_id):
        from hydra_core import host_bridge
        cfile = host_bridge.cursor_path(HYDRA_ROOT, wf, run_id)
        cfile.parent.mkdir(parents=True, exist_ok=True)
        host_bridge.save_cursor(cfile, {
            "schema": host_bridge.CURSOR_SCHEMA, "kind": "engineering",
            "workflow_id": wf, "run_id": run_id, "state": "stalled_infra",
        })
        return cfile

    def _recover(self, monkeypatch, wf, run_id, *, spy_result, seen):
        from hydra_core import host_bridge

        def _fake_recover(dispatcher, *, cursor_file, workflow_terminal=False):
            seen.append(workflow_terminal)
            return dict(spy_result)

        monkeypatch.setattr(host_bridge, "recover_stalled_stage", _fake_recover)
        args = argparse.Namespace(project=str(HYDRA_ROOT), workflow_id=wf, verbose=False)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            rc = cli._cmd_recover_stalled_stage(args, HYDRA_ROOT, wf, run_id)
        out = buf.getvalue()
        start = out.index("{")
        return rc, json.loads(out[start:])

    def test_recover_threads_workflow_terminal_true_and_charges_cost_once(
        self, hermetic, monkeypatch,
    ):
        task = TaskState(owner_squad="engineering", description="ship the feature")
        wf, sup, config = _seed_durable_terminal_workflow(
            action="reject", option=None, tasks=[task])
        run_id = "run-recover-terminal-1"
        cfile = self._seed_cursor(wf, run_id)
        try:
            seen: list[bool] = []
            rc, body = self._recover(monkeypatch, wf, run_id, spy_result={
                "ok": True, "status": "surfaced", "state": "surfaced",
                "run_id": run_id, "stage_id": "stage-1", "task_id": str(task.task_id),
                "squad_slug": "engineering", "cost_usd": 0.35,
                "tokens_in": 10, "tokens_out": 5, "cost_source": "measured",
                "merge": {"merged": False, "error": "workflow_terminal"},
                "already_charged": False,
            }, seen=seen)

            assert rc == 0, body
            assert seen == [True], (
                "the CLI must read the checkpoint's terminal status BEFORE "
                "calling host_bridge.recover_stalled_stage and thread it "
                f"through as workflow_terminal=True; saw {seen!r}"
            )
            assert body["ok"] is True
            assert body["workflow_terminal"]["action"] == "reject"
            assert body["merge"]["merged"] is False
            assert body["merge"]["error"] == "workflow_terminal"

            values = sup.get_state(config).values
            spent = float((values.get("budget") or {}).get("spent_usd") or 0.0)
            assert spent == pytest.approx(0.35), (
                "the already-incurred cost of this recovery call must still "
                f"be charged exactly once; spent_usd={spent!r}"
            )

            # A second identical recovery call (e.g. a retried operator
            # invocation) must never re-charge -- idempotency is unaffected
            # by the terminal refusal.
            seen2: list[bool] = []
            rc2, body2 = self._recover(monkeypatch, wf, run_id, spy_result={
                "ok": True, "status": "surfaced", "state": "surfaced",
                "run_id": run_id, "stage_id": "stage-1", "task_id": str(task.task_id),
                "squad_slug": "engineering", "cost_usd": 0.35,
                "tokens_in": 10, "tokens_out": 5, "cost_source": "measured",
                "merge": {"merged": False, "error": "workflow_terminal"},
                "already_charged": True,
            }, seen=seen2)
            assert rc2 == 0, body2
            values2 = sup.get_state(config).values
            spent2 = float((values2.get("budget") or {}).get("spent_usd") or 0.0)
            assert spent2 == pytest.approx(0.35), (
                "a retried recovery call must never double-charge"
            )
        finally:
            import shutil
            shutil.rmtree(cfile.parent.parent, ignore_errors=True)

    def test_recover_threads_workflow_terminal_false_for_non_terminal_workflow(
        self, hermetic, monkeypatch,
    ):
        task = TaskState(owner_squad="engineering", description="ship the feature")
        wf, sup, config = _seed_non_terminal_workflow(tasks=[task])
        run_id = "run-recover-nonterminal-1"
        cfile = self._seed_cursor(wf, run_id)
        try:
            seen: list[bool] = []
            rc, body = self._recover(monkeypatch, wf, run_id, spy_result={
                "ok": True, "status": "complete", "state": "complete",
                "run_id": run_id, "stage_id": "stage-1", "task_id": str(task.task_id),
                "squad_slug": "engineering", "cost_usd": 0.20,
                "tokens_in": 10, "tokens_out": 5, "cost_source": "measured",
                "merge": {"merged": True}, "already_charged": False,
            }, seen=seen)

            assert rc == 0, body
            assert seen == [False], (
                f"a non-terminal workflow must pass workflow_terminal=False "
                f"(recovery behaviour byte-for-byte unchanged); saw {seen!r}"
            )
            assert "workflow_terminal" not in body
        finally:
            import shutil
            shutil.rmtree(cfile.parent.parent, ignore_errors=True)
