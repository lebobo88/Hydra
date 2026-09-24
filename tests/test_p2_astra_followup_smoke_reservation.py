"""P2-3 (cross-vendor gpt-6-astra, MEDIUM): the judge-pass branch (and
``recover_stalled_stage``'s ``stalled_infra`` smoke start) used to spawn the
detached smoke WORKER before ever saving ``cursor["smoke_job"]``/
``state="await_smoke"`` to disk. A crash between spawn and that save left
the cursor at ``await_judge`` with the verdict already recorded -- a retry
re-entered the judge-pass branch from scratch and spawned a SECOND worker
racing the first for the identical result/log/sidecar paths.

Fixed: a ``state: "launching"`` reservation (with the deterministic paths
``smoke_job.job_paths`` will use) is persisted to disk -- including flipping
``cursor["state"]`` to ``"await_smoke"`` -- BEFORE the worker is ever
spawned. A retry that finds this reservation (whether it lands back in the
judge-pass branch, because the crash happened before the reservation save
even landed, or in the ordinary ``await_smoke`` poll path, because the
reservation DID land) never spawns a second worker: it adopts an
already-live/finished job via the sidecar/result paths, or resolves as a
lost job (``infra_error``), through ``_adopt_or_launch_smoke_job``.
"""
from __future__ import annotations

import json
import subprocess
import sys
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


def _count_popen_calls(monkeypatch):
    """Count every REAL `python -m hydra_core.smoke_job` worker spawn
    attempt (via `smoke_job.subprocess.Popen`), without actually blocking
    this test on a real detached process -- the fake Popen raises
    immediately (an OSError, exactly like `start_job`'s own documented
    spawn-failure class), which `start_job` already turns into a
    `spawn_error` result rather than propagating.
    """
    calls: list[int] = []
    real_popen = subprocess.Popen

    def _fake_popen(cmd, *a, **kw):
        if isinstance(cmd, (list, tuple)) and "hydra_core.smoke_job" in cmd:
            calls.append(1)
            raise OSError("no worker spawn allowed in this test (counting only)")
        return real_popen(cmd, *a, **kw)
    monkeypatch.setattr(smoke_job.subprocess, "Popen", _fake_popen)
    return calls


def test_crash_after_reservation_before_spawn_never_starts_second_worker(
    tmp_path, monkeypatch,
):
    """Simulates a crash AFTER the reservation was saved but BEFORE
    `smoke_job.start_job` ever ran (e.g. the process died between the
    reservation's `save_cursor` and the `start_job` call inside
    `_adopt_or_launch_smoke_job`). A retry (a same-call_key resubmit, which
    now routes through the `await_smoke` poll path since the reservation
    already flipped `cursor["state"]`) must resolve without ever spawning a
    worker -- there is no result file and no sidecar to adopt, so it
    resolves as `infra_error`."""
    disp = FakeDispatcher(required_cross_vendor=True)
    res, _work_path = _drive_to_await_judge(disp, tmp_path, monkeypatch)

    # Manually construct the "crash after reservation, before start_job"
    # cursor shape (exactly what `_adopt_or_launch_smoke_job`'s reservation
    # write leaves on disk). `reserved_at` is backdated well past the (D
    # follow-up) startup bound (`HYDRA_SMOKE_LAUNCH_GRACE_S`, default 60s) --
    # this retry is meant to simulate a long-since-crashed attempt with no
    # adoptable evidence, which must resolve as lost on this single call,
    # not a fresh reservation that would legitimately still be within its
    # startup window.
    cursor = host_bridge.load_cursor(res["cursor_path"])
    paths = smoke_job.job_paths(res["cursor_path"], _JUDGE_KEY)
    cursor["smoke_job"] = {
        "call_key": _JUDGE_KEY, "reserved_at": time.time() - 3600,
        "state": "launching", **paths,
    }
    cursor["state"] = "await_smoke"
    cursor["verdict_recorded_for"] = _JUDGE_KEY
    cursor["pending_action"] = {"call_key": _JUDGE_KEY, "action": "poll_smoke", "poll": True}
    host_bridge.save_cursor(res["cursor_path"], cursor)

    popen_calls = _count_popen_calls(monkeypatch)

    res2 = host_bridge.submit_host_result(
        disp, cursor_file=res["cursor_path"], call_key=_JUDGE_KEY, result=_JUDGE_PASS)

    assert not popen_calls, (
        "P2-3: a retry against an unresolved reservation with no adoption "
        f"evidence must NEVER spawn a worker; Popen was called {len(popen_calls)}x"
    )
    assert res2["state"] not in ("await_judge",), res2
    cursor2 = host_bridge.load_cursor(res["cursor_path"])
    assert cursor2["smoke_status"] == "infra_error"
    assert "no second worker" in cursor2["smoke_reason"] or "lost" in cursor2["smoke_reason"]
    # The verdict must never be re-recorded by this retry.
    assert disp.count("record_verdict") == 0, (
        "this test seeded verdict_recorded_for directly -- record_verdict "
        "must not be called again by the retry"
    )


