"""Generic squad executor.

A squad-node in the LangGraph supervisor calls `execute_squad(state, pack, envelope)`
and translates the squad's `entrypoint` declaration into a concrete invocation:

  - `mcp`                  → call MCP tool(s) declared in `pack.tools`
  - `subprocess`           → spawn a CLI (e.g. `pp` runner)
  - `agent-impersonation`  → returns a *prompt blob* the supervisor passes to
                             Claude Code; Claude impersonates the relevant
                             roster member(s) in-process (ExecutiveSuite pattern)
  - `claude-skill`         → invokes a Claude Code skill (`/rlm-team`, etc.)
  - `stub`                 → returns a structured placeholder so Hydra
                             gracefully degrades when a squad is scaffolded
                             but not yet implemented

This module is intentionally **runtime-agnostic** — the actual dispatch is
performed by an injected `Dispatcher` strategy so unit tests and other hosts
(e.g. a future Temporal-driven host) can substitute their own.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Protocol
from uuid import uuid4

import subprocess
from pathlib import Path

_log = logging.getLogger("hydra.engineering")

from .iolaus import post_dispatch, pre_dispatch
from .schemas import (
    DecisionRecord,
    Handoff,
    HITLRequest,
    HydraEnvelope,
    MemoryRef,
    validate_envelope,
)
from .squad_loader import SquadPack
from .state import HydraState, TaskState
from .tool_scope import build_tool_scope_directive
from .version import DoubleSpawnRefused, SquadDeprecated


class Dispatcher(Protocol):
    def call_mcp(self, server: str, tool: str, args: dict[str, Any],
                 *, squad_id: str | None = None) -> dict[str, Any]: ...
    def spawn_subprocess(self, cmd: list[str], env: dict[str, str] | None = None) -> dict[str, Any]: ...
    def emit_claude_prompt(self, prompt: str, *, agent: str | None = None) -> dict[str, Any]: ...
    def invoke_claude_skill(self, skill: str, args: dict[str, Any]) -> dict[str, Any]: ...


@dataclass
class SquadResult:
    envelopes: list[HydraEnvelope]
    artifacts: list[dict[str, Any]]
    status: str
    rationale: str = ""
    requires_hitl: bool = False
    hitl_request: HITLRequest | None = None
    # True when the squad's "output" is actually a host-pickup placeholder
    # (impersonation / claude-skill in headless mode). The judge plane must
    # NOT score these — there's no substance yet. The host (Claude Code)
    # fulfils the prompt out-of-band and a follow-up envelope arrives later.
    host_pickup_pending: bool = False
    # True when the engineering live drive loop already drove a cross-vendor
    # pp critique to a verdict (start_stage→generate→critique→record_verdict→
    # finalize). The supervisor's NoOp judge plane must NOT re-judge this
    # DecisionRecord — doing so downgrades a vacuous "pass" to "revise" and
    # re-dispatches engineering, spawning a fresh start_run loop. Unlike
    # host_pickup_pending the work is DONE, not awaited, so synthesis still
    # treats it as a real candidate.
    pp_loop_judged: bool = False


def execute_squad(
    state: HydraState,
    pack: SquadPack,
    inbound: HydraEnvelope,
    dispatcher: Dispatcher,
    *,
    allow_archived: bool = False,
    collect_open_runs: list | None = None,
) -> SquadResult:
    """Single entry point. Selects strategy by `pack.entrypoint`.

    Iolaus is wrapped around the strategy call: `pre_dispatch` enforces
    deprecation and refuses duplicate spawns, `post_dispatch` records the
    close of the lifecycle. Refused dispatches are returned as a `failed`
    SquadResult with the Iolaus rationale, not raised — so the supervisor
    can surface them to HITL rather than crash.
    """
    try:
        verdict = pre_dispatch(pack, inbound, allow_archived=allow_archived)
    except SquadDeprecated as e:
        return SquadResult(
            envelopes=[], artifacts=[{"kind": "lifecycle_event",
                                       "data": {"kind": "refused_deprecated",
                                                "slug": e.slug,
                                                "deprecated_after": e.deprecated_after.isoformat()}}],
            status="failed",
            rationale=f"iolaus: {e}",
        )
    except DoubleSpawnRefused as e:
        return SquadResult(
            envelopes=[], artifacts=[{"kind": "lifecycle_event",
                                       "data": {"kind": "refused_duplicate",
                                                "slug": e.slug,
                                                "envelope_id": e.envelope_id}}],
            status="failed",
            rationale=f"iolaus: {e}",
        )

    if pack.entrypoint == "stub":
        result = _stub(pack, inbound)
    elif pack.entrypoint == "mcp":
        result = _via_mcp(state, pack, inbound, dispatcher,
                          collect_open_runs=collect_open_runs)
    elif pack.entrypoint == "agent-impersonation":
        result = _via_impersonation(state, pack, inbound, dispatcher)
    elif pack.entrypoint == "claude-skill":
        result = _via_claude_skill(state, pack, inbound, dispatcher)
    elif pack.entrypoint == "subprocess":
        result = _via_subprocess(state, pack, inbound, dispatcher)
    else:
        result = SquadResult(
            envelopes=[],
            artifacts=[],
            status="failed",
            rationale=f"unknown entrypoint {pack.entrypoint!r}",
        )

    post_evt = post_dispatch(pack, inbound, status=result.status, detail=result.rationale[:200])
    result.artifacts.append({"kind": "lifecycle_event", "data": post_evt.to_dict()})
    # Tuck pre_dispatch event at the head so the trace reads chronologically.
    result.artifacts.insert(0, {"kind": "lifecycle_event", "data": verdict.event.to_dict()})
    return result


# ---------- strategies ----------

def _stub(pack: SquadPack, inbound: HydraEnvelope) -> SquadResult:
    decision = DecisionRecord(
        workflow_id=inbound.workflow_id,
        parent_id=inbound.id,
        origin_squad=pack.slug,
        target_squad=inbound.origin_squad,
        decision=f"[STUB] {pack.name} not yet implemented",
        rationale=(
            f"Squad {pack.slug!r} is scaffolded but its entrypoint is 'stub'. "
            "Implementers: add a real entrypoint (mcp / subprocess / agent-impersonation "
            "/ claude-skill) in squad.yaml and supply the corresponding tools / commands."
        ),
        artifacts=[],
        sealed=False,
    )
    return SquadResult(
        envelopes=[decision],
        artifacts=[],
        status="surfaced",
        rationale="stub squad — surfaced for human follow-up",
    )


def _pp_inner(res: Any) -> dict[str, Any]:
    """Unwrap an MCP envelope's inner result dict, tolerating bare payloads."""
    if not isinstance(res, dict):
        return {}
    inner = res.get("result", res)
    return inner if isinstance(inner, dict) else {}


def _pp_ok(res: Any) -> bool:
    """True when an MCP envelope reports unambiguous success."""
    return (
        isinstance(res, dict)
        and res.get("status") in {"done", "ok", "complete"}
        and not _pp_inner(res).get("error")
    )


# Markers in a generate response (or its text) that mean codex never wrote
# code -- a genuine failure to surface, not a low-quality attempt to judge.
_GEN_FAIL_MARKERS: tuple[str, ...] = (
    "read-only sandbox", "rejected by user approval", "approval settings",
    "usage limit", "rate limit", "quota", "timed out", "permission denied",
)


def _worktree_dirty_set(project_path: str | None) -> set[str]:
    """Set of porcelain paths with uncommitted changes in the project tree.

    Fail-soft: a non-git root or any git error returns an empty set. Used to
    scope the drive loop's "did THIS run write code?" signal and the harvest
    commit — so we never attribute (or commit) changes a run did not make.
    """
    if not project_path:
        return set()
    root = Path(project_path)
    if not root.is_dir():
        return set()
    if not (root / ".git").exists() and not (root.parent / ".git").exists():
        return set()
    try:
        res = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=root, capture_output=True, text=True, check=False,
        )
    except Exception:  # noqa: BLE001 — never crash on a git hiccup
        return set()
    if res.returncode != 0:
        return set()
    out: set[str] = set()
    for line in res.stdout.splitlines():
        # porcelain v1: "XY <path>" (path may be quoted / a "old -> new" rename).
        path = line[3:].strip() if len(line) > 3 else ""
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        path = path.strip().strip('"')
        if path:
            out.add(path)
    return out


