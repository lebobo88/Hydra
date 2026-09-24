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
# 3. non-blocking startup-bound handling for a still-launching reservation   #
#    (D, revise-round-2)                                                     #
# --------------------------------------------------------------------------- #

def _seed_launching_reservation(host_bridge, res, tmp_path):
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
    return cursor, paths


def test_still_launching_reservation_with_matching_worker_marker_adopted(
    tmp_path, monkeypatch,
):
    """A "launching" reservation whose worker has written its OWN marker
    (worker-level launch evidence, before it ever gets to the sidecar) must
    be adopted immediately as ``worker_marker_alive`` -- proving the worker
    is alive from the marker alone, without waiting for the sidecar. Revert
    the worker-marker adoption branch and this test's assertion on
    ``adopted`` fails (it would instead see no evidence and fall through to
    the startup-bound "still launching" / lost path)."""
    disp = FakeDispatcher(required_cross_vendor=True)
    res, _work_path = _drive_to_await_judge(disp, tmp_path, monkeypatch)
    cursor, paths = _seed_launching_reservation(host_bridge, res, tmp_path)

    worker = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        Path(paths["marker_path"]).parent.mkdir(parents=True, exist_ok=True)
        Path(paths["marker_path"]).write_text(json.dumps({
            "pid": worker.pid,
            "pid_identity": process_identity(worker.pid),
        }), encoding="utf-8")

        job = host_bridge._adopt_or_launch_smoke_job(
            cursor, cursor_file=res["cursor_path"], call_key=_JUDGE_KEY,
            work_path=_work_path, stage_id="stage-1")

        assert job.get("adopted") == "worker_marker_alive", (
            f"a live, identity-verified worker marker must be adopted "
            f"immediately; got {job!r}"
        )
        assert job.get("pid") == worker.pid
        assert not job.get("spawn_error")
    finally:
        worker.kill()
        worker.wait(timeout=10)


def test_still_launching_reservation_delayed_worker_reports_non_terminal_then_adopts(
    tmp_path, monkeypatch,
):
    """The false-lost repro this fix closes: a worker that cold-starts
    LONGER than the old (insufficient) ~0.45s blocking grace window --
    simulated here as 1.0s -- before it ever writes ANY evidence (marker or
    sidecar). The first poll(s), while still within the startup bound, must
    report non-terminal "still launching" (the reservation stays
    unresolved: no pid, no spawn_error) -- NEVER finalized lost. Once the
    marker appears with a verifying identity, a LATER poll adopts it and the
    job later completes normally via the ordinary result-file path. Revert
    either half of this fix and this test fails: reverting the non-blocking
    startup bound makes the early poll(s) resolve to ``spawn_error``
    immediately; reverting the worker-marker adoption makes the later poll
    never adopt (falls through to sidecar/no-evidence handling)."""
    monkeypatch.setenv("HYDRA_SMOKE_LAUNCH_GRACE_S", "5")
    disp = FakeDispatcher(required_cross_vendor=True)
    res, _work_path = _drive_to_await_judge(disp, tmp_path, monkeypatch)
    cursor, paths = _seed_launching_reservation(host_bridge, res, tmp_path)

    # No marker, no sidecar, no result yet -- the worker "hasn't cold-started
    # far enough" in this simulation. The first poll, well within the 5s
    # startup bound, must be non-terminal.
    job = host_bridge._adopt_or_launch_smoke_job(
        cursor, cursor_file=res["cursor_path"], call_key=_JUDGE_KEY,
        work_path=_work_path, stage_id="stage-1")
    assert job.get("state") == "launching", (
        f"a reservation with no evidence yet, still within the startup "
        f"bound, must stay non-terminal ('launching'), never resolve lost "
        f"or adopted on this poll; got {job!r}"
    )
    assert not job.get("pid")
    assert not job.get("spawn_error")

    # `poll_smoke_job` (the real caller) must report "still pending" (True)
    # for this exact shape, without blocking.
    cursor["smoke_job"] = job
    started = time.monotonic()
    still_pending = host_bridge.poll_smoke_job(
        disp, cursor, cursor_file=res["cursor_path"], workflow_terminal=False)
    elapsed = time.monotonic() - started
    assert still_pending is True
    assert elapsed < 1.0, (
        f"poll_smoke_job must be NON-blocking for a still-launching "
        f"reservation within the startup bound; took {elapsed:.2f}s"
    )

    # Now the worker "cold-starts past" 1.0s (longer than the old ~0.45s
    # blocking grace window) and writes its marker.
    worker = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        time.sleep(1.0)
        Path(paths["marker_path"]).parent.mkdir(parents=True, exist_ok=True)
        Path(paths["marker_path"]).write_text(json.dumps({
            "pid": worker.pid,
            "pid_identity": process_identity(worker.pid),
        }), encoding="utf-8")

        job2 = host_bridge._adopt_or_launch_smoke_job(
            cursor, cursor_file=res["cursor_path"], call_key=_JUDGE_KEY,
            work_path=_work_path, stage_id="stage-1")
        assert job2.get("adopted") == "worker_marker_alive", (
            f"a worker that eventually writes its marker within the "
            f"startup bound must be adopted on a LATER poll, not lost; "
            f"got {job2!r}"
        )
        assert job2.get("pid") == worker.pid
        assert not job2.get("spawn_error")

        # And the job "later completes normally" via the ordinary result
        # path -- write the result file (as the worker itself would) and
        # confirm poll_job resolves it as a normal pass, not an infra kill.
        Path(paths["result_path"]).parent.mkdir(parents=True, exist_ok=True)
        Path(paths["result_path"]).write_text(json.dumps({
            "status": "pass", "reason": "ok", "finished_at": time.time(),
        }), encoding="utf-8")
        result = smoke_job.poll_job(job2)
        assert result is not None and result["status"] == "pass"
    finally:
        worker.kill()
        worker.wait(timeout=10)


