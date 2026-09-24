"""Hydra#70: the attended judge-pass smoke must be a TRACKED, DETACHED job,
never run synchronously inside the single ``submit_host_result`` MCP call.

Before this fix: ``subprocess.run(timeout=HYDRA_SUBMIT_TIMEOUT_S)`` (the MCP
server's CLI-call budget, default 1800s) killed only the direct CLI child
when a smoke ran past it; the smoke's own process tree (spawned via
``run_text`` inside ``squad_node._run_smoke``, budget
``HYDRA_SMOKE_TIMEOUT_S`` default 2400s) was ORPHANED, the verdict was never
durably recorded to the cursor's terminal state, and the cursor was stuck in
``await_judge`` forever.

After: the judge-pass path records the verdict FIRST, then starts a
detached job (``hydra_core.smoke_job``) and returns ``await_smoke`` promptly.
Polling (via a same-call_key ``submit_host_result`` resubmit, or
``hydra.workflow.step`` -- see ``hydra_core.cli._cmd_attended_step``) reads
the job's result file; a lost job (deadline passed / process vanished) is
classified as an infra failure and its whole process tree is killed, so the
cursor can never wedge in ``await_smoke``.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from hydra_core import host_bridge, smoke_job
from hydra_core.proc import is_pid_alive, kill_process_tree


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
    """Mirrors test_host_bridge.FakeDispatcher (kept local so this module is
    self-contained)."""

    def __init__(self, *, required_cross_vendor=True, can_pass=True,
                 finalize_status="complete", downgraded=False):
        self.calls: list[tuple[str, str, dict, str | None]] = []
        self._required_cross = required_cross_vendor
        self._can_pass = can_pass
        self._finalize_status = finalize_status
        self._downgraded = downgraded

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
            return {"status": "done", "result": {"can_pass": self._can_pass}}
        if tool == "finalize_run":
            return {"status": "done", "result": {
                "effective_status": self._finalize_status,
                "downgraded": self._downgraded}}
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
    """This module tests the ASYNC path explicitly — never let a stray env
    var from the shell inherit into "sync" here."""
    monkeypatch.delenv("HYDRA_ATTENDED_SMOKE_MODE", raising=False)


def _fake_job(monkeypatch, *, calls: list):
    """Monkeypatch smoke_job.start_job to a no-subprocess fake that records
    every invocation (for the no-second-job assertions) and returns a job
    dict pointing at a result file the test controls directly."""
    def _start(cursor_file, *, project_path, stage_id, call_key, launch_token=None):
        calls.append(call_key)
        result_path = str(Path(cursor_file).with_name(
            f"{Path(cursor_file).stem}.fake-result.json"))
        try:
            os.remove(result_path)
        except FileNotFoundError:
            pass
        return {
            "pid": os.getpid(),  # always "alive" for the duration of the test
            "started_at": time.time(),
            "deadline": time.time() + 300,
            "result_path": result_path,
            "log_path": result_path + ".log",
            "call_key": call_key,
            # R3: the fake does not track launch_token (the tests write
            # their own result files directly, without one) -- omitting it
            # here keeps `smoke_job._result_matches_launch_token`'s legacy
            # (no-token-recorded) fallback path active, matching these
            # tests' pre-existing unconditional-trust behaviour.
        }
    monkeypatch.setattr(smoke_job, "start_job", _start)


# --------------------------------------------------------------------------- #
# 1. judge pass -> await_smoke quickly, verdict already recorded             #
# --------------------------------------------------------------------------- #

def test_judge_pass_returns_await_smoke_with_verdict_recorded(tmp_path, monkeypatch):
    calls: list[str] = []
    _fake_job(monkeypatch, calls=calls)
    disp = FakeDispatcher(required_cross_vendor=True)
    res, _work_path = _drive_to_await_judge(disp, tmp_path, monkeypatch)

    res2 = host_bridge.submit_host_result(
        disp, cursor_file=res["cursor_path"], call_key=_JUDGE_KEY, result=_JUDGE_PASS)

    assert res2["status"] == "awaiting_host"
    assert res2["state"] == "await_smoke"
    assert disp.count("record_verdict") == 1, "verdict must be recorded BEFORE the job starts"
    assert calls == [_JUDGE_KEY]
    cursor = host_bridge.load_cursor(res["cursor_path"])
    assert cursor.get("verdict_recorded_for") == _JUDGE_KEY
    assert cursor.get("smoke_job") is not None
    assert cursor["pending_action"]["call_key"] == _JUDGE_KEY


# --------------------------------------------------------------------------- #
# 2 / 6. poll before completion -> still pending, no second job, no          #
#        double charge on a same-call_key resubmit                           #
# --------------------------------------------------------------------------- #

def test_poll_before_completion_is_still_pending_and_never_restarts_the_job(
    tmp_path, monkeypatch,
):
    calls: list[str] = []
    _fake_job(monkeypatch, calls=calls)
    disp = FakeDispatcher(required_cross_vendor=True)
    res, _work_path = _drive_to_await_judge(disp, tmp_path, monkeypatch)
    res2 = host_bridge.submit_host_result(
        disp, cursor_file=res["cursor_path"], call_key=_JUDGE_KEY, result=_JUDGE_PASS)
    assert res2["state"] == "await_smoke"
    _record_verdict_calls_before = disp.count("record_verdict")

    # No result file written yet -> resubmitting the SAME call_key is a POLL.
    res3 = host_bridge.submit_host_result(
        disp, cursor_file=res["cursor_path"], call_key=_JUDGE_KEY, result=_JUDGE_PASS)
    assert res3["status"] == "awaiting_host"
    assert res3["state"] == "await_smoke"
    assert calls == [_JUDGE_KEY], "a poll must never start a second job"
    assert disp.count("record_verdict") == _record_verdict_calls_before, (
        "a poll must never double-record the verdict / double-charge"
    )
    assert disp.count("record_smoke_status") == 0


# --------------------------------------------------------------------------- #
# 3. job completes pass -> finalize + merge exactly once                     #
# --------------------------------------------------------------------------- #

def test_job_completes_pass_finalizes_and_merges_exactly_once(tmp_path, monkeypatch):
    calls: list[str] = []
    _fake_job(monkeypatch, calls=calls)
    disp = FakeDispatcher(required_cross_vendor=True)
    res, work_path = _drive_to_await_judge(disp, tmp_path, monkeypatch)
    res2 = host_bridge.submit_host_result(
        disp, cursor_file=res["cursor_path"], call_key=_JUDGE_KEY, result=_JUDGE_PASS)
    cursor = host_bridge.load_cursor(res["cursor_path"])
    job = cursor["smoke_job"]

    # Simulate the detached job finishing with a pass.
    Path(job["result_path"]).write_text(
        json.dumps({"status": "pass", "reason": "fake smoke pass",
                   "finished_at": time.time()}), encoding="utf-8")

    res3 = host_bridge.submit_host_result(
        disp, cursor_file=res["cursor_path"], call_key=_JUDGE_KEY, result=_JUDGE_PASS)
    assert res3["status"] == "complete", res3
    assert disp.count("record_smoke_status") == 1
    assert disp.count("finalize_run") == 1

    # Merged into the base repo (README.md's repo, one dir up from worktree).
    repo_root = Path(work_path).parents[1] if "wt-root" in str(work_path) else None
    cursor2 = host_bridge.load_cursor(res["cursor_path"])
    assert cursor2["smoke_status"] == "pass"
    assert cursor2.get("smoke_job") is None, "smoke_job must be cleared once resolved"

    # A second poll (same call_key) after the cursor is terminal must be a
    # cheap idempotent no-op, not a second finalize/merge.
    res4 = host_bridge.submit_host_result(
        disp, cursor_file=res["cursor_path"], call_key=_JUDGE_KEY, result=_JUDGE_PASS)
    assert res4["status"] == "complete"
    assert disp.count("finalize_run") == 1, "a repeat submit must never re-finalize"


# --------------------------------------------------------------------------- #
# 5. job process vanished without a result -> infra failure, not wedged      #
# --------------------------------------------------------------------------- #

def test_job_process_vanished_without_result_is_infra_failure(tmp_path, monkeypatch):
    calls: list[str] = []

    def _start(cursor_file, *, project_path, stage_id, call_key, launch_token=None):
        calls.append(call_key)
        # An implausibly high, almost-certainly-dead pid; deadline far in
        # the future so the "vanished" branch (not "deadline passed") fires.
        return {
            "pid": 999_999_999,
            "started_at": time.time(),
            "deadline": time.time() + 300,
            "result_path": str(tmp_path / "no-such-result.json"),
            "log_path": str(tmp_path / "no-such.log"),
            "call_key": call_key,
        }
    monkeypatch.setattr(smoke_job, "start_job", _start)

    disp = FakeDispatcher(required_cross_vendor=True)
    res, _work_path = _drive_to_await_judge(disp, tmp_path, monkeypatch)
    res2 = host_bridge.submit_host_result(
        disp, cursor_file=res["cursor_path"], call_key=_JUDGE_KEY, result=_JUDGE_PASS)
    assert res2["state"] == "await_smoke"

    res3 = host_bridge.submit_host_result(
        disp, cursor_file=res["cursor_path"], call_key=_JUDGE_KEY, result=_JUDGE_PASS)
    assert res3["status"] in ("surfaced", "complete_unpersisted"), res3
    cursor = host_bridge.load_cursor(res["cursor_path"])
    assert cursor["smoke_status"] == "infra_error"
    assert "vanished" in cursor["smoke_reason"]


def test_poll_job_tolerates_a_delayed_result_file_after_process_death(
    tmp_path, monkeypatch,
):
    """Regression (flake investigation, Hydra#70 follow-up): `is_pid_alive`
    (a process-table probe) and the result file's `os.replace` rename (a
    filesystem visibility event) are two INDEPENDENT observations of the
    same child's exit, made through two different OS channels. Reproduced
    live under synthetic CPU load (10 busy processes, 20x stress loop): the
    process can be observed dead a hair before the result file it wrote a
    moment earlier becomes visible to this process's `Path.exists()`. A
    poller that classified "not alive + no result file yet" as "vanished"
    on the FIRST check would lose a job that actually succeeded.

    This pins the fix directly (no real subprocess needed): `is_pid_alive`
    reports the process as already dead from the very first poll, while the
    result file is written by a background thread shortly afterward
    (comfortably inside the grace window) -- `poll_job` must still return
    the real (passing) result, not `infra_error`/"vanished". Reverting the
    grace-retry loop in `hydra_core.smoke_job.poll_job` makes this fail.
    """
    import threading

    result_path = tmp_path / "delayed-result.json"
    job = {
        "pid": 4_242_424,  # never actually alive -- is_pid_alive is stubbed anyway
        "started_at": time.time(),
        "deadline": time.time() + 300,
        "result_path": str(result_path),
        "log_path": str(tmp_path / "delayed.log"),
        "call_key": "judge-0",
    }
    monkeypatch.setattr(smoke_job, "is_pid_alive", lambda pid: False)
    killed: list[int] = []
    monkeypatch.setattr(smoke_job, "kill_process_tree",
                        lambda pid, **k: killed.append(pid))

    def _delayed_write():
        time.sleep(0.4)  # well inside the ~2s grace window
        result_path.write_text(
            json.dumps({"status": "pass", "reason": "delayed but real",
                       "finished_at": time.time()}),
            encoding="utf-8")

    threading.Thread(target=_delayed_write, daemon=True).start()

    result = smoke_job.poll_job(job)
    assert result is not None, "poll_job gave up before the delayed write landed"
    assert result["status"] == "pass", result
    assert not killed, "a job that actually succeeded must not have its tree killed"


# --------------------------------------------------------------------------- #
# 4. job exceeds its deadline -> whole process tree killed                   #
# --------------------------------------------------------------------------- #

def test_smoke_job_deadline_kills_whole_process_tree(tmp_path):
    """A fake smoke command that spawns a CHILD that sleeps: after
    ``_run_smoke_tracked`` times out, BOTH the direct child and the
    grandchild it spawned must be dead (Windows: ``taskkill /T /F`` reaches
    the whole subtree; POSIX: the process group)."""
    project = tmp_path / "proj"
    project.mkdir()
    pid_file = tmp_path / "pids.json"
    script = tmp_path / "spawn_grandchild.py"
    script.write_text(
        "import json, os, subprocess, sys, time\n"
        "gc = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
        f"with open(r{str(pid_file)!r}, 'w') as f:\n"
        "    json.dump({'parent': os.getpid(), 'child': gc.pid}, f)\n"
        "time.sleep(120)\n",
        encoding="utf-8",
    )
    (project / ".harness").mkdir()
    (project / ".harness" / "smoke_cmd.json").write_text(
        json.dumps({"cmd": [sys.executable, str(script)]}), encoding="utf-8")

    status, reason = smoke_job._run_smoke_tracked(str(project), "s1", timeout_s=2)
    assert status == "infra_error"
    assert "timed out" in reason

    # Give the pid file (written near-instantly by the script) a moment to
    # land, then confirm BOTH pids are gone.
    deadline = time.time() + 10
    pids = None
    while time.time() < deadline:
        if pid_file.exists():
            try:
                pids = json.loads(pid_file.read_text(encoding="utf-8"))
                break
            except json.JSONDecodeError:
                pass
        time.sleep(0.2)
    assert pids is not None, "the spawn script never wrote its pid file"

    # Kill enforcement already ran inside _run_smoke_tracked; allow a short
    # grace period for the OS to reap.
    deadline = time.time() + 10
    while time.time() < deadline and (is_pid_alive(pids["parent"])
                                      or is_pid_alive(pids["child"])):
        time.sleep(0.3)
    assert not is_pid_alive(pids["parent"]), "the direct smoke child survived the kill"
    assert not is_pid_alive(pids["child"]), "the grandchild survived the kill"


def test_kill_process_tree_reaches_grandchild(tmp_path):
    """Lower-level unit: ``kill_process_tree`` on a parent's pid also kills a
    grandchild the parent spawned, without ``_run_smoke_tracked`` in the
    loop."""
    pid_file = tmp_path / "pids2.json"
    script = tmp_path / "spawn_grandchild2.py"
    script.write_text(
        "import json, os, subprocess, sys, time\n"
        "gc = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
        f"with open(r{str(pid_file)!r}, 'w') as f:\n"
        "    json.dump({'parent': os.getpid(), 'child': gc.pid}, f)\n"
        "time.sleep(120)\n",
        encoding="utf-8",
    )
    from hydra_core.proc import detached_popen_kwargs
    proc = subprocess.Popen([sys.executable, str(script)], **detached_popen_kwargs())
    try:
        deadline = time.time() + 10
        pids = None
        while time.time() < deadline:
            if pid_file.exists():
                try:
                    pids = json.loads(pid_file.read_text(encoding="utf-8"))
                    break
                except json.JSONDecodeError:
                    pass
            time.sleep(0.2)
        assert pids is not None

        kill_process_tree(proc.pid)

        deadline = time.time() + 10
        while time.time() < deadline and (is_pid_alive(pids["parent"])
                                          or is_pid_alive(pids["child"])):
            time.sleep(0.3)
        assert not is_pid_alive(pids["parent"])
        assert not is_pid_alive(pids["child"])
    finally:
        try:
            proc.kill()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# recover_stalled_stage also routes stalled_infra -> await_smoke job         #
# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
# Hydra#70 follow-up (cross-vendor judge finding): start_job's own           #
# spawn/setup failures must never raise -- they must resolve to a durably    #
# persisted infra_error the very first time the job is polled.               #
# --------------------------------------------------------------------------- #

def test_start_job_popen_failure_never_raises_and_writes_infra_error(
    tmp_path, monkeypatch,
):
    def _raise_popen(*_a, **_kw):
        raise OSError("no such interpreter: fake-boom")
    monkeypatch.setattr(smoke_job.subprocess, "Popen", _raise_popen)

    cursor_file = tmp_path / "cursor.json"
    cursor_file.write_text("{}", encoding="utf-8")

    job = smoke_job.start_job(
        cursor_file, project_path=str(tmp_path), stage_id="s1", call_key="judge-0")

    assert job["pid"] is None
    assert job["deadline"] < time.time(), "a never-spawned job must resolve on the first poll"
    assert Path(job["result_path"]).exists(), "the infra_error result must be persisted immediately"
    written = json.loads(Path(job["result_path"]).read_text(encoding="utf-8"))
    assert written["status"] == "infra_error"
    assert "fake-boom" in written["reason"]

    result = smoke_job.poll_job(job)
    assert result is not None
    assert result["status"] == "infra_error"
    assert "fake-boom" in result["reason"]


def test_start_job_log_open_failure_never_raises_and_writes_infra_error(
    tmp_path, monkeypatch,
):
    """A permission error creating the job's log FILE (not the subprocess
    itself) must be caught the same way as a Popen failure."""
    cursor_file = tmp_path / "cursor.json"
    cursor_file.write_text("{}", encoding="utf-8")
    log_path = smoke_job.job_paths(cursor_file, "judge-0")["log_path"]

    real_open = open

    def _raise_open(path, *a, **kw):
        if str(path) == log_path:
            raise PermissionError(f"permission denied: {path}")
        return real_open(path, *a, **kw)
    monkeypatch.setattr("builtins.open", _raise_open)

    job = smoke_job.start_job(
        cursor_file, project_path=str(tmp_path), stage_id="s1", call_key="judge-0")

    assert job["pid"] is None
    written = json.loads(Path(job["result_path"]).read_text(encoding="utf-8"))
    assert written["status"] == "infra_error"
    assert "permission denied" in written["reason"]

    result = smoke_job.poll_job(job)
    assert result is not None
    assert result["status"] == "infra_error"
    assert "permission denied" in result["reason"]


def test_start_job_result_write_failure_falls_back_to_spawn_error(
    tmp_path, monkeypatch,
):
    """If the spawn fails AND persisting the infra_error result ALSO fails
    (e.g. the same permission error prevents both), start_job must still
    never raise, and poll_job must still resolve immediately -- from
    job["spawn_error"] instead of a result file."""
    def _raise_popen(*_a, **_kw):
        raise OSError("boom-spawn")
    monkeypatch.setattr(smoke_job.subprocess, "Popen", _raise_popen)

    def _raise_replace(*_a, **_kw):
        raise OSError("boom-replace: permission denied writing result")
    monkeypatch.setattr(smoke_job.os, "replace", _raise_replace)

    cursor_file = tmp_path / "cursor.json"
    cursor_file.write_text("{}", encoding="utf-8")

    job = smoke_job.start_job(
        cursor_file, project_path=str(tmp_path), stage_id="s1", call_key="judge-0")

    assert job["pid"] is None
    assert not Path(job["result_path"]).exists(), (
        "the write itself failed -- no result file should have landed")
    assert job.get("spawn_error"), "spawn_error must be set when the write also fails"
    assert "boom-spawn" in job["spawn_error"]

    result = smoke_job.poll_job(job)
    assert result is not None
    assert result["status"] == "infra_error"
    assert "boom-spawn" in result["reason"]


# --------------------------------------------------------------------------- #
# Integration: a spawn failure at the host_bridge call sites (_apply_judge   #
# and recover_stalled_stage) must reach a terminal infra smoke failure       #
# instead of raising past an already-recorded verdict.                       #
# --------------------------------------------------------------------------- #

def test_apply_judge_popen_failure_finalizes_infra_smoke_on_the_same_submit(
    tmp_path, monkeypatch,
):
    """Guidance fix (revision round): a spawn failure must NOT park the
    cursor in await_smoke waiting for a poll of a job that never started --
    it finalizes synchronously, on this SAME submit call."""
    real_popen = subprocess.Popen

    def _raise_popen(cmd, *a, **kw):
        # Only the smoke job's OWN spawn (`-m hydra_core.smoke_job`) must
        # fail here -- the test helpers' own `git` subprocess calls (via the
        # SAME shared `subprocess` module) must keep working normally.
        if isinstance(cmd, (list, tuple)) and "hydra_core.smoke_job" in cmd:
            raise OSError("no interpreter (host-bridge integration)")
        return real_popen(cmd, *a, **kw)
    monkeypatch.setattr(smoke_job.subprocess, "Popen", _raise_popen)

    disp = FakeDispatcher(required_cross_vendor=True)
    res, _work_path = _drive_to_await_judge(disp, tmp_path, monkeypatch)

    res2 = host_bridge.submit_host_result(
        disp, cursor_file=res["cursor_path"], call_key=_JUDGE_KEY, result=_JUDGE_PASS)
    assert res2["status"] in ("surfaced", "complete_unpersisted"), res2
    assert res2["state"] != "await_smoke", (
        "a spawn failure must never park the cursor in await_smoke -- "
        "there is nothing to poll"
    )
    cursor = host_bridge.load_cursor(res["cursor_path"])
    assert cursor["smoke_status"] == "infra_error"
    assert "no interpreter" in cursor["smoke_reason"]
    assert cursor.get("smoke_job") is None, "smoke_job must be cleared once resolved"
    assert disp.count("record_verdict") == 1, "verdict must be recorded exactly once"
    assert disp.count("record_smoke_status") == 1


def test_recover_stalled_stage_popen_failure_finalizes_infra_smoke_on_the_same_call(
    tmp_path, monkeypatch,
):
    """Guidance fix (revision round): recovery must also finalize
    synchronously on a spawn failure, not park in await_smoke for a poll
    that would have nothing to observe."""
    real_popen = subprocess.Popen

    def _raise_popen(cmd, *a, **kw):
        if isinstance(cmd, (list, tuple)) and "hydra_core.smoke_job" in cmd:
            raise OSError("no interpreter (recovery integration)")
        return real_popen(cmd, *a, **kw)
    monkeypatch.setattr(smoke_job.subprocess, "Popen", _raise_popen)

    disp = FakeDispatcher(required_cross_vendor=True)
    res, _work_path = _drive_to_await_judge(disp, tmp_path, monkeypatch)

    cursor = host_bridge.load_cursor(res["cursor_path"])
    cursor["state"] = "stalled_infra"
    cursor["attempt_id"] = "att-1"
    cursor["outcome"] = "pass"
    cursor["pending_verdict_payload"] = {
        "attempt_id": "att-1", "outcome": "pass", "idempotency_token": "recovery-tok-2",
        "judge_producer": "codex", "judge_model_id": None,
        "critique_md": "ok", "score_json": None, "rubric_id": "rfc-2119-normative",
    }
    cursor["pending_action"] = {"call_key": _JUDGE_KEY}
    host_bridge.save_cursor(res["cursor_path"], cursor)

    out = host_bridge.recover_stalled_stage(disp, cursor_file=res["cursor_path"])
    assert out["ok"] is True
    assert out["state"] != "await_smoke", (
        "a spawn failure must never park the cursor in await_smoke -- "
        "there is nothing to poll"
    )
    cursor2 = host_bridge.load_cursor(res["cursor_path"])
    assert cursor2["smoke_status"] == "infra_error"
    assert "no interpreter" in cursor2["smoke_reason"]
    assert disp.count("record_verdict") == 1, "recovery must never re-record the verdict"
    assert disp.count("record_smoke_status") == 1


# --------------------------------------------------------------------------- #
# Revision-round fixes: mkdir failure for the job's result/log dir, process-  #
# tree kill on a post-Popen teardown failure, and the "even the terminal     #
# cursor save fails" last-resort path.                                       #
# --------------------------------------------------------------------------- #

def test_start_job_log_dir_mkdir_failure_never_raises_and_writes_infra_error(
    tmp_path, monkeypatch,
):
    """A failure creating the job's log/result DIRECTORY (not the log file
    itself) must be caught the same way as a Popen failure -- this is
    distinct from ``test_start_job_log_open_failure_...`` above, which
    exercises the log file's ``open()`` call, not ``Path.mkdir``."""
    cursor_file = tmp_path / "cursor.json"
    cursor_file.write_text("{}", encoding="utf-8")

    real_mkdir = Path.mkdir

    def _raise_mkdir(self, *a, **kw):
        if self == Path(smoke_job.job_paths(cursor_file, "judge-0")["log_path"]).parent:
            raise PermissionError(f"permission denied creating dir: {self}")
        return real_mkdir(self, *a, **kw)
    monkeypatch.setattr(Path, "mkdir", _raise_mkdir)

    job = smoke_job.start_job(
        cursor_file, project_path=str(tmp_path), stage_id="s1", call_key="judge-0")

    assert job["pid"] is None
    assert job.get("spawn_error"), "spawn_error must be set on an mkdir failure"
    assert "permission denied" in job["spawn_error"]

    result = smoke_job.poll_job(job)
    assert result is not None
    assert result["status"] == "infra_error"
    assert "permission denied" in result["reason"]


