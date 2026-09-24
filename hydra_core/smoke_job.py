"""Detached smoke job runner (Hydra#70).

The attended judge-pass submit used to run the repo smoke SYNCHRONOUSLY
inside the single ``hydra.workflow.submit_host_result`` MCP call. The MCP
server runs the CLI via ``subprocess.run(timeout=HYDRA_SUBMIT_TIMEOUT_S)``
(default 1800s) while the smoke budget (``HYDRA_SMOKE_TIMEOUT_S``) defaults
to 2400s -- when a smoke ran past the call budget, ``subprocess.run`` killed
only the direct CLI child; the smoke process tree (node/ctest/clang-tidy,
spawned via ``run_text``) was orphaned and ran on with no timeout enforced,
the verdict was never recorded to the cursor, and the workflow could not
finish.

This module makes the smoke a TRACKED, DETACHED job that does not depend on
the submitting CLI process staying alive:

- ``start_job`` spawns ``python -m hydra_core.smoke_job`` in its own process
  group / session (:func:`hydra_core.proc.detached_popen_kwargs`) and
  returns immediately with the job's pid/paths.
- The spawned process (``main`` / ``__main__``) runs the smoke command
  directly (re-detected via ``squad_node._detect_smoke_command_and_cwd``,
  the SAME detection the synchronous path used), enforces
  ``HYDRA_SMOKE_TIMEOUT_S`` itself via a poll loop (not ``subprocess.run``'s
  own timeout, which only kills the direct child), and on timeout kills the
  WHOLE process tree with :func:`hydra_core.proc.kill_process_tree` before
  writing its result.
- ``poll_job`` reads the job's result file (if present), or -- if the
  deadline has passed and no result exists -- kills any surviving tree and
  synthesizes an ``infra_error`` result, so a lost job can never wedge the
  cursor.

The host_bridge caller records the judge verdict BEFORE calling
``start_job`` (see ``_apply_judge``), so a lost/killed job never loses the
verdict -- only the smoke gate is deferred to the async job.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .proc import (
    detached_popen_kwargs,
    is_pid_alive,
    is_same_process,
    kill_process_tree,
    process_identity,
)
from .strict_json import dumps_strict

__all__ = [
    "job_paths",
    "start_job",
    "poll_job",
    "smoke_timeout_s",
    "read_worker_marker",
]


def smoke_timeout_s() -> int:
    """Mirrors ``squad_node._smoke_timeout_s`` without importing squad_node at
    module scope (this module is invoked as ``python -m hydra_core.smoke_job``
    -- keeping the import inside functions avoids pulling in squad_node's
    heavier dependency surface at CLI-entry time)."""
    raw = os.environ.get("HYDRA_SMOKE_TIMEOUT_S")
    try:
        v = int(raw) if raw else 2400
    except (TypeError, ValueError):
        v = 2400
    return v if v > 0 else 2400


def job_paths(cursor_file: str | Path, call_key: str) -> dict[str, str]:
    """Derive the job's result/log/sidecar file paths from the cursor file's
    location. ``call_key`` is folded into the filename so a NEW judge call
    (e.g. a Reflexion retry's judge-1) never reads a stale prior job's
    result file.

    ``sidecar_path`` (P2-2) is where the worker records the SMOKE CHILD's
    own pid (and, on POSIX, its process group) immediately after spawning
    it -- separate from the worker's own pid (returned by ``start_job`` and
    recorded on ``cursor["smoke_job"]["pid"]``), so a caller can kill the
    smoke tree even once the worker process itself is gone (killed, crashed,
    or reaped) and can no longer be walked from.

    ``marker_path`` (D follow-up) is where the WORKER itself records its OWN
    pid + identity as its very first action in ``main()``, before doing
    anything slower (detecting/spawning the smoke command). A "launching"
    reservation (no pid saved on the cursor yet, see
    ``host_bridge._adopt_or_launch_smoke_job``) reads this marker to prove
    the worker is alive well before it gets far enough to write the sidecar
    -- closing the false-lost window for a worker that is simply slow to
    start (e.g. Windows interpreter cold-start)."""
    cf = Path(cursor_file)
    safe_key = "".join(c for c in call_key if c.isalnum() or c in "-_") or "job"
    base = cf.with_name(f"{cf.stem}.smoke-{safe_key}")
    return {
        "result_path": str(base.with_suffix(".result.json")),
        "log_path": str(base.with_suffix(".log")),
        "sidecar_path": str(base.with_suffix(".smokepid.json")),
        "marker_path": str(base.with_suffix(".workerpid.json")),
    }


def _atomic_write_json(path: str, data: dict[str, Any]) -> None:
    """Write the job result atomically, through ``strict_json.dumps_strict``
    (this is persisted decision state -- a bare ``json.dumps`` would silently
    accept a NaN/Infinity token that a strict downstream parser refuses).

    A ``dumps_strict`` refusal (a non-finite value, or any other
    JSON-strict-unsafe content) must never mean the job crashes with NO
    result file at all -- that reproduces exactly the "lost job" class this
    module exists to eliminate -- so it falls back to a minimal, guaranteed
    strict-safe error result instead of propagating.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    try:
        text = dumps_strict(data, label="smoke_job_result")
    except Exception as exc:  # noqa: BLE001 — see docstring: never lose the write
        text = dumps_strict({
            "status": "infra_error",
            "reason": (
                f"smoke job result was not JSON-strict-safe: {exc!r}"
            )[:2000],
            "finished_at": time.time(),
        }, label="smoke_job_result_fallback")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, p)


