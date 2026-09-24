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
from pathlib import Path
from typing import Any

__all__ = [
    "run_text",
    "no_window_creationflags",
    "kill_process_tree",
    "is_pid_alive",
    "detached_popen_kwargs",
    "process_identity",
    "is_same_process",
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


# --------------------------------------------------------------------------- #
# Process identity (cross-vendor judge finding, P2-2/P2-3 follow-up).        #
#                                                                             #
# `is_pid_alive` (above) only proves SOME process currently holds `pid` --   #
# never that it is the SAME instance a caller recorded earlier. A pid is a   #
# small, OS-recycled integer: once the original process exits, the OS (very  #
# aggressively on Windows) can hand the identical pid to a completely        #
# unrelated later process. Any code path that kills or adopts a RECORDED pid #
# (a stored worker pid, a smoke-child sidecar pid) must verify identity      #
# first via `process_identity`/`is_same_process`, never trust `is_pid_alive` #
# alone -- otherwise a kill/adopt call can act on an unrelated process tree. #
# --------------------------------------------------------------------------- #


def process_identity(pid: int | None) -> Any | None:
    """Best-effort, durable identity for ``pid`` -- a value that only
    matches the SAME OS process instance across its lifetime (its creation
    time), distinguishing it from a LATER, unrelated process the OS reused
    the identical pid for.

    Returns ``None`` when the identity cannot be determined -- the pid is
    invalid, the process is already gone, or the platform/permission model
    does not expose the needed information (e.g. ``OpenProcess`` denied on
    Windows). Callers MUST treat ``None`` as "unverifiable", never as
    evidence of sameness -- see :func:`is_same_process`.
    """
    if not pid or pid <= 0:
        return None
    if os.name == "nt":
        return _win_process_creation_time(pid)
    return _posix_process_start_ticks(pid)


def _win_process_creation_time(pid: int) -> int | None:
    """Windows: ``GetProcessTimes``' creation-time ``FILETIME``, packed into
    a single 64-bit integer. Requires only
    ``PROCESS_QUERY_LIMITED_INFORMATION`` (available even for a process
    owned by another user in most configurations); returns ``None`` on any
    failure (invalid pid, access denied, API unavailable).

    R1 follow-up (surfaced by the smoke_job.poll_job liveness-gate fix): a
    just-terminated pid can remain ``OpenProcess``-able (and
    ``GetProcessTimes``-queryable, with its ORIGINAL creation time intact)
    for a short window after exit, before Windows fully reaps the process
    object and the pid becomes reusable -- ``tasklist`` (``is_pid_alive``)
    stops listing it immediately, but this call alone would still report a
    valid, matching identity for it in that window, falsely proving
    "liveness" for an already-dead process. ``GetProcessTimes`` also fills
    ``exit_time`` the instant the process has exited (zero/all-clear while
    still running); a non-zero ``exit_time`` here means the process is
    already gone, and this function returns ``None`` for it exactly like
    any other unverifiable pid -- never a stale-but-real identity for a
    process that no longer exists."""
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:  # noqa: BLE001
        return None
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except Exception:  # noqa: BLE001
        return None
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    handle = None
    try:
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return None
        creation = wintypes.FILETIME()
        exit_time = wintypes.FILETIME()
        kernel_time = wintypes.FILETIME()
        user_time = wintypes.FILETIME()
        ok = kernel32.GetProcessTimes(
            handle, ctypes.byref(creation), ctypes.byref(exit_time),
            ctypes.byref(kernel_time), ctypes.byref(user_time),
        )
        if not ok:
            return None
        if exit_time.dwHighDateTime or exit_time.dwLowDateTime:
            # Already exited -- see the R1 follow-up note above.
            return None
        return (int(creation.dwHighDateTime) << 32) | int(creation.dwLowDateTime)
    except Exception:  # noqa: BLE001
        return None
    finally:
        if handle:
            try:
                kernel32.CloseHandle(handle)
            except Exception:  # noqa: BLE001
                pass


def _posix_process_start_ticks(pid: int) -> int | None:
    """POSIX (Linux ``/proc``): the process's ``starttime`` field from
    ``/proc/<pid>/stat`` (clock ticks since boot) -- reused pids get a
    different starttime than the process that previously held them. Returns
    ``None`` on any platform without a usable ``/proc`` (e.g. macOS) or on
    any read/parse failure."""
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii", errors="replace")
    except OSError:
        return None
    try:
        # `comm` (field 2) is parenthesized and may itself contain spaces or
        # parens -- split on the LAST ')' to skip past it safely.
        rparen = raw.rfind(")")
        if rparen < 0:
            return None
        fields = raw[rparen + 2:].split()
        # After `pid (comm) state ...`, `state` is fields[0]; `starttime` is
        # the 22nd whitespace-delimited field overall, i.e. fields[19] here.
        return int(fields[19])
    except (IndexError, ValueError):
        return None


def is_same_process(pid: "int | None", recorded_identity: Any) -> bool:
    """``True`` only when ``pid`` currently refers to the SAME OS process
    instance whose identity was captured (via :func:`process_identity`) as
    ``recorded_identity``.

    ``False`` whenever this cannot be positively verified -- including when
    ``recorded_identity`` is ``None`` (never captured) or the current
    process's identity cannot be read (gone / unverifiable). A live-but-
    different (or unverifiable) pid is therefore NEVER treated as the
    recorded process by a caller that gates a kill/adopt decision on this.
    """
    if recorded_identity is None:
        return False
    current = process_identity(pid)
    if current is None:
        return False
    return current == recorded_identity
