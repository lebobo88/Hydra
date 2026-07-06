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


# ---------------------------------------------------------------------------
# MU16 — pre-operation budget gates between candidates in _drive_best_of_loop
# ---------------------------------------------------------------------------
# _drive_pp_stage_loop is single-stage (no inter-stage iteration); the only
# gate needed is the between-candidates gate in _drive_best_of_loop.
#
# Tests (a) fleet-repo exhausted, (b) workflow-global exhausted.

from hydra_core.squad_node import _drive_best_of_loop as _dbol  # noqa: E402


def _best_of_responses_mu16(n_candidates: int = 3) -> dict:
    cands = [
        {"candidate_index": i, "attempt_slot_id": f"slot{i}",
         "worktree_path": f"/tmp/mu16c{i}", "worktree_mode": "copy"}
        for i in range(1, n_candidates + 1)
    ]
    return {
        ("pp_harness", "start_best_of_stage"): {"status": "done", "result": {
            "stage_id": "st_mu16", "candidates": cands}},
        ("pp_harness", "gate_eligible_judges"): {"status": "done", "result": {
            "required_cross_vendor": False, "rubric_id": "rfc-2119-normative"}},
        ("pp_codex", "generate"): {"status": "done", "result": {
            "text": "edited foo.py", "model": "codex-1",
            "tokens_in": 5, "tokens_out": 7, "cost_usd": 0.02}},
        ("pp_harness", "archive_artifact"): {"status": "done", "result": {"path": "x"}},
        ("pp_harness", "record_attempt"): {"status": "done", "result": {"attempt_id": "att_mu16"}},
        ("pp_codex", "critique"): {"status": "done", "result": {"parsed": {
            "outcome": "pass", "critique_md": "ok", "score": {}}}},
        ("pp_harness", "record_verdict"): {"status": "done", "result": {}},
        ("pp_harness", "record_smoke_status"): {"status": "done", "result": {}},
        ("pp_harness", "get_stage_finalize_readiness"): {"status": "done", "result": {}},
        ("pp_harness", "finalize_stage"): {"status": "done", "result": {}},
        ("pp_harness", "finalize_run"): {"status": "done", "result": {
            "effective_status": "complete", "status": "complete"}},
        ("pp_harness", "borda_count"): {"status": "done", "result": {"winner": "att_mu16"}},
        ("pp_harness", "archive_winner_and_losers"): {"status": "done", "result": {
            "merge_status": "merged", "winner_diff_path": "w.diff"}},
        ("pp_harness", "teardown_candidates"): {"status": "done", "result": {
            "teardown_status": "ok"}},
    }


class _BoDispatcher:
    def __init__(self, responses):
        self.responses = responses
        self.calls: list[tuple] = []

    def call_mcp(self, server, tool, args, *, squad_id=None):
        self.calls.append((server, tool, args))
        return self.responses.get((server, tool), {"status": "done", "result": {}})

    def tool_seq(self):
        return [t for (_, t, _) in self.calls]

    def emit_claude_prompt(self, *a, **k): raise NotImplementedError  # pragma: no cover
    def invoke_claude_skill(self, *a, **k): raise NotImplementedError  # pragma: no cover
    def spawn_subprocess(self, *a, **k): raise NotImplementedError  # pragma: no cover


def test_mu16a_fleet_repo_budget_exhausted_no_generate(monkeypatch):
    """MU16(a): best-of loop with a ledger whose repo allocation is already spent
    must produce zero pp_codex.generate calls, emit fleet.repo_budget_exhausted,
    and finalize as final_status='surfaced' with error='budget_exhausted'."""
    monkeypatch.setattr(
        "hydra_core.squad_node._run_smoke", lambda *a, **k: ("pass", "ok"))
    events: list[tuple] = []
    monkeypatch.setattr(
        "hydra_core.telemetry.emit",
        lambda _r, _wf, kind, payload: events.append((kind, payload)),
    )

    state = HydraState(root_goal="mu16a")
    state.budget.budget_usd = 5.0
    state.budget.allocate_repos(["repo-a"])
    # Pre-exhaust: spend == allocation.
    state.budget.repo_spend["repo-a"] = state.budget.repo_budgets["repo-a"]

    disp = _BoDispatcher(_best_of_responses_mu16(n_candidates=3))

    result = _dbol(
        disp, run_id="run_mu16a", project_path="/tmp/proj",
        request_text="x", n=3, workflow_id="wf-mu16a",
        state=state, repo_id="repo-a",
    )

    assert ("pp_codex", "generate") not in {(s, t) for (s, t, _) in disp.calls}, (
        "MU16(a): no generate must run when repo budget is pre-exhausted"
    )
    kinds = [k for (k, _) in events]
    assert "fleet.repo_budget_exhausted" in kinds, (
        f"MU16(a): fleet.repo_budget_exhausted trace missing; got {kinds}"
    )
    evt = next(p for (k, p) in events if k == "fleet.repo_budget_exhausted")
    assert evt.get("repo_id") == "repo-a"
    assert evt.get("candidate") == 1
    # P2: budget-exhausted path must finalize as surfaced, not aborted.
    assert result is not None, "MU16(a): _dbol must return a result dict"
    assert result["final_status"] == "surfaced", (
        f"MU16(a): expected final_status='surfaced', got {result['final_status']!r}"
    )
    assert "budget_exhausted" in (result.get("error") or ""), (
        f"MU16(a): error must mention 'budget_exhausted'; got {result.get('error')!r}"
    )


