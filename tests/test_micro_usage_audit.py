"""MU micro-usage audit regression tests (MU7, MU12). See docs/audits/MU-MICRO-USAGE-2026-07-05.md."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
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
