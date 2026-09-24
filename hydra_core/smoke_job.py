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

from .proc import detached_popen_kwargs, is_pid_alive, kill_process_tree

__all__ = [
    "job_paths",
    "start_job",
    "poll_job",
    "smoke_timeout_s",
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
    """Derive the job's result/log/lock file paths from the cursor file's
    location. ``call_key`` is folded into the filename so a NEW judge call
    (e.g. a Reflexion retry's judge-1) never reads a stale prior job's
    result file."""
    cf = Path(cursor_file)
    safe_key = "".join(c for c in call_key if c.isalnum() or c in "-_") or "job"
    base = cf.with_name(f"{cf.stem}.smoke-{safe_key}")
    return {
        "result_path": str(base.with_suffix(".result.json")),
        "log_path": str(base.with_suffix(".log")),
    }


def _atomic_write_json(path: str, data: dict[str, Any]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(tmp, p)


def start_job(cursor_file: str | Path, *, project_path: str, stage_id: str,
              call_key: str) -> dict[str, Any]:
    """Spawn the detached smoke job. Returns the ``cursor["smoke_job"]`` shape:
    ``{pid, started_at, deadline, result_path, log_path, call_key}``."""
    paths = job_paths(cursor_file, call_key)
    result_path = paths["result_path"]
    log_path = paths["log_path"]
    # A stale result from a previous run of THIS exact (cursor, call_key)
    # pair must not be read as fresh — clear it before spawning.
    try:
        os.remove(result_path)
    except FileNotFoundError:
        pass
    timeout_s = smoke_timeout_s()
    cmd = [
        sys.executable, "-m", "hydra_core.smoke_job",
        "--project-path", str(project_path),
        "--stage-id", str(stage_id),
        "--result-path", result_path,
        "--timeout-s", str(timeout_s),
    ]
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    # The job's cwd is the (worktree) project_path, not the Hydra repo root,
    # so `-m hydra_core.smoke_job` only resolves if the repo root (this
    # module's package parent) is on PYTHONPATH.
    _hydra_root = str(Path(__file__).resolve().parent.parent)
    _existing_pp = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        _hydra_root if not _existing_pp
        else os.pathsep.join([_hydra_root, _existing_pp])
    )
    Path(log_path).parent.mkdir(parents=True, exist_ok=True)
    started_at = time.time()
    with open(log_path, "ab") as log_f:
        proc = subprocess.Popen(  # noqa: S603 — fixed argv, validated tokens
            cmd, cwd=str(Path(project_path)) if Path(project_path).exists() else None,
            env=env, stdin=subprocess.DEVNULL, stdout=log_f, stderr=log_f,
            **detached_popen_kwargs(),
        )
    return {
        "pid": proc.pid,
        "started_at": started_at,
        # A grace buffer beyond the smoke's own internal timeout so a
        # slow-to-exit child (writing its result file) is not treated as
        # "vanished" by the poller the instant its internal deadline passes.
        "deadline": started_at + timeout_s + 60,
        "result_path": result_path,
        "log_path": log_path,
        "call_key": call_key,
    }


def poll_job(job: dict[str, Any]) -> dict[str, Any] | None:
    """Poll a job started by :func:`start_job`.

    Returns:
      - ``None`` if the job is still running and before its deadline
        (caller should return "still pending" without blocking).
      - ``{"status": ..., "reason": ..., ...}`` once a result is available OR
        the job is judged lost (deadline passed with no result file, or the
        process vanished without writing one) -- in the lost case this
        function KILLS any surviving tree first, so the caller never has to.
    """
    result_path = job.get("result_path")
    if result_path and Path(result_path).exists():
        try:
            return json.loads(Path(result_path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return {
                "status": "infra_error",
                "reason": f"smoke job result file unreadable: {exc!r}",
                "finished_at": time.time(),
            }

    pid = job.get("pid")
    deadline = float(job.get("deadline") or 0)
    now = time.time()
    alive = is_pid_alive(pid)

    if alive and now < deadline:
        return None  # still running, within budget — poll again later

    # Either the deadline has passed (job still running or wedged) or the
    # process is gone with no result file (crashed / killed externally /
    # never started cleanly). Either way this is an infra failure — kill
    # any surviving tree as a backstop (the job enforces its own internal
    # timeout, but this covers a wedged/zombie tree it failed to reap).
    kill_process_tree(pid)
    if now >= deadline:
        reason = f"smoke job exceeded its deadline ({job.get('deadline')!r}) and was killed"
    else:
        reason = "smoke job process vanished without writing a result file"
    return {
        "status": "infra_error",
        "reason": reason,
        "finished_at": now,
        "log_path": job.get("log_path"),
    }


def _run_smoke_tracked(project_path: str, stage_id: str,
                       timeout_s: int) -> tuple[str, str]:
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

    try:
        out_bytes, _ = proc.communicate(timeout=timeout_s)
        combined = out_bytes.decode("utf-8", errors="replace") if out_bytes else ""
        returncode = proc.returncode
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
    sees "no result" for a job that actually ran to completion or crashed."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-path", required=True)
    parser.add_argument("--stage-id", required=True)
    parser.add_argument("--result-path", required=True)
    parser.add_argument("--timeout-s", type=int, default=smoke_timeout_s())
    args = parser.parse_args(argv)

    try:
        status, reason = _run_smoke_tracked(
            args.project_path, args.stage_id, args.timeout_s)
    except Exception as e:  # noqa: BLE001 — a job crash is an infra result, not a hang
        status, reason = "infra_error", f"smoke job crashed: {e!r}"[:2000]

    _atomic_write_json(args.result_path, {
        "status": status,
        "reason": reason,
        "finished_at": time.time(),
    })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
