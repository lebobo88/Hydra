"""Hydra#70 regression: an attended engineering stage whose async smoke job is
completed through a ``hydra.workflow.step`` poll never got the workflow
checkpoint bookkeeping (budget charge, ``attended_completed_task_ids`` /
``attended_done_task_ids`` / ``attended_results``, ``open_pp_runs`` removal,
``attended_charge_applied`` / ``attended_checkpoint_reconciled`` markers) --
only a ``submit-host-result`` call ever reached it. The finished task was
re-selected on the NEXT ``step`` and a duplicate pp run was opened (live on
workflow 0710b823, run run_aypCoybNtHLH -> a second run_7W4rZAbm3Ror).

The fix (see ``hydra_core.host_bridge.poll_smoke_job`` and
``hydra_core.cli._reconcile_attended_terminal_checkpoint`` /
``_cmd_attended_step``):

1. ``poll_smoke_job`` stamps ``cursor["terminal_call_key"]`` itself (from the
   smoke job's own recorded ``call_key`` -- the judge call that started it)
   the instant the poll drives the cursor terminal, regardless of which
   caller (a bare ``step`` poll, or a same-call_key ``submit_host_result``
   resubmit acting as a poll) reached it -- previously only
   ``submit_host_result``'s own post-transition stamp did this.
2. The post-terminal checkpoint bookkeeping ``_cmd_attended_submit`` used to
   run entirely inline is now the shared
   ``_reconcile_attended_terminal_checkpoint`` function, and
   ``_cmd_attended_step``'s smoke-poll branch calls it too.
3. ``_cmd_attended_step`` self-heals: before task selection, any
   ``open_pp_runs`` entry whose cursor is ALREADY terminal but unreconciled
   (a crash between poll and bookkeeping, or a pre-fix checkpoint) is
   repaired through the same shared function before a task is ever picked.

Every test here is proven as a property: reverting the corresponding fix
makes it fail.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import time
from pathlib import Path
from uuid import uuid4

import pytest

from hydra_core import cli, host_bridge, smoke_job
from hydra_core.state import HydraState, TaskState
from hydra_core.supervisor import build_supervisor, _PurePythonRunner

from tests.test_hydra70_async_smoke import (
    FakeDispatcher,
    _commit_file,
    _git,
    _init_repo,
    _fake_job,
)

HYDRA_ROOT = Path(__file__).resolve().parents[1]

_JUDGE_PASS = {"outcome": "pass", "critique_md": "looks good",
              "judge_producer": "codex", "cost_usd": 0.05}
_JUDGE_FAIL = {"outcome": "fail", "critique_md": "nope",
              "judge_producer": "codex", "cost_usd": 0.05}


@pytest.fixture(autouse=True)
def _default_async(monkeypatch):
    monkeypatch.delenv("HYDRA_ATTENDED_SMOKE_MODE", raising=False)


@pytest.fixture()
def hermetic(tmp_path, monkeypatch):
    """Real SQLite checkpoint (isolated), no eights daemon traffic."""
    monkeypatch.setenv("HYDRA_CHECKPOINT_DB", str(tmp_path / "checkpoints.db"))
    monkeypatch.setenv("HYDRA_EIGHTS_SPOOL", str(tmp_path / "spool"))
    monkeypatch.setenv("HYDRA_EIGHTS_DEAD_LETTER", str(tmp_path / "dead"))
    monkeypatch.setenv("HYDRA_WORKTREE_ROOT", str(tmp_path / "wt-root"))
    return tmp_path


def _seed(tmp_path, monkeypatch, *, required_cross_vendor=True):
    """Open a real pp engineering cursor (host_bridge.begin_stage, isolated
    git worktree) AND seed a matching real-checkpoint HydraState (one
    engineering task, one open_pp_runs entry) -- exactly the durable state
    `_cmd_attended_step` would have left after opening this stage itself."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    disp = FakeDispatcher(required_cross_vendor=required_cross_vendor)
    monkeypatch.setattr(cli, "_attended_live_dispatcher", lambda *a, **k: disp)

    task = TaskState(owner_squad="engineering", description="ship the change")
    wf = uuid4()
    run_id = uuid4()

    res = host_bridge.begin_stage(
        disp, workflow_id=str(wf), run_id=str(run_id),
        project_path=str(repo), request_text="implement the thing",
        project_root=str(HYDRA_ROOT), task_id=str(task.task_id),
    )
    work_path = res["host_action"]["cwd"]

    state = HydraState(
        workflow_id=wf, root_goal="ship it",
        selected_squads=["engineering"],
        tasks=[task],
        open_pp_runs=[{"run_id": str(run_id), "project_path": str(repo)}],
    )
    sup = build_supervisor(project_root=HYDRA_ROOT, dispatcher=disp)
    assert not isinstance(sup, _PurePythonRunner), "langgraph required for this test"
    config = {"configurable": {"thread_id": str(wf)}}
    sup.update_state(config, state.model_dump(mode="json"), as_node="judge_per_squad")

    return {
        "wf": str(wf), "run_id": str(run_id), "task_id": str(task.task_id),
        "repo": repo, "work_path": work_path, "disp": disp, "sup": sup,
        "config": config, "cursor_path": res["cursor_path"],
    }