def test_still_launching_reservation_past_startup_bound_finalized_lost_once(
    tmp_path, monkeypatch,
):
    """A "launching" reservation OLDER than the (tiny, env-set) startup bound
    with no marker/sidecar/result at all must be finalized lost -- exactly
    once, never spawning a second worker for the same call_key. Revert the
    startup-bound check (make it wait forever / never resolve) and this
    test's assertion on ``spawn_error`` fails; revert it the other way (drop
    the bound entirely, always resolve lost) and the companion "still
    launching" test above fails instead -- the two tests are complementary
    proof the bound is applied correctly in both directions."""
    monkeypatch.setenv("HYDRA_SMOKE_LAUNCH_GRACE_S", "0.05")
    disp = FakeDispatcher(required_cross_vendor=True)
    res, _work_path = _drive_to_await_judge(disp, tmp_path, monkeypatch)
    cursor, paths = _seed_launching_reservation(host_bridge, res, tmp_path)
    # Push the reservation's `reserved_at` unambiguously past the tiny bound.
    cursor["smoke_job"]["reserved_at"] = time.time() - 10
    host_bridge.save_cursor(res["cursor_path"], cursor)

    job = host_bridge._adopt_or_launch_smoke_job(
        cursor, cursor_file=res["cursor_path"], call_key=_JUDGE_KEY,
        work_path=_work_path, stage_id="stage-1")

    assert job.get("spawn_error"), (
        f"a reservation past the startup bound with no evidence at all must "
        f"resolve as lost; got {job!r}"
    )
    assert "startup bound" in job["spawn_error"]
    assert job.get("state") is None


