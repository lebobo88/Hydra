"""MU micro-usage audit regression tests (MU1, MU3, MU7, MU12). See docs/audits/MU-MICRO-USAGE-2026-07-05.md."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

langgraph = pytest.importorskip("langgraph")

from hydra_core import cli  # noqa: E402
from hydra_core.state import HydraState  # noqa: E402
from hydra_core.supervisor import build_supervisor  # noqa: E402
from mcp_servers.hydra_memory.server import _tool_handlers as mem_handlers  # noqa: E402


class _NullDispatcher:
    def dispatch(self, *a, **k):  # pragma: no cover
        return None

    def call_tool(self, *a, **k):  # pragma: no cover
        return None


def _start_paused_workflow(tmp_path, monkeypatch) -> str:
    """Run a workflow that pauses at the approval gate; returns workflow_id.

    Uses the executive squad so the planner sets requires_human_approval and
    the graph interrupts before the `approval` node with a real pending_hitl.
    """
    monkeypatch.setenv("HYDRA_CHECKPOINT_DB", str(tmp_path / "checkpoints.db"))
    wf = uuid4()
    initial = HydraState(workflow_id=wf, root_goal="MU7 test goal: bare interrupt resume")
    initial.selected_squads = ["executive"]
    sup = build_supervisor(project_root=REPO_ROOT, dispatcher=_NullDispatcher())
    sup.invoke(initial, config={"configurable": {"thread_id": str(wf)}})
    return str(wf)


# ---------------------------------------------------------------------------
# MU7 tests
# ---------------------------------------------------------------------------

def test_mu7_approve_continues_bare_synthesis_interrupt(tmp_path, monkeypatch, capsys):
    """MU7: approve on a bare synthesis interrupt (pending_hitl=None, snap.next
    non-empty) must continue the graph, setting resumed=True and
    continued_bare_interrupt=True in the JSON output."""
    wf = _start_paused_workflow(tmp_path, monkeypatch)

    # Advance past the real approval gate to land at the bare synthesis interrupt.
    rc1 = cli.main(["--project", str(REPO_ROOT), "resume", wf, "--action", "approve"])
    capsys.readouterr()
    assert rc1 == 0

    # Verify we are now at the synthesis bare interrupt with no real gate filed.
    h = mem_handlers()
    status = h["hydra-mem.workflow_status"]({"workflow_id": wf})
    assert not status.get("pending_hitl"), (
        "pending_hitl must be None at the bare synthesis interrupt (MU7 precondition)"
    )

    # MU7 core: approve on the bare interrupt must continue the graph.
    rc2 = cli.main(["--project", str(REPO_ROOT), "resume", wf, "--action", "approve"])
    out = json.loads(capsys.readouterr().out)
    assert rc2 == 0, f"expected exit 0, got {rc2}: {out}"
    assert out["resumed"] is True, "MU7: resumed must be True for bare-interrupt approve"
    assert out["continued_bare_interrupt"] is True, (
        "MU7: continued_bare_interrupt must be True"
    )
    # Phase must have advanced past the synthesis pause.
    assert out.get("phase") != "synthesis", (
        f"MU7: phase must advance past 'synthesis', got {out.get('phase')!r}"
    )


def test_mu7_reject_bare_interrupt_parks_surfaced(tmp_path, monkeypatch, capsys):
    """MU7: reject on a bare synthesis interrupt parks the workflow at phase=surfaced
    without continuing the graph."""
    wf = _start_paused_workflow(tmp_path, monkeypatch)

    # Advance past the real approval gate to land at the bare synthesis interrupt.
    rc1 = cli.main(["--project", str(REPO_ROOT), "resume", wf, "--action", "approve"])
    capsys.readouterr()
    assert rc1 == 0

    # Confirm bare interrupt is active.
    h = mem_handlers()
    assert not h["hydra-mem.workflow_status"]({"workflow_id": wf}).get("pending_hitl")

    # MU7: reject on the bare interrupt must park the workflow as surfaced.
    rc2 = cli.main(["--project", str(REPO_ROOT), "resume", wf, "--action", "reject"])
    out = json.loads(capsys.readouterr().out)
    assert rc2 == 0, f"expected exit 0, got {rc2}: {out}"
    assert out["resumed"] is False, "MU7: resumed must be False for bare-interrupt reject"
    assert out.get("phase") == "surfaced", (
        f"MU7: phase must be 'surfaced' after bare-interrupt reject, got {out.get('phase')!r}"
    )
    assert out.get("continued_bare_interrupt") is False

    # Checkpoint must reflect the surfaced state.
    status = h["hydra-mem.workflow_status"]({"workflow_id": wf})
    assert status["phase"] == "surfaced", (
        f"checkpoint phase must be 'surfaced', got {status['phase']!r}"
    )


def test_mu7_terminal_still_no_pending_gate(tmp_path, monkeypatch, capsys):
    """MU7: a fully-completed workflow (snap.next empty) still returns
    reason='no_pending_gate' — the frozen contract is preserved."""
    wf = _start_paused_workflow(tmp_path, monkeypatch)

    # Drive through all interrupt points: approval gate, bare synthesis
    # interrupt, and bare judge_synthesis interrupt.  After three approves the
    # graph reaches END (snap.next becomes empty).
    for _ in range(3):
        rc = cli.main(["--project", str(REPO_ROOT), "resume", wf, "--action", "approve"])
        capsys.readouterr()
        assert rc == 0

    # Graph has completed.  A further approve must hit the frozen no_pending_gate
    # path — reason value is the contract that tests pin on (see MU7 spec).
    rc_final = cli.main(["--project", str(REPO_ROOT), "resume", wf, "--action", "approve"])
    out = json.loads(capsys.readouterr().out)
    assert rc_final == 0, f"expected exit 0, got {rc_final}: {out}"
    assert out["resumed"] is False
    assert out["reason"] == "no_pending_gate", (
        f"MU7: terminal workflow must return reason='no_pending_gate', got {out.get('reason')!r}"
    )


# ---------------------------------------------------------------------------
# MU12 tests — preserve judge-passed work on non-complete attended finalize
# ---------------------------------------------------------------------------
# Verify that when an attended engineering stage finalizes non-complete (e.g.
# smoke-fail, judge-fail) the engineer's uncommitted changes are committed to
# the attended branch BEFORE the worktree is removed, so work is never silently
# destroyed and the operator can pick it up from the branch.

from hydra_core import host_bridge as _hb  # noqa: E402


def _git_mu12(args, cwd):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                          text=True, check=False)


def _init_repo_mu12(path):
    _git_mu12(["init"], path)
    _git_mu12(["config", "user.email", "t@t.test"], path)
    _git_mu12(["config", "user.name", "Test"], path)
    _git_mu12(["config", "commit.gpgsign", "false"], path)
    (path / "README.md").write_text("base\n", encoding="utf-8")
    _git_mu12(["add", "-A"], path)
    _git_mu12(["commit", "-m", "base", "--no-verify"], path)


class _FakeDispatcherMU12:
    """FakeDispatcher variant for MU12 tests — smoke can be injected."""

    def __init__(self, *, required_cross_vendor=True, can_pass=True,
                 finalize_status="complete", downgraded=False):
        self.calls: list[tuple] = []
        self._required_cross = required_cross_vendor
        self._can_pass = can_pass
        self._finalize_status = finalize_status
        self._downgraded = downgraded

    def call_mcp(self, server, tool, args, squad_id=None):
        self.calls.append((server, tool, dict(args), squad_id))
        if tool == "start_stage":
            return {"status": "done", "result": {"stage_id": "stage-mu12"}}
        if tool == "record_attempt":
            return {"status": "done", "result": {"attempt_id": "att-mu12"}}
        if tool == "gate_eligible_judges":
            return {"status": "done", "result": {
                "required_cross_vendor": self._required_cross,
                "rubric_id": "rfc-2119-normative"}}
        if tool == "get_stage_finalize_readiness":
            return {"status": "done", "result": {"can_pass": self._can_pass}}
        if tool == "finalize_run":
            return {"status": "done", "result": {
                "effective_status": self._finalize_status,
                "downgraded": self._downgraded}}
        return {"status": "done", "result": {}}


def test_mu12_non_complete_finalize_preserves_work(tmp_path, monkeypatch):
    """MU12(a): when a stage finalizes non-complete (smoke-fail) with an
    engineer-created file in the worktree, the attended branch must contain
    that file afterward and the result/cursor must carry preserved_branch."""
    _init_repo_mu12(tmp_path)

    # Force smoke to fail so the judge-pass path surfaces the run.
    monkeypatch.setattr(_hb, "_run_smoke",
                        lambda *a, **k: ("fail", "MU12 injected smoke failure"))

    disp = _FakeDispatcherMU12(required_cross_vendor=True)
    res = _hb.begin_stage(
        disp, workflow_id="wf-mu12", run_id="run-mu12",
        project_path=str(tmp_path), request_text="add mu12_feature.py",
        project_root=str(tmp_path), isolate=True)
    assert res["status"] == "awaiting_host"

    wt = res["host_action"]["cwd"]
    assert "worktrees" in wt.replace("\\", "/"), "engineer must be in a worktree"

    # Simulate the engineer creating a file in the worktree.
    (Path(wt) / "mu12_feature.py").write_text("# mu12 feature\n", encoding="utf-8")

    cfile = res["cursor_path"]

    # Submit engineer result.
    res = _hb.submit_host_result(
        disp, cursor_file=cfile, call_key="generate-0",
        result={"text": "added mu12_feature.py", "cost_usd": 0.01,
                "tokens_in": 10, "tokens_out": 5, "model": "claude-test"})
    assert res["state"] == "await_judge"

    # Submit judge pass — smoke will fail → stage surfaces.
    res = _hb.submit_host_result(
        disp, cursor_file=cfile, call_key="judge-0",
        result={"outcome": "pass", "critique_md": "looks good",
                "judge_producer": "codex", "cost_usd": 0.005})
    assert res["status"] == "surfaced", (
        f"MU12: expected surfaced (smoke-fail path), got {res['status']!r}")
    assert res["final_status"] == "surfaced"

    # The worktree must be gone (cleaned up as usual).
    assert not Path(wt).exists(), "worktree must be removed after finalize"

    # The attended branch must contain the engineer's file.
    preserved = res.get("preserved_branch")
    assert preserved, (
        f"MU12: preserved_branch must be set in result on non-complete finalize, got {res!r}")

    # Verify the file is on the branch via git show.
    show = _git_mu12(["show", f"{preserved}:mu12_feature.py"], tmp_path)
    assert show.returncode == 0, (
        f"MU12: git show {preserved}:mu12_feature.py failed — work was not preserved.\n"
        f"stderr: {show.stderr}\nstdout: {show.stdout}")
    assert "mu12 feature" in show.stdout

    # Cursor must also carry preserved_branch.
    cursor = _hb.load_cursor(cfile)
    assert cursor.get("preserved_branch") == preserved, (
        "MU12: cursor preserved_branch must match result preserved_branch")


def test_mu12_complete_path_merge_unchanged(tmp_path, monkeypatch):
    """MU12(b): the complete path (passing judge + passing smoke) still merges
    the engineer's work into the base branch unchanged — the preserve logic must
    not interfere with it."""
    _init_repo_mu12(tmp_path)

    # Force smoke to pass.
    monkeypatch.setattr(_hb, "_run_smoke",
                        lambda *a, **k: ("pass", "MU12 injected smoke pass"))

    disp = _FakeDispatcherMU12(required_cross_vendor=True)
    res = _hb.begin_stage(
        disp, workflow_id="wf-mu12b", run_id="run-mu12b",
        project_path=str(tmp_path), request_text="add mu12b_feature.py",
        project_root=str(tmp_path), isolate=True)
    wt = res["host_action"]["cwd"]

    # Engineer writes a file.
    (Path(wt) / "mu12b_feature.py").write_text("# mu12b\n", encoding="utf-8")

    cfile = res["cursor_path"]
    res = _hb.submit_host_result(
        disp, cursor_file=cfile, call_key="generate-0",
        result={"text": "added mu12b_feature.py", "cost_usd": 0.01,
                "tokens_in": 10, "tokens_out": 5, "model": "claude-test"})
    assert res["state"] == "await_judge"

    res = _hb.submit_host_result(
        disp, cursor_file=cfile, call_key="judge-0",
        result={"outcome": "pass", "critique_md": "lgtm",
                "judge_producer": "codex", "cost_usd": 0.005})

    assert res["status"] == "complete", (
        f"MU12b: complete path must still succeed, got {res['status']!r}: {res!r}")
    assert res["final_status"] == "complete"
    assert res["merge"]["merged"] is True, "MU12b: merge must succeed on complete path"

    # The file landed in the repo working tree (merge succeeded).
    assert (tmp_path / "mu12b_feature.py").exists(), (
        "MU12b: engineer's file must land in the repo on complete path")

    # No preserved_branch on a complete run.
    assert "preserved_branch" not in res, (
        "MU12b: preserved_branch must NOT appear in complete-path result")


def test_mu12_pass_unlanded_preserves_branch(tmp_path, monkeypatch):
    """MU12(c): judge-pass + smoke-pass but merge failure → final_status==surfaced
    (pass_unlanded outcome) AND preserved_branch is set in result/cursor AND
    git show <branch>:<file> succeeds (work committed by _merge_worktree_back).

    The merge_worktree_back helper commits before attempting the merge, so the
    work lives on the attended branch even when the merge itself fails.
    We monkeypatch _merge_worktree_back to return a failed merge dict while
    still having committed the engineer's file to the branch via the real helper.
    """
    _init_repo_mu12(tmp_path)

    # Force smoke to pass.
    monkeypatch.setattr(_hb, "_run_smoke",
                        lambda *a, **k: ("pass", "MU12c injected smoke pass"))

    disp = _FakeDispatcherMU12(required_cross_vendor=True)
    res = _hb.begin_stage(
        disp, workflow_id="wf-mu12c", run_id="run-mu12c",
        project_path=str(tmp_path), request_text="add mu12c_feature.py",
        project_root=str(tmp_path), isolate=True)
    assert res["status"] == "awaiting_host"

    wt = res["host_action"]["cwd"]
    assert "worktrees" in wt.replace("\\", "/")

    # Engineer creates the feature file inside the worktree.
    (Path(wt) / "mu12c_feature.py").write_text("# mu12c\n", encoding="utf-8")

    cfile = res["cursor_path"]

    # Monkeypatch _merge_worktree_back to: (1) still commit the worktree changes
    # onto the branch (so git show works), but (2) report a failed merge so the
    # pass_unlanded downgrade fires.  We do the real commit part ourselves, then
    # return the failed-merge dict.
    def _fake_merge(repo_root, worktree_path, branch):
        # Commit the engineer's work to the branch (mirrors what the real helper
        # does before attempting the merge), then lie about the merge result.
        _git_mu12(["add", "-A"], worktree_path)
        _git_mu12(["-c", "user.name=hydra-attended",
                   "-c", "user.email=hydra-attended@local",
                   "commit", "-m", "mu12c test commit", "--no-verify"], worktree_path)
        return {"merged": False, "sha": None, "error": "MU12c injected merge failure"}

    monkeypatch.setattr(_hb, "_merge_worktree_back", _fake_merge)

    res = _hb.submit_host_result(
        disp, cursor_file=cfile, call_key="generate-0",
        result={"text": "added mu12c_feature.py", "cost_usd": 0.01,
                "tokens_in": 10, "tokens_out": 5, "model": "claude-test"})
    assert res["state"] == "await_judge"

    res = _hb.submit_host_result(
        disp, cursor_file=cfile, call_key="judge-0",
        result={"outcome": "pass", "critique_md": "lgtm",
                "judge_producer": "codex", "cost_usd": 0.005})

    # Merge failure downgrades to surfaced (pass_unlanded outcome).
    assert res["status"] == "surfaced", (
        f"MU12c: expected surfaced (pass_unlanded), got {res['status']!r}: {res!r}")
    assert res["final_status"] == "surfaced"

    # The worktree is gone.
    assert not Path(wt).exists(), "worktree must be removed after finalize"

    # preserved_branch must be in the result and cursor.
    preserved = res.get("preserved_branch")
    assert preserved, (
        f"MU12c: preserved_branch must be set in result on pass_unlanded, got {res!r}")

    cursor = _hb.load_cursor(cfile)
    assert cursor.get("preserved_branch") == preserved, (
        "MU12c: cursor preserved_branch must match result preserved_branch")

    # The engineer's file must be reachable from the attended branch.
    show = _git_mu12(["show", f"{preserved}:mu12c_feature.py"], tmp_path)
    assert show.returncode == 0, (
        f"MU12c: git show {preserved}:mu12c_feature.py failed — work not preserved.\n"
        f"stderr: {show.stderr}\nstdout: {show.stdout}")
    assert "mu12c" in show.stdout


# ---------------------------------------------------------------------------
# MU15 — reflect attended task completions into the graph task ledger
# ---------------------------------------------------------------------------
# The tasks channel uses an _append reducer so task.status cannot be updated
# in-place via update_state (it would duplicate the task).  The fix uses
# attended_done_task_ids (replace-by-default) as the authoritative "host
# completed this task successfully" signal.  enforce_governance skips the
# deferred_to_host and surfaced checks for tasks in that set; only 'complete'
# cursors enter attended_done_task_ids so surfaced/aborted attended outcomes
# still cause governance to surface the workflow.
#
# Tests (a) and (b) unit-test enforce_governance directly (the "sync" path
# being tested is the attended_done_task_ids read in governance.py — adding
# to that set in tests mirrors exactly what _cmd_attended_submit does via
# sup.update_state).  Test (c) is end-to-end: plan → attended_done sync via
# sup.update_state → 3 bare-interrupt resumes → assert phase != "surfaced".

from hydra_core.governance import enforce_governance as _gov  # noqa: E402
from hydra_core.state import TaskState as _TaskState  # noqa: E402


def test_mu15_attended_complete_marks_task_done(tmp_path, monkeypatch):
    """MU15(a): when the attended host finalises a task as 'complete', adding
    its task_id to attended_done_task_ids causes enforce_governance to treat the
    deferred_to_host task as done and return surfaced=False.

    The tasks channel uses _append reducer so task.status stays deferred_to_host
    in the checkpoint; attended_done_task_ids is the authoritative override.
    """
    task = _TaskState(
        owner_squad="engineering",
        description="MU15a attended complete",
        status="deferred_to_host",
    )
    state = HydraState(
        root_goal="mu15a test",
        tasks=[task],
        attended_done_task_ids=[str(task.task_id)],
    )
    verdict = _gov(state, packs={})
    assert not verdict.surfaced, (
        f"MU15(a): task in attended_done_task_ids must not surface governance; "
        f"got reason={verdict.reason!r}"
    )


def test_mu15_attended_surfaced_marks_task_surfaced(tmp_path, monkeypatch):
    """MU15(b): when the attended host finalises a task as 'surfaced' or
    'aborted', the task_id is NOT in attended_done_task_ids, so governance still
    surfaces the workflow.

    This verifies that only 'complete' cursors open the governance gate — a
    smoke-fail or judge-fail attended outcome must not let the workflow sneak
    through as 'done'.
    """
    task = _TaskState(
        owner_squad="engineering",
        description="MU15b attended surfaced",
        status="deferred_to_host",
    )
    state = HydraState(
        root_goal="mu15b test",
        tasks=[task],
        attended_done_task_ids=[],   # surfaced/aborted → not in done list
    )
    verdict = _gov(state, packs={})
    assert verdict.surfaced, (
        f"MU15(b): deferred task NOT in attended_done_task_ids must surface; "
        f"got surfaced=False reason={verdict.reason!r}"
    )
    assert "deferred" in verdict.reason.lower(), (
        f"MU15(b): surface reason must mention 'deferred'; got {verdict.reason!r}"
    )


def test_mu15_completed_workflow_resumes_to_non_surfaced(tmp_path, monkeypatch, capsys):
    """MU15(c): end-to-end — after an attended engineering stage completes
    (task_id added to attended_done_task_ids in the checkpoint via sup.update_state,
    mirroring what _cmd_attended_submit does), resuming through dispatch +
    synthesis + judge_synthesis reaches phase='done', not 'surfaced'.

    Flow:
      1. plan with engineering squad → halts before dispatch
      2. sup.update_state adds attended_done_task_ids for the engineering task
      3. 3x resume (dispatch runs→task surfaced by NullDispatcher→synthesis pause
         →judge_synthesis pause→postcheck): governance skips the surfaced task
         because it is in attended_done_task_ids → phase='done'

    Note: the CLI-driven step/submit-host-result path requires a live pp_harness
    MCP process; this test exercises the sync directly via sup.update_state (the
    exact same LangGraph call _cmd_attended_submit makes) so the governance fix
    is covered without spawning MCP servers.
    """
    monkeypatch.setenv("HYDRA_CHECKPOINT_DB", str(tmp_path / "checkpoints.db"))
    wf = uuid4()

    # 1. Build the plan: intake → planner → halt before dispatch.
    initial = HydraState(workflow_id=wf, root_goal="MU15 e2e test: attended complete")
    initial.selected_squads = ["engineering"]
    sup_plan = build_supervisor(
        project_root=REPO_ROOT, dispatcher=_NullDispatcher(), plan_only=True
    )
    config = {"configurable": {"thread_id": str(wf)}}
    sup_plan.invoke(initial, config=config)

    # 2. Read the task_id the planner synthesised.
    snap = sup_plan.get_state(config)
    assert snap is not None and snap.values, "MU15(c): plan must produce a checkpoint"
    state_after_plan = HydraState.model_validate(snap.values)
    assert state_after_plan.tasks, "MU15(c): planner must have created at least one task"
    task_id = str(state_after_plan.tasks[0].task_id)

    # 3. Simulate attended complete: write attended_done_task_ids to the checkpoint.
    #    (_cmd_attended_submit does exactly this call on a 'complete' cursor.)
    sup_plan.update_state(config, {
        "attended_completed_task_ids": [task_id],
        "attended_done_task_ids": [task_id],
    })

    # Verify the update landed.
    snap2 = sup_plan.get_state(config)
    state2 = HydraState.model_validate(snap2.values)
    assert task_id in state2.attended_done_task_ids, (
        f"MU15(c): attended_done_task_ids must contain {task_id!r} after update_state"
    )

    # 4. Resume 3 times:
    #    - resume 1: dispatch (engineering→surfaced via NullDispatcher) → synthesis pause
    #    - resume 2: synthesis → judge_synthesis pause
    #    - resume 3: judge_synthesis → postcheck (governance fix) → done / END
    for i in range(3):
        rc = cli.main(
            ["--project", str(REPO_ROOT), "resume", str(wf), "--action", "approve"]
        )
        out = json.loads(capsys.readouterr().out)
        assert rc == 0, f"MU15(c): resume {i + 1} exited {rc}: {out}"

    # 5. Assert final phase is not surfaced.
    h = mem_handlers()
    status = h["hydra-mem.workflow_status"]({"workflow_id": str(wf)})
    assert status["phase"] != "surfaced", (
        f"MU15(c): workflow with attended-complete engineering task must not surface; "
        f"got phase={status['phase']!r}"
    )


# ---------------------------------------------------------------------------
# MU6 — worktree-aware _get_base (repo_registry.py)
# ---------------------------------------------------------------------------

import types as _types  # noqa: E402  (used only in MU6 helpers below)

import hydra_core.repo_registry as _rr  # noqa: E402
from hydra_core.repo_registry import _GIT_PROBE_CACHE, _get_base  # noqa: E402


def test_mu6_worktree_probe_resolves_main_repo_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MU6: when the git probe returns a common-dir pointing at a fake main
    repo's .git, _get_base() must return the base two levels above that .git
    (i.e. tmp_path/AiApp, NOT the worktree's naive parent)."""
    # Fake layout: tmp_path/AiApp/Hydra/.git
    fake_main_git = tmp_path / "AiApp" / "Hydra" / ".git"
    fake_main_git.mkdir(parents=True)
    expected_base = tmp_path / "AiApp"

    monkeypatch.delenv("HYDRA_REPO_BASE", raising=False)

    # Replace the cache dict via monkeypatch so it is isolated and automatically
    # restored after the test — avoids polluting subsequent tests with a stale
    # tmp_path-based entry.
    monkeypatch.setattr(_rr, "_GIT_PROBE_CACHE", {})

    # Build a minimal subprocess-module replacement whose run() returns the
    # fake common-dir for --git-common-dir calls.
    def _fake_run(cmd, **kwargs):  # noqa: ANN001
        if "--git-common-dir" in cmd:
            class _R:
                returncode = 0
                stdout = str(fake_main_git) + "\n"
            return _R()
        import subprocess as _real_sp
        return _real_sp.run(cmd, **kwargs)

    monkeypatch.setattr(_rr, "subprocess", _types.SimpleNamespace(run=_fake_run))

    result = _get_base()
    assert result == expected_base, (
        f"MU6: expected worktree-resolved base {expected_base}, got {result}"
    )