def _drive_to_await_smoke(ctx, monkeypatch, *, calls=None):
    """generate-0 -> judge pass -> await_smoke, with a controlled fake job."""
    if calls is None:
        calls = []
    _fake_job(monkeypatch, calls=calls)
    _commit_file(ctx["work_path"], "foo.py", "print('hi')\n")
    res = host_bridge.submit_host_result(
        ctx["disp"], cursor_file=ctx["cursor_path"], call_key="generate-0",
        result={"text": "implemented the thing", "cost_usd": 0.10,
                "tokens_in": 100, "tokens_out": 50, "model": "claude-opus-4-8"})
    assert res["state"] == "await_judge", res
    # Mirrors host_bridge._apply_generate's
    # f"judge-{run_id}-{stage_id}-{attempt_id}-{gen_idx}" -- FakeDispatcher
    # always answers start_stage/record_attempt with stage_id="stage-1" /
    # attempt_id="att-1", and this is generation 0.
    judge_key = f"judge-{ctx['run_id']}-stage-1-att-1-0"
    res2 = host_bridge.submit_host_result(
        ctx["disp"], cursor_file=ctx["cursor_path"], call_key=judge_key,
        result=_JUDGE_PASS)
    assert res2["state"] == "await_smoke", res2
    return judge_key


def _write_job_result(ctx, *, status="pass", reason="fake smoke"):
    cursor = host_bridge.load_cursor(ctx["cursor_path"])
    job = cursor["smoke_job"]
    Path(job["result_path"]).write_text(
        json.dumps({"status": status, "reason": reason, "finished_at": time.time()}),
        encoding="utf-8")


def _step(ctx) -> tuple[int, dict]:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli._cmd_attended_step(
            argparse.Namespace(project=str(HYDRA_ROOT), workflow_id=ctx["wf"], verbose=False))
    return rc, json.loads(buf.getvalue())


def _submit(ctx, call_key, result_path) -> tuple[int, dict]:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli._cmd_attended_submit(argparse.Namespace(
            project=str(HYDRA_ROOT), workflow_id=ctx["wf"], run_id=ctx["run_id"],
            call_key=call_key, result=str(result_path), verbose=False))
    out = buf.getvalue()
    return rc, json.loads(out)


def _write_result(tmp_path, name, payload):
    p = tmp_path / name
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


def _fresh_state(ctx) -> HydraState:
    snap = ctx["sup"].get_state(ctx["config"])
    return HydraState.model_validate(snap.values)


# =========================================================================== #
# 1. step poll drives the cursor terminal -> full checkpoint bookkeeping,
#    exactly once.
# =========================================================================== #