def _generate_failure_reason(
    gen: Any, gen_text: str, wrote_changes: bool = False
) -> str | None:
    """Reason a ``pp_codex.generate`` call produced no code, else ``None``.

    The drive loop used to treat a timeout / quota / read-only-sandbox / empty
    result identically to a real low-quality attempt: it fabricated a default
    ``revise`` verdict and surfaced the run with a misleading ``stage_outcome``,
    hiding the true cause (e.g. ``workspace read-only``). When this returns a
    reason the loop records a failed attempt and surfaces with that reason
    instead of judging or burning a Reflexion retry on a condition a retry
    cannot fix.

    Diff-aware: the soft text markers (``permission denied`` etc.) describe what
    codex narrated, and under ``--sandbox workspace-write`` codex CAN edit the
    worktree but CANNOT ``git commit`` (``.git/index.lock`` is read-only in that
    sandbox) or spawn child test runners (``spawn EPERM``). When codex honestly
    reports "commit/test blocked" AFTER writing files (``wrote_changes=True``,
    computed run-scoped by the caller), that is NOT a generate failure — the
    harness owns commit + smoke outside the sandbox (see
    ``harvest_pp_run_artifacts`` / ``_run_smoke``). So a text marker only counts
    as a failure when this run wrote NOTHING. The hard cases (timeout /
    transport error / empty output) remain failures regardless, since they mean
    no code was produced.
    """
    if isinstance(gen, dict):
        if gen.get("timeout"):
            return f"codex generate timed out: {str(gen.get('error') or gen)[:300]}"
        if not _pp_ok(gen):
            inner_err = _pp_inner(gen).get("error") or gen.get("error")
            if inner_err:
                return f"codex generate failed: {str(inner_err)[:300]}"
            status = gen.get("status")
            if status and status not in {"done", "ok", "complete"}:
                return f"codex generate returned status={status!r}"
    if not (gen_text or "").strip():
        return "codex generate returned no output (no code written)"
    low = (gen_text or "").lower()
    for marker in _GEN_FAIL_MARKERS:
        if marker in low:
            # Suppress when THIS run actually wrote code: the marker is about the
            # commit/test/browser steps the harness now performs itself.
            if wrote_changes:
                _log.info(
                    "codex narrated %r but the run wrote changes — treating "
                    "generate as success (harness owns commit/smoke)", marker,
                )
                return None
            return f"codex generate blocked ({marker}): {gen_text.strip()[:300]}"
    return None


# Independent smoke: codex RUNS the project's build/test command and reports its
# real exit code. This is an execution, not self-attestation about the diff --
# which is what PP-VG-5's anti-gaming gate requires before a code stage may
# finalize 'complete'. Any failure to obtain a clear pass degrades to 'skipped'
# (run stays honestly surfaced), never a forged 'pass'.
_SMOKE_PROMPT = (
    "Run this project's smoke check from the working directory. Detect and run "
    "the standard build/test command (e.g. `pytest -q`, `npm test` or `npm run "
    "build`, `node --check <entry>`, `go build ./...`). Do NOT modify any files. "
    "Report the REAL result from the command's exit code. Output ONLY a single "
    "JSON object as the LAST line: "
    '{"status":"pass"|"fail"|"skipped","reason":"<command + exit code>"}. '
    'Use "skipped" only if the project has no runnable smoke/test/build command.'
)


def _parse_smoke_verdict(text: str) -> tuple[str, str]:
    """Extract the last ``{"status": ...}`` JSON object from smoke output.

    Conservative: anything not explicitly ``pass`` / ``fail`` / ``infra_error``
    degrades to ``skipped`` so an unparseable smoke can never forge a pass.
    """
    import json as _json
    import re as _re
    for blob in reversed(_re.findall(r'\{[^{}]*"status"[^{}]*\}', text or "")):
        try:
            obj = _json.loads(blob)
        except Exception:  # noqa: BLE001
            continue
        st = str(obj.get("status") or "").strip().lower()
        if st in {"pass", "fail", "infra_error", "skipped"}:
            return st, str(obj.get("reason") or "")[:300]
    return "skipped", "no parseable smoke verdict"


def _detect_smoke_command(project_path: str) -> list[str] | None:
    """Detect the project's build/test command, or ``None`` if there isn't one.

    Heuristic, ordered by specificity. Returns argv (no shell). Node projects
    prefer a declared ``test`` script, else ``build``; Python projects prefer
    pytest. Keep this conservative — an undetected project degrades to
    ``skipped`` (an honest non-pass), never a forged pass.
    """
    import json as _json
    root = Path(project_path)
    pkg = root / "package.json"
    if pkg.is_file():
        try:
            scripts = (_json.loads(pkg.read_text(encoding="utf-8")) or {}).get("scripts", {})
        except Exception:  # noqa: BLE001
            scripts = {}
        if isinstance(scripts, dict):
            if scripts.get("test"):
                return ["npm", "test", "--silent"]
            if scripts.get("build"):
                return ["npm", "run", "build", "--silent"]
    if (root / "pyproject.toml").is_file() or (root / "pytest.ini").is_file() \
            or (root / "tox.ini").is_file() or (root / "setup.cfg").is_file() \
            or (root / "tests").is_dir():
        return ["pytest", "-q"]
    if (root / "go.mod").is_file():
        return ["go", "build", "./..."]
    if (root / "Cargo.toml").is_file():
        return ["cargo", "build"]
    return None


def _run_smoke(
    dispatcher: "Dispatcher", *, project_path: str, stage_id: str
) -> tuple[str, str]:
    """Run an independent smoke OUTSIDE the codex sandbox; return ``(status, reason)``.

    PP-VG-5 (anti-gaming) requires a real execution before a code stage may
    finalize ``complete``. This used to run the build/test command *through*
    ``pp_codex.generate`` under ``--sandbox workspace-write`` — but that sandbox
    forbids spawning child processes (e.g. vitest/esbuild → ``spawn EPERM``), so
    the smoke could never produce a real ``pass`` on a target repo and always
    degraded to ``skipped``. We now detect and run the command directly on the
    host. Exit code is authoritative: 0 → ``pass``, non-zero → ``fail``. No
    runnable command → ``skipped`` (an honest non-pass, never a forged pass).

    The ``dispatcher`` parameter is retained for call-site stability and possible
    future use; the host-side runner does not need it.
    """
    cmd = _detect_smoke_command(project_path)
    if not cmd:
        return "skipped", "no runnable build/test command detected"
    try:
        res = subprocess.run(
            cmd, cwd=project_path, capture_output=True, text=True,
            check=False, timeout=600,
        )
    except subprocess.TimeoutExpired:
        return "skipped", f"smoke timed out after 600s: {' '.join(cmd)}"
    except Exception as e:  # noqa: BLE001 -- a smoke that cannot run is 'skipped'
        return "skipped", f"smoke run errored ({' '.join(cmd)}): {e!r}"[:300]
    label = " ".join(cmd)
    status = "pass" if res.returncode == 0 else "fail"
    tail = (res.stderr or res.stdout or "").strip().splitlines()[-1:] or [""]
    return status, f"`{label}` exit={res.returncode} :: {tail[0]}"[:300]


def _build_engineer_prompt(request_text: str, project_path: str) -> str:
    """Prompt contract for headless code generation.

    Replaces what the typed Claude `engineer` agent supplies on the skill path:
    stage intent, the working directory, repo-convention awareness, and a
    request for a change summary + smoke evidence.
    """
    return (
        "You are the engineering implementation agent for a Hydra-dispatched task. "
        f"The working directory is the target repo ({project_path}). Implement the "
        "request by editing files DIRECTLY in this directory, following existing "
        "conventions (read AGENTS.md / CLAUDE.md first). Keep the change minimal and "
        "focused. After implementing, summarize the files you changed and any "
        "tests / smoke checks you ran and their result.\n\n"
        f"REQUEST:\n{request_text}"
    )


def _augment_with_critique(base_prompt: str, critique_md: str) -> str:
    """Reflexion ×1: fold the prior critique back into the retry prompt."""
    return (
        f"{base_prompt}\n\n"
        "A prior attempt was judged and needs revision. Address this critique "
        "specifically, then re-summarize your changes:\n"
        f"<critique>\n{critique_md[:3000]}\n</critique>"
    )


def _rubric_md(rubric_id: str) -> str:
    """Resolve a rubric body for the judge; degrade to a minimal rubric."""
    try:
        from .judge.registry import get_rubric
        return get_rubric(rubric_id).body_md
    except Exception:  # noqa: BLE001 — never block the loop on a registry miss
        return (
            "Evaluate the change for correctness, adherence to the stated request, "
            "and absence of regressions. Output outcome (pass/revise/fail), a "
            "critique, and per-dimension scores."
        )