def start_job(cursor_file: str | Path, *, project_path: str, stage_id: str,
              call_key: str, launch_token: str | None = None) -> dict[str, Any]:
    """Spawn the detached smoke job. Returns the ``cursor["smoke_job"]`` shape:
    ``{pid, started_at, deadline, result_path, log_path, call_key}``.

    Hydra#70 follow-up (cross-vendor judge finding): this function must
    NEVER raise for a spawn/setup failure. The caller
    (``host_bridge._apply_judge`` / ``recover_stalled_stage``) invokes this
    AFTER the judge verdict has already been durably recorded -- a raised
    exception here would surface at the call site with no persisted
    outcome, leaving the cursor stuck in ``await_judge``/``await_smoke``
    with no job to poll and the verdict already spent. Any spawn/setup
    failure -- ``OSError``/``ValueError``/``subprocess.SubprocessError``
    (a missing interpreter on ``sys.executable``, a permission error
    creating the job's log directory or file, ``detached_popen_kwargs()``
    raising, ``subprocess.Popen`` itself raising
    ``OSError``/``FileNotFoundError``) -- is caught and turned into a
    terminal ``infra_error`` result: ``job["spawn_error"]`` is ALWAYS set to
    the reason (not only when the write below also fails), and the same
    result is best-effort written through the SAME atomic ``dumps_strict``
    writer (:func:`_atomic_write_json`) the job's own ``main()`` uses, so
    the job's very first :func:`poll_job` call resolves to that infra
    failure immediately (the returned job's ``deadline`` is already in the
    past). A caller does not have to poll at all to learn of the failure --
    it can check ``job.get("spawn_error")`` on the return value and
    finalize the stage synchronously right there (see
    ``host_bridge._finalize_immediate_smoke_spawn_failure``), never parking
    into ``await_smoke`` to await a poll of a job that never started. If the
    result-file write ALSO fails (e.g. the identical permission error that
    stopped the log directory being created), ``spawn_error`` is still set,
    so :func:`poll_job` (for any caller that DOES still park and poll) falls
    back to resolving from it directly -- the ``await_smoke`` poll ALWAYS
    finalizes as an infra smoke failure exactly like a lost job, never
    wedges, and never re-records the verdict.

    A genuine programming-error exception class (``TypeError``,
    ``AttributeError``, ...) is deliberately NOT caught here and propagates
    -- misclassifying a code bug as an infra spawn failure would silently
    hide it as an "environment problem" with no trace of what actually
    broke.

    If ``proc`` was actually started (``subprocess.Popen`` returned) but a
    LATER step in this function raises before returning, the spawned
    process tree is killed before this function reports "failed to spawn"
    -- otherwise a real, running process would be reported as never
    started and orphaned.

    R3 (cross-vendor gpt-6-astra, final re-review, LOW/hardening):
    ``launch_token`` -- when the caller supplies one (``host_bridge``'s
    ``_adopt_or_launch_smoke_job`` mints a fresh ``uuid4().hex`` per
    reservation) -- is threaded through to the worker's argv and echoed
    back into the marker/sidecar/result files it writes
    (:func:`_write_worker_marker`, :func:`_write_smoke_sidecar`, the result
    JSON in :func:`main`). A crash between the reservation save and
    ``start_job``'s own stale-result/-sidecar/-marker removal above could
    otherwise let a LATER retry adopt a result/marker/sidecar left over
    from an EARLIER run of the same deterministic ``(cursor_file,
    call_key)`` paths (e.g. ``recover_stalled_stage`` reusing the judge
    call_key). The adoption/poll call sites verify the token before
    trusting any of those files; ``None`` (a legacy caller that never
    passes one) skips the check entirely, preserving today's behaviour."""
    paths = job_paths(cursor_file, call_key)
    result_path = paths["result_path"]
    log_path = paths["log_path"]
    sidecar_path = paths["sidecar_path"]
    marker_path = paths["marker_path"]
    timeout_s = smoke_timeout_s()
    started_at = time.time()
    # Tracked separately from the try/except below (cross-vendor judge
    # finding): if Popen itself succeeds but a LATER step in this block
    # raises (e.g. the log file's `with open(...)` context-manager __exit__
    # failing on close/flush), the spawned process is real and running --
    # reporting "failed to spawn" without killing it would orphan a live
    # process tree while telling the caller nothing ever started.
    proc: "subprocess.Popen | None" = None
    try:
        # P2-4 (cross-vendor gpt-6-astra, MEDIUM): stale-result removal used
        # to sit OUTSIDE this guarded block, catching only FileNotFoundError
        # -- a PermissionError / Windows sharing violation (another process
        # still has the previous run's result file open) propagated straight
        # out of `start_job` AFTER the judge verdict was already durably
        # recorded, leaving the cursor stuck in `await_judge` instead of
        # resolving synchronously as an infra_error like every other
        # spawn/setup failure below. Moved inside the guarded setup path so
        # ANY removal failure (not just a permission error) is caught by the
        # same `except (OSError, ValueError, subprocess.SubprocessError)`
        # below and resolves the same way. A stale result that could not be
        # removed must also never be read as THIS job's result -- resolved
        # by construction here: this function returns before ever spawning a
        # NEW job whose poll could read the old file, and the infra_error
        # result written below (when the write itself succeeds) overwrites
        # it via the same atomic `os.replace` every other writer uses.
        try:
            os.remove(result_path)
        except FileNotFoundError:
            pass
        # Clear a stale sidecar the same way — an old smoke child's pid must
        # never be read as belonging to a job this call is about to spawn.
        try:
            os.remove(sidecar_path)
        except FileNotFoundError:
            pass
        # Clear a stale worker marker too -- an old worker's pid/identity
        # must never be read as belonging to THIS about-to-be-spawned job.
        try:
            os.remove(marker_path)
        except FileNotFoundError:
            pass
        cmd = [
            sys.executable, "-m", "hydra_core.smoke_job",
            "--project-path", str(project_path),
            "--stage-id", str(stage_id),
            "--result-path", result_path,
            "--sidecar-path", sidecar_path,
            "--marker-path", marker_path,
            "--timeout-s", str(timeout_s),
        ]
        if launch_token:
            cmd.extend(["--launch-token", str(launch_token)])
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        # The job's cwd is the (worktree) project_path, not the Hydra repo
        # root, so `-m hydra_core.smoke_job` only resolves if the repo root
        # (this module's package parent) is on PYTHONPATH.
        _hydra_root = str(Path(__file__).resolve().parent.parent)
        _existing_pp = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = (
            _hydra_root if not _existing_pp
            else os.pathsep.join([_hydra_root, _existing_pp])
        )
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "ab") as log_f:
            proc = subprocess.Popen(  # noqa: S603 — fixed argv, validated tokens
                cmd, cwd=str(Path(project_path)) if Path(project_path).exists() else None,
                env=env, stdin=subprocess.DEVNULL, stdout=log_f, stderr=log_f,
                **detached_popen_kwargs(),
            )
    # Cross-vendor judge finding: narrowed from a bare `except Exception` --
    # that swallowed a `TypeError`/`AttributeError` from a genuine
    # programming error (a bad argument to `Popen`, a broken
    # `detached_popen_kwargs()`) as an indistinguishable "infra spawn
    # failure", silently reclassifying a code bug as an environment problem
    # with no trace of what actually happened. Only the real spawn/setup
    # failure classes (missing interpreter, permission error, OS-level
    # launch failure) are caught here; a programming error still propagates
    # so it fails loudly instead of masquerading as infra_error.
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        if proc is not None:
            # Popen succeeded; something AFTER it (e.g. the log file's
            # context-manager teardown) is what actually raised. Do not
            # leave that process running unaccounted for.
            kill_process_tree(proc.pid)
        reason = f"smoke job failed to spawn: {exc!r}"[:2000]
        finished_at = time.time()
        job: dict[str, Any] = {
            "pid": None,
            "started_at": started_at,
            # Already-past deadline: a job that never spawned has nothing to
            # wait on — the poller must resolve it on its very first poll.
            "deadline": started_at - 1,
            "result_path": result_path,
            "log_path": log_path,
            "sidecar_path": sidecar_path,
            "marker_path": marker_path,
            "call_key": call_key,
            "launch_token": launch_token,
            # Always populated on a spawn failure (not just when the result
            # write below also fails): call sites (`host_bridge._apply_judge`
            # / `recover_stalled_stage`) check this to finalize the stage
            # synchronously as an infra smoke failure instead of parking
            # into `await_smoke` to poll a job that never started.
            "spawn_error": reason,
        }
        try:
            _atomic_write_json(result_path, {
                "status": "infra_error",
                "reason": reason,
                "finished_at": finished_at,
                "launch_token": launch_token,
            })
        except Exception:  # noqa: BLE001 — the write itself can fail (e.g. the
            # same permission error that stopped the log dir from being
            # created); job["spawn_error"] (set above unconditionally) is
            # what the caller/poller falls back to in that case, so the
            # caller still gets a correctly-reasoned terminal outcome
            # instead of a generic "vanished" one.
            pass
        return job
    return {
        "pid": proc.pid,
        # Durable identity fix (cross-vendor judge finding): captured
        # immediately after Popen returns, so a later liveness/kill check
        # against this recorded pid can verify it is still the SAME process
        # -- never just that SOME process currently holds this pid (a
        # Windows pid can be reused within moments of the original exiting).
        # See `hydra_core.proc.process_identity`/`is_same_process`.
        "pid_identity": process_identity(proc.pid),
        "started_at": started_at,
        # A grace buffer beyond the smoke's own internal timeout so a
        # slow-to-exit child (writing its result file) is not treated as
        # "vanished" by the poller the instant its internal deadline passes.
        "deadline": started_at + timeout_s + 60,
        "result_path": result_path,
        "log_path": log_path,
        "sidecar_path": sidecar_path,
        "marker_path": marker_path,
        "call_key": call_key,
        "launch_token": launch_token,
    }