def test_step_poll_completes_reconciles_checkpoint_exactly_once(hermetic, monkeypatch):
    ctx = _seed(hermetic, monkeypatch)
    judge_key = _drive_to_await_smoke(ctx, monkeypatch)
    _write_job_result(ctx, status="pass")

    rc, body = _step(ctx)
    assert rc == 0, body
    assert body["status"] == "complete", body
    assert body.get("terminal_call_key") == judge_key, body

    state = _fresh_state(ctx)
    assert state.budget.spent_usd > 0.0, "budget must have been charged"
    assert ctx["task_id"] in state.attended_completed_task_ids
    assert ctx["task_id"] in state.attended_done_task_ids
    assert len(state.attended_results) == 1
    assert state.attended_results[0]["task_id"] == ctx["task_id"]
    assert state.open_pp_runs == [], "the finished run must be removed"
    recon_key = f"{ctx['run_id']}:{judge_key}"
    assert state.attended_checkpoint_reconciled.get(recon_key) is True
    assert recon_key in state.attended_charge_applied

    cursor = host_bridge.load_cursor(ctx["cursor_path"])
    assert cursor.get("terminal_call_key") == judge_key
    # Secondary evidence (round-2 critique item C): the identity
    # `_reconcile_attended_terminal_checkpoint` reconciled on (and stamped as
    # `terminal_call_key`) is the SAME judge call_key the terminal transition
    # itself recorded on the cursor as `verdict_recorded_for` /
    # `smoke_result_for.call_key` -- proving the self-heal derivation (which
    # reads those two markers on a pre-fix cursor lacking `terminal_call_key`)
    # reconstructs exactly the identity a normal poll/submit already stamps,
    # never a different one.
    assert cursor.get("verdict_recorded_for") == judge_key
    assert cursor.get("smoke_result_for", {}).get("call_key") == judge_key


def test_next_step_after_poll_reconcile_is_ready_to_finalize_no_start_run(
    hermetic, monkeypatch,
):
    ctx = _seed(hermetic, monkeypatch)
    _drive_to_await_smoke(ctx, monkeypatch)
    _write_job_result(ctx, status="pass")
    rc, body = _step(ctx)
    assert rc == 0 and body["status"] == "complete", body

    start_run_calls_before = ctx["disp"].count("start_run")
    rc2, body2 = _step(ctx)
    assert rc2 == 0, body2
    assert body2["status"] == "ready_to_finalize", body2
    assert ctx["disp"].count("start_run") == start_run_calls_before, (
        "the finished task must never be re-selected/re-dispatched"
    )


# =========================================================================== #
# 2 / 3. step-poll completion and submit-poll completion converge on the SAME
#         reconciliation identity -- neither double-charges the other.
# =========================================================================== #

def test_step_poll_then_same_call_key_submit_no_double_charge(hermetic, monkeypatch, tmp_path):
    ctx = _seed(hermetic, monkeypatch)
    judge_key = _drive_to_await_smoke(ctx, monkeypatch)
    _write_job_result(ctx, status="pass")

    rc, body = _step(ctx)
    assert rc == 0 and body["status"] == "complete", body
    spent_after_step = _fresh_state(ctx).budget.spent_usd

    r = _write_result(tmp_path, "r.json", _JUDGE_PASS)
    rc2, body2 = _submit(ctx, judge_key, r)
    assert rc2 == 0, body2
    assert body2.get("already_charged") is True, body2

    state = _fresh_state(ctx)
    assert state.budget.spent_usd == spent_after_step, "must not double-charge"
    assert len(state.attended_results) == 1, "must not duplicate the result record"


def test_submit_poll_then_step_no_double_charge(hermetic, monkeypatch, tmp_path):
    ctx = _seed(hermetic, monkeypatch)
    judge_key = _drive_to_await_smoke(ctx, monkeypatch)
    _write_job_result(ctx, status="pass")

    # A same-call_key submit acts as a poll and reaches its own terminal
    # transition through `submit_host_result` (not the `step` poll branch).
    r = _write_result(tmp_path, "r.json", _JUDGE_PASS)
    rc, body = _submit(ctx, judge_key, r)
    assert rc == 0, body
    assert body["status"] == "complete", body
    spent_after_submit = _fresh_state(ctx).budget.spent_usd
    assert spent_after_submit > 0.0

    rc2, body2 = _step(ctx)
    assert rc2 == 0, body2
    assert body2["status"] == "ready_to_finalize", body2

    state = _fresh_state(ctx)
    assert state.budget.spent_usd == spent_after_submit, "must not double-charge"
    assert len(state.attended_results) == 1