def test_mu16b_workflow_budget_exhausted_candidates_skipped(monkeypatch):
    """MU16(b): non-fleet best-of with exhausted global workflow budget must skip
    remaining candidates and emit budget.candidates_skipped."""
    monkeypatch.setattr(
        "hydra_core.squad_node._run_smoke", lambda *a, **k: ("pass", "ok"))
    events: list[tuple] = []
    monkeypatch.setattr(
        "hydra_core.telemetry.emit",
        lambda _r, _wf, kind, payload: events.append((kind, payload)),
    )

    state = HydraState(root_goal="mu16b")
    state.budget.budget_usd = 5.0
    state.budget.spent_usd = 5.0  # 100% consumed; no per-repo allocation

    disp = _BoDispatcher(_best_of_responses_mu16(n_candidates=3))

    _dbol(
        disp, run_id="run_mu16b", project_path="/tmp/proj",
        request_text="x", n=3, workflow_id="wf-mu16b",
        state=state, repo_id=None,
    )

    assert ("pp_codex", "generate") not in {(s, t) for (s, t, _) in disp.calls}, (
        "MU16(b): no generate must run when global budget is exhausted"
    )
    kinds = [k for (k, _) in events]
    assert "budget.candidates_skipped" in kinds, (
        f"MU16(b): budget.candidates_skipped trace missing; got {kinds}"
    )
    evt = next(p for (k, p) in events if k == "budget.candidates_skipped")
    assert float(evt.get("remaining", 1.0)) <= 0.0


# ---------------------------------------------------------------------------
# MU14 — timed-out generation emits `budget.cost_unknown_timeout`
# ---------------------------------------------------------------------------

from hydra_core.squad_node import _drive_pp_stage_loop as _dpsl  # noqa: E402


def test_mu14_timeout_generate_emits_cost_unknown_trace(monkeypatch):
    """MU14: single-candidate loop — a codex-timeout generate with cost=0 and
    tokens=0 must emit budget.cost_unknown_timeout in the telemetry trace so
    unaccounted spend is visible without fabricating token numbers."""
    events: list[tuple] = []
    monkeypatch.setattr(
        "hydra_core.telemetry.emit",
        lambda _r, _wf, kind, payload: events.append((kind, payload)),
    )
    resp = {
        ("pp_harness", "start_stage"): {
            "status": "done", "result": {"stage_id": "st_mu14"}},
        ("pp_harness", "gate_eligible_judges"): {
            "status": "done", "result": {"required_cross_vendor": False}},
        # Codex timeout: status=failed, timeout=True, zero cost/tokens.
        ("pp_codex", "generate"): {
            "status": "failed", "timeout": True,
            "error": "pp_codex.generate timed out after 600s",
            "result": {"cost_usd": 0.0, "tokens_in": 0, "tokens_out": 0, "text": ""}},
        ("pp_harness", "archive_artifact"): {"status": "done", "result": {}},
        ("pp_harness", "record_attempt"): {
            "status": "done", "result": {"attempt_id": "att_mu14"}},
        ("pp_harness", "finalize_stage"): {"status": "done", "result": {}},
        ("pp_harness", "finalize_run"): {
            "status": "done", "result": {"status": "surfaced"}},
    }

    class _D:
        def __init__(self):
            self.calls: list[tuple] = []

        def call_mcp(self, s, t, a, *, squad_id=None):
            self.calls.append((s, t, a))
            return resp.get((s, t), {"status": "done", "result": {}})

        def emit_claude_prompt(self, *a, **k): raise NotImplementedError  # pragma: no cover
        def invoke_claude_skill(self, *a, **k): raise NotImplementedError  # pragma: no cover
        def spawn_subprocess(self, *a, **k): raise NotImplementedError  # pragma: no cover

    disp = _D()
    out = _dpsl(
        disp, run_id="run_mu14", project_path="/tmp/proj",
        request_text="add feature", workflow_id="wf-mu14",
    )

    # Timeout → run must not complete.
    assert out["final_status"] in {"surfaced", "aborted"}, (
        f"MU14: expected surfaced/aborted on timeout; got {out['final_status']!r}"
    )
    assert "timed out" in (out.get("error") or ""), (
        f"MU14: error must mention 'timed out'; got {out.get('error')!r}"
    )
    # MU14 trace must be in the events.
    kinds = [k for (k, _) in events]
    assert "budget.cost_unknown_timeout" in kinds, (
        f"MU14: budget.cost_unknown_timeout missing from trace; got {kinds}"
    )
    te = next(p for (k, p) in events if k == "budget.cost_unknown_timeout")
    assert te.get("run_id") == "run_mu14"


