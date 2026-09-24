"""R1 (MEDIUM, cross-vendor gpt-6-astra, final re-review of feat/planning-phase
HEAD 0bccdcb): ``hydra_core.smoke_job.poll_job``'s pre-deadline "still
running, poll again" early return trusted a bare ``is_pid_alive(pid)`` with
NO identity check -- if the worker died without writing a result and its pid
was reused by an unrelated process before this poll, the job read as
"alive" and the vanished/lost classification was delayed all the way out to
the deadline (default ~41min) even though the recorded worker is long gone.

Fixed: the liveness decision that gates the early return now verifies
``hydra_core.proc.is_same_process(pid, job.get("pid_identity"))`` the same
way the deadline-kill path already does (5a658a7), with a safe fallback: a
legacy in-flight job whose ``pid_identity`` was never recorded keeps the old
bare-``is_pid_alive`` liveness behaviour for LIVENESS only -- the kill path
independently still requires a verified identity match and simply refuses
to kill an unverifiable pid, exactly as before.

R1 dependency fix (Windows-only, surfaced by making the above the real
liveness gate instead of a bare pid check): ``hydra_core.proc``'s
``_win_process_creation_time`` fed ``is_same_process`` a stale-but-real
identity for a pid that had ALREADY exited a moment earlier -- a
just-terminated pid remains ``OpenProcess``/``GetProcessTimes``-queryable
(with its original, still-matching creation time) for a short window after
exit, before Windows fully reaps it and the pid becomes reusable, even
though ``tasklist``/``is_pid_alive`` has already stopped listing it. Fixed
by also checking ``GetProcessTimes``' own ``exit_time`` output -- non-zero
means the process has already exited, and identity resolution now returns
``None`` (unverifiable) for it exactly like any other dead/invalid pid,
never a stale "still matches" verdict.
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

from hydra_core import smoke_job
from hydra_core.proc import is_same_process, process_identity


def test_poll_job_identity_mismatch_does_not_early_return_pending(
    monkeypatch, tmp_path,
):
    """MUTATION PROOF: a job whose recorded pid is a REAL, currently-alive
    process (simulating pid reuse -- the true worker died and the OS handed
    its old pid to an unrelated process) but whose ``pid_identity`` does NOT
    match must NOT take the early "still pending" return, even though the
    deadline is still well in the future. It must fall through to the
    vanished/grace path and resolve `infra_error` -- and the mismatched pid
    must never be killed. Revert the identity gate on the early-return check
    (go back to a bare ``is_pid_alive(pid)``) and this test's first
    assertion (``result is not None``) fails: the mismatched-but-alive pid
    reads as this job still legitimately running, so `poll_job` returns
    `None` ("keep waiting") instead of resolving it lost."""
    victim = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        killed: list[int] = []
        monkeypatch.setattr(smoke_job, "kill_process_tree",
                            lambda pid, **k: killed.append(pid))

        job = {
            "pid": victim.pid,
            "pid_identity": -1,  # deliberately wrong -- simulates pid reuse
            "started_at": time.time() - 5,
            # Deadline is FAR in the future -- the old bare-is_pid_alive
            # check would have returned None ("still pending") here.
            "deadline": time.time() + 3600,
            "result_path": str(tmp_path / "no-such-result.json"),
            "sidecar_path": str(tmp_path / "no-such-sidecar.json"),
            "marker_path": str(tmp_path / "no-such-marker.json"),
            "log_path": str(tmp_path / "no-such.log"),
        }
        result = smoke_job.poll_job(job)

        assert result is not None, (
            "an identity-mismatched (pid-reused) worker must never be read "
            "as 'still legitimately running' -- poll_job returned None "
            "(pending) instead of resolving it"
        )
        assert result["status"] == "infra_error"

        # The mismatched pid (a real, unrelated live process) must never be
        # killed as if it were the recorded worker.
        time.sleep(0.3)
        assert victim.poll() is None, (
            "an identity mismatch must refuse the kill, but the victim "
            "process was killed"
        )
        assert victim.pid not in killed
    finally:
        victim.kill()
        victim.wait(timeout=10)


def test_poll_job_legacy_job_without_pid_identity_keeps_old_liveness(
    monkeypatch, tmp_path,
):
    """Safe fallback: a legacy in-flight job started before ``pid_identity``
    existed (the field is entirely absent, not just mismatched) must keep
    the OLD bare-``is_pid_alive`` liveness behaviour -- a live, un-expired
    job still returns `None` ("still pending"), never falsely resolved lost
    just because it predates the identity field."""
    worker = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        job = {
            "pid": worker.pid,
            # No "pid_identity" key at all -- a true legacy job record.
            "started_at": time.time() - 5,
            "deadline": time.time() + 3600,
            "result_path": str(tmp_path / "no-such-result.json"),
            "sidecar_path": str(tmp_path / "no-such-sidecar.json"),
            "marker_path": str(tmp_path / "no-such-marker.json"),
            "log_path": str(tmp_path / "no-such.log"),
        }
        result = smoke_job.poll_job(job)
        assert result is None, (
            f"a legacy job (no pid_identity recorded) with a live pid and a "
            f"future deadline must still report 'still pending'; got {result!r}"
        )
    finally:
        worker.kill()
        worker.wait(timeout=10)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only identity codepath")
def test_is_same_process_false_for_a_just_terminated_pid(tmp_path):
    """MUTATION PROOF (Windows-only): revert the ``exit_time`` check added to
    ``hydra_core.proc._win_process_creation_time`` and this test fails --
    ``is_same_process`` would still report ``True`` for a pid that was
    ``taskkill /F``'d moments earlier (a just-terminated process remains
    ``OpenProcess``/``GetProcessTimes``-queryable, with its ORIGINAL,
    still-matching creation time, for a short window before Windows fully
    reaps it), even though ``is_pid_alive`` already reports it gone."""
    victim = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    identity = process_identity(victim.pid)
    assert identity is not None, "must have captured a real identity while alive"
    assert is_same_process(victim.pid, identity), (
        "sanity: identity must verify while the process is genuinely alive"
    )
    try:
        subprocess.run(["taskkill", "/F", "/PID", str(victim.pid)],
                       capture_output=True, timeout=10)
        victim.wait(timeout=10)
    except Exception:
        pass
    assert not is_same_process(victim.pid, identity), (
        "a just-terminated pid must never verify as the SAME process again, "
        "even within the brief window before Windows fully reaps it"
    )
