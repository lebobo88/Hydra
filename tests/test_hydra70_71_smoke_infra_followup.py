"""Hydra#70 / #71 follow-up (today's comments, verified against
warerender-gta run_aOYODaDMINyW):

F70: the detached smoke worker's own process tree could survive the host
session ending, but the SMOKE COMMAND'S process was still killed the
instant the host session's job object tore down -- exiting with a Windows
NTSTATUS that denotes EXTERNAL interruption (0xC000013A
STATUS_CONTROL_C_EXIT, 0xC000026B STATUS_DLL_INIT_FAILED_LOGOFF,
0xC0000142 STATUS_DLL_INIT_FAILED), or the POSIX equivalent (death by
SIGINT/SIGTERM/SIGHUP/SIGKILL). Those must classify as ``infra_error``
(retryable), never ``fail`` -- discarding an already-judged, already-passing
commit because the *environment* pulled the rug out from under it is not a
real test failure. Fixed via one shared classifier
(:func:`hydra_core.proc.is_infra_interrupt_returncode`), used by BOTH smoke
classifier call sites (``squad_node._run_smoke``,
``smoke_job._run_smoke_tracked``) -- never per-site copies. Also: the smoke
command (and its descendants) now request ``CREATE_BREAKAWAY_FROM_JOB`` on
Windows (:func:`hydra_core.proc.popen_detached`) so they escape the job
object that killed them in the first place, falling back cleanly when the
current job object does not permit breakaway.

F71: ``_INFRA_SMOKE_RE`` used to be searched over the ENTIRE smoke
transcript whenever the exit code was non-zero -- a genuine, deterministic
test failure whose own output merely CONTAINS "spawn" or "ENOENT" (e.g.
clang-tidy printing "spawn" a dozen times) was reclassified ``infra_error``,
silently skipping the ``HYDRA_SMOKE_BASELINE_TESTS`` excusal gate (which
only ever runs for ``fail``). Fixed: the marker is now only ever searched
over the launcher PREAMBLE (``squad_node._smoke_infra_marker_hit``, the
first ``_INFRA_SMOKE_PREAMBLE_LINES`` lines), never the full transcript.
Also: the full smoke transcript for the ASYNC attended job now lands next
to the attended cursor file (outside the candidate worktree), not under
``<worktree>/.harness/smoke`` -- so evidence survives a discarded merge
that deletes the worktree.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from hydra_core import proc, smoke_job
from hydra_core.proc import is_infra_interrupt_returncode, popen_detached
from hydra_core.squad_node import (
    _INFRA_SMOKE_PREAMBLE_LINES,
    _run_smoke,
    _smoke_infra_marker_hit,
)


# --------------------------------------------------------------------------- #
# Unit: is_infra_interrupt_returncode                                        #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("rc", [
    3221225786,  # 0xC000013A STATUS_CONTROL_C_EXIT (unsigned, wrapper-propagated form)
    3221226091,  # 0xC000026B STATUS_DLL_INIT_FAILED_LOGOFF
    3221225794,  # 0xC0000142 STATUS_DLL_INIT_FAILED
    3221225786 - (1 << 32),  # signed 32-bit form of the same STATUS_CONTROL_C_EXIT
    3221226091 - (1 << 32),
    3221225794 - (1 << 32),
])
def test_windows_ntstatus_infra_codes_classify_as_interrupt(rc):
    assert is_infra_interrupt_returncode(rc) is True


@pytest.mark.parametrize("rc", [-1, -2, -9, -15, 129, 130, 137, 143])
def test_posix_signal_deaths_classify_as_interrupt(rc):
    assert is_infra_interrupt_returncode(rc) is True


@pytest.mark.parametrize("rc", [0, 1, 2, 3, 255, None, -3, 128])
def test_ordinary_exit_codes_do_not_classify_as_interrupt(rc):
    assert is_infra_interrupt_returncode(rc) is False


# --------------------------------------------------------------------------- #
# Integration: a smoke command that dies with an infra-interrupt exit code   #
# classifies infra_error, not fail (squad_node._run_smoke and               #
# smoke_job._run_smoke_tracked -- BOTH call sites, one shared classifier).   #
# --------------------------------------------------------------------------- #

def _write_smoke_cmd(project: Path, cmd: list[str]) -> None:
    (project / ".harness").mkdir(parents=True, exist_ok=True)
    (project / ".harness" / "smoke_cmd.json").write_text(
        json.dumps({"cmd": cmd}), encoding="utf-8")


@pytest.mark.skipif(os.name != "nt", reason="NTSTATUS exit codes are Windows-specific")
def test_run_smoke_classifies_ntstatus_control_c_exit_as_infra_error(tmp_path):
    """This is EXACTLY what warerender-gta's run_aOYODaDMINyW reproduced: a
    wrapper (there, node) exiting with the propagated NTSTATUS as its OWN
    return code -- `_run_smoke` must classify this infra_error, never
    `fail`, purely from the exit code (no transcript text needed)."""
    project = tmp_path / "proj"
    project.mkdir()
    _write_smoke_cmd(project, [sys.executable, "-c", "import sys; sys.exit(3221225786)"])

    status, reason = _run_smoke(None, project_path=str(project), stage_id="s1")
    assert status == "infra_error", (status, reason)
    assert "external interruption" in reason


@pytest.mark.skipif(os.name != "nt", reason="NTSTATUS exit codes are Windows-specific")
def test_run_smoke_tracked_classifies_ntstatus_logoff_as_infra_error(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    _write_smoke_cmd(project, [sys.executable, "-c", "import sys; sys.exit(3221226091)"])

    status, reason = smoke_job._run_smoke_tracked(str(project), "s1", timeout_s=30)
    assert status == "infra_error", (status, reason)
    assert "external interruption" in reason


# --------------------------------------------------------------------------- #
# F71: a genuine deterministic test failure whose LATER output happens to    #
# contain "spawn"/"ENOENT" must still classify fail, never infra_error.      #
# --------------------------------------------------------------------------- #

def _incidental_marker_script(tmp_path: Path) -> Path:
    """A fake test runner: prints an innocuous preamble, then "test" output
    that incidentally contains the infra marker words FAR past the
    preamble region, then exits non-zero -- mirrors clang-tidy printing
    "spawn" a dozen times inside its own (legitimate) diagnostic output."""
    script = tmp_path / "fake_runner.py"
    lines = ["print('starting test runner')", "print('collecting tests...')"]
    # Pad well past _INFRA_SMOKE_PREAMBLE_LINES before the marker text.
    for i in range(_INFRA_SMOKE_PREAMBLE_LINES + 10):
        lines.append(f"print('running test case {i}')")
    lines.append("print('FAILED test_thing -- AssertionError: spawn count mismatch (ENOENT)')")
    lines.append("import sys; sys.exit(1)")
    script.write_text("\n".join(lines), encoding="utf-8")
    return script


def test_run_smoke_deterministic_failure_with_incidental_markers_is_fail(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    script = _incidental_marker_script(tmp_path)
    _write_smoke_cmd(project, [sys.executable, str(script)])

    status, reason = _run_smoke(None, project_path=str(project), stage_id="s1")
    assert status == "fail", (status, reason)


def test_run_smoke_tracked_deterministic_failure_with_incidental_markers_is_fail(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    script = _incidental_marker_script(tmp_path)
    _write_smoke_cmd(project, [sys.executable, str(script)])

    status, reason = smoke_job._run_smoke_tracked(str(project), "s1", timeout_s=30)
    assert status == "fail", (status, reason)


# --------------------------------------------------------------------------- #
# F71: a genuine launch failure -- the marker appears in the LAUNCHER        #
# PREAMBLE (before any test runner output) -- still classifies infra_error.  #
# --------------------------------------------------------------------------- #

def test_run_smoke_launch_failure_in_preamble_is_infra_error(tmp_path):
    """Module missing in the launcher preamble (first lines) -> infra_error."""
    project = tmp_path / "proj"
    project.mkdir()
    script = tmp_path / "missing_module.py"
    script.write_text(
        "import totally_nonexistent_module_xyz\n", encoding="utf-8")
    _write_smoke_cmd(project, [sys.executable, str(script)])

    status, reason = _run_smoke(None, project_path=str(project), stage_id="s1")
    assert status == "infra_error", (status, reason)


def test_run_smoke_popen_launch_exception_is_infra_error(tmp_path, monkeypatch):
    """A genuine Popen-raising launch failure (missing interpreter/command)
    is classified infra_error directly by the except-block, independent of
    the marker region."""
    project = tmp_path / "proj"
    project.mkdir()
    _write_smoke_cmd(project, ["definitely-not-a-real-executable-xyz"])

    status, reason = _run_smoke(None, project_path=str(project), stage_id="s1")
    assert status == "infra_error", (status, reason)
    assert "could not launch" in reason


def test_smoke_infra_marker_hit_only_searches_preamble():
    """Direct unit pin on the bounded-region helper itself."""
    late_marker = "\n".join(["ok"] * (_INFRA_SMOKE_PREAMBLE_LINES + 5) + ["spawn EPERM"])
    assert _smoke_infra_marker_hit(late_marker) is False

    early_marker = "ModuleNotFoundError: No module named 'thing'\n" + "\n".join(
        ["ok"] * 5)
    assert _smoke_infra_marker_hit(early_marker) is True


# --------------------------------------------------------------------------- #
# F70: CREATE_BREAKAWAY_FROM_JOB requested on Windows; a PermissionError on  #
# the breakaway attempt falls back to spawning without it.                   #
# --------------------------------------------------------------------------- #

@pytest.mark.skipif(os.name != "nt", reason="CREATE_BREAKAWAY_FROM_JOB is Windows-only")
def test_popen_detached_falls_back_when_breakaway_denied(tmp_path, monkeypatch):
    calls: list[dict] = []
    real_popen = subprocess.Popen

    def _fake_popen(cmd, **kwargs):
        calls.append(dict(kwargs))
        creationflags = kwargs.get("creationflags", 0)
        breakaway_flag = getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0)
        if breakaway_flag and (creationflags & breakaway_flag):
            raise PermissionError("job object does not permit breakaway")
        return real_popen(cmd, **kwargs)

    monkeypatch.setattr(proc.subprocess, "Popen", _fake_popen)

    p = popen_detached([sys.executable, "-c", "pass"])
    try:
        p.wait(timeout=10)
    finally:
        try:
            p.kill()
        except Exception:
            pass

    assert len(calls) == 2, "must retry exactly once, without the breakaway flag"
    breakaway_flag = getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0)
    assert calls[0]["creationflags"] & breakaway_flag, "first attempt must request breakaway"
    assert not (calls[1]["creationflags"] & breakaway_flag), (
        "fallback attempt must NOT request breakaway"
    )


@pytest.mark.skipif(os.name != "nt", reason="CREATE_BREAKAWAY_FROM_JOB is Windows-only")
def test_detached_popen_kwargs_requests_breakaway_by_default():
    kwargs = proc.detached_popen_kwargs()
    breakaway_flag = getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0)
    assert breakaway_flag, "test environment must expose CREATE_BREAKAWAY_FROM_JOB"
    assert kwargs["creationflags"] & breakaway_flag

    kwargs_no_breakaway = proc.detached_popen_kwargs(breakaway=False)
    assert not (kwargs_no_breakaway["creationflags"] & breakaway_flag)


# --------------------------------------------------------------------------- #
# F71: the full smoke transcript lands OUTSIDE the candidate worktree, next  #
# to the sidecar/cursor -- and survives the worktree being deleted whole.    #
# --------------------------------------------------------------------------- #

def test_smoke_log_written_outside_worktree_survives_worktree_removal(tmp_path):
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    cursor_dir = tmp_path / "cursor-dir"
    cursor_dir.mkdir()
    sidecar_path = str(cursor_dir / "run.smoke-judge-0.smokepid.json")

    script = tmp_path / "failing_test.py"
    script.write_text(
        "print('FAILED test_thing -- boom')\nimport sys; sys.exit(1)\n",
        encoding="utf-8")
    _write_smoke_cmd(worktree, [sys.executable, str(script)])

    status, reason = smoke_job._run_smoke_tracked(
        str(worktree), "s1", timeout_s=30, sidecar_path=sidecar_path)
    assert status == "fail", (status, reason)
    assert "full_log=" in reason

    artifact_path = reason.split("full_log=")[-1].strip().rstrip("]")
    assert Path(artifact_path).exists(), "smoke log artifact must exist right after the run"
    assert str(worktree) not in artifact_path, (
        "the smoke log must NOT be written under the (discardable) worktree"
    )
    assert str(cursor_dir) in artifact_path, (
        "the smoke log must be written next to the sidecar/cursor, outside the worktree"
    )

    # Simulate a discarded merge: the whole worktree is deleted.
    import shutil
    shutil.rmtree(worktree)

    assert Path(artifact_path).exists(), (
        "the smoke log evidence must survive worktree deletion"
    )