def test_mu14_no_trace_when_cost_captured(monkeypatch):
    """MU14: when the failed generate DID carry cost/tokens (soft-block with
    partial usage), budget.cost_unknown_timeout must NOT be emitted."""
    events: list[tuple] = []
    monkeypatch.setattr(
        "hydra_core.telemetry.emit",
        lambda _r, _wf, kind, payload: events.append((kind, payload)),
    )
    resp = {
        ("pp_harness", "start_stage"): {
            "status": "done", "result": {"stage_id": "st_mu14b"}},
        ("pp_harness", "gate_eligible_judges"): {
            "status": "done", "result": {"required_cross_vendor": False}},
        # Soft-block: read-only sandbox marker but cost IS reported.
        ("pp_codex", "generate"): {"status": "done", "result": {
            "text": "writing is blocked by read-only sandbox",
            "model": "codex-1",
            "tokens_in": 8, "tokens_out": 4, "cost_usd": 0.03}},
        ("pp_harness", "archive_artifact"): {"status": "done", "result": {}},
        ("pp_harness", "record_attempt"): {
            "status": "done", "result": {"attempt_id": "att_mu14b"}},
        ("pp_harness", "finalize_stage"): {"status": "done", "result": {}},
        ("pp_harness", "finalize_run"): {
            "status": "done", "result": {"status": "surfaced"}},
    }

    class _D:
        def __init__(self):
            self.calls: list[tuple] = []

        def call_mcp(self, s, t, a, *, squad_id=None):
            self.calls.append((s, t, a))
            return resp.get((s, t), {"status": "done", "result": {}})

        def emit_claude_prompt(self, *a, **k): raise NotImplementedError  # pragma: no cover
        def invoke_claude_skill(self, *a, **k): raise NotImplementedError  # pragma: no cover
        def spawn_subprocess(self, *a, **k): raise NotImplementedError  # pragma: no cover

    disp = _D()
    out = _dpsl(
        disp, run_id="run_mu14b", project_path="/tmp/proj",
        request_text="x", workflow_id="wf-mu14b",
    )

    # Cost was captured — no budget.cost_unknown_timeout.
    timeout_evts = [k for (k, _) in events if k == "budget.cost_unknown_timeout"]
    assert timeout_evts == [], (
        f"MU14: budget.cost_unknown_timeout must NOT fire when cost is captured; "
        f"got {timeout_evts}"
    )
    # Cost propagates through correctly.
    assert out["cost_usd"] >= 0.03


# ---------------------------------------------------------------------------
# MU5 — conservative goal-prose repo inference in node_intake
# ---------------------------------------------------------------------------
# When no explicit --repo flag is present, node_intake scans the goal text for
# known repo ids near recognized cue phrases ("repo:", "repository", "in repo",
# "monorepo"). Exactly one cued id → infer; any other situation → warn only.

import hydra_core.supervisor as _sup_mod_mu5  # noqa: E402


class _StubDispMU5:
    def call_mcp(self, *a, **k): return {"status": "done", "result": {}}
    def spawn_subprocess(self, *a, **k): return {"status": "done", "stdout": ""}
    def emit_claude_prompt(self, *a, **k): return {"status": "host_pickup_required"}
    def invoke_claude_skill(self, *a, **k): return {"status": "host_pickup_required"}