def test_start_job_kills_process_tree_when_teardown_after_popen_raises(
    tmp_path, monkeypatch,
):
    """If ``subprocess.Popen`` actually spawns a process but a LATER step in
    ``start_job`` raises (the log file's context-manager teardown), the
    spawned process must be killed, not reported as "never started" and
    orphaned."""
    real_popen = subprocess.Popen
    spawned: list[subprocess.Popen] = []

    def _spawn_then_record(cmd, *a, **kw):
        proc = real_popen(cmd, *a, **kw)
        spawned.append(proc)
        return proc
    monkeypatch.setattr(smoke_job.subprocess, "Popen", _spawn_then_record)

    class _RaisingFile:
        """A fake log-file handle whose ``__exit__`` raises AFTER Popen has
        already been handed the real file descriptor it needs -- simulates
        a flush/close failure in the ``with open(log_path, "ab") as log_f``
        block, independent of Popen itself."""

        def __init__(self, real_f):
            self._real_f = real_f

        def __enter__(self):
            return self._real_f

        def __exit__(self, *exc_info):
            raise OSError("boom-teardown: disk full flushing log")

        def fileno(self):
            return self._real_f.fileno()

    real_open = open

    def _wrap_open(path, *a, **kw):
        f = real_open(path, *a, **kw)
        if str(path) == smoke_job.job_paths(cursor_file, "judge-0")["log_path"]:
            return _RaisingFile(f)
        return f
    cursor_file = tmp_path / "cursor.json"
    cursor_file.write_text("{}", encoding="utf-8")
    monkeypatch.setattr("builtins.open", _wrap_open)

    killed: list[int] = []
    monkeypatch.setattr(smoke_job, "kill_process_tree",
                        lambda pid, **k: killed.append(pid) or True)

    # This spawns a REAL (short-lived) child -- `python -m hydra_core.smoke_job`
    # against a bare tmp_path with no detectable smoke command, so it exits
    # almost immediately on its own; `kill_process_tree` is monkeypatched
    # above to only RECORD the pid it was asked to kill, not actually kill
    # it, so the assertion below is meaningful even if the child has
    # already exited by the time start_job's except-block runs.
    job = smoke_job.start_job(
        cursor_file, project_path=str(tmp_path), stage_id="s1", call_key="judge-0")

    assert job["pid"] is None
    assert job.get("spawn_error")
    assert "boom-teardown" in job["spawn_error"]
    assert spawned, "Popen must have actually been called"
    assert killed == [spawned[0].pid], (
        "the process that WAS spawned must be killed before reporting "
        "'failed to spawn', not orphaned"
    )
    # Best-effort real cleanup in case kill_process_tree (monkeypatched
    # above to only record, not actually kill) left the child alive.
    try:
        spawned[0].kill()
    except Exception:
        pass


