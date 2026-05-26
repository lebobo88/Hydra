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

from dataclasses import dataclass
from typing import Any, Callable, Protocol
from uuid import uuid4

import subprocess
from pathlib import Path

from .iolaus import post_dispatch, pre_dispatch
from .schemas import (
    DecisionRecord,
    Handoff,
    HITLRequest,
    HydraEnvelope,
    MemoryRef,
)
from .squad_loader import SquadPack
from .state import HydraState, TaskState
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


def execute_squad(
    state: HydraState,
    pack: SquadPack,
    inbound: HydraEnvelope,
    dispatcher: Dispatcher,
    *,
    allow_archived: bool = False,
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
        result = _via_mcp(state, pack, inbound, dispatcher)
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


def _via_mcp(
    state: HydraState,
    pack: SquadPack,
    inbound: HydraEnvelope,
    dispatcher: Dispatcher,
) -> SquadResult:
    """Wire into the pair-programmer harness (engineering squad).

    Invocation contract from `squad.yaml.invoke`:
        mode: pp_run | pp_team | pp_best_of | pp_review
        default_team: feature-team
        forum_for_review: change-advisory-board
    """
    invoke = pack.invoke or {}
    mode = invoke.get("mode", "pp_run")

    project_path = invoke.get("project_path") or str(state.workflow_id and __import__("pathlib").Path.cwd())
    if "${project_root}" in str(project_path):
        project_path = str(__import__("pathlib").Path.cwd())
    args = {
        "request_text": getattr(inbound, "instructions", None)
        or getattr(inbound, "summary", None)
        or getattr(inbound, "objective", "")
        or str(inbound.model_dump()),
        "project_path": project_path,
        "mode": "single" if mode == "pp_run" else ("team" if mode == "pp_team" else "single"),
    }
    if mode == "pp_team":
        args["team"] = invoke.get("default_team")
    if mode == "pp_best_of":
        args["mode"] = "best_of"
        args["n"] = 3
    if mode == "pp_review":
        args["mode"] = "review"
        args["forum"] = invoke.get("forum_for_review")
    # Drop None values — pp schema rejects them.
    args = {k: v for k, v in args.items() if v is not None}
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
            state.open_pp_runs.append({"run_id": run_id, "project_path": str(project_path)})
        except Exception:  # noqa: BLE001 — never crash dispatch on state writes
            pass

    # Worktree handoff: when the inbound envelope ran on its own project_path
    # (i.e. the planner allocated a worktree per the pp_harness_project_lock
    # rule) and pp-harness reported a terminal status, harvest the archived
    # artifacts into the project tree and commit them. Without this, work
    # products end up stranded in <project>/.harness/<run_id>/ and are never
    # visible on a branch — Discovery agent E2's research artifacts hit this
    # exact failure mode in the bootstrap session.
    commit_sha: str | None = None
    if run_id and pp_status in {"done", "complete", "surfaced"} and project_path:
        try:
            commit_sha = harvest_pp_run_artifacts(
                project_path=str(project_path),
                run_id=str(run_id),
                workflow_id=inbound.workflow_id,
            )
        except Exception:  # noqa: BLE001 — never crash dispatch on a git failure
            commit_sha = None

    decision = DecisionRecord(
        workflow_id=inbound.workflow_id,
        parent_id=inbound.id,
        origin_squad=pack.slug,
        target_squad=inbound.origin_squad,
        decision=f"Engineering work dispatched to pair-programmer (run_id={run_id or '?'})",
        rationale=(
            f"mode={mode}; pp dispatch status: {pp_status}; "
            f"commit_sha={commit_sha or 'none'}; inner: {str(inner)[:240]}"
        ),
        artifacts=[MemoryRef(tier="episodic", key=f"pp:run:{run_id or 'unknown'}")] if run_id else [],
    )
    return SquadResult(
        envelopes=[decision],
        artifacts=[{"kind": "pp_run", "ref": run_id, "raw": result, "commit_sha": commit_sha}],
        status="running" if pp_status == "done" and run_id else pp_status,
    )


def harvest_pp_run_artifacts(
    *,
    project_path: str,
    run_id: str,
    workflow_id: str,
) -> str | None:
    """Stage and commit any artifacts pp-harness archived under ``.harness/<run_id>``.

    Returns the commit SHA on success, or ``None`` when there is nothing to
    commit, the project isn't a git repo, or any git invocation fails. The
    helper is deliberately fail-soft — Hydra's dispatch path must never
    crash because the operator chose a non-git project root.

    Why this exists: pp-harness writes archived artifacts into
    ``<project>/.harness/<run_id>/...`` and stops there. When Hydra ran the
    work in a worktree, those bytes are stranded if no one commits them.
    This helper bundles the bytes into a single ``chore(hydra): harvest pp
    run <run_id>`` commit so synthesis + the upstream merge see them.
    """
    root = Path(project_path)
    if not root.is_dir():
        return None
    if not (root / ".git").exists() and not (root.parent / ".git").exists():
        return None
    harness_dir = root / ".harness" / run_id
    if not harness_dir.is_dir():
        return None

    def _git(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )

    add = _git("add", "--", str(harness_dir))
    if add.returncode != 0:
        return None
    status = _git("status", "--porcelain", "--", str(harness_dir))
    if not status.stdout.strip():
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
        squad_id=pack.slug, on_error=_on_mcp_err,
    )
    roster_list = (live_roster or {}).get("agents", []) if isinstance(live_roster, dict) else []
    if roster_list:
        roster = ", ".join(r["name"] for r in roster_list[:8])
    else:
        roster = ", ".join(
            f"{a.slug} ({a.role})" for a in pack.agents if a.authority != "advisory"
        ) or ", ".join(a.slug for a in pack.agents[:4])

    objective = getattr(inbound, "objective", None) or getattr(inbound, "summary", None) or "(see envelope)"
    prompt = (
        "[Hydra→Executive Squad] You are the boardroom facilitator. "
        f"Convene relevant executives ({roster}). "
        f"Topic: {objective}\n\n"
        f"Constraints: {inbound.constraints.model_dump()}\n"
        f"Envelope type: {inbound.type}\n"
        "Follow ExecutiveSuite Board Meeting Protocol. Output a "
        "C_SUITE_DECISION_PACKET with proposed_tasks decomposed for downstream "
        "squads, and a DECISION_RECORD with dissenting opinions preserved verbatim."
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


def _via_claude_skill(
    state: HydraState,
    pack: SquadPack,
    inbound: HydraEnvelope,
    dispatcher: Dispatcher,
) -> SquadResult:
    """RLM-style — invoke a Claude Code skill (e.g. /rlm-team).

    Enrichment: consult the `rlm_creative` MCP for the live skill catalogue, then
    persist the resulting host-pickup envelope under `RLM/output/{phase}/` via
    `rlm.output.write`. The returned `MemoryRef.key` points at the real path.
    """
    invoke = pack.invoke or {}
    cmd = invoke.get("command_hint", "/rlm-team")

    _on_mcp_err = _record_mcp_failure(state)
    catalogue = _mcp_call_safe(
        dispatcher, "rlm_creative", "rlm.command.list", {},
        squad_id=pack.slug, on_error=_on_mcp_err,
    )
    available_cmds = [c["name"] for c in (catalogue or {}).get("commands", [])] if isinstance(catalogue, dict) else []
    try:
        result = dispatcher.invoke_claude_skill(cmd.lstrip("/"), {
            "envelope": inbound.model_dump(mode="json"),
            "available_commands": available_cmds,
        })
    except Exception as e:
        return SquadResult(
            envelopes=[], artifacts=[], status="failed",
            rationale=f"claude-skill {cmd} failed: {e!r}",
        )

    phase = _phase_for(inbound)
    topic = (getattr(inbound, "objective", None)
             or getattr(inbound, "summary", None)
             or cmd.lstrip("/"))[:80]
    write_result = _mcp_call_safe(
        dispatcher, "rlm_creative", "rlm.output.write",
        {"phase": phase, "topic": topic,
         "content": _render_session_md(f"Creative dispatch via {cmd}",
                                       f"command_hint={cmd}\navailable={available_cmds}",
                                       result)},
        squad_id=pack.slug, on_error=_on_mcp_err,
    )
    artifacts_refs: list[MemoryRef] = []
    rel_path = (write_result or {}).get("relative") if isinstance(write_result, dict) else None
    if rel_path:
        artifacts_refs.append(MemoryRef(
            tier="episodic",
            key=f"rlm:output:{rel_path}",
            summary=f"Creative dispatch: {topic}",
        ))
    else:
        artifacts_refs.append(MemoryRef(tier="episodic", key=f"rlm:{uuid4()}"))

    decision = DecisionRecord(
        workflow_id=inbound.workflow_id,
        parent_id=inbound.id,
        origin_squad=pack.slug,
        target_squad=inbound.origin_squad,
        decision=f"Creative work dispatched via {cmd}",
        rationale=str(result.get("summary", ""))[:1000],
        artifacts=artifacts_refs,
    )
    host_pickup = (
        isinstance(result, dict)
        and result.get("status") == "host_pickup_required"
    )
    return SquadResult(
        envelopes=[decision],
        artifacts=[{"kind": "creative_output", "raw": result, "persisted": write_result}],
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
) -> dict[str, Any] | None:
    """Best-effort MCP call. Returns the inner result dict, or None on any failure.

    The dispatchers wrap the daemon response as
    `{"status": "done", "tool": ..., "result": {...}}`; we unwrap that here.

    Retries exactly once on exception (no exponential backoff — we are a
    governance layer, not a resilience layer). When `on_error` is supplied
    it is invoked on every failed attempt with (server, tool, repr(exc),
    attempt_index) so callers can increment counters / emit telemetry. The
    supervisor wires this to `state.error_counters["mcp_failure:<server>"]`
    so postcheck can surface mcp_disconnect:<server> at the configured
    threshold instead of degrading silently with stale data.
    """
    last_exc_repr: str | None = None
    for attempt in (1, 2):
        try:
            envelope = dispatcher.call_mcp(server, tool, args,
                                           squad_id=squad_id)
        except Exception as exc:
            last_exc_repr = repr(exc)
            if on_error is not None:
                try:
                    on_error(server, tool, last_exc_repr, attempt)
                except Exception:
                    pass
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
            dispatcher.call_mcp(
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
            drained.append(entry)
        except Exception:  # noqa: BLE001 — leave the entry so force_unlock can salvage
            remaining.append(entry)
    # Replace in place so the LangGraph reducer sees the assignment as a
    # full overwrite — `Annotated[..., _append]` would otherwise concat the
    # original list with whatever we set, producing duplicates.
    state.open_pp_runs = remaining
    return drained