def test_mu6_probe_failure_falls_back_naive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MU6: when the git probe raises an exception, _get_base() falls back to
    the naive __file__-derived base."""
    monkeypatch.delenv("HYDRA_REPO_BASE", raising=False)
    # Isolate the cache so stale entries from prior runs don't short-circuit the probe.
    monkeypatch.setattr(_rr, "_GIT_PROBE_CACHE", {})

    def _raise(*args, **kwargs):  # noqa: ANN001,ANN002,ANN003
        raise OSError("git not found (simulated)")

    monkeypatch.setattr(_rr, "subprocess", _types.SimpleNamespace(run=_raise))

    result = _get_base()
    expected = Path(_rr.__file__).resolve().parents[1].parent
    assert result == expected, (
        f"MU6: expected naive fallback {expected}, got {result}"
    )


def test_mu6_env_override_still_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MU6: HYDRA_REPO_BASE always wins, even when the probe cache is poisoned
    with a wrong value."""
    # Poison a fresh isolated cache with a wrong entry; monkeypatch restores the
    # original dict after the test so no cross-test contamination occurs.
    repo_dir_key = str(Path(_rr.__file__).resolve().parents[1])
    monkeypatch.setattr(
        _rr, "_GIT_PROBE_CACHE", {repo_dir_key: tmp_path / "wrong" / ".git"}
    )

    override = tmp_path / "override"
    monkeypatch.setenv("HYDRA_REPO_BASE", str(override))

    result = _get_base()
    assert result == override, (
        f"MU6: env override must win; expected {override}, got {result}"
    )