def test_apply_judge_spawn_failure_reraises_when_terminal_cursor_save_also_fails(
    tmp_path, monkeypatch,
):
    """Guidance fix: if start_job's spawn ITSELF fails AND the call site's
    attempt to persist the resulting terminal infra_error cursor outcome
    (save_cursor inside _apply_smoke_and_finalize) also fails, the call
    site must not silently succeed with nothing persisted -- it must emit a
    trace event and re-raise."""
    def _raise_start_job(cursor_file, *, project_path, stage_id, call_key, launch_token=None):
        return {
            "pid": None, "started_at": time.time(), "deadline": time.time() - 1,
            "result_path": str(tmp_path / "no-write.result.json"),
            "log_path": str(tmp_path / "no-write.log"),
            "call_key": call_key,
            "spawn_error": "smoke job failed to spawn: OSError('boom-spawn-2')",
        }
    disp = FakeDispatcher(required_cross_vendor=True)
    res, _work_path = _drive_to_await_judge(disp, tmp_path, monkeypatch)

    monkeypatch.setattr(smoke_job, "start_job", _raise_start_job)

    real_save_cursor = host_bridge.save_cursor
    save_calls = {"n": 0}

    def _raise_save_cursor(path, cursor):
        save_calls["n"] += 1
        # Only fail the SPECIFIC save this test targets -- the terminal
        # smoke-outcome persist inside _apply_smoke_and_finalize, which is
        # uniquely identifiable by "smoke_result_for" having just been
        # stamped onto the cursor immediately before that call. Earlier
        # saves in _apply_judge (the pending_verdict_payload /
        # verdict_recorded_for markers) must succeed normally so the test
        # actually reaches the code path under test instead of tripping an
        # unrelated pre-existing save earlier in the same function.
        if "smoke_result_for" in cursor:
            raise OSError("boom-cursor-save: disk full")
        return real_save_cursor(path, cursor)
    monkeypatch.setattr(host_bridge, "save_cursor", _raise_save_cursor)

    with pytest.raises(OSError, match="boom-cursor-save"):
        host_bridge.submit_host_result(
            disp, cursor_file=res["cursor_path"], call_key=_JUDGE_KEY, result=_JUDGE_PASS)
    assert save_calls["n"] >= 1, "save_cursor must actually have been attempted"

    # Restore the real save_cursor and confirm a trace event documenting the
    # persist failure landed on an INDEPENDENT channel (trace.jsonl), even
    # though the cursor file itself never got the terminal outcome.
    monkeypatch.setattr(host_bridge, "save_cursor", real_save_cursor)
    cursor_after = host_bridge.load_cursor(res["cursor_path"])
    trace_path = host_bridge._telemetry.trace_path(
        Path(cursor_after["project_path"]), cursor_after["workflow_id"])
    assert trace_path.exists(), "a trace event must be emitted even when the cursor save fails"
    trace_lines = trace_path.read_text(encoding="utf-8").splitlines()
    assert any(
        "attended.smoke_job_terminal_persist_failed" in ln and "boom-cursor-save" in ln
        for ln in trace_lines
    ), "the trace must document the spawn reason AND the persist failure"