# =========================================================================== #
# 4. surfaced smoke (fail) via step poll -> charged once, task surfaced (in
#    attended_completed_task_ids so it is never re-picked) but NOT done.
# =========================================================================== #

def test_step_poll_surfaced_smoke_charged_once_not_done(hermetic, monkeypatch):
    ctx = _seed(hermetic, monkeypatch)
    _drive_to_await_smoke(ctx, monkeypatch)
    _write_job_result(ctx, status="fail", reason="tests failed")

    rc, body = _step(ctx)
    assert rc == 0, body
    assert body["status"] == "surfaced", body

    state = _fresh_state(ctx)
    assert state.budget.spent_usd > 0.0
    assert ctx["task_id"] in state.attended_completed_task_ids, (
        "a surfaced task must still be excluded from re-selection"
    )
    assert ctx["task_id"] not in state.attended_done_task_ids, (
        "a surfaced (non-complete) outcome must not enter attended_done_task_ids"
    )
    assert state.open_pp_runs == []


# =========================================================================== #
# 5. self-heal: a checkpoint left terminal-but-unreconciled (pre-fix, or a
#    crash between poll and bookkeeping) is repaired before task selection,
#    exactly once.
# =========================================================================== #

def test_self_heal_repairs_prefix_terminal_checkpoint_idempotently(hermetic, monkeypatch):
    ctx = _seed(hermetic, monkeypatch)
    judge_key = _drive_to_await_smoke(ctx, monkeypatch)
    _write_job_result(ctx, status="pass")

    # Simulate the PRE-FIX bug directly: drive the cursor terminal via the
    # raw host_bridge poll (bypassing both `submit_host_result`'s stamp and
    # the CLI's own reconciliation call) so the cursor ends up terminal with
    # NO `terminal_call_key` and the checkpoint completely unreconciled --
    # exactly the shape found live on workflow 0710b823.
    cursor = host_bridge.load_cursor(ctx["cursor_path"])
    assert cursor["state"] == "await_smoke"
    still_pending = host_bridge.poll_smoke_job(
        ctx["disp"], cursor, cursor_file=ctx["cursor_path"])
    assert still_pending is False
    # Emulate a pre-fix cursor by erasing the stamp poll_smoke_job just left.
    cursor.pop("terminal_call_key", None)
    host_bridge.save_cursor(ctx["cursor_path"], cursor)
    assert cursor["state"] == "complete"

    state_before = _fresh_state(ctx)
    assert state_before.budget.spent_usd == 0.0
    assert state_before.open_pp_runs, "run must still be open pre-heal"

    rc, body = _step(ctx)
    assert rc == 0, body
    # No await_smoke cursor remains (already terminal), so the FIRST thing
    # step can do for this workflow is heal, then report ready_to_finalize.
    assert body["status"] == "ready_to_finalize", body

    state = _fresh_state(ctx)
    assert state.budget.spent_usd > 0.0, "self-heal must charge the budget"
    assert ctx["task_id"] in state.attended_completed_task_ids
    assert ctx["task_id"] in state.attended_done_task_ids
    assert len(state.attended_results) == 1
    assert state.open_pp_runs == []
    assert any(v is True for v in state.attended_checkpoint_reconciled.values())

    healed_cursor = host_bridge.load_cursor(ctx["cursor_path"])
    assert healed_cursor.get("terminal_call_key") == judge_key, (
        "self-heal must derive the identity from verdict_recorded_for/"
        "smoke_result_for on a pre-fix cursor"
    )

    # Idempotent: a second step must not re-charge or duplicate the result.
    spent_after_heal = state.budget.spent_usd
    rc2, body2 = _step(ctx)
    assert rc2 == 0 and body2["status"] == "ready_to_finalize", body2
    state2 = _fresh_state(ctx)
    assert state2.budget.spent_usd == spent_after_heal
    assert len(state2.attended_results) == 1


# =========================================================================== #
# 6. a checkpoint-persist failure in the step poll path surfaces ok:false,
#    never a silent happy-path response.
# =========================================================================== #