# ---------------------------------------------------------------------------
# MU6b — smoke failure persists full log + embeds forensics in reason
# ---------------------------------------------------------------------------

def test_mu6b_smoke_failure_writes_full_log(tmp_path, monkeypatch):
    """MU6b: _run_smoke on a failed subprocess writes the full combined output
    to .harness/smoke/<stage_id>-<ts>.log and embeds FAILED lines + the log
    path in the reason string."""
    from hydra_core import squad_node

    stdout_content = (
        "collected 7 items\n"
        "FAILED tests/test_x.py::test_a - AssertionError: expected 1 got 2\n"
        "FAILED tests/test_x.py::test_b - ValueError: bad input\n"
        "2 failed, 5 passed\n"
    )

    class _Res:
        returncode = 1
        stdout = stdout_content
        stderr = ""

    monkeypatch.setattr(squad_node, "_detect_smoke_command", lambda _p: ["pytest", "-q"])
    monkeypatch.setattr(squad_node.subprocess, "run", lambda *_a, **_k: _Res())

    status, reason = squad_node._run_smoke(
        None, project_path=str(tmp_path), stage_id="stage-mu6b-test"
    )

    assert status == "fail", f"MU6b: expected 'fail', got {status!r}"
    assert "FAILED tests/test_x.py::test_a" in reason, (
        f"MU6b: reason must contain FAILED line; got {reason!r}"
    )
    assert "full_log=" in reason, (
        f"MU6b: reason must contain full_log= artifact path; got {reason!r}"
    )

    # Verify the .harness/smoke/ dir was created and the log contains full output.
    smoke_dir = tmp_path / ".harness" / "smoke"
    assert smoke_dir.exists(), "MU6b: .harness/smoke/ must be created on failure"
    log_files = list(smoke_dir.glob("stage-mu6b-test-*.log"))
    assert len(log_files) == 1, f"MU6b: expected 1 log file, got {log_files}"
    log_content = log_files[0].read_text(encoding="utf-8")
    assert stdout_content in log_content, (
        "MU6b: log file must contain the full stdout content"
    )

    # The path embedded in the reason must point to the same existing file.
    log_path = Path(reason.split("full_log=", 1)[1])
    assert log_path.exists(), (
        f"MU6b: full_log= path in reason must exist; got {log_path!r}"
    )