def test_mu5_repo_inferred_from_goal_single_cued(monkeypatch):
    """MU5(a): goal 'Fix the parser bug. Repo: mc-test' — 'Repo:' is a cue phrase
    and 'mc-test' is the only matched id within 40 chars → target_repo_id is
    inferred and intake.repo_inferred_from_goal trace is emitted."""
    from hydra_core.supervisor import build_supervisor, _PurePythonRunner

    events: list[tuple] = []

    def _capture(root, wf_id, event_name, payload):
        events.append((event_name, payload))

    monkeypatch.setattr(_sup_mod_mu5, "emit_trace", _capture)

    state = HydraState(root_goal="Fix the parser bug. Repo: mc-test")
    sup = build_supervisor(
        project_root=REPO_ROOT, dispatcher=_StubDispMU5(), force_pure_python=True
    )
    assert isinstance(sup, _PurePythonRunner)
    result = sup.invoke(state, stop_before="planner")

    assert result.target_repo_id == "mc-test", (
        f"MU5(a): expected target_repo_id='mc-test', got {result.target_repo_id!r}"
    )
    infer_events = [(n, p) for n, p in events if n == "intake.repo_inferred_from_goal"]
    assert infer_events, (
        f"MU5(a): intake.repo_inferred_from_goal trace must be emitted; "
        f"captured: {[n for n, _ in events]}"
    )
    assert infer_events[0][1].get("repo_id") == "mc-test", (
        f"MU5(a): trace repo_id must be 'mc-test'; got {infer_events[0][1]!r}"
    )


def test_mu5_multiple_repo_mentions_warn_no_inference(monkeypatch):
    """MU5(b): goal mentioning 'mc-test and candc repositories' — both ids match
    but neither is cued (cue comes AFTER the ids) → no inference, warning trace
    lists both ids."""
    from hydra_core.supervisor import build_supervisor, _PurePythonRunner

    events: list[tuple] = []

    def _capture(root, wf_id, event_name, payload):
        events.append((event_name, payload))

    monkeypatch.setattr(_sup_mod_mu5, "emit_trace", _capture)

    state = HydraState(root_goal="Fix the parser bug in mc-test and candc repositories")
    sup = build_supervisor(
        project_root=REPO_ROOT, dispatcher=_StubDispMU5(), force_pure_python=True
    )
    assert isinstance(sup, _PurePythonRunner)
    result = sup.invoke(state, stop_before="planner")

    assert not result.target_repo_id, (
        f"MU5(b): must not infer when multiple repos mentioned without single cue; "
        f"got {result.target_repo_id!r}"
    )
    warn_events = [(n, p) for n, p in events if n == "intake.repo_mention_without_target"]
    assert warn_events, (
        f"MU5(b): intake.repo_mention_without_target trace must be emitted; "
        f"captured: {[n for n, _ in events]}"
    )
    mentioned = warn_events[0][1].get("mentioned", [])
    assert "mc-test" in mentioned and "candc" in mentioned, (
        f"MU5(b): both ids must be in trace 'mentioned' list; got {mentioned}"
    )


# ---------------------------------------------------------------------------
# MU11 — memory tag unknown key returns error + CLI exit 1
# ---------------------------------------------------------------------------

def test_mu11_memory_tag_unknown_key_exit_1(tmp_path, monkeypatch, capsys):
    """MU11: cli.main memory tag with a nonexistent key returns rc=1 and outputs
    a JSON object with 'error' key. Existing-key behavior is unchanged."""
    import hydra_core.memory as _mem

    # Redirect all tag_episodic calls to a fresh tmp db (the key won't exist there).
    real_tag = _mem.tag_episodic

    def _tmp_tag(key, cells, *, replace=False, db=None):  # noqa: ANN001
        return real_tag(key, cells, replace=replace, db=tmp_path / "episodic.db")

    monkeypatch.setattr(_mem, "tag_episodic", _tmp_tag)

    rc = cli.main(["--project", str(REPO_ROOT), "memory", "tag", "bogus-nonexistent-key",
                   "--cells", "qian"])
    captured = capsys.readouterr()
    assert rc == 1, f"MU11: expected rc=1 for unknown key, got {rc}"
    out = captured.out.strip()
    assert out, "MU11: must produce output when key is unknown"
    data = json.loads(out)
    assert "error" in data, f"MU11: output must contain 'error' key; got {data}"
    assert data.get("key") == "bogus-nonexistent-key", (
        f"MU11: output must echo the key; got {data}"
    )


# ---------------------------------------------------------------------------
# MU13 — byproduct exclusion before git add -A in attended commits
# ---------------------------------------------------------------------------

