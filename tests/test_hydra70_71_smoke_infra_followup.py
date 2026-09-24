"""Hydra#70 / #71 follow-up (today's comments, verified against
warerender-gta run_aOYODaDMINyW), REVISED for cross-vendor re-review
(codex gpt-5.6-terra):

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

Re-review fix (1): the POSIX signal-death shapes (negative ``returncode``,
``128 + N``) are POSIX-only conventions -- on Windows, 129/130/137/143 are
ordinary application exit codes a real smoke command can legitimately
return, so treating them as "interrupted" there excused real failures.
``is_infra_interrupt_returncode`` is now platform-gated via a
``platform_is_windows`` keyword (defaults to the actual ``os.name``) so
POSIX signal shapes only match off Windows and NTSTATUS shapes only match
on Windows.

F71: ``_INFRA_SMOKE_RE`` used to be searched over the ENTIRE smoke
transcript whenever the exit code was non-zero -- a genuine, deterministic
test failure whose own output merely CONTAINS "spawn" or "ENOENT" (e.g.
clang-tidy printing "spawn" a dozen times) was reclassified ``infra_error``,
silently skipping the ``HYDRA_SMOKE_BASELINE_TESTS`` excusal gate (which
only ever runs for ``fail``).

Re-review fix (2): a first pass bounded the marker search to the transcript's
first N lines ("launcher preamble"), but line POSITION alone still does not
prove the RUNNER failed to launch -- a test suite that fast-fails on its very
first assertion, whose assertion text happens to mention "spawn"/"ENOENT",
sits in that same early region. Replaced with
``squad_node._smoke_infra_marker_hit`` matching only IDENTIFIABLE
launcher/structured-error patterns (cmd.exe's "is not recognized", a POSIX
shell's "command not found"/"not found", node's own
"Error: spawn <x> ENOENT|EACCES|EPERM", npm's own "npm ERR! code
ENOENT|EPERM|EACCES", and a Python interpreter's own "No module named" /ne a
terminating ``ModuleNotFoundError`` traceback with no test-runner summary
line anywhere in the transcript) -- never a bare-word or line-position
heuristic. Also: the full smoke transcript for the ASYNC attended job now
lands next to the attended cursor file (outside the candidate worktree), not
under ``<worktree>/.harness/smoke`` -- so evidence survives a discarded
merge that deletes the worktree.

Re-review fix (3): ``popen_detached``'s Windows breakaway fallback used to
retry on ANY ``OSError`` from the first spawn, including a missing
executable (``FileNotFoundError``) or an unrelated access failure -- masking
a genuine launch error behind a pointless second ``Popen``. Now gated to
EXACTLY the breakaway-denied condition (``OSError.winerror == 5``,
``ERROR_ACCESS_DENIED``); any other ``OSError`` re-raises immediately with
no second spawn.
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
from hydra_core.squad_node import _run_smoke, _smoke_infra_marker_hit


# --------------------------------------------------------------------------- #
# Unit: is_infra_interrupt_returncode, platform-gated                        #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("rc", [
    3221225786,  # 0xC000013A STATUS_CONTROL_C_EXIT (unsigned, wrapper-propagated form)
    3221226091,  # 0xC000026B STATUS_DLL_INIT_FAILED_LOGOFF
    3221225794,  # 0xC0000142 STATUS_DLL_INIT_FAILED
    3221225786 - (1 << 32),  # signed 32-bit form of the same STATUS_CONTROL_C_EXIT
    3221226091 - (1 << 32),
    3221225794 - (1 << 32),
])
def test_windows_ntstatus_infra_codes_classify_as_interrupt_on_windows(rc):
    assert is_infra_interrupt_returncode(rc, platform_is_windows=True) is True


@pytest.mark.parametrize("rc", [-1, -2, -9, -15, 129, 130, 137, 143])
def test_posix_signal_deaths_classify_as_interrupt_on_posix(rc):
    assert is_infra_interrupt_returncode(rc, platform_is_windows=False) is True


@pytest.mark.parametrize("rc", [0, 1, 2, 3, 255, None, -3, 128])
def test_ordinary_exit_codes_do_not_classify_as_interrupt_on_posix(rc):
    assert is_infra_interrupt_returncode(rc, platform_is_windows=False) is False


def test_default_platform_matches_actual_os_name():
    """No explicit kwarg -> behaviour matches `os.name`, i.e. the two
    platform-gated cases above agree with the unqualified call on THIS
    host."""
    is_windows = os.name == "nt"
    for rc in (129, 130, 137, 143, -2, -9):
        assert is_infra_interrupt_returncode(rc) == is_infra_interrupt_returncode(
            rc, platform_is_windows=is_windows
        )


@pytest.mark.parametrize("rc", [129, 130, 137, 143])
def test_posix_signal_shaped_codes_are_ordinary_exit_codes_on_windows(rc):
    """Critique item 1: 129/130/137/143 are legitimate application exit
    codes on Windows (no shell/wrapper 128+N convention exists there) --
    must NOT be excused as an interruption on that platform."""
    assert is_infra_interrupt_returncode(rc, platform_is_windows=True) is False


@pytest.mark.parametrize("rc", [-2, -9, -15, -1])
def test_negative_posix_signal_codes_are_not_interrupts_on_windows(rc):
    assert is_infra_interrupt_returncode(rc, platform_is_windows=True) is False


@pytest.mark.parametrize("rc", [3221225786, 3221226091, 3221225794])
def test_windows_ntstatus_codes_do_not_match_on_posix(rc):
    """NTSTATUS shapes are a Windows-only concept -- explicit platform gate
    keeps them from ever matching when `platform_is_windows=False`."""
    assert is_infra_interrupt_returncode(rc, platform_is_windows=False) is False


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


@pytest.mark.skipif(os.name != "nt", reason="Windows-only real-app-exit-code regression")
@pytest.mark.parametrize("rc", [129, 130, 137, 143])
def test_run_smoke_windows_129_143_exit_codes_are_real_failures_not_infra(tmp_path, rc):
    """Critique item 1, integration proof: on Windows, a smoke command that
    genuinely exits with 129/130/137/143 (an ordinary application exit code
    there, NOT a shell/wrapper signal-death convention) must classify
    `fail`, never `infra_error`."""
    project = tmp_path / "proj"
    project.mkdir()
    _write_smoke_cmd(project, [sys.executable, "-c", f"import sys; sys.exit({rc})"])

    status, reason = _run_smoke(None, project_path=str(project), stage_id="s1")
    assert status == "fail", (status, reason)


@pytest.mark.skipif(os.name != "nt", reason="Windows-only real-app-exit-code regression")
@pytest.mark.parametrize("rc", [129, 130, 137, 143])
def test_run_smoke_tracked_windows_129_143_exit_codes_are_real_failures_not_infra(
    tmp_path, rc
):
    project = tmp_path / "proj"
    project.mkdir()
    _write_smoke_cmd(project, [sys.executable, "-c", f"import sys; sys.exit({rc})"])

    status, reason = smoke_job._run_smoke_tracked(str(project), "s1", timeout_s=30)
    assert status == "fail", (status, reason)


# --------------------------------------------------------------------------- #
# F71: a genuine deterministic test failure whose LATER output happens to    #
# contain "spawn"/"ENOENT" must still classify fail, never infra_error.      #
# --------------------------------------------------------------------------- #

def _incidental_marker_script(tmp_path: Path) -> Path:
    """A fake test runner: a genuine, deterministic failure whose own
    assertion text incidentally contains infra marker words -- mirrors
    clang-tidy printing "spawn" a dozen times inside its own (legitimate)
    diagnostic output. Critique item 2: this must classify `fail` even
    though the marker text is right at the START of the transcript (no
    "late in the transcript" padding needed any more -- line position is
    no longer part of the classifier at all)."""
    script = tmp_path / "fake_runner.py"
    lines = [
        "print('starting test runner')",
        "print('collecting tests...')",
        "print('FAILED test_thing -- AssertionError: spawn count mismatch (ENOENT)')",
        "import sys; sys.exit(1)",
    ]
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


@pytest.mark.parametrize("marker", [
    "spawn count mismatch",
    "ENOENT was expected but not raised",
    "EPERM should have been thrown",
    "esbuild plugin returned wrong output",
    "segfault handler was not installed",
    "No module named in the user-facing error string",
])
def test_run_smoke_early_genuine_failure_with_ambiguous_marker_is_fail(tmp_path, marker):
    """Critique item 2, exhaustive: an EARLY (first-line) genuine test
    failure whose assertion/output text contains each ambiguous bare-word
    marker must still classify `fail` -- bare words no longer qualify on
    their own, regardless of position."""
    project = tmp_path / "proj"
    project.mkdir()
    script = tmp_path / f"early_fail_{abs(hash(marker))}.py"
    script.write_text(
        "\n".join([
            f"print('FAILED test_thing -- AssertionError: {marker}')",
            "import sys; sys.exit(1)",
        ]),
        encoding="utf-8",
    )
    _write_smoke_cmd(project, [sys.executable, str(script)])

    status, reason = _run_smoke(None, project_path=str(project), stage_id="s1")
    assert status == "fail", (status, reason)


def test_warerender_style_transcript_with_incidental_spawn_lines_and_ctest_summary_is_fail(
    tmp_path,
):
    """Many incidental `spawn` lines (e.g. clang-tidy/ctest chatter) plus a
    genuine ctest failure summary must classify `fail`, not `infra_error`."""
    project = tmp_path / "proj"
    project.mkdir()
    transcript_lines = ["Test project C:/warerender-gta/build"]
    for i in range(30):
        transcript_lines.append(f"    Start {i}: spawn subprocess for check {i}")
    transcript_lines.append("The following tests FAILED:")
    transcript_lines.append("\t  7 - water_data_roundtrip (Failed)")
    transcript_lines.append("Errors while running CTest")
    script = tmp_path / "fake_ctest.py"
    script.write_text(
        "\n".join(
            [f"print({line!r})" for line in transcript_lines]
            + ["import sys; sys.exit(8)"]
        ),
        encoding="utf-8",
    )
    _write_smoke_cmd(project, [sys.executable, str(script)])

    status, reason = _run_smoke(None, project_path=str(project), stage_id="s1")
    assert status == "fail", (status, reason)


# --------------------------------------------------------------------------- #
# F71: identifiable launcher/structured-error patterns still classify        #
# infra_error -- one per pattern family.                                     #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("transcript,expected", [
    ("'npx' is not recognized as an internal or external command,\n"
     "operable program or batch file.", True),
    ("bash: some-missing-tool: command not found", True),
    ("sh: 1: some-missing-tool: not found", True),
    ("Error: spawn some-missing-tool ENOENT\n    at ChildProcess._handle.onexit",
     True),
    ("Error: spawn some-missing-tool EACCES", True),
    ("Error: spawn some-missing-tool EPERM", True),
    ("npm ERR! code ENOENT\nnpm ERR! syscall spawn", True),
    ("npm ERR! code EPERM", True),
    ("/usr/bin/python3: No module named pytest", True),
    ("Traceback (most recent call last):\n"
     '  File "run.py", line 1, in <module>\n'
     "    import totally_missing_thing\n"
     "ModuleNotFoundError: No module named 'totally_missing_thing'", True),
    # Non-matches: bare words with no launcher shape, or a summary present.
    ("FAILED test_thing -- AssertionError: spawn count mismatch (ENOENT)", False),
    ("segfault detected in worker 3, see core dump", False),
    ("esbuild build finished with 1 error", False),
])
def test_smoke_infra_marker_hit_matches_only_identifiable_launcher_patterns(
    transcript, expected
):
    assert _smoke_infra_marker_hit(transcript) is expected


def test_smoke_infra_marker_hit_module_not_found_with_summary_present_is_not_infra():
    """A `ModuleNotFoundError` that terminates the transcript but is
    preceded by a real test-runner summary line means the runner DID run --
    e.g. a collection-time import error that pytest itself reported and
    summarized -- so this must NOT classify as a launch failure."""
    transcript = (
        "============================= test session starts ==============================\n"
        "collected 3 items / 1 error\n"
        "FAILED tests/test_thing.py::test_one\n"
        "=========================== short test summary info ============================\n"
        "1 failed, 2 passed in 0.42s\n"
        "Traceback (most recent call last):\n"
        "ModuleNotFoundError: No module named 'unrelated_plugin'"
    )
    assert _smoke_infra_marker_hit(transcript) is False


def test_run_smoke_launch_failure_module_not_found_is_infra_error(tmp_path):
    """Module missing with no test-runner summary anywhere -> infra_error."""
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


# --------------------------------------------------------------------------- #
# F70: CREATE_BREAKAWAY_FROM_JOB requested on Windows; only the              #
# breakaway-denied error (winerror 5) falls back to spawning without it.     #
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
            # ERROR_ACCESS_DENIED (winerror 5) -- exactly what CreateProcess
            # raises when the current job object disallows breakaway.
            raise OSError(13, "Access is denied", None, 5)
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
def test_popen_detached_reraises_non_breakaway_oserror_without_second_popen(monkeypatch):
    """Critique item 3: a genuine launch failure (e.g. a missing executable,
    winerror 2 ERROR_FILE_NOT_FOUND) unrelated to breakaway must propagate
    immediately -- exactly one Popen call, no fallback retry."""
    calls: list[dict] = []

    def _fake_popen(cmd, **kwargs):
        calls.append(dict(kwargs))
        raise FileNotFoundError(2, "The system cannot find the file specified", None, 2)

    monkeypatch.setattr(proc.subprocess, "Popen", _fake_popen)

    with pytest.raises(FileNotFoundError):
        popen_detached(["definitely-not-a-real-executable-xyz"])

    assert len(calls) == 1, "a non-breakaway OSError must not trigger a second Popen attempt"


def test_run_smoke_tracked_filenotfound_launch_is_infra_error_single_popen(
    tmp_path, monkeypatch
):
    """End-to-end proof for critique item 3: a FileNotFoundError from the
    underlying Popen (missing executable) propagates out of
    `popen_detached` with exactly one spawn attempt, and
    `_run_smoke_tracked` still classifies it `infra_error` at its launch-
    exception path exactly as before."""
    project = tmp_path / "proj"
    project.mkdir()
    _write_smoke_cmd(project, ["definitely-not-a-real-executable-xyz"])

    calls: list[dict] = []
    real_popen = subprocess.Popen

    def _counting_popen(cmd, **kwargs):
        calls.append(dict(kwargs))
        return real_popen(cmd, **kwargs)

    monkeypatch.setattr(smoke_job.subprocess, "Popen", _counting_popen)

    status, reason = smoke_job._run_smoke_tracked(str(project), "s1", timeout_s=30)
    assert status == "infra_error", (status, reason)
    assert "could not launch" in reason
    assert len(calls) <= 1, "a genuine missing-executable failure must not retry Popen"


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