def test_mu6b_smoke_pass_writes_no_log(tmp_path, monkeypatch):
    """MU6b: a passing smoke run must NOT create the .harness/smoke/ directory."""
    from hydra_core import squad_node

    class _Res:
        returncode = 0
        stdout = "5 passed\n"
        stderr = ""

    monkeypatch.setattr(squad_node, "_detect_smoke_command", lambda _p: ["pytest", "-q"])
    monkeypatch.setattr(squad_node.subprocess, "run", lambda *_a, **_k: _Res())

    status, reason = squad_node._run_smoke(
        None, project_path=str(tmp_path), stage_id="stage-mu6b-pass"
    )

    assert status == "pass", f"MU6b: expected 'pass', got {status!r}"
    smoke_dir = tmp_path / ".harness" / "smoke"
    assert not smoke_dir.exists() or not any(smoke_dir.iterdir()), (
        "MU6b: .harness/smoke/ must be absent/empty on a passing smoke"
    )


# ---------------------------------------------------------------------------
# MU8b — _run_cli_json TimeoutExpired surfaces partial_stdout / partial_stderr
# ---------------------------------------------------------------------------

def test_mu8b_timeout_includes_partial_output(monkeypatch):
    """MU8b: when _run_cli_json's subprocess.run raises TimeoutExpired the
    returned dict must carry partial_stdout and partial_stderr (last 1000 chars)
    so callers can diagnose a hung CLI without losing buffered context."""
    import subprocess as _sp
    from mcp_servers.hydra_control import server as _srv

    def _raise(*_a, **_k):
        raise _sp.TimeoutExpired(
            cmd=["x"], timeout=1, output="PARTIAL-OUT", stderr="PARTIAL-ERR"
        )

    monkeypatch.setattr(_srv.subprocess, "run", _raise)

    result = _srv._run_cli_json(["plan", "goal"], timeout_s=1, err_label="plan")

    assert result["error"] == "plan_timeout", (
        f"MU8b: error must be 'plan_timeout'; got {result['error']!r}"
    )
    assert result["partial_stdout"].endswith("PARTIAL-OUT"), (
        f"MU8b: partial_stdout must end with 'PARTIAL-OUT'; got {result['partial_stdout']!r}"
    )
    assert result["partial_stderr"].endswith("PARTIAL-ERR"), (
        f"MU8b: partial_stderr must end with 'PARTIAL-ERR'; got {result['partial_stderr']!r}"
    )