def test_mu13_byproduct_excluded_from_preserved_branch(tmp_path, monkeypatch):
    """MU13: when the attended host preserves non-complete work, __pycache__/
    pyc files must NOT appear in the committed branch while real source files
    must be included."""
    _init_repo_mu12(tmp_path)

    # Force smoke to fail so the non-complete preserve path fires.
    monkeypatch.setattr(_hb, "_run_smoke",
                        lambda *a, **k: ("fail", "MU13 injected smoke failure"))

    disp = _FakeDispatcherMU12(required_cross_vendor=True)
    res = _hb.begin_stage(
        disp, workflow_id="wf-mu13", run_id="run-mu13",
        project_path=str(tmp_path), request_text="add mu13_feature.py",
        project_root=str(tmp_path), isolate=True)
    assert res["status"] == "awaiting_host", (
        f"MU13: expected awaiting_host, got {res['status']!r}"
    )

    wt = res["host_action"]["cwd"]
    assert "worktrees" in wt.replace("\\", "/"), "MU13: engineer must be in a worktree"

    # Engineer creates a real source file AND a __pycache__/junk.pyc byproduct.
    (Path(wt) / "mu13_feature.py").write_text("# mu13 feature\n", encoding="utf-8")
    pyc_dir = Path(wt) / "__pycache__"
    pyc_dir.mkdir(exist_ok=True)
    (pyc_dir / "junk.pyc").write_bytes(b"\x00\x00byproduct")

    cfile = res["cursor_path"]

    res = _hb.submit_host_result(
        disp, cursor_file=cfile, call_key="generate-0",
        result={"text": "added mu13_feature.py", "cost_usd": 0.01,
                "tokens_in": 10, "tokens_out": 5, "model": "claude-test"})
    assert res["state"] == "await_judge"

    # Submit judge pass — smoke fails → preserve fires.
    res = _hb.submit_host_result(
        disp, cursor_file=cfile, call_key="judge-0",
        result={"outcome": "pass", "critique_md": "looks good",
                "judge_producer": "codex", "cost_usd": 0.005})
    assert res["status"] == "surfaced", (
        f"MU13: expected surfaced (smoke-fail), got {res['status']!r}"
    )

    preserved = res.get("preserved_branch")
    assert preserved, f"MU13: preserved_branch must be set; got {res!r}"

    # Real file must be on the branch.
    show_real = _git_mu12(["show", f"{preserved}:mu13_feature.py"], tmp_path)
    assert show_real.returncode == 0, (
        f"MU13: git show {preserved}:mu13_feature.py failed — real file not preserved.\n"
        f"stderr: {show_real.stderr}"
    )
    assert "mu13 feature" in show_real.stdout

    # Pyc must NOT be on the branch.
    show_pyc = _git_mu12(["show", f"{preserved}:__pycache__/junk.pyc"], tmp_path)
    assert show_pyc.returncode != 0, (
        f"MU13: __pycache__/junk.pyc must NOT be committed to the attended branch; "
        f"git show returned {show_pyc.returncode}"
    )


# ---------------------------------------------------------------------------
# MU15d — status rendering: attended-done tasks show "done (attended)"
# ---------------------------------------------------------------------------

def test_mu15d_status_renders_done_attended(tmp_path, monkeypatch, capsys):
    """MU15d: hydra status <wf> renders a deferred_to_host task as 'done (attended)'
    when its task_id is present in state.attended_done_task_ids."""
    monkeypatch.setenv("HYDRA_CHECKPOINT_DB", str(tmp_path / "checkpoints.db"))
    wf = uuid4()

    # 1. Build the plan so a task is created in the checkpoint.
    initial = HydraState(workflow_id=wf, root_goal="MU15d status render test")
    initial.selected_squads = ["engineering"]
    sup = build_supervisor(
        project_root=REPO_ROOT, dispatcher=_NullDispatcher(), plan_only=True
    )
    config = {"configurable": {"thread_id": str(wf)}}
    sup.invoke(initial, config=config)

    snap = sup.get_state(config)
    state_after_plan = HydraState.model_validate(snap.values)
    assert state_after_plan.tasks, "MU15d: planner must have created at least one task"
    task = state_after_plan.tasks[0]
    task_id = str(task.task_id)

    # 2. Simulate attended complete: add task_id to attended_done_task_ids.
    sup.update_state(config, {
        "attended_done_task_ids": [task_id],
    })

    # 3. Run cli status on the workflow.
    rc = cli.main(["--project", str(REPO_ROOT), "status", str(wf)])
    captured = capsys.readouterr()
    assert rc == 0, f"MU15d: status must succeed, got rc={rc}"
    data = json.loads(captured.out)

    tasks_view = data.get("tasks", [])
    assert tasks_view, "MU15d: tasks list must be non-empty"

    # The attended-complete task must show "done (attended)".
    matching = [t for t in tasks_view if t["task_id"] == task_id[:8]]
    assert matching, (
        f"MU15d: task {task_id[:8]} must appear in tasks view; got {tasks_view}"
    )
    assert matching[0]["status"] == "done (attended)", (
        f"MU15d: task status must be 'done (attended)'; got {matching[0]['status']!r}"
    )