def _drop_open_run(
    state: "HydraState", collect_open_runs: list | None, run_id: str
) -> None:
    """Remove a finalized run from the open-runs ledger so node_postcheck's
    abort path does not try to finalize an already-closed run."""
    try:
        if collect_open_runs is not None:
            collect_open_runs[:] = [
                e for e in collect_open_runs if e.get("run_id") != run_id
            ]
        else:
            state.open_pp_runs = [
                e for e in state.open_pp_runs if e.get("run_id") != run_id
            ]
    except Exception:  # noqa: BLE001
        pass


def _drive_pp_stage_loop(
    dispatcher: Dispatcher,
    *,
    run_id: str,
    project_path: str,
    request_text: str,
    model_tier: str | None = None,
    judge_rubric_id: str = "rfc-2119-normative",
) -> dict[str, Any]:
    """Drive a pp `code` stage to a finalized run, headless (no Claude driver).

    The pp daemon is a pure state machine — ``start_run`` only scaffolds. On the
    skill path the Claude session drives the lifecycle; on the headless live /
    fleet path THIS function is that driver. It calls only tools the engineering
    squad declares (RBAC-safe):

        start_stage → pp_codex.generate (codex edits the worktree directly)
                    → archive_artifact + record_attempt
                    → pp_gemini.critique (cross-vendor) + record_verdict
                    → [Reflexion ×1 on revise]
                    → finalize_stage(winner_attempt_id) → finalize_run

    Fail-soft: ANY exception triggers a best-effort ``finalize_run(aborted)`` to
    release the project lock, and returns ``final_status="aborted"``. NEVER
    raises — dispatch must not crash on a daemon hiccup.
    """
    sq = "engineering"
    cm = dispatcher.call_mcp
    out: dict[str, Any] = {
        "final_status": "aborted", "stage_outcome": None,
        "attempt_id": None, "critique": "", "error": None, "finalized": False,
        "wrote_changes": False, "smoke_status": "skipped", "smoke_reason": "",
        "harvest_sha": None, "harvest_error": None, "changed_paths": [],
    }
    # Snapshot the working tree BEFORE any generation so we can attribute (and
    # later commit) ONLY the files this run touches — never pre-existing dirt.
    pre_dirty = _worktree_dirty_set(project_path)
    try:
        st = cm("pp_harness", "start_stage",
                {"run_id": run_id, "kind": "code", "gate_type": "code"},
                squad_id=sq)
        stage_id = _pp_inner(st).get("stage_id")
        if not stage_id:
            raise RuntimeError(f"start_stage returned no stage_id: {st!r}")

        base_prompt = _build_engineer_prompt(request_text, project_path)
        attempt_id: str | None = None
        outcome: str | None = None
        critique_md = ""
        rubric_body = _rubric_md(judge_rubric_id)
        gen_failed = False

        # Reflexion ×1 → at most two attempts.
        for retry_index in range(2):
            prompt = (base_prompt if retry_index == 0
                      else _augment_with_critique(base_prompt, critique_md))
            # sandbox=workspace-write so codex can actually edit the worktree.
            # Without it the generate call defaults to read-only (codex-server
            # GenerateSchema), apply_patch is rejected, and the engineering
            # drive loop produces a patch *plan* but never writes code — the
            # stage then fails with nothing committed.
            gen = cm("pp_codex", "generate",
                     {"prompt": prompt, "cwd": project_path,
                      "sandbox": "workspace-write"}, squad_id=sq)
            gi = _pp_inner(gen)
            gen_text = str(gi.get("text") or "")

            # Run-scoped: paths dirtied since the pre-generate snapshot. Excludes
            # any files that were already modified before this run started.
            run_changed = _worktree_dirty_set(project_path) - pre_dirty
            wrote_changes = bool(run_changed)
            out["wrote_changes"] = out["wrote_changes"] or wrote_changes
            out["changed_paths"] = sorted(set(out["changed_paths"]) | run_changed)
            gen_fail = _generate_failure_reason(gen, gen_text, wrote_changes)
            if gen_fail:
                # Real generate failure (timeout / transport error / read-only
                # sandbox with NO diff / empty): surface the TRUE reason instead
                # of fabricating an empty 'revise' verdict, and don't spend a
                # Reflexion retry on a condition a retry cannot fix. Note: a
                # "commit/test blocked" narration AFTER codex wrote files is NOT
                # a failure here — _generate_failure_reason suppresses it because
                # the harness owns commit + smoke outside the sandbox.
                _log.warning(
                    "drive_loop generate failed (run=%s wrote_changes=%s): %s",
                    run_id, wrote_changes, gen_fail,
                )
                out["error"] = gen_fail
                out["stage_outcome"] = "error"
                gen_failed = True
                fail_status = ("timeout" if (isinstance(gen, dict)
                               and gen.get("timeout")) else "error")
                try:
                    cm("pp_harness", "archive_artifact", {
                        "run_id": run_id,
                        "relative_path": f"code/codex-attempt-{retry_index}.failed.md",
                        "bytes": f"GENERATE FAILED: {gen_fail}\n\n{gen_text or '(no output)'}",
                        "stage_id": stage_id, "kind": "code", "encoding": "utf8",
                    }, squad_id=sq)
                except Exception:  # noqa: BLE001
                    pass
                try:
                    att = cm("pp_harness", "record_attempt", {
                        "stage_id": stage_id, "producer": "codex",
                        "model_id": str(gi.get("model") or model_tier or "codex-default"),
                        "status": fail_status, "retry_index": retry_index,
                        "notes": {"candidate_index": 1},
                        **({"parent_attempt_id": attempt_id} if attempt_id else {}),
                    }, squad_id=sq)
                    attempt_id = _pp_inner(att).get("attempt_id") or attempt_id
                except Exception:  # noqa: BLE001
                    pass
                break

            # codex writes files in cwd directly; archive the producer summary
            # as the stage's code-record artifact (best-effort).
            try:
                cm("pp_harness", "archive_artifact", {
                    "run_id": run_id,
                    "relative_path": f"code/codex-attempt-{retry_index}.md",
                    "bytes": gen_text or "(no summary returned)",
                    "stage_id": stage_id, "kind": "code", "encoding": "utf8",
                }, squad_id=sq)
            except Exception:  # noqa: BLE001
                pass

            att = cm("pp_harness", "record_attempt", {
                "stage_id": stage_id,
                "producer": "codex",
                "model_id": str(gi.get("model") or model_tier or "codex-default"),
                "tokens_in": int(gi.get("tokens_in") or 0),
                "tokens_out": int(gi.get("tokens_out") or 0),
                "cost_usd": float(gi.get("cost_usd") or 0.0),
                "wall_ms": int(gi.get("wall_ms") or 0),
                "status": "ok",
                "retry_index": retry_index,
                "notes": {"candidate_index": 1},
                **({"parent_attempt_id": attempt_id} if attempt_id else {}),
            }, squad_id=sq)
            attempt_id = _pp_inner(att).get("attempt_id") or attempt_id

            # Cross-vendor critique: gemini judges codex's output.
            crit = cm("pp_gemini", "critique", {
                "artifact_text": gen_text or "(no diff summary returned)",
                "rubric_md": rubric_body,
                "cwd": project_path,
            }, squad_id=sq)
            ci = _pp_inner(crit)
            parsed = ci.get("parsed") if isinstance(ci.get("parsed"), dict) else ci
            if not isinstance(parsed, dict):
                parsed = {}
            outcome = parsed.get("outcome") or parsed.get("verdict") or "revise"
            critique_md = parsed.get("critique_md") or parsed.get("critique") or ""
            score_json = parsed.get("score") or parsed.get("score_json") or {}

            if attempt_id:
                try:
                    cm("pp_harness", "record_verdict", {
                        "attempt_id": attempt_id,
                        "judge_producer": "gemini",
                        "judge_model_id": str(ci.get("model") or "gemini-default"),
                        "outcome": outcome if outcome in {"pass", "revise", "fail"} else "revise",
                        "critique_md": str(critique_md)[:4000],
                        "score_json": score_json,
                        "rubric_id": judge_rubric_id,
                    }, squad_id=sq)
                except Exception:  # noqa: BLE001
                    pass

            if outcome == "pass":
                break  # accept; no Reflexion needed

        out["attempt_id"] = attempt_id

        # PP-VG-5 (anti-gaming): a code stage may finalize 'complete' only when a
        # real smoke result is recorded for the winning candidate. Run an
        # INDEPENDENT smoke (codex executes the project's build/test command and
        # reports its true exit code) and record it -- never a forged 'pass'.
        passed = False
        smoke_status = "skipped"
        smoke_reason = ""
        if not gen_failed:
            out["stage_outcome"] = outcome
            out["critique"] = str(critique_md)[:1000]
            if outcome == "pass" and attempt_id:
                smoke_status, smoke_reason = _run_smoke(
                    dispatcher, project_path=project_path, stage_id=stage_id)
                try:
                    cm("pp_harness", "record_smoke_status", {
                        "stage_id": stage_id, "candidate_index": 1,
                        "status": smoke_status,
                        "reason": (smoke_reason or "headless drive-loop smoke")[:300],
                    }, squad_id=sq)
                except Exception:  # noqa: BLE001
                    pass
                passed = smoke_status == "pass"
                _log.info(
                    "drive_loop smoke (run=%s): status=%s reason=%s",
                    run_id, smoke_status, smoke_reason[:200],
                )
        out["smoke_status"] = smoke_status
        out["smoke_reason"] = smoke_reason

        try:
            cm("pp_harness", "finalize_stage", {
                "stage_id": stage_id,
                "status": "passed" if passed else "surfaced",
                **({"winner_attempt_id": attempt_id} if (passed and attempt_id) else {}),
            }, squad_id=sq)
        except Exception:  # noqa: BLE001
            pass

        if gen_failed:
            summary = f"Headless drive loop: generate failed -- {out.get('error')}"
        elif passed:
            summary = f"Headless drive loop: stage_outcome=pass; smoke={smoke_status}."
        else:
            summary = (f"Headless drive loop: stage_outcome={outcome}; "
                       f"smoke={smoke_status} ({smoke_reason[:120]}).")

        # finalize_run runs PP gates server-side and may downgrade -- don't
        # assume success; reflect the returned status.
        fin = cm("pp_harness", "finalize_run", {
            "run_id": run_id,
            "status": "complete" if passed else "surfaced",
            "summary_md": summary,
        }, squad_id=sq)
        out["finalized"] = True
        fin_status = _pp_inner(fin).get("status")
        if passed and (fin_status in {"complete", "done", "ok"} or _pp_ok(fin)) \
                and fin_status not in {"surfaced", "failed", "aborted", "blocked"}:
            out["final_status"] = "complete"
        else:
            out["final_status"] = "surfaced"
        return out
    except Exception as e:  # noqa: BLE001 — fail-soft; always release the lock
        out["error"] = repr(e)
        try:
            cm("pp_harness", "finalize_run", {
                "run_id": run_id, "status": "aborted",
                "reason": f"drive_loop_error: {e!r}",
                "project_path": project_path,
            }, squad_id=sq)
            out["finalized"] = True
        except Exception:  # noqa: BLE001
            pass
        out["final_status"] = "aborted"
        return out