def test_recover_stalled_stage_starts_a_smoke_job(tmp_path, monkeypatch):
    calls: list[str] = []
    _fake_job(monkeypatch, calls=calls)
    disp = FakeDispatcher(required_cross_vendor=True)
    res, work_path = _drive_to_await_judge(disp, tmp_path, monkeypatch)

    cursor = host_bridge.load_cursor(res["cursor_path"])
    cursor["state"] = "stalled_infra"
    cursor["attempt_id"] = "att-1"
    cursor["outcome"] = "pass"
    cursor["pending_verdict_payload"] = {
        "attempt_id": "att-1", "outcome": "pass", "idempotency_token": "recovery-tok",
        "judge_producer": "codex", "judge_model_id": None,
        "critique_md": "ok", "score_json": None, "rubric_id": "rfc-2119-normative",
    }
    cursor["pending_action"] = {"call_key": _JUDGE_KEY}
    host_bridge.save_cursor(res["cursor_path"], cursor)

    out = host_bridge.recover_stalled_stage(disp, cursor_file=res["cursor_path"])
    assert out["ok"] is True
    assert out["state"] == "await_smoke"
    assert calls, "recovery must start a smoke job rather than blocking inline"
    assert disp.count("record_verdict") == 1


# --------------------------------------------------------------------------- #
# Integration: submit -> await_smoke -> poll -> complete with a REAL         #
# (tiny) smoke command and NO monkeypatching of smoke_job.start_job.         #
# --------------------------------------------------------------------------- #