# ---------------------------------------------------------------------------
# MU11b — hydra-mem.tag_memory handler propagates unknown-key error cleanly
# ---------------------------------------------------------------------------

def test_mu11b_tag_memory_handler_unknown_key(tmp_path, monkeypatch):
    """MU11(b): the hydra-mem.tag_memory MCP handler must return a top-level
    'error' key (not wrap the error dict inside 'cells') when the key is unknown."""
    import hydra_core.memory as _mem
    from mcp_servers.hydra_memory.server import _tool_handlers as mem_handlers

    real_tag = _mem.tag_episodic

    def _tmp_tag(key, cells, *, replace=False, db=None):  # noqa: ANN001
        return real_tag(key, cells, replace=replace, db=tmp_path / "episodic.db")

    monkeypatch.setattr(_mem, "tag_episodic", _tmp_tag)

    h = mem_handlers()
    result = h["hydra-mem.tag_memory"]({"key": "does-not-exist", "cells": ["qian"]})

    assert "error" in result, (
        f"MU11(b): result must have top-level 'error'; got {result!r}"
    )
    assert "cells" not in result or not isinstance(result.get("cells"), dict), (
        f"MU11(b): 'cells' must not wrap the error dict; got {result!r}"
    )


# ---------------------------------------------------------------------------
# P1 — scoped smoke profile
# ---------------------------------------------------------------------------
# (a) .harness/smoke_cmd.json with a valid cmd list is returned directly.
# (b) Malformed smoke_cmd.json falls through to pytest heuristic detection.
# (c) HYDRA_SMOKE_TIMEOUT_S=123 is passed as timeout kwarg to subprocess.run.

def test_p1_smoke_cmd_json_override(tmp_path):
    """P1(a): .harness/smoke_cmd.json with valid {"cmd": [...]} is returned
    directly, bypassing all heuristic detection."""
    from hydra_core.squad_node import _detect_smoke_command
    harness = tmp_path / ".harness"
    harness.mkdir()
    cmd = ["my-custom-runner", "--fast", "smoke"]
    (harness / "smoke_cmd.json").write_text(json.dumps({"cmd": cmd}), encoding="utf-8")
    result = _detect_smoke_command(str(tmp_path))
    assert result == cmd, f"P1(a): expected {cmd!r}, got {result!r}"


def test_p1_smoke_cmd_json_malformed_falls_through(tmp_path):
    """P1(b): malformed smoke_cmd.json (missing 'cmd' key) logs a warning and
    falls through to pytest heuristic detection (tests/ dir present)."""
    from hydra_core.squad_node import _detect_smoke_command
    import sys as _sys
    harness = tmp_path / ".harness"
    harness.mkdir()
    (harness / "smoke_cmd.json").write_text('{"not_cmd": []}', encoding="utf-8")
    # Provide a tests/ dir so the pytest heuristic fires.
    (tmp_path / "tests").mkdir()
    result = _detect_smoke_command(str(tmp_path))
    assert result is not None, "P1(b): malformed override must fall through to pytest"
    assert "pytest" in result, f"P1(b): expected pytest in result, got {result!r}"
    assert result == [_sys.executable, "-m", "pytest", "-q"], (
        f"P1(b): expected sys.executable -m pytest -q, got {result!r}"
    )


def test_p1_smoke_timeout_env(tmp_path, monkeypatch):
    """P1(c): HYDRA_SMOKE_TIMEOUT_S=123 is forwarded as timeout=123 kwarg to
    subprocess.run (captured via monkeypatched subprocess.run)."""
    from hydra_core import squad_node
    monkeypatch.setenv("HYDRA_SMOKE_TIMEOUT_S", "123")
    captured: list[dict] = []

    class _FakeRes:
        returncode = 0
        stdout = ""
        stderr = ""

    def _fake_run(*_args, **kwargs):
        captured.append(kwargs)
        return _FakeRes()

    monkeypatch.setattr(squad_node.subprocess, "run", _fake_run)
    monkeypatch.setattr(squad_node, "_detect_smoke_command", lambda _p: ["pytest", "-q"])
    squad_node._run_smoke(None, project_path=str(tmp_path), stage_id="p1-timeout")
    assert captured, "P1(c): subprocess.run must be called"
    assert captured[0].get("timeout") == 123, (
        f"P1(c): expected timeout=123, got {captured[0].get('timeout')!r}"
    )