# ---------------------------------------------------------------------------
# MU10 — envelope.record accepts typed envelopes without reintroducing
#         payload-key shadowing (Phase 3a sequel)
# ---------------------------------------------------------------------------
# envelope_record previously built hydra_env from only the 7 canonical outer
# fields + a nested payload blob.  Type-specific REQUIRED fields could never
# reach the dict, so validate_envelope always failed on typed envelopes.
# MU10 adds _ENVELOPE_EXTRA_FIELDS to promote those fields safely.


def test_mu10_handoff_with_payload_field_validates(monkeypatch):
    """MU10(a): HANDOFF with payload_envelope_id nested inside the payload
    dict must promote it to the envelope top level and validate successfully
    (ok True)."""
    import mcp_servers.hydra_control.server as _srv
    import hydra_core.memory as _mem

    monkeypatch.setattr(_srv, "_get_attestor", lambda: None)
    monkeypatch.setattr(_mem, "append_episodic", lambda **_k: None)

    h = _srv._tool_handlers()
    result = h["hydra.envelope.record"]({
        "kind": "HANDOFF",
        "from_squad": "engineering",
        "workflow_id": str(uuid4()),
        "payload": {
            "payload_envelope_id": str(uuid4()),
            "summary": "handoff to garland",
        },
    })
    assert result.get("ok") is True, f"MU10(a): expected ok=True, got {result}"


def test_mu10_decision_record_top_level_fields(monkeypatch):
    """MU10(b): DECISION_RECORD with decision/rationale supplied at top level
    (not nested in payload) must validate successfully (ok True)."""
    import mcp_servers.hydra_control.server as _srv
    import hydra_core.memory as _mem

    monkeypatch.setattr(_srv, "_get_attestor", lambda: None)
    monkeypatch.setattr(_mem, "append_episodic", lambda **_k: None)

    h = _srv._tool_handlers()
    result = h["hydra.envelope.record"]({
        "kind": "DECISION_RECORD",
        "from_squad": "executive",
        "workflow_id": str(uuid4()),
        "decision": "go",
        "rationale": "because",
    })
    assert result.get("ok") is True, f"MU10(b): expected ok=True, got {result}"


