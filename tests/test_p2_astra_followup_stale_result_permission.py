"""P2-4 (cross-vendor gpt-6-astra, MEDIUM): ``smoke_job.start_job`` removed a
stale result file from a previous run of the SAME (cursor, call_key) pair
via a bare ``try: os.remove(result_path) except FileNotFoundError: pass``
sitting OUTSIDE the guarded spawn/setup ``try`` block. A ``PermissionError``
(e.g. a Windows sharing violation -- another process still has the file
open) propagated straight out of ``start_job`` AFTER the judge verdict was
already durably recorded by the caller, leaving the cursor stuck in
``await_judge`` instead of resolving synchronously as an ``infra_error``
like every other spawn/setup failure.

Fixed: the stale-result (and stale-sidecar) removal now happens INSIDE the
guarded ``try`` block, so any removal failure is caught by the same
``except (OSError, ValueError, subprocess.SubprocessError)`` and resolves
identically to a Popen/mkdir/log-open failure -- ``start_job`` never raises,
and the caller (``host_bridge._apply_judge`` via
``_adopt_or_launch_smoke_job``) finalizes synchronously as an infra smoke
failure on the SAME submit call.
"""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest

from hydra_core import host_bridge, smoke_job


def _git(args, cwd):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                          text=True, check=False)


def _init_repo(path):
    _git(["init"], path)
    _git(["config", "user.email", "t@t.test"], path)
    _git(["config", "user.name", "Test"], path)
    _git(["config", "commit.gpgsign", "false"], path)
    (path / "README.md").write_text("base\n", encoding="utf-8")
    _git(["add", "-A"], path)
    _git(["commit", "-m", "base", "--no-verify"], path)


def _commit_file(work_path, rel, content, message="engineer commit"):
    p = Path(work_path) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    _git(["add", "-A"], work_path)
    _git(["-c", "user.email=e@e.test", "-c", "user.name=Engineer",
          "commit", "-m", message, "--no-verify"], work_path)


class FakeDispatcher:
    def __init__(self, *, required_cross_vendor=True):
        self.calls: list[tuple[str, str, dict, str | None]] = []
        self._required_cross = required_cross_vendor

    def call_mcp(self, server, tool, args, squad_id=None):
        self.calls.append((server, tool, dict(args), squad_id))
        if tool == "start_stage":
            return {"status": "done", "result": {"stage_id": "stage-1"}}
        if tool == "record_attempt":
            return {"status": "done", "result": {"attempt_id": "att-1"}}
        if tool == "gate_eligible_judges":
            return {"status": "done", "result": {
                "required_cross_vendor": self._required_cross,
                "rubric_id": "rfc-2119-normative"}}
        if tool == "get_stage_finalize_readiness":
            return {"status": "done", "result": {"can_pass": True}}
        if tool == "finalize_run":
            return {"status": "done", "result": {
                "effective_status": "complete", "downgraded": False}}
        return {"status": "done", "result": {}}

    def count(self, tool):
        return sum(1 for _s, t, _a, _q in self.calls if t == tool)


def _begin_isolated(disp, tmp_path, monkeypatch, **kw):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    monkeypatch.setenv("HYDRA_WORKTREE_ROOT", str(tmp_path / "wt-root"))
    return host_bridge.begin_stage(
        disp, workflow_id="wf-1", run_id="run-1",
        project_path=str(repo), request_text="implement the thing",
        project_root=str(repo), **kw)


def _drive_to_await_judge(disp, tmp_path, monkeypatch):
    res = _begin_isolated(disp, tmp_path, monkeypatch)
    work_path = res["host_action"]["cwd"]
    _commit_file(work_path, "foo.py", "print('hi')\n")
    res = host_bridge.submit_host_result(
        disp, cursor_file=res["cursor_path"], call_key="generate-0",
        result={"text": "implemented the thing", "cost_usd": 0.10,
                "tokens_in": 100, "tokens_out": 50, "model": "claude-opus-4-8"})
    assert res["state"] == "await_judge", res
    return res, work_path


_JUDGE_PASS = {"outcome": "pass", "critique_md": "looks good",
              "judge_producer": "codex", "cost_usd": 0.05}
_JUDGE_KEY = "judge-run-1-stage-1-att-1-0"


@pytest.fixture(autouse=True)
def _default_async(monkeypatch):
    monkeypatch.delenv("HYDRA_ATTENDED_SMOKE_MODE", raising=False)


def test_start_job_permission_error_removing_stale_result_never_raises(
    tmp_path, monkeypatch,
):
    """Unit-level: start_job must resolve to a spawn_error, never raise, when
    removing a stale result file hits a PermissionError."""
    cursor_file = tmp_path / "cursor.json"
    cursor_file.write_text("{}", encoding="utf-8")
    # A stale result genuinely exists from "a previous run" -- irrelevant to
    # whether os.remove raises (it's monkeypatched unconditionally below),
    # but keeps the repro realistic.
    result_path = smoke_job.job_paths(cursor_file, "judge-0")["result_path"]
    Path(result_path).parent.mkdir(parents=True, exist_ok=True)
    Path(result_path).write_text("{}", encoding="utf-8")

    def _raise_remove(path):
        raise PermissionError(f"sharing violation: {path}")
    monkeypatch.setattr(smoke_job.os, "remove", _raise_remove)

    # Must not raise.
    job = smoke_job.start_job(
        cursor_file, project_path=str(tmp_path), stage_id="s1", call_key="judge-0")

    assert job["pid"] is None
    assert job.get("spawn_error"), (
        "P2-4: a PermissionError removing the stale result must resolve as "
        "a normal spawn_error, exactly like every other setup failure"
    )
    assert "sharing violation" in job["spawn_error"]

    result = smoke_job.poll_job(job)
    assert result is not None
    assert result["status"] == "infra_error", result


def test_judge_pass_submit_with_stale_result_permission_error_finalizes_synchronously(
    tmp_path, monkeypatch,
):
    """Integration: a judge-pass submit whose stale-result removal hits a
    PermissionError must finalize synchronously as infra_error on THIS same
    submit call -- never leave the cursor parked in await_judge (or
    await_smoke, which would have nothing to poll)."""
    disp = FakeDispatcher(required_cross_vendor=True)
    res, _work_path = _drive_to_await_judge(disp, tmp_path, monkeypatch)

    def _raise_remove(path):
        raise PermissionError(f"sharing violation: {path}")
    monkeypatch.setattr(smoke_job.os, "remove", _raise_remove)

    res2 = host_bridge.submit_host_result(
        disp, cursor_file=res["cursor_path"], call_key=_JUDGE_KEY, result=_JUDGE_PASS)

    assert res2["state"] not in ("await_judge", "await_smoke"), (
        f"P2-4: a stale-result PermissionError must never leave the cursor "
        f"parked at await_judge/await_smoke; got state={res2.get('state')!r}"
    )
    cursor = host_bridge.load_cursor(res["cursor_path"])
    assert cursor["smoke_status"] == "infra_error"
    assert "sharing violation" in cursor["smoke_reason"]
    assert disp.count("record_verdict") == 1, "the verdict must be recorded exactly once"