# Industries (Constraints.industries / squad.yaml industries) that signal game
# work and auto-default engineering dispatch to a pair-programmer game team.
_GAME_INDUSTRIES: frozenset[str] = frozenset({
    "games", "game-development", "interactive-entertainment",
    "live-service-games", "mobile-games", "aaa-games", "indie-games", "esports",
})
# Originating squads whose handed-off engineering work defaults to a game team.
_GAME_ORIGIN_SQUADS: frozenset[str] = frozenset({"rlm-gaming"})
# The default pp team for auto-detected game work. rlm-gaming sets a more
# specific team (game-netcode-team / game-cert-team / …) explicitly via pp_team
# when the DEV_TASK warrants it; this is only the safety-net default.
_DEFAULT_GAME_TEAM = "game-feature-team"


def _resolve_pp_team(inbound: HydraEnvelope) -> tuple[str | None, str]:
    """Resolve the pair-programmer team for an engineering dispatch.

    Precedence (per operator decision: explicit + auto-default):
      1. An explicit ``inbound.pp_team`` (set by the orchestrator squad, e.g.
         rlm-gaming on a forwarded DEV_TASK) always wins.
      2. Otherwise auto-default to a game team when the work originates from a
         game squad (``origin_squad in _GAME_ORIGIN_SQUADS``) or the envelope's
         ``constraints.industries`` intersect ``_GAME_INDUSTRIES``.
      3. Otherwise ``None`` — the caller falls back to the engineering
         ``squad.yaml`` default (unchanged legacy behaviour).

    Returns ``(team_or_None, reason)``; ``reason`` is recorded in the
    SquadResult rationale for observability.
    """
    explicit = getattr(inbound, "pp_team", None)
    if explicit:
        return str(explicit), f"explicit pp_team={explicit!r}"
    origin = getattr(inbound, "origin_squad", None)
    industries: set[str] = set()
    try:
        raw = getattr(getattr(inbound, "constraints", None), "industries", None) or []
        industries = {str(i).strip().lower() for i in raw}
    except Exception:  # noqa: BLE001 — never crash dispatch on a malformed envelope
        industries = set()
    if origin in _GAME_ORIGIN_SQUADS or (industries & _GAME_INDUSTRIES):
        return _DEFAULT_GAME_TEAM, (
            f"auto-default game team (origin={origin!r}, "
            f"game_industries={sorted(industries & _GAME_INDUSTRIES)})"
        )
    return None, "no game signal; squad default"


def _maybe_write_claude_shim(project_path: str) -> None:
    """Backfill the tool-specific shim when AGENTS.md exists but CLAUDE.md does not."""
    root = Path(project_path)
    agents_md = root / "AGENTS.md"
    claude_md = root / "CLAUDE.md"
    if agents_md.is_file() and not claude_md.exists():
        claude_md.write_text("@AGENTS.md\n", encoding="utf-8")


