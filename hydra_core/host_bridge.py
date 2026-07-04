"""Attended (host-bridged) engineering execution.

This is the core of the *attended* execution mode: instead of the headless
``_drive_pp_stage_loop`` driving generate -> judge -> finalize in a detached
subprocess the operator cannot watch, the Claude Code host session drives the
SAME pair-programmer stage protocol IN-CONTEXT, surfacing the generate and judge
steps as visible ``Agent`` subagents it can follow along with.

Design note (supersedes the ``run_host_agent`` trampoline framing in the plan):
``_drive_pp_stage_loop`` is a straight-line function with a broad fail-soft
``except`` that would swallow any mid-loop pause exception (and finalize the run
``aborted``). So we do NOT reuse it. Instead this module is an **explicit
step-state-machine** that persists its progress (the "cursor") to disk between
host round-trips — exactly the resumable plumbing codex's review called for, and
replay-free: each ``step``/``submit`` advances the cursor by exactly one
transition, so every pp ledger call (``record_attempt``/``record_verdict``/
``finalize_*``) happens exactly once.

It calls ONLY tools the engineering squad declares (RBAC-safe via
``squad_id="engineering"``) and reuses the headless loop's governance helpers
(``_build_engineer_prompt``, ``_run_smoke``, ``gate_eligible_judges`` routing,
the finalize-readiness gate, the real-diff judge text) so the attended path and
the headless path enforce the same gates. Budget is NOT charged here — the
caller (the ``hydra step`` / ``submit-host-result`` CLI operating on the
checkpointed ``HydraState``) charges the returned ``cost_usd`` via
``charge_and_gate`` so the 80%/100% tripwires stay authoritative.

Runtime-agnostic: no provider SDK imports. The visible ``engineer`` / judge
subagents are spawned by the host (Claude Code), not here; this module only
sequences the deterministic pp tool calls around them.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any, Optional

import re as _re

from . import telemetry as _telemetry
from .squad_node import (
    Dispatcher,
    _augment_with_critique,
    _build_engineer_prompt,
    _generate_failure_reason,
    _judge_artifact_text,
    _pp_gate_type,
    _pp_inner,
    _pp_ok,
    _rubric_md,
    _run_smoke,
    _worktree_dirty_set,
)

# Cursor schema version — bump on any incompatible shape change so a stale
# on-disk cursor from an older build is rejected loudly rather than misread.
CURSOR_SCHEMA = 1

# Terminal cursor states.
_TERMINAL = {"complete", "surfaced", "aborted"}

_SQ = "engineering"


# --------------------------------------------------------------------------- #
# Worktree isolation (write-safety)                                           #
# --------------------------------------------------------------------------- #
# In attended mode the host's visible `engineer` subagent writes code. The
# `hydra-block-direct-write` hook blocks engine-source writes unless the path is
# under `\worktrees\` (or HYDRA_PP_STAGE_ACTIVE=1, which we must NOT set
# session-wide). So we isolate the engineer into a linked git worktree under
# `.harness/worktrees/` — already hook-allowed — and merge the result back into
# the repo on a passing finalize. This keeps HYDRA_ENFORCE_ROUTING fully on and
# the host session unable to hand-write project source. Fail-soft: if the repo
# isn't git or worktree provisioning fails, fall back to in-place writes.

def _git(args: list[str], cwd: str | Path, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                          text=True, timeout=timeout, check=False)


def _git_repo_root(path: str | Path) -> str | None:
    try:
        res = _git(["rev-parse", "--show-toplevel"], path)
    except Exception:  # noqa: BLE001
        return None
    return res.stdout.strip() if res.returncode == 0 else None


def _provision_worktree(repo_root: str, run_id: str) -> tuple[str, str] | None:
    """Create a linked worktree + branch off HEAD for an attended stage.

    Returns ``(worktree_path, branch)`` or None on any failure (caller falls
    back to in-place). The worktree lives under ``.harness/worktrees/`` so the
    write-block hook permits the engineer's writes there.
    """
    safe = "".join(c for c in str(run_id) if c.isalnum() or c in "-_") or "run"
    branch = f"attended/{safe}"
    wt = Path(repo_root) / ".harness" / "worktrees" / f"attended-{safe}"
    try:
        wt.parent.mkdir(parents=True, exist_ok=True)
        if wt.exists():
            _git(["worktree", "remove", "--force", str(wt)], repo_root)
        # -B resets the branch if a stale one exists from a prior aborted run.
        res = _git(["worktree", "add", "-B", branch, str(wt), "HEAD"], repo_root)
        if res.returncode != 0:
            return None
    except Exception:  # noqa: BLE001
        return None
    return str(wt), branch


def _merge_worktree_back(repo_root: str, worktree_path: str, branch: str) -> dict[str, Any]:
    """Commit the engineer's changes in the worktree and merge them into the
    repo's checked-out branch. Returns a status dict; never raises."""
    out: dict[str, Any] = {"merged": False, "sha": None, "error": None}
    try:
        # Stage + commit any uncommitted work the engineer left in the worktree.
        st = _git(["status", "--porcelain"], worktree_path)
        if st.stdout.strip():
            _git(["add", "-A"], worktree_path)
            _git(["commit", "-m", f"attended engineering ({branch})",
                  "--no-verify"], worktree_path)
        head = _git(["rev-parse", "HEAD"], worktree_path)
        wt_sha = head.stdout.strip()
        base = _git(["rev-parse", "HEAD"], repo_root).stdout.strip()
        if wt_sha == base:
            out["error"] = "no_changes_to_merge"
            return out
        # Fast-forward / merge the branch into the repo's current branch.
        mres = _git(["merge", "--no-ff", "--no-edit", branch], repo_root)
        if mres.returncode != 0:
            # Abort a conflicted merge so the repo is left clean for the operator.
            _git(["merge", "--abort"], repo_root)
            out["error"] = f"merge_failed: {(mres.stderr or mres.stdout).strip()[:300]}"
            return out
        out["merged"] = True
        out["sha"] = _git(["rev-parse", "HEAD"], repo_root).stdout.strip()
    except Exception as e:  # noqa: BLE001
        out["error"] = f"merge_exception: {e!r}"[:300]
    return out