def test_mu10_anti_shadow_preserved(monkeypatch):
    """MU10(c): payload keys 'workflow_id' and 'type' must never shadow the
    outer envelope's reserved fields even when the extra-field promotion
    mechanism is active.  The persisted envelope must carry the OUTER
    workflow_id and type==HANDOFF."""
    import mcp_servers.hydra_control.server as _srv
    import hydra_core.memory as _mem

    monkeypatch.setattr(_srv, "_get_attestor", lambda: None)

    captured: list[dict] = []

    def _capture(**kwargs):
        captured.append(kwargs)

    monkeypatch.setattr(_mem, "append_episodic", _capture)

    outer_wf = str(uuid4())
    different_wf = str(uuid4())

    h = _srv._tool_handlers()
    result = h["hydra.envelope.record"]({
        "kind": "HANDOFF",
        "from_squad": "engineering",
        "workflow_id": outer_wf,
        "payload": {
            "payload_envelope_id": str(uuid4()),
            "workflow_id": different_wf,   # must NOT overwrite the outer field
            "type": "PRD",                 # must NOT overwrite the outer field
        },
    })
    assert result.get("ok") is True, f"MU10(c): expected ok=True, got {result}"
    assert captured, "MU10(c): append_episodic must have been called"

    persisted = captured[0]["payload"]["envelope"]
    assert persisted["type"] == "HANDOFF", (
        f"MU10(c): persisted type must be HANDOFF, got {persisted['type']!r}"
    )
    assert persisted["workflow_id"] == outer_wf, (
        f"MU10(c): persisted workflow_id must be outer {outer_wf!r}, "
        f"got {persisted.get('workflow_id')!r}"
    )


def test_mu10_missing_required_still_fails_closed(monkeypatch):
    """MU10(d): DECISION_RECORD with no decision/rationale supplied anywhere
    must still fail validation and return ok=False with an error mentioning
    'validation'."""
    import mcp_servers.hydra_control.server as _srv
    import hydra_core.memory as _mem

    monkeypatch.setattr(_srv, "_get_attestor", lambda: None)
    monkeypatch.setattr(_mem, "append_episodic", lambda **_k: None)

    h = _srv._tool_handlers()
    result = h["hydra.envelope.record"]({
        "kind": "DECISION_RECORD",
        "from_squad": "executive",
        "workflow_id": str(uuid4()),
        # no decision, no rationale — validation must reject
    })
    assert result.get("ok") is False, f"MU10(d): expected ok=False, got {result}"
    assert "validation" in result.get("error", "").lower(), (
        f"MU10(d): error must mention 'validation', got {result.get('error')!r}"
    )


# ---------------------------------------------------------------------------
# MU17 — baseline-failure capture: per-HEAD cache + no double-run on timeout
# ---------------------------------------------------------------------------
# _capture_baseline_failures runs the full suite at every begin_stage; on large
# repos that costs minutes and (pre-fix) a timeout fell through to a SECOND
# full-suite run in the next candidate dir, blowing the attended step budget.
# MU17 caches completed baselines per (anchor, HEAD sha) and returns [] on
# timeout without trying further candidates.


def _mu17_repo(tmp_path):
    _init_repo_mu12(tmp_path)  # reuse the MU12 git fixture
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_t.py").write_text(
        "def test_t():\n    assert True\n", encoding="utf-8")
    return tmp_path


def test_mu17_baseline_cached_per_head(tmp_path, monkeypatch):
    """MU17(a): the second baseline capture at the same HEAD must be served
    from the cache without spawning pytest again."""
    repo = _mu17_repo(tmp_path)
    calls = {"n": 0}

    class _Res:
        returncode = 1
        stdout = "FAILED tests/test_t.py::test_t - boom\n1 failed\n"
        stderr = ""

    _real_run = _hb.subprocess.run

    def _fake_run(cmd, **kwargs):
        if cmd and cmd[0] == "git":
            return _real_run(cmd, **kwargs)
        calls["n"] += 1
        return _Res()

    monkeypatch.setattr(_hb.subprocess, "run", _fake_run)
    first = _hb._capture_baseline_failures(str(repo), repo_root=str(repo))
    assert first == ["tests/test_t.py::test_t"]
    assert calls["n"] == 1
    # Cache file exists for the current HEAD.
    sha = _git_mu12(["rev-parse", "HEAD"], repo).stdout.strip()
    assert (repo / ".harness" / "baseline" / f"{sha}.json").is_file(), (
        "MU17: completed baseline must be cached per HEAD sha")
    # Second call: cache hit, no new pytest spawn.
    second = _hb._capture_baseline_failures(str(repo), repo_root=str(repo))
    assert second == first
    assert calls["n"] == 1, "MU17: cached baseline must not re-run the suite"


def test_mu17_timeout_returns_empty_no_second_candidate(tmp_path, monkeypatch):
    """MU17(b): a baseline suite timeout must return [] immediately — no
    second candidate run, and nothing cached (timeouts are transient)."""
    import subprocess as _sp
    repo = _mu17_repo(tmp_path)
    calls = {"n": 0}

    _real_run = _hb.subprocess.run

    def _maybe_timeout(cmd, **kwargs):
        if cmd and cmd[0] == "git":
            return _real_run(cmd, **kwargs)
        calls["n"] += 1
        raise _sp.TimeoutExpired(cmd=cmd, timeout=240)

    monkeypatch.setattr(_hb.subprocess, "run", _maybe_timeout)
    # project_path different from repo_root → two candidates pre-fix.
    wt = repo / ".harness" / "worktrees" / "wt"
    (wt / "tests").mkdir(parents=True)
    (wt / "tests" / "test_t.py").write_text("def test_t():\n    pass\n",
                                            encoding="utf-8")
    result = _hb._capture_baseline_failures(str(wt), repo_root=str(repo))
    assert result == [], "MU17: timeout must yield empty baseline (safe default)"
    assert calls["n"] == 1, (
        f"MU17: timeout must NOT trigger a second candidate run; got {calls['n']} runs")
    sha = _git_mu12(["rev-parse", "HEAD"], repo).stdout.strip()
    assert not (repo / ".harness" / "baseline" / f"{sha}.json").exists(), (
        "MU17: a timed-out baseline must not be cached")


# ---------------------------------------------------------------------------
# MU1 — hydra doctor probe table uses real tool names
# ---------------------------------------------------------------------------