def _via_mcp(
    state: HydraState,
    pack: SquadPack,
    inbound: HydraEnvelope,
    dispatcher: Dispatcher,
    *,
    collect_open_runs: list | None = None,
) -> SquadResult:
    """Wire into the pair-programmer harness (engineering squad).

    Invocation contract from `squad.yaml.invoke`:
        mode: pp_run | pp_team | pp_best_of | pp_review
        default_team: feature-team
        forum_for_review: change-advisory-board
        model_tier: (optional) haiku | sonnet | opus | fable | deep

    WS9 — model_tier propagation:
        Effective tier = (inbound.model_tier if present) else
                         invoke.get("model_tier") else None.
        Validated via normalize_tier; unknown tier -> failed SquadResult (fail-closed).
        "fable" / "deep" -> FORCE mode="pp_team" + team="deep-reasoning-team".
        Fable is reachable ONLY by explicit tier; no automatic escalation.
        Other tiers (haiku/sonnet/opus): keep existing mode/team; tier is
        recorded in the SquadResult rationale for observability only (pp's
        start_run schema does not accept a raw model_tier arg).
    """
    from .tiers import normalize_tier, FABLE_TIERS

    invoke = pack.invoke or {}
    mode = invoke.get("mode", "pp_run")

    # WS9: resolve effective model_tier. Envelope wins over squad.yaml default.
    # Fix 1: use `is not None` guard so that an empty-string inbound tier ("") is
    # treated as an explicit (invalid) value rather than falling through to the
    # squad.yaml default — empty string is fail-closed, not silently ignored.
    inbound_tier = getattr(inbound, "model_tier", None)
    raw_tier = inbound_tier if inbound_tier is not None else invoke.get("model_tier")
    try:
        effective_tier = normalize_tier(raw_tier)
    except ValueError as tier_err:
        return SquadResult(
            envelopes=[], artifacts=[], status="failed",
            rationale=f"unknown model_tier={raw_tier!r}: {tier_err}",
        )

    # Repo-targeting: resolve target_repo_id FIRST, before reading any
    # invoke["project_path"] from squad.yaml — the registry is the only
    # authoritative path source when a repo override is requested.
    # resolve_repo_path is called immediately before dispatch (minimal TOCTOU
    # window) and the registry dirs are operator-trusted config, not user input.
    # A rejected id (unknown key, raw path, git verification failure, base
    # escape) short-circuits the dispatch with a "failed" result rather than
    # silently falling back to the default CWD.
    # Repo-targeting is only honoured for the engineering squad — other mcp
    # squads (e.g. executive) must not be retargeted via this mechanism.
    # resolve_repo_path is called immediately before dispatch (minimal TOCTOU
    # window); registry dirs are operator-trusted sibling repos so an attacker
    # who can swap those dirs already owns the host — no further mitigation needed.
    target_repo_id = getattr(inbound, "target_repo_id", None)
    target_repo_subpath = getattr(inbound, "target_repo_subpath", None)
    if target_repo_id and pack.slug == "engineering":
        from hydra_core.repo_registry import resolve_repo_project_path
        try:
            project_path = str(resolve_repo_project_path(target_repo_id, target_repo_subpath))
            if target_repo_subpath:
                Path(project_path).mkdir(parents=True, exist_ok=True)
        except Exception as e:
            return SquadResult(
                envelopes=[], artifacts=[], status="failed",
                rationale=(
                    "repo-targeting rejected "
                    f"target_repo_id={target_repo_id!r} "
                    f"target_repo_subpath={target_repo_subpath!r}: {e}"
                ),
            )
    else:
        # No repo override (or non-engineering mcp squad) — use the trusted
        # operator config from squad.yaml, with ${project_root} -> cwd.
        project_path = invoke.get("project_path") or str(state.workflow_id and __import__("pathlib").Path.cwd())
        if "${project_root}" in str(project_path):
            project_path = str(__import__("pathlib").Path.cwd())

    # WS9: Fable routing — explicit tier="fable"/"deep" forces deep-reasoning-team.
    # This is the ONLY path to Fable; no auto-escalation is performed here.
    #
    # Fix 6: Reserve "deep-reasoning-team". The squad.yaml default_team is checked
    # AFTER tier routing. If the default_team happens to be "deep-reasoning-team"
    # but the effective tier is NOT fable/deep, we REJECT rather than silently
    # routing to the Fable team without an explicit tier. Match is case- and
    # whitespace-insensitive so minor config variants are caught.
    _DEEP_TEAM = "deep-reasoning-team"

    if effective_tier in FABLE_TIERS:
        mode = "pp_team"
        fable_team = _DEEP_TEAM
        resolved_team: str | None = None
        team_reason = "fable/deep tier -> deep-reasoning-team"
    else:
        fable_team = None  # not a Fable dispatch
        # Game-team routing (explicit pp_team or auto-default for game work).
        # A resolved team forces team-mode and overrides the squad.yaml default;
        # a None result preserves the legacy mode/default_team behaviour.
        resolved_team, team_reason = _resolve_pp_team(inbound)
        if resolved_team is not None:
            mode = "pp_team"

    # Fix 6 guard: if default_team points at the reserved Fable team but no fable
    # tier was given, reject — the deep-reasoning-team is only reachable via an
    # explicit fable/deep tier. Only relevant when we'd fall back to the
    # squad.yaml default_team (no fable team, no resolved game team).
    if fable_team is None and resolved_team is None and mode == "pp_team":
        default_team = (invoke.get("default_team") or "").strip().lower()
        if default_team == _DEEP_TEAM:
            return SquadResult(
                envelopes=[], artifacts=[], status="failed",
                rationale=(
                    f"deep-reasoning-team requires model_tier=fable/deep; "
                    f"current effective_tier={effective_tier!r}. "
                    "Set model_tier=fable in the dispatch envelope or squad.yaml invoke."
                ),
            )

    args = {
        "request_text": getattr(inbound, "instructions", None)
        or getattr(inbound, "summary", None)
        or getattr(inbound, "objective", "")
        or str(inbound.model_dump()),
        "project_path": project_path,
        "mode": "single" if mode == "pp_run" else ("team" if mode == "pp_team" else "single"),
    }
    if mode == "pp_team":
        # Precedence: Fable tier -> deep-reasoning-team; else a resolved game
        # team (explicit pp_team or auto-default); else the squad.yaml default.
        args["team"] = fable_team or resolved_team or invoke.get("default_team")
    if mode == "pp_best_of":
        args["mode"] = "best_of"
        args["n"] = 3
    if mode == "pp_review":
        args["mode"] = "review"
        args["forum"] = invoke.get("forum_for_review")
    # Drop None values — pp schema rejects them.
    args = {k: v for k, v in args.items() if v is not None}
    # WS9: record effective tier in rationale for observability.
    # Do NOT pass model_tier as an arg — pp's start_run schema rejects unknown args.
    try:
        result = dispatcher.call_mcp("pp_harness", "start_run", args,
                                     squad_id=pack.slug)
    except Exception as e:
        return SquadResult(
            envelopes=[], artifacts=[], status="failed",
            rationale=f"pp_harness unreachable: {e!r}",
        )

    # MCP results come back as {"status":"done","tool":"start_run","result":{...}}
    inner = result.get("result", result) if isinstance(result, dict) else {}
    run_id = (inner or {}).get("run_id") if isinstance(inner, dict) else None
    pp_status = result.get("status", "unknown") if isinstance(result, dict) else "unknown"

    # B7: register the pp run on state so node_postcheck can finalize-abort it
    # if the workflow surfaces. start_run acquired <project>/.harness/.lock and
    # the pp daemon will only release on a matching finalize_run. Without this
    # registration a supervisor crash leaves the lock orphaned past the pp-side
    # TTL — the failure surface the bootstrap session hit twice. We register
    # whenever pp returned a run_id, regardless of pp_status; even a "done"
    # response means the run row exists in pp's db and the lock is held until
    # finalize.
    if run_id and isinstance(run_id, str) and project_path:
        try:
            _entry: dict[str, str] = {"run_id": run_id, "project_path": str(project_path)}
            if collect_open_runs is not None:
                # Fleet path: caller owns a per-task collector; the fleet merges
                # all collectors into state.open_pp_runs in the MAIN thread after
                # the parallel join so workers never mutate shared state.
                collect_open_runs.append(_entry)
            else:
                # Sequential / non-fleet path: mutate state directly (safe —
                # single-threaded node_dispatch context).
                state.open_pp_runs.append(_entry)
        except Exception:  # noqa: BLE001 — never crash dispatch on state writes
            pass

    if run_id and isinstance(run_id, str) and pp_status == "done" and project_path:
        try:
            dispatcher.call_mcp(
                "pp_harness",
                "ensure_agents_md",
                {"project_path": str(project_path)},
                squad_id=pack.slug,
            )
            _maybe_write_claude_shim(str(project_path))
        except Exception:  # noqa: BLE001 — AGENTS/CLAUDE bootstrap is fail-soft
            pass

    # Headless drive loop: the live CLI / fleet dispatcher sets drive_pp_loop so
    # the engineering squad DRIVES pp to actual code generation (start_run alone
    # only scaffolds). On the skill path drive_pp_loop is absent/False and we
    # keep the legacy "scaffold + return running" behavior unchanged.
    loop_outcome: dict[str, Any] | None = None
    # `is True` (not just truthy) so a Mock/stub dispatcher whose attribute
    # access auto-vivifies a truthy object does NOT accidentally engage the
    # loop. Real callers (cli.py live path, fleet factory) set a literal True.
    if getattr(dispatcher, "drive_pp_loop", False) is True and run_id and pp_status == "done":
        loop_outcome = _drive_pp_stage_loop(
            dispatcher,
            run_id=str(run_id),
            project_path=str(project_path),
            request_text=str(args.get("request_text", "")),
            model_tier=effective_tier,
        )
        # The loop already finalized the run (complete/surfaced/aborted) → the
        # project lock is released. Drop the ledger entry so node_postcheck's
        # abort path does not double-finalize a closed run.
        if loop_outcome.get("finalized"):
            _drop_open_run(state, collect_open_runs, str(run_id))
        # Drive the downstream harvest + status mapping off the loop's result.
        pp_status = loop_outcome.get("final_status", pp_status)

    # Worktree handoff: when the inbound envelope ran on its own project_path
    # (i.e. the planner allocated a worktree per the pp_harness_project_lock
    # rule) and pp-harness reported a terminal status, harvest the archived
    # artifacts into the project tree and commit them. Without this, work
    # products end up stranded in <project>/.harness/<run_id>/ and are never
    # visible on a branch — Discovery agent E2's research artifacts hit this
    # exact failure mode in the bootstrap session.
    commit_sha: str | None = None
    # Harvest when pp reported a terminal status, OR when the drive loop reports
    # codex wrote changes (covers the case where codex produced good code but its
    # own commit was blocked by the workspace-write sandbox — the harness must
    # land those edits itself, outside the sandbox).
    wrote = bool(loop_outcome and loop_outcome.get("wrote_changes"))
    if run_id and project_path and (pp_status in {"done", "complete", "surfaced"} or wrote):
        try:
            commit_sha = harvest_pp_run_artifacts(
                project_path=str(project_path),
                run_id=str(run_id),
                workflow_id=inbound.workflow_id,
                changed_paths=(loop_outcome or {}).get("changed_paths"),
            )
            if loop_outcome is not None:
                loop_outcome["harvest_sha"] = commit_sha
            _log.info("harvest committed run=%s sha=%s", run_id, commit_sha or "none")
        except Exception as e:  # noqa: BLE001 — never crash dispatch on a git failure
            commit_sha = None
            if loop_outcome is not None:
                loop_outcome["harvest_error"] = repr(e)[:300]
            _log.warning("harvest failed for run=%s: %r", run_id, e)

    # Status mapping. Drive-loop runs report a terminal final_status; legacy
    # scaffold-only dispatch reports "running" (the pp daemon owns the rest).
    if loop_outcome is not None:
        fs = loop_outcome.get("final_status")
        result_status = "done" if fs == "complete" else ("failed" if fs == "aborted" else "surfaced")
        loop_summary = (
            f"; drive_loop: final_status={fs}, stage_outcome="
            f"{loop_outcome.get('stage_outcome')}"
            f", wrote_changes={loop_outcome.get('wrote_changes')}"
            f", smoke={loop_outcome.get('smoke_status')}"
            + (f", harvest_sha={loop_outcome.get('harvest_sha')}"
               if loop_outcome.get("harvest_sha") else "")
            + (f", harvest_error={loop_outcome.get('harvest_error')}"
               if loop_outcome.get("harvest_error") else "")
            + (f", error={loop_outcome.get('error')}" if loop_outcome.get("error") else "")
        )
    else:
        result_status = "running" if pp_status == "done" and run_id else pp_status
        loop_summary = ""

    decision = DecisionRecord(
        workflow_id=inbound.workflow_id,
        parent_id=inbound.id,
        origin_squad=pack.slug,
        target_squad=inbound.origin_squad,
        decision=f"Engineering work dispatched to pair-programmer (run_id={run_id or '?'})",
        rationale=(
            f"mode={mode}; team={args.get('team', 'n/a')} ({team_reason}); "
            f"pp_profile={getattr(inbound, 'pp_profile', None) or 'default'}; "
            f"model_tier={effective_tier or 'default'}; "
            f"pp dispatch status: {pp_status}; "
            f"commit_sha={commit_sha or 'none'}{loop_summary}; inner: {str(inner)[:240]}"
        ),
        artifacts=[MemoryRef(tier="episodic", key=f"pp:run:{run_id or 'unknown'}")] if run_id else [],
    )
    return SquadResult(
        envelopes=[decision],
        artifacts=[{"kind": "pp_run", "ref": run_id, "raw": result, "commit_sha": commit_sha,
                    "drive_loop": loop_outcome}],
        status=result_status,
        # A driven run has already been cross-vendor judged inside pp — exempt
        # its DecisionRecord from the supervisor's NoOp re-judge / retry loop.
        pp_loop_judged=loop_outcome is not None,
    )


