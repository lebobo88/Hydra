"""B11 — no console-subsystem child may flash a visible window on Windows.

``CREATE_NO_WINDOW`` is nowhere in Hydra's Python; a console-subsystem child
(git, python, node, a CLI tool) spawned from a process with no console of its
own (an MCP stdio server, or an already-detached child) gets a brand-new
console window allocated by Windows. ``hydra_core.proc.no_window_creationflags``
centralises the fix; this test guards its cross-platform behaviour and that
the detached-launch call sites in ``mcp_servers/hydra_control/server.py``
keep ``CREATE_NEW_PROCESS_GROUP`` alongside ``CREATE_NO_WINDOW``.

Hydra#170: Windows ignores ``CREATE_NO_WINDOW`` when it is combined with
``DETACHED_PROCESS`` in the same ``creationflags`` mask, so the three sites
previously defeated their own ``CREATE_NO_WINDOW`` bit and every
console-subsystem grandchild they spawned (git, python, ...) opened its own
visible console window — a full-suite smoke job doing this opened hundreds
of windows on the operator's desktop and forced a reboot. The DETACHED_PROCESS
bit was removed from all three sites; see
``test_hydra_control_detached_launches_keep_detachment_and_add_no_window``'s
own docstring below for the (inverted) assertion this test now makes.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

from hydra_core.proc import detached_popen_kwargs, kill_process_tree, no_window_creationflags, popen_detached


def test_no_window_creationflags_on_windows(monkeypatch) -> None:
    monkeypatch.setattr("os.name", "nt")
    assert no_window_creationflags() == getattr(subprocess, "CREATE_NO_WINDOW", 0)
    # On any real Windows Python build this constant exists and is non-zero.
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        assert no_window_creationflags() != 0


def test_no_window_creationflags_off_windows(monkeypatch) -> None:
    monkeypatch.setattr("os.name", "posix")
    assert no_window_creationflags() == 0


def test_hydra_control_detached_launches_keep_detachment_and_add_no_window(
    monkeypatch, tmp_path
) -> None:
    """Regression guard: the ACTUAL ``creationflags`` kwarg passed to
    ``subprocess.Popen`` by each of the three real detached-launch call sites
    in ``mcp_servers/hydra_control/server.py`` — ``_launch_resume``,
    ``_launch_ingest``, ``_launch_run`` — includes ``CREATE_NEW_PROCESS_GROUP``
    and ``CREATE_NO_WINDOW``, and DOES NOT include ``DETACHED_PROCESS``.

    Hydra#170 inversion: this assertion used to require ``DETACHED_PROCESS``
    to be PRESENT (additive alongside the new ``CREATE_NO_WINDOW``). That was
    wrong — Windows ignores ``CREATE_NO_WINDOW`` when it is combined with
    ``DETACHED_PROCESS``, so the old mask silently defeated its own fix and
    every console-subsystem grandchild one of these detached processes later
    started (git, python, ...) still opened a visible console window. The
    assertion below is inverted for Hydra#170: it now requires
    ``DETACHED_PROCESS`` to be ABSENT from the actual captured mask.

    This exercises the real functions (not a locally re-derived expression),
    so it fails if a launch block regresses. Falsifiability check: OR
    ``getattr(subprocess, "DETACHED_PROCESS", 0)`` back into any of the three
    blocks and this test fails, because the captured ``creationflags`` then
    has that bit set again.
    """
    import mcp_servers.hydra_control.server as server_mod

    monkeypatch.setattr(server_mod.os, "name", "nt", raising=False)
    monkeypatch.setattr(server_mod, "_HYDRA_ROOT", tmp_path)
    monkeypatch.setenv("HYDRA_ALLOW_DETACHED", "1")

    # Ensure the flag constants exist even on a non-Windows test host so the
    # forced-"nt" branch has real, non-zero bits to check for.
    monkeypatch.setattr(server_mod.subprocess, "DETACHED_PROCESS", 0x8, raising=False)
    monkeypatch.setattr(server_mod.subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200, raising=False)
    monkeypatch.setattr(server_mod.subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)

    detached = server_mod.subprocess.DETACHED_PROCESS
    new_group = server_mod.subprocess.CREATE_NEW_PROCESS_GROUP
    no_window = server_mod.subprocess.CREATE_NO_WINDOW

    captured_flags: list[int] = []

    class _FakeProc:
        pid = 4242

    def _fake_popen(*args, **kwargs):
        captured_flags.append(kwargs.get("creationflags", 0))
        return _FakeProc()

    monkeypatch.setattr(server_mod.subprocess, "Popen", _fake_popen)

    server_mod._launch_resume("wf-resume-test", "approve", None)
    server_mod._launch_ingest("wf-ingest-test", [])
    server_mod._launch_run(
        "goal text", squad=None, budget=None, workflow_id="wf-run-test",
    )

    assert len(captured_flags) == 3, (
        "expected exactly one Popen call per launch path "
        f"(_launch_resume, _launch_ingest, _launch_run); got {captured_flags}"
    )
    for flags in captured_flags:
        assert flags & detached == 0, (
            f"DETACHED_PROCESS bit present in actual creationflags={flags!r} "
            "(Hydra#170: it defeats CREATE_NO_WINDOW when combined with it)"
        )
        assert flags & new_group == new_group, (
            f"CREATE_NEW_PROCESS_GROUP bit missing from actual creationflags={flags!r}"
        )
        assert flags & no_window == no_window, (
            f"CREATE_NO_WINDOW bit missing from actual creationflags={flags!r}"
        )


def test_run_text_ors_in_no_window_creationflags(monkeypatch) -> None:
    """run_text must OR the no-window flag into whatever creationflags the
    caller passed, rather than overwriting it."""
    monkeypatch.setattr("os.name", "nt")
    captured: dict = {}

    def _fake_run(cmd, **kwargs):
        captured.update(kwargs)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", _fake_run)

    from hydra_core.proc import run_text

    caller_flag = 0x1000
    run_text(["irrelevant"], creationflags=caller_flag, capture_output=True)

    assert captured["creationflags"] & caller_flag == caller_flag
    assert captured["creationflags"] & no_window_creationflags() == no_window_creationflags()


@pytest.mark.skipif(os.name != "nt", reason="Windows-only creationflags semantics (Hydra#170)")
def test_detached_popen_kwargs_has_no_window_without_detached_process() -> None:
    """Hydra#170 — t1: real Windows run (not monkeypatched), both breakaway
    variants. ``detached_popen_kwargs()['creationflags']`` must have
    ``CREATE_NO_WINDOW`` set, have ``CREATE_NEW_PROCESS_GROUP`` set, and must
    NOT have ``DETACHED_PROCESS`` set — the old mask ORed in
    ``DETACHED_PROCESS``, which defeats ``CREATE_NO_WINDOW`` on real Windows
    and let a detached process's own console-subsystem grandchildren (git,
    python, ...) pop a visible console window.
    """
    no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    new_group = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    detached = getattr(subprocess, "DETACHED_PROCESS", 0)

    for breakaway in (True, False):
        flags = detached_popen_kwargs(breakaway=breakaway)["creationflags"]
        assert flags & no_window == no_window, (
            f"breakaway={breakaway}: CREATE_NO_WINDOW missing from creationflags={flags!r}"
        )
        assert flags & new_group == new_group, (
            f"breakaway={breakaway}: CREATE_NEW_PROCESS_GROUP missing from creationflags={flags!r}"
        )
        assert flags & detached == 0, (
            f"breakaway={breakaway}: DETACHED_PROCESS present in creationflags={flags!r} "
            "(Hydra#170: it defeats CREATE_NO_WINDOW when combined with it)"
        )


# Grandchild script template (Hydra#170 t3): started by the CHILD via a plain
# ``subprocess.run`` with NO creationflags of its own — it inherits whatever
# console the child owns. If the child was started with the OLD mask
# (CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS | CREATE_NO_WINDOW), the child
# has NO console at all (DETACHED_PROCESS defeated CREATE_NO_WINDOW's intent
# of giving it a HIDDEN one), so this grandchild — a console-subsystem
# program — gets a brand-new, VISIBLE console window allocated by Windows.
# With the fix (CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW, no
# DETACHED_PROCESS) the child owns a hidden console that this grandchild
# inherits instead, so no window appears.
#
# Diagnostic fields beyond the plain visible/hwnd pair (cross-vendor review
# point C): GetConsoleWindow can return a handle that genuinely exists but is
# not reported visible by every console host, which would make this test
# pass for the wrong reason (no console at all) rather than the right one (a
# HIDDEN console). ``console_cp`` (0 only when the process has no console
# whatsoever) and a direct ``GetConsoleMode`` probe on ``CONOUT$`` distinguish
# "no console" from "console exists but not IsWindowVisible".
_GRANDCHILD_SCRIPT = '''
import ctypes
import os
import sys
from ctypes import wintypes

report_path = sys.argv[1]

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
user32 = ctypes.WinDLL("user32", use_last_error=True)

kernel32.GetConsoleWindow.restype = wintypes.HWND
kernel32.GetConsoleWindow.argtypes = []
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsWindowVisible.restype = wintypes.BOOL
kernel32.GetConsoleCP.restype = wintypes.UINT
kernel32.GetConsoleCP.argtypes = []

hwnd = kernel32.GetConsoleWindow()
visible = 1 if (hwnd and user32.IsWindowVisible(hwnd)) else 0
console_cp = kernel32.GetConsoleCP()

has_conout = 0
try:
    fh = os.open("CONOUT$", os.O_RDWR | os.O_BINARY)
    mode = wintypes.DWORD()
    ok = kernel32.GetConsoleMode(wintypes.HANDLE(fh), ctypes.byref(mode))
    has_conout = 1 if ok else 0
    os.close(fh)
except OSError:
    has_conout = 0

line = (
    f"visible={visible} hwnd={int(hwnd or 0)} console_cp={console_cp} "
    f"has_conout={has_conout}"
)

tmp_path = report_path + ".tmp"
with open(tmp_path, "w", encoding="ascii") as f:
    f.write(line)
os.replace(tmp_path, report_path)
# Exits immediately -- starts no further process.
'''

# Child script (Hydra#170 t3): started by the TEST via
# ``hydra_core.proc.popen_detached``. Its only job is to run the grandchild
# via a plain ``subprocess.run`` (no creationflags of its own) with a SHORT
# bounded timeout, so the grandchild can never be orphaned even if something
# goes wrong -- the grandchild itself writes its report and exits at once, and
# this parent call either returns promptly or is killed by the timeout, after
# which the child process itself also exits.
_CHILD_SCRIPT = '''
import subprocess
import sys

grandchild_script = sys.argv[1]
report_path = sys.argv[2]

try:
    subprocess.run(
        [sys.executable, grandchild_script, report_path],
        timeout=15,
    )
except subprocess.TimeoutExpired:
    pass
'''


@pytest.mark.skipif(os.name != "nt", reason="Windows-only console-window semantics (Hydra#170)")
def test_detached_child_console_subsystem_grandchild_opens_no_visible_window(tmp_path):
    """Hydra#170 — t3: behavioural proof of the actual defect, not just the
    creationflags bitmask. A child started via ``popen_detached`` (the fixed
    mask) runs a grandchild via a plain ``subprocess.run`` with NO
    creationflags of its own, exactly as a real detached smoke job runs
    ``git``/``python`` beneath it. The grandchild is a console-subsystem
    program, so it is the one place a visible window would actually appear.
    It reports whether ``GetConsoleWindow`` + ``IsWindowVisible`` see a
    visible console, plus ``GetConsoleCP()`` and a ``CONOUT$``
    ``GetConsoleMode`` probe so a failure here is diagnosable (a console that
    exists-but-unreported-visible is a different failure mode than no console
    at all).
    """
    report_path = tmp_path / "t3_report.txt"
    grandchild_script = tmp_path / "t3_grandchild.py"
    child_script = tmp_path / "t3_child.py"
    grandchild_script.write_text(_GRANDCHILD_SCRIPT, encoding="utf-8")
    child_script.write_text(_CHILD_SCRIPT, encoding="utf-8")

    proc = popen_detached(
        [sys.executable, str(child_script), str(grandchild_script), str(report_path)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.time() + 30
        report_line = None
        while time.time() < deadline:
            if report_path.exists():
                try:
                    report_line = report_path.read_text(encoding="ascii")
                    break
                except OSError:
                    pass
            time.sleep(0.2)

        assert report_line is not None, (
            f"grandchild never wrote {report_path} within the 30s deadline "
            "(the test failed cleanly rather than hanging)"
        )

        fields = dict(
            item.split("=", 1) for item in report_line.strip().split()
        )
        visible = int(fields["visible"])

        assert visible == 0, (
            f"grandchild reported a VISIBLE console window ({report_line!r}) -- "
            "Hydra#170 regression: a console-subsystem child of a detached "
            "process opened its own window instead of inheriting a hidden one"
        )
    finally:
        # No process started by this test may survive it, pass or fail. The
        # grandchild itself starts nothing further and exits immediately
        # after writing its report (or the child's bounded subprocess.run
        # timeout reaps it); killing the child's whole process tree here
        # covers both the ordinary case and any wedge.
        kill_process_tree(proc.pid, timeout=10)
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                proc.wait(timeout=0.2)
                break
            except subprocess.TimeoutExpired:
                continue
