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

from . import telemetry as _telemetry
from .squad_node import (
    Dispatcher,
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
        "producer": "claude",
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

    if gen_fail:
        cursor["error"] = gen_fail
        cursor["outcome"] = "error"
        try:
            cm("pp_harness", "archive_artifact", {
                "run_id": run_id,
                "relative_path": f"code/{producer}-attempt-0.failed.md",
                "bytes": f"GENERATE FAILED: {gen_fail}\n\n{gen_text or '(no output)'}",
                "stage_id": stage_id, "kind": "code", "encoding": "utf8",
            }, squad_id=_SQ)
        except Exception:  # noqa: BLE001
            pass
        try:
            att = cm("pp_harness", "record_attempt", {
                "stage_id": stage_id, "producer": producer, "model_id": model_id,
                "tokens_in": int(result.get("tokens_in") or 0),
                "tokens_out": int(result.get("tokens_out") or 0),
                "cost_usd": float(result.get("cost_usd") or 0.0),
                "status": "error", "retry_index": 0,
                "notes": {"candidate_index": 1, "attended": True},
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
            "relative_path": f"code/{producer}-attempt-0.md",
            "bytes": gen_text or "(no summary returned)",
            "stage_id": stage_id, "kind": "code", "encoding": "utf8",
        }, squad_id=_SQ)
    except Exception:  # noqa: BLE001
        pass
    att = cm("pp_harness", "record_attempt", {
        "stage_id": stage_id, "producer": producer, "model_id": model_id,
        "tokens_in": int(result.get("tokens_in") or 0),
        "tokens_out": int(result.get("tokens_out") or 0),
        "cost_usd": float(result.get("cost_usd") or 0.0),
        "status": "ok", "retry_index": 0,
        "notes": {"candidate_index": 1, "attended": True},
    }, squad_id=_SQ)
    cursor["attempt_id"] = _pp_inner(att).get("attempt_id")

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
    cursor["pending_action"] = {
        "call_key": "judge-0",
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
    })


def _apply_judge(dispatcher: Dispatcher, cursor: dict[str, Any],
                 result: dict[str, Any]) -> None:
    """await_judge -> terminal: record the verdict, run smoke on pass, honour the
    finalize-readiness gate, then finalize the stage + run."""
    cm = dispatcher.call_mcp
    producer = cursor["producer"]
    attempt_id = cursor.get("attempt_id")
    gate_rubric = cursor.get("gate_rubric") or cursor["judge_rubric_id"]
    required_cross = bool(cursor.get("required_cross"))

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

    cursor["outcome"] = outcome
    if attempt_id:
        try:
            cm("pp_harness", "record_verdict", {
                "attempt_id": attempt_id,
                "judge_producer": judge_producer,
                "judge_model_id": str(result.get("judge_model_id")
                                      or result.get("model") or f"{judge_producer}-default"),
                "outcome": outcome,
                "critique_md": critique_md[:4000],
                "score_json": score_json,
                "rubric_id": gate_rubric,
            }, squad_id=_SQ)
        except Exception:  # noqa: BLE001
            pass
    _trace(cursor, "attended.verdict", {
        "stage_id": cursor["stage_id"], "rubric_id": gate_rubric,
        "attempt_id": attempt_id, "producer": producer,
        "judge_producer": judge_producer, "outcome": outcome,
        "cross_vendor": cross_vendor,
    })

    # PP-VG-5: a code stage may finalize 'complete' only with a real smoke result.
    passed = False
    smoke_status = "skipped"
    smoke_reason = ""
    if outcome == "pass" and attempt_id:
        smoke_status, smoke_reason = _run_smoke(
            dispatcher,
            project_path=cursor.get("work_path") or cursor["project_path"],
            stage_id=cursor["stage_id"])
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
    headless loop's downgrade-honouring finalize_run handling."""
    cm = dispatcher.call_mcp
    stage_id = cursor["stage_id"]
    run_id = cursor["run_id"]
    attempt_id = cursor.get("attempt_id")
    try:
        cm("pp_harness", "finalize_stage", {
            "stage_id": stage_id,
            "status": "passed" if passed else "surfaced",
            **({"winner_attempt_id": attempt_id} if (passed and attempt_id) else {}),
        }, squad_id=_SQ)
    except Exception:  # noqa: BLE001
        pass

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

    # Worktree write-safety: merge the engineer's isolated changes back into the
    # repo ONLY on a complete finalize; otherwise discard. Always remove the
    # worktree so it never accumulates. The merge is recorded for the operator.
    worktree_path = cursor.get("worktree_path")
    repo_root = cursor.get("repo_root")
    branch = cursor.get("branch")
    if worktree_path and repo_root and branch:
        if cursor["final_status"] == "complete":
            merge = _merge_worktree_back(repo_root, worktree_path, branch)
            cursor["merge"] = merge
            if not merge.get("merged"):
                # Code passed gates but could not land — surface honestly rather
                # than report a clean complete with no committed change.
                cursor["final_status"] = "surfaced"
                cursor["state"] = "surfaced"
                cursor["error"] = (cursor.get("error") or "") + \
                    f" merge-back failed: {merge.get('error')}"
        else:
            cursor["merge"] = {"merged": False, "error": "discarded_non_complete"}
        _remove_worktree(repo_root, worktree_path)

    cursor["pending_action"] = None
    cursor["finalized"] = True
    _trace(cursor, "attended.finalized", {
        "stage_id": stage_id, "final_status": cursor["final_status"],
        "smoke_status": cursor.get("smoke_status"), "cost_usd": cursor.get("cost_usd"),
        "merged": (cursor.get("merge") or {}).get("merged"),
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
    else:  # pragma: no cover — defensive
        cursor["state"] = "aborted"
        cursor["final_status"] = "aborted"
        cursor["error"] = f"unknown attended state {state!r}"

    save_cursor(cursor_file, cursor)
    return _step_result(cursor, cursor_file)


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
        dispatcher.call_mcp("pp_harness", "finalize_run", {
            "run_id": cursor["run_id"], "status": "aborted",
            "reason": f"attended_abort: {reason}",
            "project_path": cursor.get("project_path"),
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