def harvest_pp_run_artifacts(
    *,
    project_path: str,
    run_id: str,
    workflow_id: str,
    changed_paths: list[str] | None = None,
) -> str | None:
    """Stage and commit the pp run's outputs into the project tree.

    Returns the commit SHA on success, or ``None`` when there is nothing to
    commit, the project isn't a git repo, or any git invocation fails. The
    helper is deliberately fail-soft — Hydra's dispatch path must never
    crash because the operator chose a non-git project root.

    Why this exists: codex (the headless generator) edits files DIRECTLY in the
    project tree under ``--sandbox workspace-write`` but CANNOT ``git commit``
    itself — that sandbox makes ``.git`` read-only (``.git/index.lock`` →
    Permission denied). pp-harness additionally archives metadata under
    ``<project>/.harness/<run_id>/``. Both are stranded if no one commits them.
    This helper lands them, OUTSIDE the sandbox, in one
    ``chore(hydra): harvest pp run <run_id>`` commit so synthesis + the upstream
    merge see the work.

    Scope (RUN-SCOPED — never a blanket ``git add -u``): stages exactly
    ``changed_paths`` (the files THIS run dirtied, computed by the drive loop as
    the delta from a pre-generate snapshot — so pre-existing uncommitted edits in
    the operator's tree are never swept in) plus the run's archived metadata
    under ``.harness/<run_id>``. When ``changed_paths`` is None/empty only the
    archived metadata is committed. Paths are added with explicit pathspecs so
    git respects ``.gitignore`` and we never touch unrelated files.
    """
    root = Path(project_path)
    if not root.is_dir():
        return None
    if not (root / ".git").exists() and not (root.parent / ".git").exists():
        return None

    def _git(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )

    # 1) archived run metadata (best-effort — may not exist on every path).
    harness_dir = root / ".harness" / run_id
    if harness_dir.is_dir():
        _git("add", "--", str(harness_dir))
    # 2) ONLY the files this run touched (explicit pathspecs, .gitignore-aware).
    for rel in (changed_paths or []):
        rel = str(rel).strip()
        if rel and ".." not in rel:  # defensive: no path escape
            _git("add", "--", rel)

    # Anything staged? `git diff --cached --quiet` exits 1 when there is.
    if _git("diff", "--cached", "--quiet").returncode == 0:
        return None  # nothing new to commit

    commit = _git(
        "-c", "user.name=hydra-dispatcher",
        "-c", "user.email=hydra@local",
        "commit",
        "-m", f"chore(hydra): harvest pp run {run_id} (workflow={workflow_id})",
    )
    if commit.returncode != 0:
        return None
    sha = _git("rev-parse", "HEAD")
    return sha.stdout.strip() or None


def _via_impersonation(
    state: HydraState,
    pack: SquadPack,
    inbound: HydraEnvelope,
    dispatcher: Dispatcher,
) -> SquadResult:
    """ExecutiveSuite pattern — Claude Code impersonates the roster IN PROCESS.

    Enrichment pass: consult the `executive_suite` MCP for the live roster, then
    after the host-pickup envelope, persist the prompt to the pack's `output/`
    tree via `es.output.write`. The returned `MemoryRef.key` points at the real
    on-disk path so downstream consumers can resolve it.
    """
    # Pre-call: pull live roster from the MCP shim (falls back to pack.agents).
    _on_mcp_err = _record_mcp_failure(state)
    live_roster = _mcp_call_safe(
        dispatcher, "executive_suite", "es.roster.list", {},
        squad_id=pack.slug, on_error=_on_mcp_err, idempotent=True,
    )
    roster_list = (live_roster or {}).get("agents", []) if isinstance(live_roster, dict) else []
    if roster_list:
        roster = ", ".join(r["name"] for r in roster_list[:8])
    else:
        roster = ", ".join(
            f"{a.slug} ({a.role})" for a in pack.agents if a.authority != "advisory"
        ) or ", ".join(a.slug for a in pack.agents[:4])

    objective = getattr(inbound, "objective", None) or getattr(inbound, "summary", None) or "(see envelope)"
    tool_scope = build_tool_scope_directive(pack)
    prompt = (
        "[Hydra→Executive Squad] You are the boardroom facilitator. "
        f"Convene relevant executives ({roster}). "
        f"Topic: {objective}\n\n"
        f"Constraints: {inbound.constraints.model_dump()}\n"
        f"Envelope type: {inbound.type}\n"
        "Follow ExecutiveSuite Board Meeting Protocol. Output a "
        "C_SUITE_DECISION_PACKET with proposed_tasks decomposed for downstream "
        "squads, and a DECISION_RECORD with dissenting opinions preserved verbatim."
        + (f"\n\n{tool_scope}" if tool_scope else "")
    )
    try:
        result = dispatcher.emit_claude_prompt(prompt, agent="boardroom")
    except Exception as e:
        return SquadResult(
            envelopes=[], artifacts=[], status="failed",
            rationale=f"impersonation dispatch failed: {e!r}",
        )

    # Post-call: persist the prompt + host-pickup envelope under ExecutiveSuite/output/.
    domain = _domain_for(pack, inbound)
    topic = (objective or "boardroom")[:80]
    write_result = _mcp_call_safe(
        dispatcher, "executive_suite", "es.output.write",
        {"domain": domain, "topic": topic,
         "content": _render_session_md("Boardroom Session", prompt, result)},
        squad_id=pack.slug, on_error=_on_mcp_err,
    )
    artifacts_refs: list[MemoryRef] = []
    rel_path = (write_result or {}).get("relative") if isinstance(write_result, dict) else None
    if rel_path:
        artifacts_refs.append(MemoryRef(
            tier="episodic",
            key=f"es:output:{rel_path}",
            summary=f"Boardroom session for {topic}",
        ))
    else:
        artifacts_refs.append(MemoryRef(tier="episodic", key=f"es:boardroom:{uuid4()}"))

    decision = DecisionRecord(
        workflow_id=inbound.workflow_id,
        parent_id=inbound.id,
        origin_squad=pack.slug,
        target_squad=inbound.origin_squad,
        decision="Boardroom session run",
        rationale=str(result.get("summary", "(see artifact)"))[:1000],
        artifacts=artifacts_refs,
    )
    host_pickup = (
        isinstance(result, dict)
        and result.get("status") == "host_pickup_required"
    )
    return SquadResult(
        envelopes=[decision],
        artifacts=[{"kind": "boardroom_minutes", "raw": result, "persisted": write_result}],
        status="done",
        host_pickup_pending=host_pickup,
    )


