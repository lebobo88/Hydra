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
from typing import Any, Protocol
from uuid import uuid4

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
    def call_mcp(self, server: str, tool: str, args: dict[str, Any]) -> dict[str, Any]: ...
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
        result = dispatcher.call_mcp("pp-daemon", "start_run", args)
    except Exception as e:
        return SquadResult(
            envelopes=[], artifacts=[], status="failed",
            rationale=f"pp-daemon unreachable: {e!r}",
        )

    # MCP results come back as {"status":"done","tool":"start_run","result":{...}}
    inner = result.get("result", result) if isinstance(result, dict) else {}
    run_id = (inner or {}).get("run_id") if isinstance(inner, dict) else None
    pp_status = result.get("status", "unknown") if isinstance(result, dict) else "unknown"
    decision = DecisionRecord(
        workflow_id=inbound.workflow_id,
        parent_id=inbound.id,
        origin_squad=pack.slug,
        target_squad=inbound.origin_squad,
        decision=f"Engineering work dispatched to pair-programmer (run_id={run_id or '?'})",
        rationale=f"mode={mode}; pp dispatch status: {pp_status}; inner: {str(inner)[:300]}",
        artifacts=[MemoryRef(tier="episodic", key=f"pp:run:{run_id or 'unknown'}")] if run_id else [],
    )
    return SquadResult(
        envelopes=[decision],
        artifacts=[{"kind": "pp_run", "ref": run_id, "raw": result}],
        status="running" if pp_status == "done" and run_id else pp_status,
    )


def _via_impersonation(
    state: HydraState,
    pack: SquadPack,
    inbound: HydraEnvelope,
    dispatcher: Dispatcher,
) -> SquadResult:
    """ExecutiveSuite pattern — Claude Code impersonates the roster IN PROCESS.

    Enrichment pass: consult the `executive-suite` MCP for the live roster, then
    after the host-pickup envelope, persist the prompt to the pack's `output/`
    tree via `es.output.write`. The returned `MemoryRef.key` points at the real
    on-disk path so downstream consumers can resolve it.
    """
    # Pre-call: pull live roster from the MCP shim (falls back to pack.agents).
    live_roster = _mcp_call_safe(dispatcher, "executive-suite", "es.roster.list", {})
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
        dispatcher, "executive-suite", "es.output.write",
        {"domain": domain, "topic": topic,
         "content": _render_session_md("Boardroom Session", prompt, result)},
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

    Enrichment: consult the `rlm-creative` MCP for the live skill catalogue, then
    persist the resulting host-pickup envelope under `RLM/output/{phase}/` via
    `rlm.output.write`. The returned `MemoryRef.key` points at the real path.
    """
    invoke = pack.invoke or {}
    cmd = invoke.get("command_hint", "/rlm-team")

    catalogue = _mcp_call_safe(dispatcher, "rlm-creative", "rlm.command.list", {})
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
        dispatcher, "rlm-creative", "rlm.output.write",
        {"phase": phase, "topic": topic,
         "content": _render_session_md(f"Creative dispatch via {cmd}",
                                       f"command_hint={cmd}\navailable={available_cmds}",
                                       result)},
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

def _mcp_call_safe(dispatcher: Dispatcher, server: str, tool: str,
                   args: dict[str, Any]) -> dict[str, Any] | None:
    """Best-effort MCP call. Returns the inner result dict, or None on any failure.

    The dispatchers wrap the daemon response as
    `{"status": "done", "tool": ..., "result": {...}}`; we unwrap that here.
    """
    try:
        envelope = dispatcher.call_mcp(server, tool, args)
    except Exception:
        return None
    if not isinstance(envelope, dict):
        return None
    if envelope.get("status") not in ("done", None):
        return None
    inner = envelope.get("result", envelope)
    return inner if isinstance(inner, dict) else None


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