def test_unverified_worker_marker_not_adopted_lost_past_bound_nothing_killed(
    tmp_path, monkeypatch,
):
    """A worker marker whose identity does NOT verify (pid reused / mismatch)
    must never be adopted -- and, past the startup bound, must still be
    finalized lost, with nothing killed on the unverified pid (this
    function never kills; only a later ``poll_job`` deadline-kill would, and
    it only acts on a job's own recorded ``pid``/``pid_identity``, which an
    unverified marker never sets). Revert the identity verification on the
    marker-adoption branch and this test's assertion on ``adopted`` fails
    (it would wrongly adopt the mismatched marker pid)."""
    monkeypatch.setenv("HYDRA_SMOKE_LAUNCH_GRACE_S", "0.05")
    disp = FakeDispatcher(required_cross_vendor=True)
    res, _work_path = _drive_to_await_judge(disp, tmp_path, monkeypatch)
    cursor, paths = _seed_launching_reservation(host_bridge, res, tmp_path)
    cursor["smoke_job"]["reserved_at"] = time.time() - 10
    host_bridge.save_cursor(res["cursor_path"], cursor)

    victim = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        Path(paths["marker_path"]).parent.mkdir(parents=True, exist_ok=True)
        Path(paths["marker_path"]).write_text(json.dumps({
            "pid": victim.pid,
            "pid_identity": -1,  # deliberately wrong
        }), encoding="utf-8")

        killed: list[int] = []
        monkeypatch.setattr(smoke_job, "kill_process_tree",
                            lambda pid, **k: killed.append(pid))

        job = host_bridge._adopt_or_launch_smoke_job(
            cursor, cursor_file=res["cursor_path"], call_key=_JUDGE_KEY,
            work_path=_work_path, stage_id="stage-1")

        assert job.get("adopted") != "worker_marker_alive", (
            f"an unverified/mismatched worker marker must never be adopted; "
            f"got {job!r}"
        )
        assert job.get("spawn_error"), (
            f"past the startup bound with only an unverified marker, the "
            f"reservation must resolve as lost; got {job!r}"
        )
        assert victim.pid not in killed, (
            "nothing must be killed on the unverified marker pid, but "
            f"kill_process_tree was called with pids {killed}"
        )
    finally:
        victim.kill()
        victim.wait(timeout=10)


def test_real_worker_writes_marker_before_anything_slow(tmp_path, monkeypatch):
    """The REAL worker entry point (``smoke_job.main``) writes its marker as
    its VERY FIRST action -- before ``_run_smoke_tracked`` even runs, let
    alone finishes. Proven by a slow fake smoke command: while it is still
    running (has not yet exited), the marker must already exist on disk and
    verify against the (still-running) worker process's own pid. Revert the
    marker write to happen AFTER ``_run_smoke_tracked`` (or drop it
    entirely) and this test's assertion inside the polling loop times out /
    fails, since the marker would only appear once the slow smoke command
    (and thus the whole worker) has already finished."""
    project = tmp_path / "proj"
    project.mkdir()
    (project / ".harness").mkdir()
    # A "slow" fake smoke command -- sleeps long enough that a marker write
    # AFTER `_run_smoke_tracked` would not be observable while it is still
    # running.
    (project / ".harness" / "smoke_cmd.json").write_text(
        json.dumps({"cmd": [sys.executable, "-c",
                            "import time; time.sleep(2); import sys; sys.exit(0)"]}),
        encoding="utf-8")

    result_path = tmp_path / "job.result.json"
    marker_path = tmp_path / "job.workerpid.json"
    sidecar_path = tmp_path / "job.smokepid.json"

    worker = subprocess.Popen([
        sys.executable, "-m", "hydra_core.smoke_job",
        "--project-path", str(project), "--stage-id", "s1",
        "--result-path", str(result_path),
        "--sidecar-path", str(sidecar_path),
        "--marker-path", str(marker_path),
        "--timeout-s", "30",
    ], cwd=str(Path(__file__).resolve().parent.parent))
    try:
        marker = None
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if marker_path.exists():
                try:
                    marker = json.loads(marker_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    marker = None
                if marker:
                    break
            time.sleep(0.05)

        assert marker is not None, "the worker never wrote its marker in time"
        assert marker.get("pid") == worker.pid
        assert is_same_process(worker.pid, marker.get("pid_identity")), (
            f"the marker's identity must verify against the still-running "
            f"worker process; marker={marker!r}"
        )
        # The slow smoke command (2s sleep) must still be running -- proves
        # the marker was written BEFORE `_run_smoke_tracked` finished, i.e.
        # as the worker's very first action, not after.
        assert not result_path.exists(), (
            "the marker appeared only after the job already finished -- "
            "it must be written as the worker's FIRST action, well before "
            "the slow smoke command completes"
        )
    finally:
        worker.wait(timeout=30)