# Per-squad shim registry for claude-skill packs. Each entry names the MCP
# shim server, its tool prefix (`<prefix>.command.list`, `<prefix>.output.write`),
# how the output-write path is keyed ("phase" → _phase_for, "domain" →
# _domain_for), and the user-facing labels. Unknown claude-skill squads fall
# back to the RLM entry (the original behavior) so legacy packs keep working.
_SKILL_PACK_SHIMS: dict[str, dict[str, str]] = {
    "garland": {
        "server": "rlm_creative", "prefix": "rlm", "default_cmd": "/rlm-team",
        "path_key": "phase", "label": "Creative work", "artifact_kind": "creative_output",
    },
    "legal-compliance": {
        "server": "senate", "prefix": "senate", "default_cmd": "/senate",
        "path_key": "domain", "label": "Legal counsel", "artifact_kind": "legal_output",
    },
    "rlm-gaming": {
        "server": "rlm_gaming", "prefix": "rlmgaming", "default_cmd": "/game-studio",
        "path_key": "phase", "label": "Game studio", "artifact_kind": "game_design_output",
    },
}


# Envelope types a claude-skill orchestrator (e.g. rlm-gaming) may emit for
# delegation to a sibling squad. The supervisor routes these onward
# (DEV_TASK/PRD -> engineering; CREATIVE_BRIEF/SHOT_LIST/ASSET_JOB -> garland).
_DELEGATION_EMIT_TYPES: frozenset[str] = frozenset({
    "PRD", "DEV_TASK", "ARCH_RFC",
    "CREATIVE_BRIEF", "SHOT_LIST", "ASSET_JOB", "HANDOFF",
})


def _extract_emitted_envelopes(
    result: Any, inbound: HydraEnvelope, producer_slug: str,
) -> list[HydraEnvelope]:
    """Pull typed delegation envelopes out of a claude-skill result.

    RC1: the skill adapter historically returned ONLY a DecisionRecord, so the
    PRD/DEV_TASK/CREATIVE_BRIEF envelopes a game-studio run emits to delegate
    implementation never reached engineering/garland. When the host / gateway
    actually runs the skill it returns those envelopes under ``emitted_envelopes``
    (or ``envelopes``); we validate each, stamp the producing squad as
    ``origin_squad`` (so engineering auto-defaults to a game pp team) and the
    parent/workflow ids, and surface them so the supervisor can route them.

    Defensive: a malformed entry is skipped, never crashes dispatch. Non-dict
    results (e.g. a bare host-pickup stub) yield ``[]``.
    """
    if not isinstance(result, dict):
        return []
    raw = result.get("emitted_envelopes")
    if raw is None:
        raw = result.get("envelopes")
    if not isinstance(raw, list):
        return []
    from .repo_registry import is_known_repo, normalize_repo_subpath
    out: list[HydraEnvelope] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        if item.get("type") not in _DELEGATION_EMIT_TYPES:
            continue
        env = dict(item)
        # FORCE origin_squad to the producing squad — never trust a skill-set
        # value (audit integrity + game-team auto-default both key off it, so a
        # spoofed origin must not slip through). workflow_id/parent_id are
        # inherited from the inbound only when absent.
        env["origin_squad"] = producer_slug
        env.setdefault("workflow_id", str(inbound.workflow_id))
        env.setdefault("parent_id", str(inbound.id))
        # Repo targeting: a DEV_TASK whose `repo` names an allow-listed repo_id
        # gets target_repo_id mirrored so _via_mcp resolves the real path (e.g.
        # repo="candc" -> the CandC checkout) instead of the workflow CWD.
        if (
            env.get("type") == "DEV_TASK"
            and not env.get("target_repo_id")
            and is_known_repo(env.get("repo") or "")
        ):
            env["target_repo_id"] = str(env["repo"]).strip().lower()
        if env.get("type") == "DEV_TASK":
            raw_subpath = (
                env.get("target_repo_subpath")
                or env.get("repo_subpath")
                or env.get("subdir")
            )
            if raw_subpath:
                try:
                    env["target_repo_subpath"] = normalize_repo_subpath(str(raw_subpath))
                except ValueError:
                    # Fail closed later in _via_mcp if an explicit target_repo_subpath
                    # is malformed; aliases are ignored unless they normalize cleanly.
                    if env.get("target_repo_subpath"):
                        env["target_repo_subpath"] = str(raw_subpath)
        try:
            out.append(validate_envelope(env))
        except Exception:  # noqa: BLE001 — skip malformed; never crash dispatch
            continue
    return out


def _via_claude_skill(
    state: HydraState,
    pack: SquadPack,
    inbound: HydraEnvelope,
    dispatcher: Dispatcher,
) -> SquadResult:
    """Pack-skill pattern — invoke a Claude Code skill (e.g. /rlm-team, /senate).

    Enrichment: consult the squad's MCP shim (per `_SKILL_PACK_SHIMS`) for the
    live command catalogue, then persist the resulting host-pickup envelope via
    `<prefix>.output.write`. The returned `MemoryRef.key` points at the real
    on-disk path. Squads without a shim entry use the RLM shim (legacy default).
    """
    shim = _SKILL_PACK_SHIMS.get(pack.slug, _SKILL_PACK_SHIMS["garland"])
    server, prefix = shim["server"], shim["prefix"]
    invoke = pack.invoke or {}
    cmd = invoke.get("command_hint", shim["default_cmd"])

    _on_mcp_err = _record_mcp_failure(state)
    catalogue = _mcp_call_safe(
        dispatcher, server, f"{prefix}.command.list", {},
        squad_id=pack.slug, on_error=_on_mcp_err, idempotent=True,
    )
    available_cmds = [c["name"] for c in (catalogue or {}).get("commands", [])] if isinstance(catalogue, dict) else []
    tool_scope = build_tool_scope_directive(pack)
    # RC5: cross-squad delegation priming so the host-run skill emits properly
    # typed DEV_TASK/CREATIVE_BRIEF envelopes (with pp_team + game context) for
    # Hydra to forward — instead of returning prose that strands implementation.
    from .node_context import get_squad_dispatch_priming
    priming = get_squad_dispatch_priming(pack.slug)
    skill_args: dict[str, Any] = {
        "envelope": inbound.model_dump(mode="json"),
        "available_commands": available_cmds,
        "tool_scope": tool_scope,
    }
    if priming:
        skill_args["priming"] = priming
    try:
        result = dispatcher.invoke_claude_skill(cmd.lstrip("/"), skill_args)
    except Exception as e:
        return SquadResult(
            envelopes=[], artifacts=[], status="failed",
            rationale=f"claude-skill {cmd} failed: {e!r}",
        )

    path_val = (_domain_for(pack, inbound) if shim["path_key"] == "domain"
                else _phase_for(inbound))
    topic = (getattr(inbound, "objective", None)
             or getattr(inbound, "summary", None)
             or cmd.lstrip("/"))[:80]
    write_result = _mcp_call_safe(
        dispatcher, server, f"{prefix}.output.write",
        {shim["path_key"]: path_val, "topic": topic,
         "content": _render_session_md(f"{shim['label']} dispatch via {cmd}",
                                       f"command_hint={cmd}\navailable={available_cmds}",
                                       result)},
        squad_id=pack.slug, on_error=_on_mcp_err,
    )
    artifacts_refs: list[MemoryRef] = []
    rel_path = (write_result or {}).get("relative") if isinstance(write_result, dict) else None
    if rel_path:
        artifacts_refs.append(MemoryRef(
            tier="episodic",
            key=f"{prefix}:output:{rel_path}",
            summary=f"{shim['label']} dispatch: {topic}",
        ))
    else:
        artifacts_refs.append(MemoryRef(tier="episodic", key=f"{prefix}:{uuid4()}"))

    decision = DecisionRecord(
        workflow_id=inbound.workflow_id,
        parent_id=inbound.id,
        origin_squad=pack.slug,
        target_squad=inbound.origin_squad,
        decision=f"{shim['label']} dispatched via {cmd}",
        rationale=str(result.get("summary", ""))[:1000],
        artifacts=artifacts_refs,
    )
    host_pickup = (
        isinstance(result, dict)
        and result.get("status") == "host_pickup_required"
    )
    # RC1: surface any delegation envelopes the skill emitted (DEV_TASK/PRD ->
    # engineering, CREATIVE_BRIEF/SHOT_LIST/ASSET_JOB -> garland) so the
    # supervisor can route them onward. The DecisionRecord stays first.
    emitted = _extract_emitted_envelopes(result, inbound, pack.slug)
    return SquadResult(
        envelopes=[decision, *emitted],
        artifacts=[{"kind": shim["artifact_kind"], "raw": result, "persisted": write_result}],
        status=result.get("status", "done"),
        host_pickup_pending=host_pickup,
    )


