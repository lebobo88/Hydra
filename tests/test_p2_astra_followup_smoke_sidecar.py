"""P2-2 (cross-vendor gpt-6-astra, MEDIUM): the smoke worker
(``python -m hydra_core.smoke_job``) launches the smoke command in its OWN
detached process group, but the job record on the cursor only ever carried
the WORKER's pid. If the worker dies (killed, crashed) before the smoke
child finishes, ``kill_process_tree(worker_pid)`` cannot reach the smoke
tree once the worker is gone: a Windows ``taskkill /T`` needs a LIVE root to
walk from (it fails once the target pid no longer exists), and a POSIX
``killpg`` targets the WORKER's own process group, not the smoke child's
SEPARATE session/group (``detached_popen_kwargs`` gives the smoke child its
own session too).

Fixed: the worker writes the smoke child's pid (and pgid on POSIX) to a
sidecar file next to the result file immediately after ``Popen`` returns.
``poll_job``'s lost-worker cleanup now kills BOTH the worker tree and the
sidecar-recorded smoke pid/group, and tolerates the sidecar being absent
(worker died before ever writing it).

This test uses a REAL detached worker process (no monkeypatching of
``start_job``/``Popen``) -- it is the only way to reproduce "the worker
itself is gone" as a real OS-level fact, per the task brief.
"""
from __future__ import annotations

import json
import os
import signal
import sys
import time
from pathlib import Path

import pytest

from hydra_core import smoke_job
from hydra_core.proc import is_pid_alive


def _write_sleepy_smoke_cmd(project: Path, *, seconds: int) -> None:
    (project / ".harness").mkdir(parents=True, exist_ok=True)
    (project / ".harness" / "smoke_cmd.json").write_text(
        json.dumps({"cmd": [sys.executable, "-c",
                            f"import time; time.sleep({seconds})"]}),
        encoding="utf-8")


def _kill_worker_directly(pid: int) -> None:
    """Kill ONLY the worker pid itself, exactly like an external OOM-killer
    or operator `kill -9`/`taskkill /PID` (no `/T`) would -- deliberately
    NOT `hydra_core.proc.kill_process_tree`, which is the very mechanism
    this fix works around the limits of."""
    if os.name == "nt":
        import subprocess
        subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                       capture_output=True, timeout=10)
    else:
        os.kill(pid, signal.SIGKILL)


def test_worker_killed_externally_still_kills_smoke_child_via_sidecar(
    tmp_path, monkeypatch,
):
    # Keep the grace window tiny so this test stays fast.
    monkeypatch.setattr(smoke_job, "_VANISHED_GRACE_RETRIES", 3)
    monkeypatch.setattr(smoke_job, "_VANISHED_GRACE_INTERVAL_S", 0.1)

    project = tmp_path / "proj"
    project.mkdir()
    _write_sleepy_smoke_cmd(project, seconds=120)
    cursor_file = tmp_path / "cursor.json"
    cursor_file.write_text("{}", encoding="utf-8")

    job = smoke_job.start_job(
        cursor_file, project_path=str(project), stage_id="s1", call_key="judge-0")
    assert not job.get("spawn_error"), job
    worker_pid = job["pid"]
    assert worker_pid, "a real worker must have been spawned"

    # Wait for the worker to actually launch the smoke child and write the
    # sidecar recording its pid -- this is the fix under test.
    sidecar_path = job["sidecar_path"]
    deadline = time.time() + 15
    sidecar = None
    while time.time() < deadline:
        if Path(sidecar_path).exists():
            try:
                sidecar = json.loads(Path(sidecar_path).read_text(encoding="utf-8"))
                if sidecar.get("pid"):
                    break
            except (OSError, json.JSONDecodeError):
                pass
        time.sleep(0.2)
    assert sidecar is not None and sidecar.get("pid"), (
        "the worker never wrote the smoke-pid sidecar in time"
    )
    smoke_pid = sidecar["pid"]
    assert is_pid_alive(smoke_pid), "the smoke child must be alive before the kill"

    # Kill ONLY the worker -- the smoke child (its own detached session)
    # survives this on its own.
    _kill_worker_directly(worker_pid)
    deadline = time.time() + 10
    while time.time() < deadline and is_pid_alive(worker_pid):
        time.sleep(0.1)
    assert not is_pid_alive(worker_pid), "the worker itself must be dead for this repro"
    assert is_pid_alive(smoke_pid), (
        "the smoke child must still be alive immediately after the worker "
        "alone was killed -- proves it is genuinely orphaned, not already "
        "reaped as a side effect of killing the worker"
    )

    result = smoke_job.poll_job(job)
    assert result is not None, "a dead worker with no result file must resolve immediately"
    assert result["status"] == "infra_error", result

    deadline = time.time() + 10
    while time.time() < deadline and is_pid_alive(smoke_pid):
        time.sleep(0.2)
    assert not is_pid_alive(smoke_pid), (
        "P2-2: poll_job must kill the smoke child recorded in the sidecar "
        "even though the worker that spawned it is already gone"
    )