# ---------------------------------------------------------------------------
# P2 — honest budget-exhausted finalize
# ---------------------------------------------------------------------------
# When the MU16 gate fires before any candidate generates, the best-of loop
# must finalize as 'surfaced' with error='budget_exhausted' (not 'aborted' via
# an uncaught RuntimeError). The MU16a test above already validates this for
# the fleet-repo path; this test covers the workflow-global exhaustion path.

def test_p2_global_budget_exhausted_finalizes_surfaced(monkeypatch):
    """P2: non-fleet best-of with pre-exhausted global budget finalizes as
    final_status='surfaced' with error='budget_exhausted', not 'aborted'."""
    monkeypatch.setattr(
        "hydra_core.squad_node._run_smoke", lambda *a, **k: ("pass", "ok"))
    events: list[tuple] = []
    monkeypatch.setattr(
        "hydra_core.telemetry.emit",
        lambda _r, _wf, kind, payload: events.append((kind, payload)),
    )

    state = HydraState(root_goal="p2-global")
    state.budget.budget_usd = 5.0
    state.budget.spent_usd = 5.0  # 100% consumed

    disp = _BoDispatcher(_best_of_responses_mu16(n_candidates=3))
    result = _dbol(
        disp, run_id="run_p2g", project_path="/tmp/proj",
        request_text="x", n=3, workflow_id="wf-p2g",
        state=state, repo_id=None,
    )

    assert result is not None, "P2: _dbol must return a result dict"
    assert result["final_status"] == "surfaced", (
        f"P2: expected final_status='surfaced', got {result['final_status']!r}"
    )
    assert "budget_exhausted" in (result.get("error") or ""), (
        f"P2: error must mention 'budget_exhausted'; got {result.get('error')!r}"
    )
    assert result.get("finalized") is True, (
        "P2: finalized must be True (finalize_run was called)"
    )
    # No generate must have been called.
    assert ("pp_codex", "generate") not in {(s, t) for (s, t, _) in disp.calls}, (
        "P2: no generate must run when global budget is exhausted"
    )


# ---------------------------------------------------------------------------
# P3 — prose-safe repo flag parsing
# ---------------------------------------------------------------------------
# (a) '--repo flag' mid-prose in a short goal → no HITL; trace emitted.
# (b) '--repo not-a-real-repo' at tail of a long string (>120 chars) → ValueError.
# (c) '--repo mc-test' with a known id mid-prose → target_repo_id == 'mc-test'.

def test_p3_mid_prose_unknown_token_no_intake_hitl(tmp_path, monkeypatch):
    """P3(a): '--repo flag' deep in the middle of a long goal (>120 chars from end)
    must not surface an intake HITL; intake.repo_flag_like_token_ignored is emitted.

    With the new tail rule (m.start() >= max(0, len-120)), short strings are always
    tail (typo protection), so the prose-safe escape only fires when the match is
    genuinely far from the end of a long string.

    emit_trace in supervisor.py is bound as a module-level alias
    (from .telemetry import emit as emit_trace); patch the supervisor module's
    attribute directly so closures built inside build_supervisor see the patch.
    """
    monkeypatch.setenv("HYDRA_CHECKPOINT_DB", str(tmp_path / "cp_p3a.db"))
    events: list[tuple] = []
    # Patch the module-level alias in supervisor so the node_intake closure picks
    # it up via LOAD_GLOBAL (patching telemetry.emit does NOT reach the alias).
    import hydra_core.supervisor as _sv
    monkeypatch.setattr(_sv, "emit_trace",
                        lambda _r, _wf, kind, payload: events.append((kind, payload)))
    sup = build_supervisor(project_root=REPO_ROOT, dispatcher=_NullDispatcher(),
                           plan_only=True)
    wf_id = uuid4()
    # Goal is long (>200 chars) with ONE '--repo' at position ~16 — well over 120
    # chars from the end, so it is NOT in the tail zone and fires RepoFlagIgnored.
    # Deliberately uses only one '--repo' occurrence to avoid the duplicate-check guard.
    goal = (
        "improve how the --repo flag parsing works in intake "
        "so that prose descriptions of CLI flags are not treated as explicit "
        "targets — this affects long-form workflow goal descriptions where operators "
        "naturally discuss cli flag semantics without intending to set a target repo"
    )
    assert len(goal) > 200, "precondition: goal must be >200 chars for prose detection"
    assert goal.lower().count("--repo") == 1, "precondition: exactly one --repo token"
    initial = HydraState(workflow_id=wf_id, root_goal=goal)
    initial.selected_squads = ["executive"]
    snap = sup.invoke(initial, config={"configurable": {"thread_id": str(wf_id)}})

    # No intake HITL from bad --repo: if the workflow paused, it must be at the
    # normal executive approval gate (gate_node != "intake").
    _hitl = snap.get("pending_hitl") or {}
    assert _hitl.get("gate_node") != "intake", (
        f"P3(a): prose --repo must not surface intake HITL; got pending_hitl={_hitl!r}"
    )
    kinds = [k for (k, _) in events]
    assert "intake.repo_flag_like_token_ignored" in kinds, (
        f"P3(a): intake.repo_flag_like_token_ignored trace must be emitted; got {kinds}"
    )