def test_mu1_doctor_probe_tool_names():
    """MU1: _DOCTOR_MCP_PROBES must reference real tool names for pp_harness and
    hydra_memory so `hydra doctor` no longer emits false 'unreachable' WARNs."""
    from hydra_core import cli

    probes = cli._DOCTOR_MCP_PROBES
    assert isinstance(probes, dict), "MU1: _DOCTOR_MCP_PROBES must be a dict"
    assert "pp_harness" in probes, "MU1: pp_harness must be in _DOCTOR_MCP_PROBES"
    assert "hydra_memory" in probes, "MU1: hydra_memory must be in _DOCTOR_MCP_PROBES"

    pp_tool, _pp_args = probes["pp_harness"]
    mem_tool, _mem_args = probes["hydra_memory"]

    assert pp_tool != "ping", (
        f"MU1: pp_harness probe must not use 'ping' (non-existent); got {pp_tool!r}"
    )
    assert mem_tool != "list_tools", (
        f"MU1: hydra_memory probe must not use 'list_tools' (non-existent); got {mem_tool!r}"
    )
    assert pp_tool, "MU1: pp_harness probe tool name must be non-empty"
    assert mem_tool, "MU1: hydra_memory probe tool name must be non-empty"

    # Spot-check known correct values (regression guard).
    assert pp_tool == "budget_status", (
        f"MU1: pp_harness probe must be 'budget_status'; got {pp_tool!r}"
    )
    assert mem_tool == "hydra-mem.ping", (
        f"MU1: hydra_memory probe must be 'hydra-mem.ping'; got {mem_tool!r}"
    )


def _run_doctor_with_dispatcher(monkeypatch, capsys, call_mcp_return):
    """Invoke `_cmd_doctor` with every probe server 'registered' and a stub
    dispatcher whose call_mcp returns ``call_mcp_return``. Returns doctor stdout."""
    import hydra_core.dispatcher as _disp

    monkeypatch.setattr(
        _disp, "_load_mcp_config",
        lambda project: {k: {} for k in cli._DOCTOR_MCP_PROBES},
    )

    class _StubDispatcher:
        def __init__(self, *a, **k):
            pass

        def call_mcp(self, server, tool, args, **k):
            return call_mcp_return(server, tool)

    monkeypatch.setattr(_disp, "MCPStdioDispatcher", _StubDispatcher)
    cli._cmd_doctor(SimpleNamespace(project=str(REPO_ROOT), quick=False))
    return capsys.readouterr().out


def test_mu1_doctor_reports_down_server_unreachable(monkeypatch, capsys):
    """MU1: a genuinely-unreachable server must be reported 'unreachable'.

    ``call_mcp`` catches transport/connect failures internally and returns a
    ``{"status": "failed", ...}`` *dict* — it does not raise (dispatcher.py).
    So an ``isinstance(res, dict)`` reachability test mislabels a down server as
    reachable, defeating the purpose of `hydra doctor`. Reachability must key on
    ``status == "done"``.
    """
    out = _run_doctor_with_dispatcher(
        monkeypatch, capsys,
        lambda server, tool: {
            "status": "failed", "server": server, "tool": tool,
            "error": "ConnectionRefusedError: server down",
        },
    )
    for server in cli._DOCTOR_MCP_PROBES:
        assert f"OK:   {server} reachable" not in out, (
            f"MU1: down server {server!r} must NOT be reported reachable")
        assert f"WARN: {server} unreachable" in out, (
            f"MU1: down server {server!r} must be reported unreachable; got:\n{out}")


def test_mu1_doctor_reports_healthy_server_reachable(monkeypatch, capsys):
    """MU1: a healthy server (call_mcp returns status 'done') is 'reachable'."""
    out = _run_doctor_with_dispatcher(
        monkeypatch, capsys,
        lambda server, tool: {"status": "done", "tool": tool,
                              "result": {"ok": True}},
    )
    for server in cli._DOCTOR_MCP_PROBES:
        assert f"OK:   {server} reachable" in out, (
            f"MU1: healthy server {server!r} must be reported reachable; got:\n{out}")


# ---------------------------------------------------------------------------
# MU3 — SqliteSaver serde registers hydra_core.state types (no deprecation warning)
# ---------------------------------------------------------------------------

def test_mu3_checkpoint_roundtrip_no_deprecation_warning(tmp_path, monkeypatch):
    """MU3: build_supervisor must pass a JsonPlusSerializer with hydra_core.state
    types in allowed_msgpack_modules.  Loading a checkpoint must not populate the
    'Deserializing unregistered type' warning dedup set for any hydra_core type.

    We monkeypatch the module-level _warned_unregistered_types set so the test
    is isolated from dedup state left by earlier tests in the session.
    """
    from langgraph.checkpoint.serde import jsonplus as _jps

    monkeypatch.setenv("HYDRA_CHECKPOINT_DB", str(tmp_path / "checkpoints.db"))

    # Replace the dedup set with a fresh one so we see first-occurrence warnings.
    fresh_warned: set = set()
    monkeypatch.setattr(_jps, "_warned_unregistered_types", fresh_warned)

    wf = uuid4()
    initial = HydraState(workflow_id=wf, root_goal="MU3 roundtrip test")
    initial.selected_squads = ["executive"]

    # First pass: invoke to create the checkpoint (serialises state).
    sup1 = build_supervisor(project_root=REPO_ROOT, dispatcher=_NullDispatcher())
    sup1.invoke(initial, config={"configurable": {"thread_id": str(wf)}})

    # Second pass: re-open the supervisor (new SqliteSaver) and get_state
    # (triggers deserialization of checkpointed state via JsonPlusSerializer).
    sup2 = build_supervisor(project_root=REPO_ROOT, dispatcher=_NullDispatcher())
    snap = sup2.get_state({"configurable": {"thread_id": str(wf)}})

    # No hydra_core.* type should have been added to the unregistered-warning set.
    hydra_warned = {k for k in fresh_warned if k[0].startswith("hydra_core.")}
    assert not hydra_warned, (
        f"MU3: 'Deserializing unregistered type' fired for hydra_core types: "
        f"{hydra_warned}. Add them to allowed_msgpack_modules in build_supervisor."
    )

    # State round-trips correctly: root_goal survives the serialize/deserialize cycle.
    assert snap is not None and snap.values, "MU3: checkpoint must be non-empty"
    state = HydraState.model_validate(snap.values)
    assert state.root_goal == "MU3 roundtrip test", (
        f"MU3: root_goal must survive checkpoint roundtrip; got {state.root_goal!r}"
    )