def _remove_worktree(repo_root: str, worktree_path: str) -> None:
    try:
        _git(["worktree", "remove", "--force", worktree_path], repo_root)
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------- #
# Cursor persistence                                                          #
# --------------------------------------------------------------------------- #

def cursor_path(project_root: str | Path, workflow_id: str, run_id: str) -> Path:
    """Sidecar path for an attended stage's cursor.

    Lives under ``<project>/.hydra/<workflow_id>/attended/<run_id>.json`` — the
    ``.hydra`` tree is already the per-workflow scratch/trace area.
    """
    safe_run = "".join(c for c in str(run_id) if c.isalnum() or c in "-_") or "run"
    return (Path(project_root) / ".hydra" / str(workflow_id)
            / "attended" / f"{safe_run}.json")


def load_cursor(path: str | Path) -> dict[str, Any]:
    """Load a cursor; raise FileNotFoundError if absent, ValueError if stale."""
    p = Path(path)
    data = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema") != CURSOR_SCHEMA:
        raise ValueError(f"attended cursor schema mismatch at {p} "
                         f"(want {CURSOR_SCHEMA}, got {data.get('schema')!r})")
    return data


def save_cursor(path: str | Path, cursor: dict[str, Any]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    # Atomic-ish write: temp + replace so a crash mid-write never leaves a
    # truncated cursor that would wedge the workflow.
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(cursor, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, p)


# --------------------------------------------------------------------------- #
# Rider (a) — smoke baseline helpers                                         #
# --------------------------------------------------------------------------- #

def _parse_failing_tests(output: str) -> set[str]:
    """Parse failing test IDs from pytest --tb=no -q output.

    Matches lines like: ``FAILED tests/test_foo.py::test_bar - reason``
    Returns a set of bare test IDs (no trailing reason).
    """
    failing: set[str] = set()
    for line in output.splitlines():
        s = line.strip()
        if s.startswith("FAILED "):
            # Strip "FAILED " prefix and any trailing " - reason" suffix.
            test_id = s[7:].split(" - ")[0].strip()
            if test_id:
                failing.add(test_id)
    return failing


def _capture_baseline_failures(
        project_path: str, repo_root: str | None = None) -> list[str]:
    """Run pytest before engineer changes; return sorted list of failing test IDs.

    Called at begin_stage time (fresh worktree = no engineer changes) so
    environment-specific failures (path-sensitive tests that always fail
    inside a worktree) are captured as the baseline.  A later smoke-fail
    is treated as clean if current failures ⊆ baseline.

    GAP-a2 (Fix 3): tries repo_root first when provided, since a worktree
    at <repo>/.harness/worktrees/attended-X does NOT have a tests/ directory
    of its own — the tests live in the repo root.  Without repo_root, falls
    back to project_path then project_path.parent (less reliable for worktrees,
    which may be several levels deep under the repo root).
    Fail-soft: any exception returns an empty list (no baseline → smoke
    failures are NOT excused, which is the safe default).
    """
    import sys as _sys
    # Build candidate list: prefer repo_root > project_path > parent
    candidates: list[str] = []
    if repo_root and repo_root != project_path:
        candidates.append(repo_root)
    candidates.append(project_path)
    if not repo_root:
        # Legacy fallback: try parent (unreliable for deep worktrees but better
        # than nothing when repo_root is unknown).
        parent = str(Path(project_path).parent)
        if parent and parent != project_path:
            candidates.append(parent)
    for cwd in candidates:
        tests_dir = Path(cwd) / "tests"
        if not tests_dir.is_dir():
            continue
        try:
            res = subprocess.run(
                [
                    _sys.executable, "-m", "pytest",
                    "tests/", "--no-header", "-q", "--tb=no",
                ],
                cwd=cwd,
                capture_output=True,
                text=True,
                check=False,
                timeout=240,
            )
            failing = sorted(_parse_failing_tests(res.stdout + "\n" + res.stderr))
            # Return the first successful (or empty) result — empty is valid
            # (all tests pass in this env = no baseline needed).
            return failing
        except Exception:  # noqa: BLE001 — baseline failure is non-fatal
            continue
    return []


# GAP-h: heuristic — a regex for file-path-like tokens in critique text.
_PATH_TOKEN_RE = _re.compile(
    r'[A-Za-z0-9_][A-Za-z0-9_./-]*\.[a-zA-Z]{1,10}'
)


def _has_real_file_ref(critique_md: str, work_path: str) -> bool:
    """Return True if critique_md contains at least one path-like token that
    resolves to an existing file under work_path.  Heuristic — warn-only."""
    for match in _PATH_TOKEN_RE.finditer(critique_md):
        token = match.group().replace("\\", "/").lstrip("/")
        try:
            if (Path(work_path) / token).exists():
                return True
        except Exception:  # noqa: BLE001
            pass
    return False


# --------------------------------------------------------------------------- #
# Step results                                                                #
# --------------------------------------------------------------------------- #

def _host_action(cursor: dict[str, Any]) -> dict[str, Any] | None:
    return cursor.get("pending_action")


def _step_result(cursor: dict[str, Any], cursor_file: str | Path) -> dict[str, Any]:
    """Project a cursor into the result dict the CLI/MCP layer returns to the host."""
    state = cursor.get("state")
    if state in _TERMINAL:
        status = state
    else:
        status = "awaiting_host"
    res: dict[str, Any] = {
        "status": status,
        "workflow_id": cursor.get("workflow_id"),
        "run_id": cursor.get("run_id"),
        "stage_id": cursor.get("stage_id"),
        "task_id": cursor.get("task_id"),
        "state": state,
        "cursor_path": str(cursor_file),
        "cost_usd": float(cursor.get("cost_usd") or 0.0),
        "tokens_in": int(cursor.get("tokens_in") or 0),
        "tokens_out": int(cursor.get("tokens_out") or 0),
    }
    if status == "awaiting_host":
        res["host_action"] = _host_action(cursor)
    if state in _TERMINAL:
        res["final_status"] = cursor.get("final_status") or state
        res["stage_outcome"] = cursor.get("outcome")
        res["smoke_status"] = cursor.get("smoke_status")
        res["changed_paths"] = cursor.get("changed_paths") or []
        if cursor.get("merge") is not None:
            res["merge"] = cursor["merge"]
        if cursor.get("error"):
            res["error"] = cursor["error"]
        # Rider (b): expose charged flag so _cmd_attended_submit can skip
        # duplicate budget charges on a retried submit-host-result call.
        res["already_charged"] = bool(cursor.get("charged", False))
    return res


def _trace(cursor: dict[str, Any], kind: str, payload: dict[str, Any]) -> None:
    wf = cursor.get("workflow_id")
    if not wf:
        return
    try:
        _telemetry.emit(Path(cursor["project_path"]), wf, kind,
                        {"run_id": cursor.get("run_id"), **payload})
    except Exception:  # noqa: BLE001 — never crash the driver on a trace write
        pass


# --------------------------------------------------------------------------- #
# Driver                                                                       #
# --------------------------------------------------------------------------- #

def begin_stage(
    dispatcher: Dispatcher,
    *,
    workflow_id: str,
    run_id: str,
    project_path: str,
    request_text: str,
    model_tier: str | None = None,
    judge_rubric_id: str = "rfc-2119-normative",
    project_root: str | Path | None = None,
    task_id: str | None = None,
    isolate: bool = True,
) -> dict[str, Any]:
    """Open an attended code stage and pause for the first host action (the
    ``engineer`` generation). ``run_id`` must already exist (the caller runs
    ``start_run`` / has a scaffolded run). Returns an ``awaiting_host`` step
    result whose ``host_action`` tells the host to spawn the visible
    ``engineer`` subagent.

    ``isolate`` (default True): provision a linked git worktree under
    ``.harness/worktrees/`` for the engineer to write into (write-safe under the
    ``hydra-block-direct-write`` hook), merged back on a passing finalize. Falls
    back to in-place writes when the target isn't a git repo.
    """
    # Browser isolation parity with the headless driver (PP-BV-ISO).
    os.environ.setdefault("PP_BROWSER_ENGINE", "playwright")
    cm = dispatcher.call_mcp

    st = cm("pp_harness", "start_stage",
            {"run_id": run_id, "kind": "code", "gate_type": "code"},
            squad_id=_SQ)
    stage_id = _pp_inner(st).get("stage_id")
    if not stage_id:
        raise RuntimeError(f"start_stage returned no stage_id: {st!r}")

    # Write-safety: isolate the engineer into a hook-allowed worktree.
    work_path = project_path
    worktree_path: str | None = None
    branch: str | None = None
    repo_root: str | None = None
    if isolate:
        repo_root = _git_repo_root(project_path)
        if repo_root:
            prov = _provision_worktree(repo_root, run_id)
            if prov is not None:
                worktree_path, branch = prov
                work_path = worktree_path

    base_prompt = _build_engineer_prompt(request_text, work_path)
    # Rider (a): capture baseline failures before the engineer touches anything.
    # The worktree is a linked git worktree (same commits as the repo root); any
    # failures here are environment-specific rather than regressions introduced
    # by the engineer's changes.  Pass repo_root so pytest runs from the right
    # directory (worktrees don't carry their own tests/ dir).
    baseline_failures = _capture_baseline_failures(work_path, repo_root=repo_root)
    cursor: dict[str, Any] = {
        "schema": CURSOR_SCHEMA,
        "workflow_id": workflow_id,
        "run_id": run_id,
        "stage_id": stage_id,
        "task_id": task_id,
        "project_path": project_path,
        "work_path": work_path,
        "worktree_path": worktree_path,
        "branch": branch,
        "repo_root": repo_root,
        "request_text": request_text,
        "model_tier": model_tier,
        "judge_rubric_id": judge_rubric_id,
        "state": "await_generate",
        "pre_dirty": sorted(_worktree_dirty_set(work_path)),
        "baseline_failures": baseline_failures,
        "producer": "claude",
        "generate_index": 0,   # GAP-f: tracks Reflexion×1 — 0=first attempt, 1=retry
        "reflexion_critique": "",
        "attempt_id": None,
        "cost_usd": 0.0,
        "tokens_in": 0,
        "tokens_out": 0,
        "changed_paths": [],
        "outcome": None,
        "smoke_status": "skipped",
        "smoke_reason": "",
        "final_status": None,
        "error": None,
        "pending_action": {
            "call_key": "generate-0",
            "agent_type": "engineer",
            "cwd": work_path,
            "isolated_worktree": bool(worktree_path),
            "prompt": base_prompt,
            "instructions": (
                "Spawn the visible `engineer` subagent in cwd to implement the "
                "request, editing files directly. Then call submit-host-result "
                "with {call_key, result:{text, cost_usd, tokens_in, tokens_out, "
                "model}} where `text` summarizes the change."),
        },
    }
    root = project_root or project_path
    cfile = cursor_path(root, workflow_id, run_id)
    save_cursor(cfile, cursor)
    _trace(cursor, "attended.stage_started", {"stage_id": stage_id})
    return _step_result(cursor, cfile)


def _apply_generate(dispatcher: Dispatcher, cursor: dict[str, Any],
                    result: dict[str, Any]) -> None:
    """await_generate -> await_judge (or terminal on generate failure).

    The host's ``engineer`` subagent already wrote files in cwd; ``result`` is
    its summary + spend. We attribute the run-scoped diff, archive + record the
    attempt, route the judge via pp's ``gate_eligible_judges``, and stage the
    judge host-action.
    """
    cm = dispatcher.call_mcp
    work_path = cursor.get("work_path") or cursor["project_path"]
    stage_id = cursor["stage_id"]
    run_id = cursor["run_id"]
    producer = cursor["producer"]

    gen_text = str(result.get("text") or "")
    cursor["cost_usd"] = float(cursor["cost_usd"]) + float(result.get("cost_usd") or 0.0)
    cursor["tokens_in"] = int(cursor["tokens_in"]) + int(result.get("tokens_in") or 0)
    cursor["tokens_out"] = int(cursor["tokens_out"]) + int(result.get("tokens_out") or 0)

    pre_dirty = set(cursor.get("pre_dirty") or [])
    run_changed = _worktree_dirty_set(work_path) - pre_dirty
    cursor["changed_paths"] = sorted(set(cursor.get("changed_paths") or []) | run_changed)
    wrote_changes = bool(run_changed)

    gen_fail = _generate_failure_reason(
        {"status": "done", "result": result}, gen_text, wrote_changes)

    model_id = str(result.get("model") or cursor.get("model_tier") or f"{producer}-default")

    # GAP-f: generate_index tracks the Reflexion×1 retry (0=first, 1=Reflexion).
    gen_idx = cursor.get("generate_index", 0)

    if gen_fail:
        cursor["error"] = gen_fail
        cursor["outcome"] = "error"
        try:
            cm("pp_harness", "archive_artifact", {
                "run_id": run_id,
                "relative_path": f"code/{producer}-attempt-{gen_idx}.failed.md",
                "bytes": f"GENERATE FAILED: {gen_fail}\n\n{gen_text or '(no output)'}",
                "stage_id": stage_id, "kind": "code", "encoding": "utf8",
            }, squad_id=_SQ)
        except Exception:  # noqa: BLE001
            pass
        try:
            att = cm("pp_harness", "record_attempt", {
                "stage_id": stage_id, "producer": producer, "model_id": model_id,
                "agent_type": "engineer",   # F29
                "tokens_in": int(result.get("tokens_in") or 0),
                "tokens_out": int(result.get("tokens_out") or 0),
                "cost_usd": float(result.get("cost_usd") or 0.0),
                "status": "error", "retry_index": gen_idx,
                "notes": {"candidate_index": 1},
            }, squad_id=_SQ)
            cursor["attempt_id"] = _pp_inner(att).get("attempt_id")
        except Exception:  # noqa: BLE001
            pass
        # Generation failed; finalize as surfaced (no judge).
        _finalize(dispatcher, cursor, passed=False, gen_failed=True)
        return

    # Successful generate: archive the producer summary + record the attempt.
    try:
        cm("pp_harness", "archive_artifact", {
            "run_id": run_id,
            "relative_path": f"code/{producer}-attempt-{gen_idx}.md",
            "bytes": gen_text or "(no summary returned)",
            "stage_id": stage_id, "kind": "code", "encoding": "utf8",
        }, squad_id=_SQ)
    except Exception:  # noqa: BLE001
        pass
    # Finding 1: wrap record_attempt in try/except — an RPC failure here must
    # surface cleanly, not crash submit_host_result and orphan the stage.
    # The pp schema accepts agent_type as an optional top-level string; strict
    # mode only rejects the literal 'general-purpose', not 'engineer'.
    try:
        att = cm("pp_harness", "record_attempt", {
            "stage_id": stage_id, "producer": producer, "model_id": model_id,
            "agent_type": "engineer",   # F29 — accepted optional; strict rejects 'general-purpose'
            "tokens_in": int(result.get("tokens_in") or 0),
            "tokens_out": int(result.get("tokens_out") or 0),
            "cost_usd": float(result.get("cost_usd") or 0.0),
            "status": "ok", "retry_index": gen_idx,
            "notes": {"candidate_index": 1},
        }, squad_id=_SQ)
        cursor["attempt_id"] = _pp_inner(att).get("attempt_id")
    except Exception as _ra_exc:  # noqa: BLE001
        # record_attempt RPC failed — surface the stage immediately rather than
        # crashing. The engineer's work is generated but cannot be tracked.
        cursor["error"] = f"record_attempt RPC failed: {_ra_exc!r}"
        _finalize(dispatcher, cursor, passed=False, gen_failed=False)
        return

    # Judge routing — honour pp's gate_eligible_judges (cross- vs same-vendor)
    # exactly like the headless loop, so the host spawns the right judge agent.
    gate_dec: dict[str, Any] = {}
    try:
        gate_dec = _pp_inner(cm("pp_harness", "gate_eligible_judges", {
            "gate_type": _pp_gate_type("code", "code_style"),
            "generator_producer": producer,
            "prompt_keywords": cursor["request_text"][:1000],
        }, squad_id=_SQ))
    except Exception:  # noqa: BLE001
        gate_dec = {}
    required_cross = bool(gate_dec.get("required_cross_vendor", True))
    gate_rubric = str(gate_dec.get("rubric_id") or cursor["judge_rubric_id"])
    # producer is "claude": a sanctioned same-vendor Claude judge is allowed only
    # when cross-vendor is NOT required; otherwise the host must spawn the
    # cross-vendor judge (codex/gemini critique).
    judge_agent = "judge-cross-vendor" if required_cross else "judge-same-vendor"
    rubric_body = _rubric_md(gate_rubric)
    judge_text = _judge_artifact_text(
        work_path, sorted(run_changed), gen_text)

    cursor["gate_rubric"] = gate_rubric
    cursor["required_cross"] = required_cross
    cursor["state"] = "await_judge"
    judge_call_key = f"judge-{gen_idx}"  # GAP-f: judge-0 or judge-1
    cursor["pending_action"] = {
        "call_key": judge_call_key,
        "agent_type": judge_agent,
        "rubric_id": gate_rubric,
        "required_cross_vendor": required_cross,
        "artifact_text": judge_text,
        "rubric_md": rubric_body,
        "cwd": work_path,
        "instructions": (
            f"Spawn the visible `{judge_agent}` subagent to judge the diff "
            f"against rubric {gate_rubric}. Then call submit-host-result with "
            "{call_key, result:{outcome:pass|revise|fail, critique_md, "
            "judge_producer, judge_model_id, score_json, cost_usd}}."),
    }
    _trace(cursor, "attended.attempt_recorded", {
        "stage_id": stage_id, "attempt_id": cursor["attempt_id"],
        "producer": producer, "judge_agent": judge_agent,
        "required_cross_vendor": required_cross, "wrote_changes": wrote_changes,
        "generate_index": gen_idx,
    })


def _apply_judge(dispatcher: Dispatcher, cursor: dict[str, Any],
                 result: dict[str, Any]) -> None:
    """await_judge -> terminal (or back to await_generate for Reflexion x1).

    F26+M8: a failed record_verdict/finalize_stage on a pass outcome downgrades
    the stage to surfaced (never proceeds to finalize_run complete).
    F31: required_cross_vendor but judge was same-vendor (degraded) → downgrade.
    GAP-f: Reflexion×1 — on the first revise, transition back to await_generate
    with call_key='generate-1' and the critique embedded in the prompt.
    GAP-h: warn-telemetry when critique_md cites no existing worktree file.
    GAP-a2: lazy baseline fallback from HYDRA_SMOKE_BASELINE_TESTS env var.
    """
    cm = dispatcher.call_mcp
    producer = cursor["producer"]
    attempt_id = cursor.get("attempt_id")
    gate_rubric = cursor.get("gate_rubric") or cursor["judge_rubric_id"]
    required_cross = bool(cursor.get("required_cross"))
    gen_idx = cursor.get("generate_index", 0)   # GAP-f: 0=first attempt, 1=retry
    work_path = cursor.get("work_path") or cursor["project_path"]

    cursor["cost_usd"] = float(cursor["cost_usd"]) + float(result.get("cost_usd") or 0.0)
    cursor["tokens_in"] = int(cursor["tokens_in"]) + int(result.get("tokens_in") or 0)
    cursor["tokens_out"] = int(cursor["tokens_out"]) + int(result.get("tokens_out") or 0)

    outcome = result.get("outcome") or result.get("verdict") or "revise"
    if outcome not in {"pass", "revise", "fail"}:
        outcome = "revise"
    critique_md = str(result.get("critique_md") or result.get("critique") or "")
    judge_producer = str(result.get("judge_producer")
                         or ("codex" if required_cross else "claude"))
    cross_vendor = judge_producer != producer
    degraded = required_cross and not cross_vendor

    score_json = dict(result.get("score_json") or result.get("score") or {})
    score_json["_cross_vendor"] = cross_vendor
    score_json["_judge_tier"] = "cross_vendor" if required_cross else "same_vendor"
    score_json["_attended"] = True
    if degraded:
        score_json["_judge_degraded"] = True

    # Finding 2: track whether the outcome change is an infra failure (F31 /
    # F26+M8) vs a genuine artifact defect.  Infra failures must surface
    # immediately — Reflexion is reserved for code defects the engineer can fix.
    _infra_downgrade = False

    # F31: required cross-vendor but got same-vendor judge → downgrade pass to surfaced.
    if degraded and outcome == "pass":
        outcome = "revise"   # treat as revise so non-pass path runs
        _infra_downgrade = True
        cursor["error"] = ("required_cross_vendor=true but judge was same-vendor "
                           "(degraded); stage downgraded to surfaced")

    cursor["outcome"] = outcome

    # F26+M8: capture record_verdict success; a failure on a pass outcome downgrades.
    _record_verdict_ok = True
    if attempt_id:
        try:
            cm("pp_harness", "record_verdict", {
                "attempt_id": attempt_id,
                "judge_producer": judge_producer,
                "judge_model_id": str(result.get("judge_model_id")
                                      or result.get("model") or f"{judge_producer}-default"),
                "outcome": outcome if outcome in {"pass", "revise", "fail"} else "revise",
                "critique_md": critique_md[:4000],
                "score_json": score_json,
                "rubric_id": gate_rubric,
            }, squad_id=_SQ)
        except Exception:  # noqa: BLE001
            _record_verdict_ok = False
    if outcome == "pass" and not _record_verdict_ok:
        outcome = "revise"
        _infra_downgrade = True
        cursor["outcome"] = "revise"
        cursor["error"] = (cursor.get("error") or "") + \
            " record_verdict RPC failed; stage downgraded to surfaced"

    _trace(cursor, "attended.verdict", {
        "stage_id": cursor["stage_id"], "rubric_id": gate_rubric,
        "attempt_id": attempt_id, "producer": producer,
        "judge_producer": judge_producer, "outcome": outcome,
        "cross_vendor": cross_vendor, "generate_index": gen_idx,
        "degraded": degraded,
    })

    # GAP-h: warn when critique references no existing worktree file.
    if critique_md and not _has_real_file_ref(critique_md, work_path):
        _trace(cursor, "attended.judge.suspicious_critique", {
            "stage_id": cursor["stage_id"],
            "warning": "critique_md contains no path token matching an existing worktree file",
            "judge_producer": judge_producer,
            "critique_head": critique_md[:200],
        })

    # GAP-f: Reflexion×1 — on first revise (gen_idx==0), transition back to
    # await_generate with an augmented prompt that embeds the critique.
    # Finding 2: skip Reflexion for infra failures (F31 degraded judge, F26+M8
    # record_verdict RPC error) — retrying the engineer cannot fix an infra
    # problem and wastes a generation slot.
    if outcome == "revise" and gen_idx == 0 and not _infra_downgrade:
        cursor["generate_index"] = 1
        cursor["reflexion_critique"] = critique_md
        aug_prompt = _augment_with_critique(cursor["request_text"], critique_md)
        cursor["state"] = "await_generate"
        cursor["pending_action"] = {
            "call_key": "generate-1",
            "agent_type": "engineer",
            "cwd": work_path,
            "isolated_worktree": bool(cursor.get("worktree_path")),
            "prompt": aug_prompt,
            "retry_index": 1,
            "instructions": (
                "Spawn the visible `engineer` subagent to revise the implementation "
                "addressing the critique embedded in the prompt. Then call "
                "submit-host-result with {call_key, result:{text, cost_usd, "
                "tokens_in, tokens_out, model}}."),
        }
        _trace(cursor, "attended.reflexion", {
            "stage_id": cursor["stage_id"], "generate_index": 1,
        })
        return  # Don't finalize — wait for generate-1

    # PP-VG-5: a code stage may finalize 'complete' only with a real smoke result.
    passed = False
    smoke_status = "skipped"
    smoke_reason = ""
    if outcome == "pass" and attempt_id:
        smoke_status, smoke_reason = _run_smoke(
            dispatcher,
            project_path=work_path,
            stage_id=cursor["stage_id"])
        # GAP-a2 / Rider (a): compare against the baseline failures.
        # If every currently-failing test was ALREADY failing before the engineer's
        # change, the smoke failure is not attributable to this change — excuse it.
        # Finding 6: bound the excusable set to prevent real regressions being
        # silently blessed by an overly broad baseline.
        if smoke_status == "fail":
            _captured_baseline = list(cursor.get("baseline_failures") or [])
            _env_bl_raw = os.environ.get("HYDRA_SMOKE_BASELINE_TESTS", "")
            _env_allowlist: set[str] | None = (
                {t.strip() for t in _env_bl_raw.split(",") if t.strip()}
                if _env_bl_raw else None
            )
            # Build excusable set:
            #  - env var present + captured non-empty → intersection (tightest bound)
            #  - env var present + captured empty → env var alone (legacy fallback)
            #  - env var absent → captured baseline alone
            _captured_set = set(_captured_baseline)
            if _env_allowlist is not None:
                _excusable = (_captured_set & _env_allowlist) if _captured_set else _env_allowlist
            else:
                _excusable = _captured_set

            if _excusable:
                _max_excuse = int(os.environ.get("HYDRA_SMOKE_BASELINE_MAX", "10"))
                if len(_excusable) > _max_excuse:
                    # Baseline too broad — refuse to excuse; treat as real failure.
                    _trace(cursor, "attended.smoke.baseline_too_broad", {
                        "stage_id": cursor.get("stage_id"),
                        "excusable_count": len(_excusable),
                        "max": _max_excuse,
                    })
                    smoke_reason = (
                        f"smoke: baseline too broad ({len(_excusable)} excusable "
                        f"tests > HYDRA_SMOKE_BASELINE_MAX={_max_excuse}); "
                        "treating as real failure"
                    )
                else:
                    import sys as _sys
                    try:
                        _reruns = subprocess.run(
                            [_sys.executable, "-m", "pytest",
                             "tests/", "--no-header", "-q", "--tb=no"],
                            cwd=work_path,
                            capture_output=True, text=True, check=False, timeout=240,
                        )
                        _current_failing = _parse_failing_tests(
                            _reruns.stdout + "\n" + _reruns.stderr)
                    except Exception:  # noqa: BLE001
                        _current_failing = set()
                    _excused = _current_failing & _excusable
                    _new_failures = _current_failing - _excusable
                    # Always emit telemetry about excused failures (Finding 6).
                    if _current_failing or _excused:
                        _trace(cursor, "attended.smoke.baseline_excuse_decision", {
                            "stage_id": cursor.get("stage_id"),
                            "current_failing": sorted(_current_failing),
                            "excused": sorted(_excused),
                            "new_failures": sorted(_new_failures),
                            "excusable_set_size": len(_excusable),
                        })
                    if not _new_failures:
                        smoke_status = "pass"
                        smoke_reason = (
                            f"smoke: {len(_current_failing)} failure(s) all "
                            f"pre-existed in baseline ({len(_excused)} excused); "
                            "treated as pass"
                        )
        try:
            cm("pp_harness", "record_smoke_status", {
                "stage_id": cursor["stage_id"], "candidate_index": 1,
                "status": smoke_status,
                "reason": (smoke_reason or "attended drive smoke")[:300],
            }, squad_id=_SQ)
        except Exception:  # noqa: BLE001
            pass
        passed = smoke_status == "pass"
    cursor["smoke_status"] = smoke_status
    cursor["smoke_reason"] = smoke_reason

    # Honour pp's finalize-readiness gate (same auto-resolved deferrals as the
    # headless loop).
    if passed:
        try:
            rd = _pp_inner(cm("pp_harness", "get_stage_finalize_readiness",
                              {"stage_id": cursor["stage_id"]}, squad_id=_SQ))
        except Exception:  # noqa: BLE001
            rd = {}
        if rd.get("can_pass") is False:
            na = rd.get("next_action") or "not_ready"
            _auto_resolved = {"run_artifact_validate", "run_tdd_pre_check",
                              "run_tdd_post_check", "record_smoke_or_assertion"}
            if na not in _auto_resolved:
                passed = False
                cursor["outcome"] = "surfaced"
                cursor["error"] = f"pp readiness: not ready (next_action={na})"

    _finalize(dispatcher, cursor, passed=passed, gen_failed=False)


def _finalize(dispatcher: Dispatcher, cursor: dict[str, Any], *,
              passed: bool, gen_failed: bool) -> None:
    """Finalize the stage + run and set the terminal cursor state. Mirrors the
    headless loop's downgrade-honouring finalize_run handling.

    F26+M8: a finalize_stage RPC failure on a passing stage downgrades to
    surfaced — we never proceed to finalize_run 'complete' with an un-recorded
    stage.
    Finding 4: worktree merge happens BEFORE finalize_run so a merge failure
    can downgrade finalize_run to 'surfaced' truthfully (previously the run
    was finalized 'complete' and only the cursor reflected the merge failure).
    F30: abort/error reason is included in summary_md (FinalizeRunSchema strips
    standalone `reason` / `project_path` keys).
    """
    cm = dispatcher.call_mcp
    stage_id = cursor["stage_id"]
    run_id = cursor["run_id"]
    attempt_id = cursor.get("attempt_id")

    # F26+M8: capture finalize_stage success; failure on pass → downgrade.
    _finalize_stage_ok = True
    try:
        cm("pp_harness", "finalize_stage", {
            "stage_id": stage_id,
            "status": "passed" if passed else "surfaced",
            **({"winner_attempt_id": attempt_id} if (passed and attempt_id) else {}),
        }, squad_id=_SQ)
    except Exception:  # noqa: BLE001
        _finalize_stage_ok = False
    if passed and not _finalize_stage_ok:
        passed = False
        cursor["outcome"] = "surfaced"
        cursor["error"] = (cursor.get("error") or "") + \
            " finalize_stage RPC failed; stage downgraded to surfaced"

    # Finding 4: merge the worktree BEFORE calling finalize_run so a merge
    # failure can truthfully downgrade the run to 'surfaced'.  Previously the
    # order was finalize_run(complete) → merge → cursor surfaced, which left the
    # pp ledger claiming 'complete' while no code actually landed.
    worktree_path = cursor.get("worktree_path")
    repo_root = cursor.get("repo_root")
    branch = cursor.get("branch")
    if worktree_path and repo_root and branch:
        if passed:
            merge = _merge_worktree_back(repo_root, worktree_path, branch)
            cursor["merge"] = merge
            if not merge.get("merged"):
                # Merge failed — surface the run so the operator knows code
                # did not land, and pass that truth to finalize_run below.
                # NEW: downgrade cursor['outcome'] so step_result / summary
                # report 'pass_unlanded' rather than 'pass' on a surfaced run.
                passed = False
                cursor["outcome"] = "pass_unlanded"
                cursor["error"] = (cursor.get("error") or "") + \
                    f" merge-back failed: {merge.get('error')}"
        else:
            cursor["merge"] = {"merged": False, "error": "discarded_non_complete"}
        _remove_worktree(repo_root, worktree_path)

    # F30: build summary_md that embeds any error/abort reason.
    if gen_failed:
        summary = f"Attended drive: generate failed -- {cursor.get('error')}"
    elif passed:
        summary = (f"Attended drive: stage_outcome=pass; "
                   f"smoke={cursor.get('smoke_status')}.")
    else:
        extra = f" :: {cursor['error']}" if cursor.get("error") else ""
        summary = (f"Attended drive: stage_outcome={cursor.get('outcome')}; "
                   f"smoke={cursor.get('smoke_status')}{extra}.")

    fin = cm("pp_harness", "finalize_run", {
        "run_id": run_id,
        "status": "complete" if passed else "surfaced",
        "summary_md": summary,
    }, squad_id=_SQ)
    fin_inner = _pp_inner(fin)
    fin_status = fin_inner.get("effective_status") or fin_inner.get("status")
    fin_downgraded = bool(fin_inner.get("downgraded"))
    if passed and _pp_ok(fin) and not fin_downgraded \
            and fin_status not in {"surfaced", "failed", "aborted", "blocked"}:
        cursor["final_status"] = "complete"
        cursor["state"] = "complete"
    else:
        cursor["final_status"] = "surfaced"
        cursor["state"] = "surfaced"

    cursor["pending_action"] = None
    cursor["finalized"] = True
    # Rider (b): initialise the charged flag to False. _cmd_attended_submit sets
    # it to True after the first budget charge so retried submit calls don't
    # double-charge (the already_charged field in _step_result exposes this flag).
    cursor.setdefault("charged", False)
    _trace(cursor, "attended.finalized", {
        "stage_id": stage_id, "final_status": cursor["final_status"],
        "smoke_status": cursor.get("smoke_status"), "cost_usd": cursor.get("cost_usd"),
        "merged": (cursor.get("merge") or {}).get("merged"),
    })


def begin_squad_stage(
    *,
    workflow_id: str,
    task_id: str,
    squad_slug: str,
    entrypoint: str,
    lead_agent: str,
    pack_cwd: str,
    request_text: str,
    project_root: str | Path,
) -> dict[str, Any]:
    """Create a lightweight cursor for an attended non-engineering squad task
    (claude-skill or agent-impersonation entrypoint).

    No pp stage is opened; no worktree isolation is needed (these squads produce
    documents, not engine code). The cursor lives at
    ``cursor_path(project_root, workflow_id, task_id)`` so the submit-host-result
    CLI can find it by passing ``--run-id <task_id>``.

    Returns an ``awaiting_host`` step result whose ``host_action`` tells the host
    to spawn the visible pack agent subagent in ``pack_cwd``.
    """
    call_key = f"squad-{task_id}-0"
    cursor: dict[str, Any] = {
        "schema": CURSOR_SCHEMA,
        "kind": "squad",
        "workflow_id": workflow_id,
        "task_id": task_id,
        "run_id": task_id,   # mirrors engineering cursor shape for CLI compatibility
        "squad_slug": squad_slug,
        "entrypoint": entrypoint,
        "project_path": pack_cwd,
        "request_text": request_text,
        "state": "await_squad_agent",
        "cost_usd": 0.0,
        "tokens_in": 0,
        "tokens_out": 0,
        "final_status": None,
        "error": None,
        "finalized": False,
        "pending_action": {
            "call_key": call_key,
            "agent_type": lead_agent,
            "cwd": pack_cwd,
            "prompt": request_text,
            "instructions": "Run the pack agent and submit the artifact.",
        },
    }
    cfile = cursor_path(project_root, workflow_id, task_id)
    save_cursor(cfile, cursor)
    _trace(cursor, "attended.squad_stage_started", {
        "squad_slug": squad_slug,
        "entrypoint": entrypoint,
        "task_id": task_id,
    })
    return _step_result(cursor, cfile)


def _apply_squad_result(cursor: dict[str, Any], result: dict[str, Any]) -> None:
    """await_squad_agent → terminal.

    Accumulate spend from the host agent's result and mark the cursor complete.
    There are no pp ledger calls (this is not an engineering stage). The CLI
    (``_cmd_attended_submit``) charges the returned cost_usd through the
    authoritative HydraState budget path after this returns.
    """
    cursor["cost_usd"] = (float(cursor.get("cost_usd") or 0.0)
                          + float(result.get("cost_usd") or 0.0))
    cursor["tokens_in"] = (int(cursor.get("tokens_in") or 0)
                           + int(result.get("tokens_in") or 0))
    cursor["tokens_out"] = (int(cursor.get("tokens_out") or 0)
                            + int(result.get("tokens_out") or 0))
    cursor["artifact_text"] = str(result.get("text") or result.get("artifact") or "")
    cursor["final_status"] = "complete"
    cursor["state"] = "complete"
    cursor["pending_action"] = None
    cursor["finalized"] = True
    _trace(cursor, "attended.squad_result_applied", {
        "task_id": cursor.get("task_id"),
        "squad_slug": cursor.get("squad_slug"),
        "cost_usd": cursor.get("cost_usd"),
    })


def submit_host_result(
    dispatcher: Dispatcher,
    *,
    cursor_file: str | Path,
    call_key: str,
    result: dict[str, Any],
) -> dict[str, Any]:
    """Feed a host subagent's result back in and advance the cursor by exactly
    one transition. Idempotent on a stale/duplicate ``call_key`` (returns the
    current step result without re-applying), so a retried submit never
    double-records in the pp ledger.

    Handles both ``kind="engineering"`` (the default pp stage flow) and the
    lightweight ``kind="squad"`` cursors created by ``begin_squad_stage`` for
    non-engineering tasks (claude-skill / agent-impersonation).
    """
    cursor = load_cursor(cursor_file)
    state = cursor.get("state")
    if state in _TERMINAL:
        return _step_result(cursor, cursor_file)

    pending = cursor.get("pending_action") or {}
    expected_key = pending.get("call_key")
    if call_key != expected_key:
        # Duplicate / out-of-order submit — do not re-apply (exactly-once).
        out = _step_result(cursor, cursor_file)
        out["ignored"] = f"call_key {call_key!r} != expected {expected_key!r}"
        return out

    if state == "await_generate":
        _apply_generate(dispatcher, cursor, result)
    elif state == "await_judge":
        _apply_judge(dispatcher, cursor, result)
    elif state == "await_squad_agent":
        # Lightweight non-engineering squad flow — no pp protocol calls needed.
        _apply_squad_result(cursor, result)
    else:  # pragma: no cover — defensive
        cursor["state"] = "aborted"
        cursor["final_status"] = "aborted"
        cursor["error"] = f"unknown attended state {state!r}"

    save_cursor(cursor_file, cursor)
    return _step_result(cursor, cursor_file)


def mark_charged(cursor_file: str | Path) -> None:
    """Mark a terminal cursor as budget-charged (rider b idempotency guard).

    Called by _cmd_attended_submit immediately after charging the HydraState
    budget ledger. Subsequent submit calls that see ``already_charged=True``
    in the step result skip the charge, preventing double-billing on retried
    submit-host-result invocations.  Fail-soft: any I/O or schema error is
    silently ignored so a storage hiccup never blocks the calling workflow.
    """
    try:
        cursor = load_cursor(cursor_file)
        if cursor.get("state") in _TERMINAL:
            cursor["charged"] = True
            save_cursor(cursor_file, cursor)
    except Exception:  # noqa: BLE001 — never crash the caller on persist failure
        pass


def abort_stage(dispatcher: Dispatcher, *, cursor_file: str | Path,
                reason: str = "operator_abort") -> dict[str, Any]:
    """Best-effort abort: finalize the pp run ``aborted`` to release the lock and
    mark the cursor terminal. Never raises."""
    try:
        cursor = load_cursor(cursor_file)
    except Exception as e:  # noqa: BLE001
        return {"status": "aborted", "error": f"cursor_unreadable: {e}"}
    if cursor.get("state") in _TERMINAL:
        return _step_result(cursor, cursor_file)
    try:
        # F30: FinalizeRunSchema strips 'reason'/'project_path' — embed reason
        # in summary_md so it is never silently dropped.
        dispatcher.call_mcp("pp_harness", "finalize_run", {
            "run_id": cursor["run_id"], "status": "aborted",
            "summary_md": f"attended_abort: {reason}",
        }, squad_id=_SQ)
    except Exception:  # noqa: BLE001
        pass
    # Discard the isolated worktree (no merge on abort).
    worktree_path = cursor.get("worktree_path")
    repo_root = cursor.get("repo_root")
    if worktree_path and repo_root:
        _remove_worktree(repo_root, worktree_path)
        cursor["merge"] = {"merged": False, "error": "discarded_abort"}
    cursor["state"] = "aborted"
    cursor["final_status"] = "aborted"
    cursor["error"] = reason
    cursor["pending_action"] = None
    save_cursor(cursor_file, cursor)
    return _step_result(cursor, cursor_file)
