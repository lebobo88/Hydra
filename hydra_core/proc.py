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
    "popen_detached",
    "process_identity",
    "is_same_process",
    "is_infra_interrupt_returncode",
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


def detached_popen_kwargs(*, breakaway: bool = True) -> dict[str, Any]:
    """``creationflags``/``start_new_session`` kwargs for a process that must
    survive its parent's death (Hydra#70 smoke job).

    Windows: ``CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS | CREATE_NO_WINDOW``
    so the child is not attached to the parent's console/job object and a
    ``taskkill /T /F /PID <parent>`` (or the parent's own ``subprocess.run``
    timeout) does not reach into the child's process tree.
    POSIX: ``start_new_session=True`` (new session/process group) so the
    child is not in the parent's process group and survives a
    ``SIGTERM``/``SIGKILL`` sent to the parent alone.

    Hydra#70 follow-up (grandchild survives the WORKER but not the HOST
    SESSION): the flags above detach the child from the direct parent's own
    console/job object, but do NOT necessarily escape a job object that the
    PARENT's own process tree is itself enrolled in (e.g. the attended host
    session's job object, a CI runner that groups its whole process tree) --
    tearing down THAT job object can still reach down and kill every process
    still assigned to it, "detached" or not. ``breakaway=True`` (the
    default) additionally requests ``CREATE_BREAKAWAY_FROM_JOB`` on Windows
    so the child escapes the CURRENT job object when it permits breakaway
    (``JOB_OBJECT_LIMIT_BREAKAWAY_OK``). Not every job object allows this --
    when it does not, ``CreateProcess`` fails and ``subprocess.Popen`` raises
    ``OSError``/``PermissionError``; callers should use :func:`popen_detached`
    rather than this function directly, which retries the same spawn with
    ``breakaway=False`` on that failure instead of failing the spawn
    outright. ``breakaway=False`` is exposed here only for that retry (and
    for tests) -- new call sites should prefer :func:`popen_detached`.
    """
    if os.name == "nt":
        flags = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "DETACHED_PROCESS", 0)
            | no_window_creationflags()
        )
        if breakaway:
            flags |= getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0)
        return {"creationflags": flags}
    return {"start_new_session": True}


def popen_detached(cmd: Any, **kwargs: Any) -> "subprocess.Popen[Any]":
    """Spawn ``cmd`` fully detached (:func:`detached_popen_kwargs`), with a
    Windows-only breakaway fallback (Hydra#70 follow-up).

    On Windows, first attempts the spawn WITH ``CREATE_BREAKAWAY_FROM_JOB``
    (``breakaway=True``) so the child escapes the current job object (see
    :func:`detached_popen_kwargs`'s docstring for why that matters beyond
    the plain detached flags). When the current job object does not permit
    breakaway, ``subprocess.Popen`` raises ``OSError`` (frequently
    ``PermissionError``, a subclass of ``OSError``) whose ``winerror`` is
    exactly ``5`` (``ERROR_ACCESS_DENIED``) -- this, and ONLY this specific
    condition, retries the IDENTICAL spawn once more WITHOUT the breakaway
    flag rather than letting the caller's spawn fail outright. Any OTHER
    ``OSError`` (a missing executable -- ``FileNotFoundError``, winerror
    ``2``; a different access failure; a malformed argv) is a genuine
    launch failure unrelated to breakaway and re-raises immediately, WITHOUT
    a second ``Popen`` attempt -- retrying it would mask the real error
    while still failing identically the second time.

    What remains out of reach even with a successful breakaway: a full
    desktop logoff/shutdown that tears down the SESSION itself (not merely a
    job object) can still reap a detached child depending on how Windows
    session termination is configured for that station -- breakaway only
    ever addresses job-object-scoped kills (the parent's own console/job,
    a CI runner's or host session's process-tree job object), not a
    session-wide teardown.

    POSIX: no job-object concept exists; this is a thin pass-through to
    ``subprocess.Popen`` with :func:`detached_popen_kwargs`'s
    ``start_new_session=True``.
    """
    if os.name != "nt":
        return subprocess.Popen(cmd, **kwargs, **detached_popen_kwargs())
    try:
        return subprocess.Popen(cmd, **kwargs, **detached_popen_kwargs(breakaway=True))
    except OSError as e:
        # Only the breakaway-denied condition falls back to a second spawn
        # without the flag. On Windows, `CreateProcess` failing because the
        # current job object does not permit breakaway
        # (`JOB_OBJECT_LIMIT_BREAKAWAY_OK` unset) surfaces to Python as an
        # `OSError`/`PermissionError` whose `winerror` attribute is exactly
        # `5` (`ERROR_ACCESS_DENIED`) -- CPython's `subprocess` module maps
        # that Win32 error verbatim onto the raised exception's `winerror`.
        # Any OTHER `OSError` (a missing executable -> `FileNotFoundError`,
        # winerror 2 `ERROR_FILE_NOT_FOUND`; a different access failure; a
        # malformed argv) is a genuine launch failure unrelated to the
        # breakaway flag and must propagate immediately -- retrying it
        # without breakaway would silently mask the real error and still
        # fail identically on the second attempt anyway.
        if getattr(e, "winerror", None) != 5:
            raise
        return subprocess.Popen(cmd, **kwargs, **detached_popen_kwargs(breakaway=False))


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


