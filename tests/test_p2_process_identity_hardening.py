"""Cross-vendor judge FAIL (regression_safety=0, security_hygiene=0) on the
merged P2-2/P2-3 attended-loop work: both the sidecar smoke-child kill
(``smoke_job._kill_recorded_smoke_child``) and the worker-pid deadline-kill
(``smoke_job.poll_job``), plus P2-3's sidecar-pid-alive adoption
(``host_bridge._adopt_or_launch_smoke_job``), acted on a RECORDED pid after
only checking ``is_pid_alive`` -- which only proves SOME process currently
holds that pid, never that it is the SAME process instance that was
originally recorded. A pid is a small, OS-recycled integer (very aggressively
reused on Windows); once the real process exits, a completely unrelated
LATER process can be handed the identical pid.

Fixed: every kill/adopt decision against a recorded pid now also verifies
durable process identity (``hydra_core.proc.process_identity`` /
``is_same_process`` -- the creation-time-derived identity captured
alongside every recorded pid at spawn time) before acting. A live-but-
mismatched (or unverifiable) pid is refused, never killed/adopted as if it
were the recorded process. The P2-2 sidecar is also retired on normal smoke
completion so a later cleanup pass has nothing stale to act on, and a
"launching" reservation with no evidence yet gets a short bounded poll
before being declared lost (rather than a single presence/absence check),
so a genuinely-alive-but-slow worker is not falsely finalized as lost.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from hydra_core import host_bridge, smoke_job
from hydra_core.proc import is_same_process, process_identity


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


# --------------------------------------------------------------------------- #
# 0. process_identity / is_same_process -- unit-level sanity                  #
# --------------------------------------------------------------------------- #

def test_is_same_process_true_for_this_running_process():
    ident = process_identity(__import__("os").getpid())
    assert ident is not None
    assert is_same_process(__import__("os").getpid(), ident)


def test_is_same_process_false_when_identity_never_captured():
    assert is_same_process(__import__("os").getpid(), None) is False


# --------------------------------------------------------------------------- #
# 1. identity mismatch -> _kill_recorded_smoke_child refuses to kill          #
# --------------------------------------------------------------------------- #

def test_kill_recorded_smoke_child_refuses_on_identity_mismatch(tmp_path):
    """A REAL, still-alive process is recorded in the sidecar with a
    DELIBERATELY WRONG ``pid_identity`` (simulating pid reuse -- the sidecar
    names a pid the OS has since handed to a different process than the one
    that originally wrote it). ``_kill_recorded_smoke_child`` must refuse to
    kill it. Revert the identity check (fold it back to a bare
    ``is_pid_alive`` gate) and this test kills the live process, which this
    assertion catches directly."""
    victim = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        sidecar_path = tmp_path / "job.smokepid.json"
        sidecar_path.write_text(json.dumps({
            "pid": victim.pid,
            # Wrong on purpose -- a real identity value, just not this
            # process's. `process_identity` for pid 1 is unreachable in this
            # test process anyway; using a fabricated-but-well-typed
            # mismatched value pins the mismatch behavior deterministically.
            "pid_identity": -1,
        }), encoding="utf-8")

        job = {"sidecar_path": str(sidecar_path)}
        smoke_job._kill_recorded_smoke_child(job)

        # The victim must still be alive -- the mismatched identity refused
        # the kill.
        time.sleep(0.3)
        assert victim.poll() is None, (
            "identity mismatch must refuse the kill, but the victim process "
            "was killed"
        )
    finally:
        victim.kill()
        victim.wait(timeout=10)


def test_poll_job_deadline_kill_refuses_on_worker_identity_mismatch(monkeypatch, tmp_path):
    """Same hardening on the OTHER recorded-pid kill path: the worker-pid
    deadline-kill inside ``poll_job``. A job dict names a REAL, alive pid as
    its worker, but with a mismatched ``pid_identity`` -- `kill_process_tree`
    must never be called for it. Revert the identity gate on this path (call
    `kill_process_tree(pid)` unconditionally again) and this test fails."""
    victim = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        killed: list[int] = []
        monkeypatch.setattr(smoke_job, "kill_process_tree",
                            lambda pid, **k: killed.append(pid))
        monkeypatch.setattr(smoke_job, "is_pid_alive", lambda pid: True)

        job = {
            "pid": victim.pid,
            "pid_identity": -1,  # deliberately wrong
            "started_at": time.time() - 10,
            "deadline": time.time() - 1,  # already past -> deadline-kill path
            "result_path": str(tmp_path / "no-such-result.json"),
            "sidecar_path": str(tmp_path / "no-such-sidecar.json"),
            "log_path": str(tmp_path / "no-such.log"),
        }
        result = smoke_job.poll_job(job)
        assert result is not None
        assert result["status"] == "infra_error"
        assert victim.pid not in killed, (
            "a mismatched worker identity must refuse the deadline-kill, "
            f"but kill_process_tree was called with pids {killed}"
        )
    finally:
        victim.kill()
        victim.wait(timeout=10)


def test_adopt_or_launch_smoke_job_refuses_sidecar_pid_alive_on_identity_mismatch(
    tmp_path, monkeypatch,
):
    """P2-3's ``sidecar_pid_alive`` adoption branch: a live-but-mismatched
    sidecar pid must never be adopted as this stage's smoke job -- it must
    resolve as lost (``spawn_error``) instead, exactly like no evidence at
    all. Revert the identity check on this adoption branch and this test's
    assertion on ``spawn_error`` fails (it would instead adopt the victim's
    pid as this job's "smoke pid")."""
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

    victim = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        Path(paths["sidecar_path"]).parent.mkdir(parents=True, exist_ok=True)
        Path(paths["sidecar_path"]).write_text(json.dumps({
            "pid": victim.pid,
            "pid_identity": -1,  # deliberately wrong
        }), encoding="utf-8")

        monkeypatch.setattr(host_bridge, "_ADOPT_LOST_GRACE_RETRIES", 0)

        job = host_bridge._adopt_or_launch_smoke_job(
            cursor, cursor_file=res["cursor_path"], call_key=_JUDGE_KEY,
            work_path=_work_path, stage_id="stage-1")

        assert job.get("adopted") != "sidecar_pid_alive"
        assert job.get("spawn_error"), (
            "a mismatched sidecar pid must resolve as lost (spawn_error), "
            f"got {job!r}"
        )
    finally:
        victim.kill()
        victim.wait(timeout=10)


# --------------------------------------------------------------------------- #
# 2. sidecar cleared after normal completion -> later kill-helper is a       #
#    safe no-op                                                              #
# --------------------------------------------------------------------------- #

def test_sidecar_cleared_after_normal_smoke_completion(tmp_path):
    """(C) ``_run_smoke_tracked`` must retire the sidecar once the smoke
    child it describes exits normally -- a LATER call to
    ``_kill_recorded_smoke_child`` against the same job dict then has
    nothing to act on (belt-and-suspenders with the identity check). Revert
    the sidecar-clearing call in ``_run_smoke_tracked`` and the sidecar file
    still exists after a normal completion, which this test's first
    assertion catches directly."""
    project = tmp_path / "proj"
    project.mkdir()
    (project / ".harness").mkdir()
    (project / ".harness" / "smoke_cmd.json").write_text(
        json.dumps({"cmd": [sys.executable, "-c", "import sys; sys.exit(0)"]}),
        encoding="utf-8")
    sidecar_path = tmp_path / "job.smokepid.json"

    status, _reason = smoke_job._run_smoke_tracked(
        str(project), "s1", timeout_s=30, sidecar_path=str(sidecar_path))
    assert status == "pass"

    assert not sidecar_path.exists(), (
        "the sidecar must be retired after the smoke child exits normally, "
        "but it still exists on disk"
    )

    # Belt-and-suspenders: even if some caller still holds a job dict
    # pointing at this (now-cleared) sidecar path, the kill helper must be a
    # safe no-op -- there is no pid left to (mis)act on.
    smoke_job._kill_recorded_smoke_child({"sidecar_path": str(sidecar_path)})


# --------------------------------------------------------------------------- #
# 3. bounded poll for a still-launching reservation (D)                      #
# --------------------------------------------------------------------------- #

def test_still_launching_reservation_with_matching_worker_not_finalized_lost(
    tmp_path, monkeypatch,
):
    """A "launching" reservation whose worker is genuinely alive but just
    hasn't written its sidecar YET (simulated: the sidecar appears mid-poll,
    inside the bounded grace window) must NOT be finalized as lost. Revert
    the bounded-poll grace window (back to a single presence/absence check)
    and this test's assertion on ``adopted`` fails."""
    import threading

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

    monkeypatch.setattr(host_bridge, "_ADOPT_LOST_GRACE_RETRIES", 8)
    monkeypatch.setattr(host_bridge, "_ADOPT_LOST_GRACE_INTERVAL_S", 0.1)

    worker = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        def _delayed_sidecar_write():
            time.sleep(0.35)  # inside the ~0.8s grace window configured above
            Path(paths["sidecar_path"]).parent.mkdir(parents=True, exist_ok=True)
            Path(paths["sidecar_path"]).write_text(json.dumps({
                "pid": worker.pid,
                "pid_identity": process_identity(worker.pid),
            }), encoding="utf-8")

        threading.Thread(target=_delayed_sidecar_write, daemon=True).start()

        job = host_bridge._adopt_or_launch_smoke_job(
            cursor, cursor_file=res["cursor_path"], call_key=_JUDGE_KEY,
            work_path=_work_path, stage_id="stage-1")

        assert job.get("adopted") == "sidecar_pid_alive", (
            "a worker that writes its sidecar within the bounded grace "
            f"window must be adopted, not finalized lost; got {job!r}"
        )
        assert not job.get("spawn_error")
    finally:
        worker.kill()
        worker.wait(timeout=10)


def test_still_launching_reservation_genuinely_gone_worker_is_finalized_lost(
    tmp_path, monkeypatch,
):
    """The counterpart: a "launching" reservation that NEVER produces a
    result file or a live sidecar (even across the bounded grace window)
    must still resolve as lost -- the grace window bounds the wait, it does
    not wait forever."""
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

    monkeypatch.setattr(host_bridge, "_ADOPT_LOST_GRACE_RETRIES", 2)
    monkeypatch.setattr(host_bridge, "_ADOPT_LOST_GRACE_INTERVAL_S", 0.05)

    job = host_bridge._adopt_or_launch_smoke_job(
        cursor, cursor_file=res["cursor_path"], call_key=_JUDGE_KEY,
        work_path=_work_path, stage_id="stage-1")

    assert job.get("spawn_error"), (
        f"a genuinely gone worker (no result, no sidecar ever) must resolve "
        f"as lost after the bounded grace window; got {job!r}"
    )