def test_p3_tail_position_unknown_raises():
    """P3(b): '--repo not-a-real-repo' in the tail zone of a long string must
    raise plain ValueError (not RepoFlagIgnored) — typo protection preserved.

    Tail zone = m.start() >= max(0, len-120). With a 150-char prefix + short
    suffix the --repo match lands in the last 120 chars → explicit → ValueError.
    """
    from hydra_core.repo_registry import parse_repo_arg, RepoFlagIgnored
    # Long prefix puts --repo in the final 120 chars of a >120-char string.
    goal = "x" * 150 + " fix the tests --repo not-a-real-repo"
    with pytest.raises(ValueError) as exc_info:
        parse_repo_arg(goal)
    assert not isinstance(exc_info.value, RepoFlagIgnored), (
        "P3(b): tail-position unknown id must raise plain ValueError, not RepoFlagIgnored"
    )


def test_p3_known_id_mid_prose_is_explicit():
    """P3(c): '--repo mc-test' with a known id is always explicit regardless of
    position — parse_repo_arg returns ('mc-test', cleaned_text)."""
    from hydra_core.repo_registry import parse_repo_arg
    repo_id, rest = parse_repo_arg("use --repo mc-test for this")
    assert repo_id == "mc-test", f"P3(c): expected 'mc-test', got {repo_id!r}"
    assert "--repo" not in rest
    assert "mc-test" not in rest


def test_p3_fleet_mid_prose_unknown_token_no_intake_hitl(tmp_path, monkeypatch):
    """P3(d): a mid-prose '--repos <unknown>' in a long goal (>120 chars from end)
    must NOT surface an intake HITL — the fleet intake path must handle
    RepoFlagIgnored before the generic ValueError handler, mirroring the single
    --repo path (P3a). Without the dedicated handler, RepoFlagIgnored (a ValueError
    subclass) would be swallowed by the fleet ValueError branch and surface HITL.
    """
    monkeypatch.setenv("HYDRA_CHECKPOINT_DB", str(tmp_path / "cp_p3d.db"))
    events: list[tuple] = []
    import hydra_core.supervisor as _sv
    monkeypatch.setattr(_sv, "emit_trace",
                        lambda _r, _wf, kind, payload: events.append((kind, payload)))
    sup = build_supervisor(project_root=REPO_ROOT, dispatcher=_NullDispatcher(),
                           plan_only=True)
    wf_id = uuid4()
    # Long goal (>200 chars) with ONE '--repos flag' near the start — well over 120
    # chars from the end, so it is NOT in the tail zone and fires RepoFlagIgnored.
    # The captured token 'fleet' is not an allow-listed repo id. '--repos' does not
    # match the single --repo parser (lookahead requires =/space/end after 'repo'),
    # so intake reaches the fleet parser cleanly.
    goal = (
        "improve how the --repos fleet flag parsing works in intake "
        "so that prose descriptions of CLI flags are not treated as explicit "
        "targets — this affects long-form workflow goal descriptions where operators "
        "naturally discuss cli flag semantics without intending to set target repos"
    )
    assert len(goal) > 200, "precondition: goal must be >200 chars for prose detection"
    assert goal.lower().count("--repos") == 1, "precondition: exactly one --repos token"
    initial = HydraState(workflow_id=wf_id, root_goal=goal)
    initial.selected_squads = ["executive"]
    snap = sup.invoke(initial, config={"configurable": {"thread_id": str(wf_id)}})

    _hitl = snap.get("pending_hitl") or {}
    assert _hitl.get("gate_node") != "intake", (
        f"P3(d): prose --repos must not surface intake HITL; got pending_hitl={_hitl!r}"
    )
    kinds = [k for (k, _) in events]
    assert "intake.repo_flag_like_token_ignored" in kinds, (
        f"P3(d): intake.repo_flag_like_token_ignored trace must be emitted; got {kinds}"
    )
    # And it must NOT have surfaced the fleet bad-arg trace.
    assert "supervisor.bad_repos_arg" not in kinds, (
        f"P3(d): fleet prose token must not trip supervisor.bad_repos_arg; got {kinds}"
    )
