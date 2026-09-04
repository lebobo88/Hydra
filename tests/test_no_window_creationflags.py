"""B11 — no console-subsystem child may flash a visible window on Windows.

``CREATE_NO_WINDOW`` is nowhere in Hydra's Python; a console-subsystem child
(git, python, node, a CLI tool) spawned from a process with no console of its
own (an MCP stdio server, or an already-detached child) gets a brand-new
console window allocated by Windows. ``hydra_core.proc.no_window_creationflags``
centralises the fix; this test guards its cross-platform behaviour and that
the detached-launch call sites in ``mcp_servers/hydra_control/server.py``
still keep ``DETACHED_PROCESS`` + ``CREATE_NEW_PROCESS_GROUP`` alongside it
(additive, not a replacement).
"""

from __future__ import annotations

import subprocess

from hydra_core.proc import no_window_creationflags


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
    ``_launch_ingest``, ``_launch_run`` — still includes ``DETACHED_PROCESS``
    and ``CREATE_NEW_PROCESS_GROUP`` alongside the new ``CREATE_NO_WINDOW``.

    This exercises the real functions (not a locally re-derived expression),
    so it fails if a launch block regresses. Falsifiability check: comment
    out ``| getattr(subprocess, "DETACHED_PROCESS", 0)`` in any of the three
    blocks and this test fails, because the captured ``creationflags`` no
    longer has that bit set.
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
        assert flags & detached == detached, (
            f"DETACHED_PROCESS bit missing from actual creationflags={flags!r}"
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