def test_mu3_hydra_memory_workflow_status_no_deprecation_warning(tmp_path, monkeypatch):
    """MU3(b): hydra_memory's _load_state_values (used by workflow_status handler)
    must use make_checkpoint_serde so loading a checkpoint via the MCP server path
    also suppresses the 'Deserializing unregistered type' warning."""
    from langgraph.checkpoint.serde import jsonplus as _jps
    from mcp_servers.hydra_memory.server import _tool_handlers as mem_handlers

    db_path = str(tmp_path / "checkpoints.db")
    monkeypatch.setenv("HYDRA_CHECKPOINT_DB", db_path)

    # Create a checkpoint via build_supervisor so _load_state_values has data to read.
    wf = uuid4()
    initial = HydraState(workflow_id=wf, root_goal="MU3b hydra_memory path test")
    initial.selected_squads = ["executive"]
    sup = build_supervisor(project_root=REPO_ROOT, dispatcher=_NullDispatcher())
    sup.invoke(initial, config={"configurable": {"thread_id": str(wf)}})

    # Replace the dedup set to isolate this test from prior dedup entries.
    fresh_warned: set = set()
    monkeypatch.setattr(_jps, "_warned_unregistered_types", fresh_warned)

    # Call the workflow_status handler — this triggers _load_state_values which
    # constructs SqliteSaver and calls get_tuple (deserializes the checkpoint).
    h = mem_handlers()
    result = h["hydra-mem.workflow_status"]({"workflow_id": str(wf)})

    # workflow_status must return the workflow data (not a degraded error).
    assert "phase" in result, (
        f"MU3(b): workflow_status must return a valid result; got {result!r}"
    )

    # No hydra_core.* types should have been warned as unregistered.
    hydra_warned = {k for k in fresh_warned if k[0].startswith("hydra_core.")}
    assert not hydra_warned, (
        f"MU3(b): 'Deserializing unregistered type' fired for hydra_core types via "
        f"hydra_memory path: {hydra_warned}"
    )


# ---------------------------------------------------------------------------
# MU9 — router must not auto-select stub squads; marketing keyword siphon pruned
# ---------------------------------------------------------------------------
# MU9a: classify_intent excludes squads whose entrypoint=="stub" from automatic
#        selection.  Explicit --squad / pre-seeded selected_squads still passes
#        through; supervisor.node_intake emits "stub_squad_explicitly_selected"
#        trace but proceeds.
# MU9b: "scheduling" pruned from marketing-production keywords — it is a generic
#        English word that matched the clinical goal below.

from hydra_core.router import classify_intent as _classify_intent  # noqa: E402
from hydra_core.squad_loader import _coerce_pack as _coerce_pack  # noqa: E402


def _mu9_packs() -> dict:
    """Worktree packs extended with synthetic marketing-* squads (claude-skill).

    Marketing-* packs are filesystem symlinks absent from the worktree squads/
    directory.  We inject minimal synthetic packs so classify_intent sees them
    via _KEYWORDS (which keys on the slug, not the pack's own description).
    """
    from hydra_core.squad_loader import discover_squads
    real = discover_squads(REPO_ROOT)
    synthetic = {
        slug: _coerce_pack(slug, {
            "name": slug,
            "entrypoint": "claude-skill",
            "description": slug,
        })
        for slug in (
            "marketing-production", "marketing-strategy", "marketing-ops",
            "marketing-creative", "marketing-research",
        )
        if slug not in real
    }
    return {**real, **synthetic}


_CLINICAL_GOAL = (
    "triage patient intake symptoms and recommend clinical follow-up scheduling"
)
_CAMPAIGN_GOAL = "plan a paid social campaign for the product launch"


def test_mu9a_clinical_goal_excludes_stub_and_marketing_production():
    """MU9(a): clinical goal must not select any stub squad (healthcare) and must
    not select marketing-production after pruning 'scheduling'."""
    packs = _mu9_packs()
    # Confirm healthcare is present and is a stub.
    assert "healthcare" in packs, "healthcare pack must be discoverable"
    assert packs["healthcare"].entrypoint == "stub", (
        "healthcare entrypoint must be 'stub' for this test to be meaningful"
    )
    decision = _classify_intent(_CLINICAL_GOAL, packs)
    stub_selected = [s for s in decision.squads if s in packs and packs[s].entrypoint == "stub"]
    assert not stub_selected, (
        f"MU9(a): stub squad(s) {stub_selected!r} auto-selected for clinical goal; "
        f"full selection: {decision.squads!r}"
    )
    assert "marketing-production" not in decision.squads, (
        f"MU9(a): marketing-production must not be selected for clinical goal after "
        f"pruning 'scheduling'; full selection: {decision.squads!r}"
    )


def test_mu9b_explicit_stub_squad_passes_through(monkeypatch):
    """MU9(b): explicit selected_squads=['healthcare'] (the --squad path) still
    yields healthcare in selected_squads — stub exclusion is for auto-selection only.
    Also verifies that supervisor.node_intake emits 'stub_squad_explicitly_selected'."""
    import hydra_core.supervisor as _sup_mod
    from hydra_core.supervisor import build_supervisor, _PurePythonRunner

    class _StubDispMU9:
        def call_mcp(self, *a, **k):
            return {"status": "done", "result": {}}

        def spawn_subprocess(self, *a, **k):
            return {"status": "done", "stdout": ""}

        def emit_claude_prompt(self, *a, **k):
            return {"status": "host_pickup_required"}

        def invoke_claude_skill(self, *a, **k):
            return {"status": "host_pickup_required"}

    captured_events: list[tuple] = []

    def _capture_emit(root, wf_id, event_name, payload):
        captured_events.append((event_name, payload))

    monkeypatch.setattr(_sup_mod, "emit_trace", _capture_emit)

    state = HydraState(
        root_goal="healthcare explicit test",
        selected_squads=["healthcare"],
    )
    sup = build_supervisor(
        project_root=REPO_ROOT,
        dispatcher=_StubDispMU9(),
        force_pure_python=True,
    )
    assert isinstance(sup, _PurePythonRunner)
    result = sup.invoke(state, stop_before="planner")
    assert "healthcare" in result.selected_squads, (
        f"MU9(b): explicit stub squad 'healthcare' must pass through to selected_squads; "
        f"got {result.selected_squads!r}"
    )
    stub_events = [
        (name, payload) for name, payload in captured_events
        if name == "supervisor.stub_squad_explicitly_selected"
    ]
    assert stub_events, (
        f"MU9(b): 'supervisor.stub_squad_explicitly_selected' trace event must be emitted "
        f"when a stub squad is explicitly selected; captured events: "
        f"{[n for n, _ in captured_events]}"
    )
    assert "healthcare" in stub_events[0][1].get("stub_squads", []), (
        f"MU9(b): trace payload must name 'healthcare' in stub_squads; "
        f"got {stub_events[0][1]!r}"
    )


def test_mu9c_campaign_goal_selects_marketing_squad():
    """MU9(c): 'plan a paid social campaign for the product launch' must select
    at least one marketing-* squad (keyword pruning must not break marketing routing).
    marketing-ops matches via 'paid social' (added after MU9b pruning review)."""
    packs = _mu9_packs()
    decision = _classify_intent(_CAMPAIGN_GOAL, packs)
    marketing_selected = [s for s in decision.squads if s.startswith("marketing-")]
    assert marketing_selected, (
        f"MU9(c): at least one marketing-* squad must be selected for campaign goal; "
        f"full selection: {decision.squads!r}"
    )