def test_step_poll_persist_failure_returns_ok_false(hermetic, monkeypatch):
    ctx = _seed(hermetic, monkeypatch)
    _drive_to_await_smoke(ctx, monkeypatch)
    _write_job_result(ctx, status="pass")

    real_update_state = ctx["sup"].update_state

    def _flaky_update_state(config, patch, as_node=None):
        if "budget" in patch:
            raise RuntimeError("simulated checkpoint persistence failure")
        return real_update_state(config, patch, as_node=as_node)

    monkeypatch.setattr(
        "hydra_core.supervisor.build_supervisor",
        lambda **k: _FlakySupProxy(ctx["sup"], _flaky_update_state),
    )

    rc, body = _step(ctx)
    assert rc == 1, body
    assert body.get("error") == "checkpoint_persist_failed", body
    assert body.get("checkpoint_persist_errors"), body


class _FlakySupProxy:
    """Delegates to a real compiled supervisor but substitutes update_state."""

    def __init__(self, real_sup, update_state_fn):
        self._real = real_sup
        self._update_state_fn = update_state_fn

    def get_state(self, config):
        return self._real.get_state(config)

    def update_state(self, config, patch, as_node=None):
        return self._update_state_fn(config, patch, as_node=as_node)

    def invoke(self, *a, **k):  # pragma: no cover -- never reached here
        return self._real.invoke(*a, **k)


# =========================================================================== #
# 7-9. round-2 critique fix: a workflow that has gone terminal (operator
#      abort/reject) must still reach the SAME poll/self-heal reconciliation
#      before returning `status: "workflow_terminal"` -- the previous early
#      return skipped it entirely for any `open_pp_runs` cursor left
#      `await_smoke` or terminal-but-unreconciled when the workflow went
#      terminal.
# =========================================================================== #

def _set_terminal_resolution(ctx, *, action="abort", option="abort"):
    """Durably mark the workflow terminal (operator abort), exactly the shape
    `hydra_core.state.workflow_terminal_resolution` reads -- without
    disturbing `tasks`/`open_pp_runs`, which the fixture already seeded."""
    ctx["sup"].update_state(ctx["config"], {
        "terminal_resolution": {
            "gate_node": "approval", "hitl_request_id": None,
            "action": action, "option": option, "plan_revision": None,
            "resolved_at": "2026-09-24T00:00:00+00:00",
        },
    })


def test_workflow_terminal_with_await_smoke_reconciles_no_merge_no_start_run(
    hermetic, monkeypatch,
):
    ctx = _seed(hermetic, monkeypatch)
    judge_key = _drive_to_await_smoke(ctx, monkeypatch)
    _write_job_result(ctx, status="pass")
    _set_terminal_resolution(ctx)

    head_before = _git(["rev-parse", "HEAD"], ctx["repo"]).stdout.strip()
    start_run_calls_before = ctx["disp"].count("start_run")

    rc, body = _step(ctx)
    assert rc == 0, body
    assert body["status"] == "workflow_terminal", body
    assert body["terminal_resolution"]["option"] == "abort", body
    assert body.get("reconciled") == [
        {"run_id": ctx["run_id"], "status": "surfaced"}
    ], body

    # Bookkeeping landed exactly once even though the workflow is terminal --
    # this is the ALREADY-INCURRED spend for the judge-pass + smoke-pass
    # attempt, not a forward-looking continuation.
    state = _fresh_state(ctx)
    assert state.budget.spent_usd > 0.0, "budget must have been charged"
    assert ctx["task_id"] in state.attended_completed_task_ids
    assert len(state.attended_results) == 1
    assert state.open_pp_runs == [], "the finished run must be removed"
    recon_key = f"{ctx['run_id']}:{judge_key}"
    assert state.attended_checkpoint_reconciled.get(recon_key) is True
    assert recon_key in state.attended_charge_applied

    # The forward-looking side effect -- merging the candidate worktree back
    # into the target repo -- must be refused: the branch is preserved, the
    # repo's own HEAD never moves, and no new pp run/task is dispatched.
    head_after = _git(["rev-parse", "HEAD"], ctx["repo"]).stdout.strip()
    assert head_after == head_before, "a terminal workflow must never merge code"
    cursor = host_bridge.load_cursor(ctx["cursor_path"])
    assert (cursor.get("merge") or {}).get("error") == "workflow_terminal", cursor
    assert ctx["disp"].count("start_run") == start_run_calls_before, (
        "a terminal workflow must never dispatch a new task/pp run"
    )

    # Idempotent: a second step against the still-terminal workflow must not
    # re-charge or duplicate the result.
    spent_after = state.budget.spent_usd
    rc2, body2 = _step(ctx)
    assert rc2 == 0 and body2["status"] == "workflow_terminal", body2
    assert body2.get("reconciled") == [], (
        "an already-reconciled/removed run has nothing left to poll or heal"
    )
    state2 = _fresh_state(ctx)
    assert state2.budget.spent_usd == spent_after
    assert len(state2.attended_results) == 1


