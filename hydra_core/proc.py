"""UTF-8-safe subprocess helpers (E2-36).

``subprocess.run(..., text=True)`` decodes the child's stdout/stderr with
``locale.getpreferredencoding()``.  On Windows that is the ANSI codepage
(cp1252 on a US install), so any byte the codepage cannot map raises
``UnicodeDecodeError`` inside the reader thread — the thread dies and that
stream's content is lost silently.  Live evidence: a detached ingest run whose
codex-CLI stdout contained ``0x90`` killed ``Thread-5 (_readerthread)``.

Every text-mode subprocess call in this repo must therefore pin
``encoding="utf-8", errors="replace"``.  Use :func:`run_text` for new code;
``tests/test_no_bare_text_subprocess.py`` fails the build on any bare
``text=True``.
"""

from __future__ import annotations

import os
import subprocess
from typing import Any

__all__ = [
    "run_text",
    "no_window_creationflags",
    "kill_process_tree",
    "is_pid_alive",
    "detached_popen_kwargs",
]


def no_window_creationflags() -> int:
    """``CREATE_NO_WINDOW`` on Windows, ``0`` elsewhere.

    Prevents a console-subsystem child (git, python, node, a CLI tool) from
    allocating a visible console window when the parent process has none of
    its own — e.g. an MCP stdio server launched by the host, or an already
    detached child. Uses ``getattr(..., 0)`` so this is a no-op on any
    platform or Python build lacking the constant (never hard-code
    ``0x08000000``).
    """
    if os.name != "nt":
        return 0
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def run_text(cmd: Any, **kwargs: Any) -> "subprocess.CompletedProcess[str]":
    """``subprocess.run`` in text mode with UTF-8 decoding forced.

    Pins ``text=True, encoding="utf-8", errors="replace"`` so an undecodable
    byte degrades to U+FFFD instead of killing the reader thread, and merges
    ``PYTHONIOENCODING=utf-8`` into the child environment so a Python child
    *encodes* its own output as UTF-8 too.  When ``env`` is not supplied it is
    derived from ``os.environ`` (equivalent to the inherited environment).
    Also ORs in :func:`no_window_creationflags` so the child never flashes a
    console window on Windows.
    """
    env = kwargs.pop("env", None)
    child_env = dict(os.environ if env is None else env)
    child_env["PYTHONIOENCODING"] = "utf-8"
    kwargs.pop("text", None)
    kwargs.pop("encoding", None)
    kwargs.pop("errors", None)
    creationflags = kwargs.pop("creationflags", 0) | no_window_creationflags()
    return subprocess.run(
        cmd,
        env=child_env,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=creationflags,
        **kwargs,
    )


def detached_popen_kwargs() -> dict[str, Any]:
    """``creationflags``/``start_new_session`` kwargs for a process that must
    survive its parent's death (Hydra#70 smoke job).

    Windows: ``CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS | CREATE_NO_WINDOW``
    so the child is not attached to the parent's console/job object and a
    ``taskkill /T /F /PID <parent>`` (or the parent's own ``subprocess.run``
    timeout) does not reach into the child's process tree.
    POSIX: ``start_new_session=True`` (new session/process group) so the
    child is not in the parent's process group and survives a
    ``SIGTERM``/``SIGKILL`` sent to the parent alone.
    """
    if os.name == "nt":
        flags = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "DETACHED_PROCESS", 0)
            | no_window_creationflags()
        )
        return {"creationflags": flags}
    return {"start_new_session": True}


def kill_process_tree(pid: int | None, *, timeout: int = 20) -> bool:
    """Kill ``pid`` AND every descendant process (Hydra#70).

    Windows: ``taskkill /T /F /PID`` walks the OS process tree rooted at
    ``pid`` regardless of process-group membership, so it reaches a
    grandchild spawned by an intermediate shell (e.g. ``npm`` -> ``node``).
    POSIX: ``os.killpg`` on the process's own group -- correct as long as the
    tree was started via :func:`detached_popen_kwargs` (``start_new_session``
    makes ``pid`` its own group leader, so its descendants share that pgid
    unless they detach themselves too).

    Never raises. Returns True if a kill signal/command was issued (does not
    guarantee timely death -- callers that need certainty should re-check
    :func:`is_pid_alive` after a short grace period).
    """
    if not pid or pid <= 0:
        return False
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(pid)],
                capture_output=True, timeout=timeout,
                creationflags=no_window_creationflags(),
            )
            return True
        except Exception:  # noqa: BLE001 — best-effort cleanup
            return False
    import signal
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
        return True
    except Exception:  # noqa: BLE001
        try:
            os.kill(pid, signal.SIGKILL)
            return True
        except Exception:  # noqa: BLE001
            return False


def is_pid_alive(pid: int | None) -> bool:
    """Best-effort liveness check for a job PID (Hydra#70 poll path).

    Windows has no ``os.kill(pid, 0)`` signal-0 probe, so this shells out to
    ``tasklist``. Fails OPEN (returns True) on any probe error so a transient
    ``tasklist`` hiccup never causes the poller to prematurely treat a live
    job as vanished.
    """
    if not pid or pid <= 0:
        return False
    if os.name == "nt":
        try:
            # E2-36: text-mode subprocess output must be decoded UTF-8 with
            # errors="replace" (run_text), never a bare text=True (which
            # decodes via the Windows ANSI codepage and can kill the reader
            # thread on an undecodable byte).
            r = run_text(
                ["tasklist", "/FI", f"PID eq {pid}"],
                capture_output=True, timeout=10,
            )
            return str(pid) in (r.stdout or "")
        except Exception:  # noqa: BLE001
            return True
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except Exception:  # noqa: BLE001
        return True