# --------------------------------------------------------------------------- #
# Infra-interrupt exit-code classification (Hydra#70 follow-up).             #
#                                                                             #
# A smoke child killed by something EXTERNAL to the run under test -- Ctrl-C #
# propagated by console attach, a DLL failing to init because the desktop    #
# session is logging off, the whole process tree torn down mid-run -- exits  #
# with one of a small, well-known set of codes.  A smoke classifier that     #
# treats those the same as a genuine test failure (`fail`) discards an       #
# already-judged, already-passing commit for a reason that has nothing to do #
# with the code under test.  Shared by both smoke classifier call sites      #
# (`squad_node._run_smoke`, `smoke_job._run_smoke_tracked`) so the           #
# classification never drifts between them.                                 #
# --------------------------------------------------------------------------- #

# Windows NTSTATUS values (unsigned 32-bit form) that denote the process was
# torn down by something external to the run itself, not by the program's
# own logic:
#   0xC000013A STATUS_CONTROL_C_EXIT           -- Ctrl-C / Ctrl-Break delivered
#   0xC000026B STATUS_DLL_INIT_FAILED_LOGOFF   -- DLL init failed during logoff
#   0xC0000142 STATUS_DLL_INIT_FAILED          -- DLL init failed (desktop
#                                                  teardown / session churn)
_WIN_INFRA_NTSTATUS: frozenset[int] = frozenset({
    0xC000013A,
    0xC000026B,
    0xC0000142,
})

# POSIX: killed by SIGHUP(1)/SIGINT(2)/SIGKILL(9)/SIGTERM(15) -- either
# observed directly as Python's `returncode == -signum` (the child was
# reaped by this process, e.g. `Popen.communicate()`), or via the
# `128 + signum` convention a shell/wrapper uses when IT reports the exit
# status of a process it ran that died from that signal. Hardcoded numeric
# values (not `signal.SIGHUP` etc.) because those constants are unavailable
# on the `signal` module on Windows, and the numeric values themselves are
# stable across every POSIX platform.
_POSIX_INFRA_SIGNALS: frozenset[int] = frozenset({1, 2, 9, 15})
_POSIX_INFRA_128_PLUS_N: frozenset[int] = frozenset(128 + n for n in _POSIX_INFRA_SIGNALS)


def is_infra_interrupt_returncode(
    returncode: "int | None", *, platform_is_windows: "bool | None" = None
) -> bool:
    """``True`` when ``returncode`` denotes the process was interrupted by
    something EXTERNAL to the program under test (external Ctrl-C, a
    session logoff mid-run, a signal delivered from outside), never a
    genuine non-zero exit from the program's own logic.

    Critique follow-up: the POSIX signal-death shapes (a negative
    ``returncode``, or the ``128 + N`` shell/wrapper convention) are POSIX-
    only conventions. On Windows, 129/130/137/143 etc. are ordinary,
    unrelated application exit codes a real smoke command can legitimately
    return on a genuine failure -- treating them as "interrupted" there
    would excuse a real smoke failure as retryable infra. Likewise the
    Windows NTSTATUS values are a Windows-only concept. This function is
    therefore platform-gated: which shape it checks depends on
    ``platform_is_windows`` (defaults to the ACTUAL running platform,
    ``os.name == "nt"``, but is exposed as a keyword so tests can exercise
    both platforms' behaviour deterministically regardless of which OS
    pytest itself runs on).

    On Windows (``platform_is_windows`` true):
      - matches the raw Windows NTSTATUS values above, in EITHER their
        unsigned 32-bit form (``3221225786`` -- what a wrapper/shell
        typically reports as ITS OWN propagated exit code, e.g. ``node``
        printing ``ctest exited 3221225786``) or Python's signed 32-bit
        ``Popen.returncode`` form for the same value (a negative
        ``returncode`` is normalized to unsigned before the NTSTATUS
        comparison)
      - does NOT match the POSIX signal-death shapes at all

    On POSIX (``platform_is_windows`` false):
      - matches a POSIX signal death: ``returncode == -N`` (Python's own
        convention when it reaped the child) or ``returncode == 128 + N``
        (a shell/wrapper's reported exit status for a signal-killed child)
        for SIGHUP/SIGINT/SIGKILL/SIGTERM
      - does NOT match the Windows NTSTATUS shapes at all (they are far
        outside any plausible POSIX exit-code range in practice, but the
        explicit platform gate keeps the contract unambiguous either way)

    Deliberately conservative: a returncode that does not match one of these
    known external-interruption shapes returns ``False`` -- callers must
    still run their own infra-marker checks (e.g. a launcher-pattern regex)
    for other infra classes; this function is only ever ONE contributing
    signal, not the sole infra classifier.
    """
    if returncode is None:
        return False
    try:
        rc = int(returncode)
    except (TypeError, ValueError):
        return False
    is_windows = (os.name == "nt") if platform_is_windows is None else platform_is_windows
    if not is_windows:
        if rc < 0:
            return -rc in _POSIX_INFRA_SIGNALS
        return rc in _POSIX_INFRA_128_PLUS_N
    if rc < 0:
        rc &= 0xFFFFFFFF  # normalize a signed NTSTATUS-shaped value to unsigned
    return rc in _WIN_INFRA_NTSTATUS