def test_workflow_terminal_self_heals_unreconciled_cursor_idempotently(
    hermetic, monkeypatch,
):
    ctx = _seed(hermetic, monkeypatch)
    judge_key = _drive_to_await_smoke(ctx, monkeypatch)
    _write_job_result(ctx, status="pass")

    # Drive the cursor terminal via the raw host_bridge poll (bypassing both
    # `submit_host_result`'s stamp and the CLI's own reconciliation) so it
    # ends up terminal-but-unreconciled, then mark the workflow terminal --
    # reproduces a workflow that was aborted while a pre-fix (or crashed)
    # cursor sat unreconciled in `open_pp_runs`.
    cursor = host_bridge.load_cursor(ctx["cursor_path"])
    still_pending = host_bridge.poll_smoke_job(
        ctx["disp"], cursor, cursor_file=ctx["cursor_path"])
    assert still_pending is False
    cursor.pop("terminal_call_key", None)
    host_bridge.save_cursor(ctx["cursor_path"], cursor)
    assert cursor["state"] == "complete"
    _set_terminal_resolution(ctx)

    state_before = _fresh_state(ctx)
    assert state_before.budget.spent_usd == 0.0
    assert state_before.open_pp_runs, "run must still be open pre-heal"

    rc, body = _step(ctx)
    assert rc == 0, body
    assert body["status"] == "workflow_terminal", body
    assert len(body.get("reconciled") or []) == 1, body

    state = _fresh_state(ctx)
    assert state.budget.spent_usd > 0.0, "self-heal must charge even on a terminal workflow"
    assert ctx["task_id"] in state.attended_completed_task_ids
    assert len(state.attended_results) == 1
    assert state.open_pp_runs == []
    assert any(v is True for v in state.attended_checkpoint_reconciled.values())

    healed_cursor = host_bridge.load_cursor(ctx["cursor_path"])
    assert healed_cursor.get("terminal_call_key") == judge_key

    spent_after_heal = state.budget.spent_usd
    rc2, body2 = _step(ctx)
    assert rc2 == 0 and body2["status"] == "workflow_terminal", body2
    assert body2.get("reconciled") == []
    state2 = _fresh_state(ctx)
    assert state2.budget.spent_usd == spent_after_heal
    assert len(state2.attended_results) == 1


def test_workflow_terminal_persist_failure_returns_ok_false(hermetic, monkeypatch):
    ctx = _seed(hermetic, monkeypatch)
    _drive_to_await_smoke(ctx, monkeypatch)
    _write_job_result(ctx, status="pass")
    _set_terminal_resolution(ctx)

    real_update_state = ctx["sup"].update_state

    def _flaky_update_state(config, patch, as_node=None):
        if "budget" in patch:
            raise RuntimeError("simulated checkpoint persistence failure")
        return real_update_state(config, patch, as_node=as_node)

    monkeypatch.setattr(
        "hydra_core.supervisor.build_supervisor",
        lambda **k: _FlakySupProxy(ctx["sup"], _flaky_update_state),
    )

    rc, body = _step(ctx)
    assert rc == 1, body
    assert body.get("status") == "workflow_terminal", body
    assert body.get("error") == "checkpoint_persist_failed", body
    assert body.get("checkpoint_persist_errors"), body