def test_integration_real_job_submit_poll_complete(tmp_path, monkeypatch):
    disp = FakeDispatcher(required_cross_vendor=True)
    res = _begin_isolated(disp, tmp_path, monkeypatch)
    work_path = res["host_action"]["cwd"]
    _commit_file(work_path, "foo.py", "print('hi')\n")
    (Path(work_path) / ".harness").mkdir(exist_ok=True)
    (Path(work_path) / ".harness" / "smoke_cmd.json").write_text(
        json.dumps({"cmd": [sys.executable, "-c", "import sys; sys.exit(0)"]}),
        encoding="utf-8")
    monkeypatch.setenv("HYDRA_SMOKE_TIMEOUT_S", "60")

    res = host_bridge.submit_host_result(
        disp, cursor_file=res["cursor_path"], call_key="generate-0",
        result={"text": "implemented", "cost_usd": 0.10, "tokens_in": 100,
                "tokens_out": 50, "model": "claude-opus-4-8"})
    assert res["state"] == "await_judge"

    res2 = host_bridge.submit_host_result(
        disp, cursor_file=res["cursor_path"], call_key=_JUDGE_KEY, result=_JUDGE_PASS)
    assert res2["state"] == "await_smoke"

    # Poll (mirrors hydra.workflow.step's poll route -- host_bridge.poll_smoke_job).
    # Derive the wait budget from the JOB'S OWN deadline (+ a fixed grace
    # margin for pytest/host overhead) rather than a fixed guess -- a fixed
    # 30s budget is itself what a prior flake investigation traced a false
    # failure to under heavy system load (the real job stayed well within
    # its own configured budget but pytest's independent 30s clock ran out
    # first). This still fails loudly (not silently extends forever) if the
    # job genuinely never resolves.
    cursor = host_bridge.load_cursor(res["cursor_path"])
    job = cursor.get("smoke_job") or {}
    deadline = float(job.get("deadline") or (time.time() + 120)) + 30
    while time.time() < deadline:
        still_pending = host_bridge.poll_smoke_job(
            disp, cursor, cursor_file=res["cursor_path"])
        if not still_pending:
            break
        time.sleep(0.3)
        cursor = host_bridge.load_cursor(res["cursor_path"])
    else:
        _diag_result_path = job.get("result_path")
        _diag_result = None
        if _diag_result_path and Path(_diag_result_path).exists():
            _diag_result = Path(_diag_result_path).read_text(
                encoding="utf-8", errors="replace")
        pytest.fail(
            "real smoke job never completed within its own deadline + "
            f"grace ({deadline - time.time():.1f}s left)\n"
            f"cursor: {json.dumps(cursor, default=str, indent=2)}\n"
            f"job: {json.dumps(job, default=str, indent=2)}\n"
            f"result_path contents: {_diag_result!r}"
        )

    host_bridge.save_cursor(res["cursor_path"], cursor)
    final = host_bridge.step_result(cursor, res["cursor_path"])
    if final["status"] != "complete":
        pytest.fail(
            f"final: {json.dumps(final, default=str, indent=2)}\n"
            f"cursor: {json.dumps(cursor, default=str, indent=2)}\n"
            f"job: {json.dumps(job, default=str, indent=2)}"
        )
    assert final["smoke_status"] == "pass"