# Hydra#70 follow-up (flake investigation): `is_pid_alive` and the result
# file's `os.replace` rename are two INDEPENDENT observations of the SAME
# child process's exit, made from a DIFFERENT process (this poller) through
# two different OS-level channels (a process-table probe vs. a filesystem
# rename's visibility). Under heavy CPU/disk load (an antivirus scanner
# holding a lock on the rename, an overloaded NTFS MFT, a delayed
# `tasklist` snapshot) those two observations can land on either side of a
# race: the child has ALREADY exited (`is_pid_alive` correctly reports
# False) a hair before the result file it wrote a moment earlier becomes
# visible to THIS process's `Path.exists()`. A poller that immediately
# classifies "not alive + no result file" as "vanished" (Hydra#70's
# original shape) can therefore genuinely lose a job that succeeded --
# reproduced live under synthetic CPU load (10 busy processes) in a stress
# loop: 1/20 real integration-test runs surfaced with `smoke_status
# infra_error` / "vanished" even though the job actually completed. Give
# the result file a short grace window to appear once the process is
# observed dead before concluding it is lost.
_VANISHED_GRACE_RETRIES = 8
_VANISHED_GRACE_INTERVAL_S = 0.25


def _read_result_file(result_path: str) -> dict[str, Any] | None:
    """Read+parse the result file if it exists; ``None`` if absent. A file
    that exists but fails to parse (a genuine race with the writer's
    ``os.replace`` — vanishingly rare, since the rename is atomic, but not
    provably impossible under a hostile filesystem driver) is treated the
    SAME as absent here so the retry loop below gives it another lap
    instead of a caller seeing a transient parse error as a hard failure."""
    if not Path(result_path).exists():
        return None
    try:
        return json.loads(Path(result_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _read_smoke_sidecar(sidecar_path: "str | None") -> dict[str, Any] | None:
    """Read+parse the P2-2 smoke-pid sidecar if present; ``None`` if absent
    or unreadable -- the sidecar is written best-effort by the worker (see
    :func:`_write_smoke_sidecar`), so its absence (worker died before ever
    writing it, or never spawned the smoke child at all) must be tolerated,
    not treated as an error."""
    if not sidecar_path:
        return None
    p = Path(sidecar_path)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def read_worker_marker(marker_path: "str | None") -> dict[str, Any] | None:
    """Read+parse the (D-follow-up) worker marker if present; ``None`` if
    absent or unreadable. Public (unlike ``_read_smoke_sidecar``) so
    ``host_bridge._adopt_or_launch_smoke_job`` can read it too without
    reimplementing the same best-effort parse. The worker writes this as its
    very FIRST action in ``main()`` -- its presence with a VERIFIED identity
    (:func:`hydra_core.proc.is_same_process`) is proof the worker is alive
    long before it gets far enough to write the P2-2 sidecar."""
    if not marker_path:
        return None
    p = Path(marker_path)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _write_worker_marker(
    marker_path: "str | None", launch_token: "str | None" = None,
) -> None:
    """(D follow-up) Write THIS process's own pid + identity to
    ``marker_path`` -- called as the very first action in ``main()``, before
    importing/detecting/spawning anything slower. Best-effort: a write
    failure here must never abort the smoke run itself (mirrors every other
    best-effort marker/sidecar writer in this module) -- worst case, a
    "launching" reservation adoption simply falls back to the sidecar/result
    evidence it already used before this marker existed.

    R3: ``launch_token`` (when the caller supplied one) is echoed into the
    marker so an adopting caller can verify this marker belongs to THIS
    reservation, not a stale one left over from an earlier run of the same
    deterministic call_key paths."""
    if not marker_path:
        return
    try:
        _atomic_write_json(marker_path, {
            "pid": os.getpid(),
            "pid_identity": process_identity(os.getpid()),
            "launch_token": launch_token,
        })
    except Exception:  # noqa: BLE001 — best-effort, see docstring
        pass


def _clear_worker_marker(marker_path: "str | None") -> None:
    """Retire the worker marker once the job has written its terminal
    result -- nothing later should ever need to adopt a worker that has
    already finished (mirrors :func:`_clear_smoke_sidecar`). Best-effort."""
    if not marker_path:
        return
    try:
        os.remove(marker_path)
    except FileNotFoundError:
        pass
    except OSError:  # noqa: BLE001 — best-effort cleanup only
        pass


def _kill_recorded_smoke_child(job: dict[str, Any]) -> None:
    """P2-2: kill the smoke child recorded in the sidecar, independent of
    the worker's own pid. Called from every ``poll_job`` cleanup path that
    already kills the worker tree (``kill_process_tree(pid)``) -- that call
    alone cannot always reach the smoke tree once the worker itself is gone
    (Windows ``taskkill /T`` needs a live root to walk from; POSIX
    ``killpg`` on the worker's own pgid never reaches the smoke child's
    SEPARATE session/group). Tolerates an absent sidecar entirely (the
    worker died, or was killed, before ever writing one).

    Durable identity fix (cross-vendor judge finding): the sidecar-recorded
    pid is verified via :func:`hydra_core.proc.is_same_process` against the
    identity captured at sidecar-write time (see :func:`_write_smoke_sidecar`)
    BEFORE any kill is issued. ``is_pid_alive`` alone only proves SOME
    process currently holds that pid -- if the real smoke child already
    exited and the OS reused the pid (common and fast on Windows), an
    unverified kill would tear down an unrelated process tree. A pid whose
    identity cannot be verified (no identity was recorded, or the current
    holder's identity cannot be read / does not match) is treated as NOT the
    recorded smoke child and is never killed."""
    sidecar = _read_smoke_sidecar(job.get("sidecar_path"))
    if not sidecar:
        return
    smoke_pid = sidecar.get("pid")
    smoke_identity = sidecar.get("pid_identity")
    if not (isinstance(smoke_pid, int) and smoke_pid > 0
            and is_same_process(smoke_pid, smoke_identity)):
        return
    # Reaches the smoke child directly (Windows: a live `taskkill /T`
    # root; POSIX: `os.getpgid(smoke_pid)` while it is still lookupable).
    kill_process_tree(smoke_pid)
    if os.name != "nt":
        pgid = sidecar.get("pgid")
        if isinstance(pgid, int) and pgid > 0:
            # Belt-and-braces: once the smoke pid itself has already been
            # reaped (zombied), `os.getpgid(smoke_pid)` inside
            # `kill_process_tree` above can no longer resolve it, stranding
            # any surviving grandchild still in that same process group.
            # The pgid was captured explicitly at spawn time (P2-2) so it
            # is still killable directly here even in that case. Gated on
            # the SAME identity check above -- the pgid is only trustworthy
            # as long as `smoke_pid` was verified to still be the recorded
            # process (a pgid alone carries no independent identity proof).
            import signal
            try:
                os.killpg(pgid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass


def _result_matches_launch_token(found: dict[str, Any], job: dict[str, Any]) -> bool:
    """R3: a job whose reservation minted a ``launch_token`` only ever
    trusts a result file that echoes the SAME token back -- a crash between
    the reservation save and ``start_job``'s own stale-result removal could
    otherwise leave a LATER retry reading a result left over from an
    EARLIER run of the same deterministic ``(cursor_file, call_key)`` paths
    (e.g. ``recover_stalled_stage`` reusing the judge call_key). A job with
    no ``launch_token`` recorded (a legacy in-flight job, or any caller that
    never passed one to :func:`start_job`) keeps today's behaviour --
    trusts whatever result file it finds, unconditionally."""
    job_token = job.get("launch_token")
    if job_token is None:
        return True
    return found.get("launch_token") == job_token


def poll_job(job: dict[str, Any]) -> dict[str, Any] | None:
    """Poll a job started by :func:`start_job`.

    Returns:
      - ``None`` if the job is still running and before its deadline
        (caller should return "still pending" without blocking).
      - ``{"status": ..., "reason": ..., ...}`` once a result is available OR
        the job is judged lost (deadline passed with no result file even
        after a short grace window, or the process vanished without ever
        writing one) -- in the lost case this function KILLS any surviving
        tree first, so the caller never has to.
    """
    result_path = job.get("result_path")
    if result_path:
        found = _read_result_file(result_path)
        if found is not None and _result_matches_launch_token(found, job):
            return found

    spawn_error = job.get("spawn_error")
    if spawn_error:
        # start_job failed to spawn AND failed to persist a result file for
        # it (see start_job's docstring) — resolve immediately from the
        # captured reason instead of falling through to the vanished-grace
        # window / generic "process vanished" reason below, which would lose
        # the actual spawn failure detail.
        return {
            "status": "infra_error",
            "reason": spawn_error,
            "finished_at": time.time(),
            "log_path": job.get("log_path"),
        }

    pid = job.get("pid")
    deadline = float(job.get("deadline") or 0)
    now = time.time()
    alive = is_pid_alive(pid)

    # R1 (cross-vendor gpt-6-astra, MEDIUM): the pre-deadline "still
    # running, poll again" early return used to trust a bare
    # `is_pid_alive(pid)` -- if the worker died without writing a result
    # and its pid was reused by an unrelated process before this poll, the
    # job read as "alive" and the vanished/lost classification was delayed
    # all the way out to the deadline (default ~41min) even though the
    # recorded worker is long gone. Verify identity the same way the kill
    # path below already does. A legacy in-flight job whose `pid_identity`
    # was never recorded (started before this field existed) keeps the old
    # bare-`is_pid_alive` liveness behaviour here -- for liveness ONLY, never
    # for a kill (the kill path below independently requires a verified
    # identity match and simply skips killing an unverifiable pid).
    pid_identity = job.get("pid_identity")
    if pid_identity is not None:
        alive_verified = bool(pid) and is_same_process(pid, pid_identity)
    else:
        alive_verified = alive

    if alive_verified and now < deadline:
        return None  # still running, within budget — poll again later

    if not alive and result_path:
        # The process is gone (or was never seen alive) and no result file
        # exists YET -- see the module-level race note above. Retry across a
        # short grace window before concluding the job is truly lost; a
        # genuinely crashed/killed job still resolves to infra_error, just
        # not on a false-negative race.
        for _ in range(_VANISHED_GRACE_RETRIES):
            time.sleep(_VANISHED_GRACE_INTERVAL_S)
            found = _read_result_file(result_path)
            if found is not None and _result_matches_launch_token(found, job):
                return found

    # Either the deadline has passed (job still running or wedged) or the
    # process is gone with no result file even after the grace window
    # (crashed / killed externally / never started cleanly). Either way
    # this is an infra failure — kill any surviving tree as a backstop (the
    # job enforces its own internal timeout, but this covers a
    # wedged/zombie tree it failed to reap). P2-2: the worker's own pid
    # alone may not reach the smoke tree once the worker is gone -- also
    # kill the smoke child recorded in the sidecar (tolerates its absence).
    #
    # Durable identity fix (cross-vendor judge finding): `pid` here is the
    # WORKER pid recorded at spawn time (`start_job`'s `pid_identity`) -- an
    # `is_pid_alive(pid)` pass alone does not prove it is still that SAME
    # worker (the OS can reuse a pid the instant the real worker exits).
    # Verify identity before killing; an unverifiable/mismatched pid is
    # never killed as if it were the recorded worker.
    if pid and is_same_process(pid, job.get("pid_identity")):
        kill_process_tree(pid)
    _kill_recorded_smoke_child(job)
    now = time.time()
    if now >= deadline:
        reason = f"smoke job exceeded its deadline ({job.get('deadline')!r}) and was killed"
    else:
        reason = (
            "smoke job process vanished without writing a result file "
            f"(waited {_VANISHED_GRACE_RETRIES * _VANISHED_GRACE_INTERVAL_S}s grace period)"
        )
    return {
        "status": "infra_error",
        "reason": reason,
        "finished_at": now,
        "log_path": job.get("log_path"),
    }


def _write_smoke_sidecar(
    sidecar_path: str | None, proc: "subprocess.Popen",
    launch_token: "str | None" = None,
) -> None:
    """P2-2 (cross-vendor gpt-6-astra, MEDIUM): record the SMOKE CHILD's own
    pid (and, on POSIX, its process group) atomically to the sidecar,
    immediately after ``Popen`` returns -- BEFORE this process blocks on
    ``communicate()``. The job record on the cursor only ever carries the
    WORKER's pid; if the worker itself dies (killed, crashed) or the poller
    enforces the deadline after losing the worker, ``kill_process_tree
    (worker_pid)`` alone cannot reach the smoke tree once the worker is gone
    (a live Windows ``taskkill /T`` needs a live root to walk from; POSIX
    ``killpg`` targets the WORKER's own process group, not the smoke
    child's). The smoke child is spawned via
    :func:`detached_popen_kwargs` too (``start_new_session=True`` on POSIX),
    so it is its own session/group leader -- ``proc.pid`` doubles as the
    pgid there, but the pgid is captured explicitly (rather than re-derived
    later via ``os.getpgid``) so cleanup still has it even once the pid
    itself can no longer be looked up (already reaped/zombied).

    Best-effort: a write failure here must never abort the smoke run itself
    (the sidecar is an optimization for the "worker died" cleanup path, not
    a correctness requirement for the ordinary case where the worker
    survives to poll its own child directly)."""
    if not sidecar_path:
        return
    payload: dict[str, Any] = {
        "pid": proc.pid,
        # Durable identity fix (cross-vendor judge finding): captured
        # alongside the pid so a later kill/adopt decision against this
        # sidecar can verify the pid still refers to the SAME smoke child
        # before acting on it -- see `hydra_core.proc.is_same_process`.
        "pid_identity": process_identity(proc.pid),
        # R3: echoed so an adopting caller can verify this sidecar belongs
        # to THIS reservation, not a stale one left by an earlier run of the
        # same deterministic call_key paths.
        "launch_token": launch_token,
    }
    if os.name != "nt":
        try:
            payload["pgid"] = os.getpgid(proc.pid)
        except OSError:
            pass
    try:
        _atomic_write_json(sidecar_path, payload)
    except Exception:  # noqa: BLE001 — best-effort, see docstring
        pass


def _clear_smoke_sidecar(sidecar_path: "str | None") -> None:
    """(C) Retire the P2-2 sidecar once the smoke child it describes has
    exited normally -- belt-and-suspenders with the identity check in
    :func:`_kill_recorded_smoke_child`/``poll_job``'s deadline path: a
    cleared sidecar leaves nothing stale for a LATER poll (e.g. a slow
    worker-exit race, or a wedged poller retry) to ever act on, even before
    considering identity. Best-effort -- a removal failure here must never
    fail the smoke run itself."""
    if not sidecar_path:
        return
    try:
        os.remove(sidecar_path)
    except FileNotFoundError:
        pass
    except OSError:  # noqa: BLE001 — best-effort cleanup only
        pass


def _run_smoke_tracked(project_path: str, stage_id: str,
                       timeout_s: int, *,
                       sidecar_path: str | None = None,
                       launch_token: str | None = None) -> tuple[str, str]:
    """Run the smoke command directly (Popen, not ``run_text``) so this
    process controls the child's pid and can kill its WHOLE tree
    (:func:`kill_process_tree`) on timeout -- reaching grandchildren
    ``subprocess.run``'s own ``timeout=`` kill does not reach.

    Mirrors ``squad_node._run_smoke``'s command detection + classification so
    the async job and the (still-supported, sync-mode) inline path agree on
    status semantics."""
    from .squad_node import (  # local import — see module docstring
        _INFRA_SMOKE_RE,
        _detect_smoke_command_and_cwd,
        _write_smoke_log,
    )

    cmd, smoke_cwd = _detect_smoke_command_and_cwd(project_path)
    if not cmd:
        return "skipped", "no runnable build/test command detected"

    use_shell = os.name == "nt" and cmd[0].lower() in ("npm", "npx", "yarn", "pnpm")
    launch_cmd = " ".join(cmd) if use_shell else cmd
    try:
        proc = subprocess.Popen(  # noqa: S603 — argv detected, not user text
            launch_cmd, cwd=smoke_cwd, shell=use_shell,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            **detached_popen_kwargs(),
        )
    except Exception as e:  # noqa: BLE001 — launch failure is infra
        return "infra_error", f"smoke could not launch ({' '.join(cmd)}): {e!r}"[:300]

    # P2-2: write the sidecar immediately after Popen succeeds, before this
    # process blocks on `communicate()` below -- a worker killed mid-smoke
    # must still leave the sidecar behind for the poller's cleanup path.
    _write_smoke_sidecar(sidecar_path, proc, launch_token)

    try:
        out_bytes, _ = proc.communicate(timeout=timeout_s)
        combined = out_bytes.decode("utf-8", errors="replace") if out_bytes else ""
        returncode = proc.returncode
        # (C) Normal completion -- retire the sidecar now, not just on the
        # (rarer) timeout path, so nothing stale can be acted on later.
        _clear_smoke_sidecar(sidecar_path)
    except subprocess.TimeoutExpired:
        kill_process_tree(proc.pid)
        try:
            out_bytes, _ = proc.communicate(timeout=15)
        except Exception:  # noqa: BLE001
            out_bytes = b""
        combined = out_bytes.decode("utf-8", errors="replace") if out_bytes else ""
        artifact = _write_smoke_log(project_path, stage_id, combined)
        log_suffix = f" :: full_log={artifact}" if artifact else ""
        return (
            "infra_error",
            f"smoke timed out after {timeout_s}s (whole process tree killed): "
            f"{' '.join(cmd)}{log_suffix}"[:2000],
        )

    label = " ".join(cmd)
    if returncode != 0 and _INFRA_SMOKE_RE.search(combined):
        artifact = _write_smoke_log(project_path, stage_id, combined)
        tail = combined.strip().splitlines()[-1:] or [""]
        reason = f"`{label}` exit={returncode} (infra) :: {tail[0]}"
        if artifact:
            reason += f" :: full_log={artifact}"
        return "infra_error", reason[:2000]

    status = "pass" if returncode == 0 else "fail"
    tail = combined.strip().splitlines()[-1:] or [""]
    if status == "fail":
        artifact = _write_smoke_log(project_path, stage_id, combined)
        failed_lines = [ln for ln in combined.splitlines() if ln.startswith("FAILED ")][:20]
        reason = f"`{label}` exit={returncode} :: {tail[0]}"
        if failed_lines:
            reason += f" :: failed=[{'; '.join(failed_lines)}]"
        if artifact:
            reason += f" :: full_log={artifact}"
        return status, reason[:2000]
    return status, f"`{label}` exit={returncode} :: {tail[0]}"[:300]


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``python -m hydra_core.smoke_job`` (spawned detached by
    :func:`start_job`). Writes the result atomically to ``--result-path`` on
    every exit path, including an unexpected exception, so the poller never
    sees "no result" for a job that actually ran to completion or crashed.

    (D follow-up) The VERY FIRST action after parsing argv is writing this
    process's own worker marker (pid + durable identity) to
    ``--marker-path`` -- before ``_run_smoke_tracked`` is even called (that
    is where the slow work lives: importing ``squad_node``, detecting the
    smoke command, spawning it). A "launching" reservation with no pid saved
    on the cursor yet can then prove this worker is alive from the marker
    alone, long before it gets far enough to write the P2-2 sidecar."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-path", required=True)
    parser.add_argument("--stage-id", required=True)
    parser.add_argument("--result-path", required=True)
    parser.add_argument("--sidecar-path", default=None)
    parser.add_argument("--marker-path", default=None)
    parser.add_argument("--timeout-s", type=int, default=smoke_timeout_s())
    # R3 (cross-vendor gpt-6-astra, final re-review, LOW/hardening): opaque
    # per-reservation token echoed into every marker/sidecar/result file
    # this worker writes, so an adopting caller can verify none of it is
    # stale evidence from an earlier run of the same deterministic
    # (cursor_file, call_key) paths. Absent for a legacy invocation.
    parser.add_argument("--launch-token", default=None)
    args = parser.parse_args(argv)

    _write_worker_marker(args.marker_path, args.launch_token)

    try:
        status, reason = _run_smoke_tracked(
            args.project_path, args.stage_id, args.timeout_s,
            sidecar_path=args.sidecar_path, launch_token=args.launch_token)
    except Exception as e:  # noqa: BLE001 — a job crash is an infra result, not a hang
        status, reason = "infra_error", f"smoke job crashed: {e!r}"[:2000]

    _atomic_write_json(args.result_path, {
        "status": status,
        "reason": reason,
        "finished_at": time.time(),
        "launch_token": args.launch_token,
    })
    # The worker has now reached a terminal result -- nothing should ever
    # adopt it as "still launching" again. Best-effort, symmetrical with
    # (C)'s sidecar retirement on normal smoke completion.
    _clear_worker_marker(args.marker_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