# ---------- enrichment helpers ----------

def _mcp_call_safe(
    dispatcher: Dispatcher,
    server: str,
    tool: str,
    args: dict[str, Any],
    *,
    squad_id: str | None = None,
    on_error: "Callable[[str, str, str, int], None] | None" = None,
    idempotent: bool = False,
) -> dict[str, Any] | None:
    """Best-effort MCP call. Returns the inner result dict, or None on any failure.

    The dispatchers wrap the daemon response as
    `{"status": "done", "tool": ..., "result": {...}}`; we unwrap that here.

    WS3b — idempotency-aware retry:
      idempotent=True  → retry exactly once on exception, with a small deterministic
                         jitter (derived from server+tool name, not wallclock).
      idempotent=False → single attempt; never retry, so non-idempotent ops
                         (start_run, finalize_run, venom-class, writes) cannot
                         double-execute.

    When `on_error` is supplied it is invoked on every failed attempt with
    (server, tool, repr(exc), attempt_index) so callers can increment counters
    / emit telemetry. The supervisor wires this to
    `state.error_counters["mcp_failure:<server>"]` so postcheck can surface
    mcp_disconnect:<server> at the configured threshold.
    """
    attempts = (1, 2) if idempotent else (1,)
    for attempt in attempts:
        try:
            envelope = dispatcher.call_mcp(server, tool, args,
                                           squad_id=squad_id)
        except Exception as exc:
            exc_repr = repr(exc)
            if on_error is not None:
                try:
                    on_error(server, tool, exc_repr, attempt)
                except Exception:
                    pass
            if idempotent and attempt == 1:
                # Fix 6: deterministic jitter via SHA-256; no process-salted
                # hash() — same (server, tool) pair always waits the same
                # amount so replays are stable.
                import hashlib as _hashlib
                import time as _time
                _seed = (server + tool).encode()
                _n = int.from_bytes(
                    _hashlib.sha256(_seed).digest()[:4], "big"
                ) % 100
                _jitter = 0.05 + _n / 1000.0
                _time.sleep(_jitter)
            continue
        if not isinstance(envelope, dict):
            return None
        if envelope.get("status") not in ("done", None):
            return None
        inner = envelope.get("result", envelope)
        return inner if isinstance(inner, dict) else None
    return None


def _record_mcp_failure(state: "HydraState | None") -> "Callable[[str, str, str, int], None] | None":
    """Build an on_error callback bound to `state.error_counters`.

    Returns None when state is None (test/CLI paths that don't carry state),
    so _mcp_call_safe falls back to its pre-existing silent behavior.
    """
    if state is None:
        return None

    def _cb(server: str, _tool: str, _exc_repr: str, _attempt: int) -> None:
        key = f"mcp_failure:{server}"
        state.error_counters[key] = state.error_counters.get(key, 0) + 1

    return _cb


def _domain_for(pack: SquadPack, inbound: HydraEnvelope) -> str:
    industries = getattr(inbound.constraints, "industries", []) or []
    if industries:
        return industries[0]
    if pack.industries:
        return pack.industries[0]
    return "general"


def _phase_for(inbound: HydraEnvelope) -> str:
    # CreativeBrief envelopes carry a `phase` field; default to "draft".
    return getattr(inbound, "phase", None) or "draft"


def _render_session_md(title: str, prompt: str, result: dict[str, Any]) -> str:
    summary = (result or {}).get("summary", "")
    return (
        f"# {title}\n\n"
        f"## Prompt\n\n```\n{prompt}\n```\n\n"
        f"## Host-pickup result\n\n"
        f"- status: {(result or {}).get('status', 'unknown')}\n"
        f"- summary: {summary}\n\n"
        f"## Raw\n\n```json\n{result}\n```\n"
    )


def _via_subprocess(
    state: HydraState,
    pack: SquadPack,
    inbound: HydraEnvelope,
    dispatcher: Dispatcher,
) -> SquadResult:
    invoke = pack.invoke or {}
    cmd = invoke.get("argv", [])
    if not cmd:
        return SquadResult(
            envelopes=[], artifacts=[], status="failed",
            rationale="entrypoint=subprocess but invoke.argv missing",
        )
    try:
        result = dispatcher.spawn_subprocess(cmd)
    except Exception as e:
        return SquadResult(
            envelopes=[], artifacts=[], status="failed",
            rationale=f"subprocess failed: {e!r}",
        )
    decision = DecisionRecord(
        workflow_id=inbound.workflow_id,
        parent_id=inbound.id,
        origin_squad=pack.slug,
        target_squad=inbound.origin_squad,
        decision=f"{pack.name} subprocess complete",
        rationale=str(result.get("stdout", ""))[:1000],
        artifacts=[],
    )
    return SquadResult(
        envelopes=[decision],
        artifacts=[{"kind": "subprocess_result", "raw": result}],
        status="done",
    )


def abort_open_pp_runs(
    state: "HydraState",
    dispatcher: Dispatcher,
    *,
    reason: str = "supervisor_surfaced",
) -> list[dict[str, str]]:
    """B7 — release pp-harness locks for any open runs tracked on state.

    Called from `node_postcheck` ONLY when the workflow surfaces. Iterates
    `state.open_pp_runs` and emits `pp_harness.finalize_run(run_id, status=
    "aborted", reason=<reason>)` for each entry. Returns the list of entries
    that were drained so callers can emit a trace event.

    Fail-soft on every MCP call — a daemon-side error during cleanup must
    NOT mask the original surface reason. Entries that fail to finalize
    are left on `state.open_pp_runs` so an operator-driven `force_unlock`
    (see pair-programmer P3) can still salvage the project lock.
    """
    drained: list[dict[str, str]] = []
    remaining: list[dict[str, str]] = []
    for entry in list(state.open_pp_runs):
        run_id = entry.get("run_id")
        project_path = entry.get("project_path")
        if not run_id:
            continue
        try:
            env = dispatcher.call_mcp(
                "pp_harness",
                "finalize_run",
                {
                    "run_id": run_id,
                    "status": "aborted",
                    "reason": reason,
                    "project_path": project_path,
                },
                squad_id="engineering",
            )
        except Exception:  # noqa: BLE001 — leave the entry so force_unlock can salvage
            remaining.append(entry)
            continue

        # WS3a/Fix 4 — only count as drained when the MCP envelope indicates
        # unambiguous success:
        #   - outer status in {done, ok, complete}         (envelope transport OK)
        #   - inner result has no "error" field            (no daemon-side error)
        #   - inner result status (if present) is NOT a failure
        #     i.e. not in {failed, error, surfaced}
        # "outer done + inner {status:failed}" is NOT a successful drain.
        _FAILURE_STATUSES = {"failed", "error", "surfaced"}
        env_status = env.get("status") if isinstance(env, dict) else None
        inner = env.get("result", {}) if isinstance(env, dict) else {}
        if not isinstance(inner, dict):
            inner = {}
        inner_error = inner.get("error")
        inner_status = inner.get("status")
        success = (
            env_status in {"done", "ok", "complete"}
            and not inner_error
            and inner_status not in _FAILURE_STATUSES
        )
        if success:
            drained.append(entry)
        else:
            remaining.append(entry)

    # Replace in place so the LangGraph reducer sees the assignment as a
    # full overwrite — `Annotated[..., _append]` would otherwise concat the
    # original list with whatever we set, producing duplicates.
    state.open_pp_runs = remaining
    return drained