def test_crash_after_spawn_before_pid_save_adopts_live_worker_no_second_spawn(
    tmp_path, monkeypatch,
):
    """Simulates a crash AFTER `smoke_job.start_job` actually spawned the
    worker (which wrote its sidecar) but BEFORE the caller persisted the
    returned job (with its pid) onto the cursor. A retry must ADOPT the
    live worker via the sidecar instead of spawning a second one."""
    disp = FakeDispatcher(required_cross_vendor=True)
    res, _work_path = _drive_to_await_judge(disp, tmp_path, monkeypatch)

    cursor = host_bridge.load_cursor(res["cursor_path"])
    paths = smoke_job.job_paths(res["cursor_path"], _JUDGE_KEY)
    cursor["smoke_job"] = {
        "call_key": _JUDGE_KEY, "reserved_at": time.time(),
        "state": "launching", **paths,
    }
    cursor["state"] = "await_smoke"
    cursor["verdict_recorded_for"] = _JUDGE_KEY
    cursor["pending_action"] = {"call_key": _JUDGE_KEY, "action": "poll_smoke", "poll": True}
    host_bridge.save_cursor(res["cursor_path"], cursor)

    # Simulate the worker's own sidecar write (it got far enough to spawn
    # and record its smoke child's pid) using a REAL short-lived child
    # process -- deliberately NOT this test process's own pid: a
    # regression that failed to short-circuit adoption and fell through to
    # `poll_job`'s lost-job cleanup would call `kill_process_tree` on
    # whatever pid the sidecar names, and this test must survive that
    # possibility rather than risk killing itself.
    sleeper = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        from hydra_core.proc import process_identity
        Path(paths["sidecar_path"]).parent.mkdir(parents=True, exist_ok=True)
        Path(paths["sidecar_path"]).write_text(
            json.dumps({
                "pid": sleeper.pid,
                # Durable identity fix: production `_write_smoke_sidecar`
                # always records this alongside the pid -- match that shape
                # here so adoption's identity check (which refuses to adopt
                # an unverified pid) can positively verify this really is
                # the sleeper process this test just spawned.
                "pid_identity": process_identity(sleeper.pid),
            }), encoding="utf-8")

        popen_calls = _count_popen_calls(monkeypatch)

        res2 = host_bridge.submit_host_result(
            disp, cursor_file=res["cursor_path"], call_key=_JUDGE_KEY, result=_JUDGE_PASS)

        assert not popen_calls, (
            "P2-3: a retry that can adopt a live worker via the sidecar must "
            f"never spawn a second one; Popen was called {len(popen_calls)}x"
        )
        # Still pending (the adopted job's pid -- the sidecar's sleeper
        # child -- is alive and its deadline is in the future), never a
        # second spawn, never a second verdict.
        assert res2["state"] == "await_smoke", res2
        assert disp.count("record_verdict") == 0

        cursor2 = host_bridge.load_cursor(res["cursor_path"])
        assert cursor2["smoke_job"].get("adopted") == "sidecar_pid_alive"
    finally:
        sleeper.kill()
        sleeper.wait(timeout=10)
