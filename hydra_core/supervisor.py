"""LangGraph supervisor graph for Hydra.

Phase machine (8 nodes):
    intake → planner → approval(?) → dispatch → judge_per_squad → synthesis → judge_synthesis → postcheck → done

`interrupt_before` is set on approval, synthesis, and judge_synthesis,
so HITL via Claude Code's `/hydra:approve <workflow_id>` resumes the run.

The graph is built lazily — discovers squads from the registry and adds one
squad-node per pack so the graph is self-describing.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable, Optional
from uuid import uuid4

from .eights.attestation import EightsAttestor
from .governance import (
    charge_and_gate,
    enforce_governance,
    GovernanceVerdict,
    record_cost,
    redact_for_squad_boundary,
    should_block_for_budget,
    should_downgrade_model,
)
from .heads import cathedral_name, crown_label_for_squad, heads_in_crown
from .immortal_head import load_constitution
from .judge import dispatch_judge, route_judge, load_policy
from .judge.dispatcher import CritiqueClient, NoOpCritiqueClient
from .judge.reflexion import MAX_RETRY_INDEX, effective_max_retry_index, package_retry
from .judge.schemas import JudgeVerdict
from .router import RoutingDecision, classify_intent, compute_tool_scope
from .telemetry import emit as emit_trace
from .venom import load_cerberus_venoms
from .schemas import (
    CSuiteDecisionPacket,
    DecisionRecord,
    HITLRequest,
    HydraEnvelope,
    ProposedTask,
    validate_envelope,
)
from .squad_loader import SquadPack, discover_squads
from .fleet import dispatch_fleet
from .squad_node import Dispatcher, SquadResult, execute_squad
from .state import HydraState, TaskState


# --- LangGraph is an optional runtime dependency. If missing we still expose
# --- the pure-function graph so unit tests run.
try:
    from langgraph.graph import StateGraph, END  # type: ignore
    from langgraph.checkpoint.sqlite import SqliteSaver  # type: ignore
    _HAS_LANGGRAPH = True
except ImportError:                                                        # pragma: no cover
    StateGraph = None                                                       # type: ignore
    SqliteSaver = None                                                      # type: ignore
    END = "__END__"                                                         # type: ignore
    _HAS_LANGGRAPH = False


def _extract_squad_cost(result: "Any") -> tuple[float, int]:
    """FS-4 — extract cost from a SquadResult.

    _via_mcp stores artifacts as:
        {"kind": "pp_run", "ref": run_id, "raw": <outer_mcp_envelope>, ...}

    The outer MCP envelope returned by MCPStdioDispatcher is:
        {"status": "done", "tool": "start_run", "result": {"cost_usd": ..., ...}}

    So the cost fields live under raw["result"], NOT in raw itself. We unwrap
    raw -> inner = raw.get("result", raw) and read from inner. This covers
    both the real dispatcher (which wraps in "result") and test stubs that
    put cost fields at the top level (raw == inner in that case).

    Fields (in priority order):
      - cost_usd  (float) — preferred
      - cost      (float) — alias used by some pp versions
      - tokens_in + tokens_out (int) — preferred token counts
      - tokens    (int) — aggregate alias

    Defaults to (0.0, 0) when no cost field is found; caller emits
    "cost_unavailable" trace. Always call record_cost even on 0/0 so
    the ledger is monotonically updated on every squad invocation.
    """
    usd: float = 0.0
    tokens: int = 0
    for artifact in getattr(result, "artifacts", []):
        if not isinstance(artifact, dict):
            continue
        if artifact.get("kind") != "pp_run":
            continue
        raw = artifact.get("raw") or {}
        if not isinstance(raw, dict):
            continue
        # Fix 3: unwrap the outer MCP envelope — cost fields live under "result".
        inner = raw.get("result", raw)
        if not isinstance(inner, dict):
            inner = raw
        # Prefer cost_usd; fall back to cost
        if "cost_usd" in inner:
            try:
                usd = max(usd, float(inner["cost_usd"]))
            except (TypeError, ValueError):
                pass
        elif "cost" in inner:
            try:
                usd = max(usd, float(inner["cost"]))
            except (TypeError, ValueError):
                pass
        # Token counts
        tok_raw = 0
        if "tokens_in" in inner or "tokens_out" in inner:
            try:
                tok_raw = int(inner.get("tokens_in") or 0) + int(inner.get("tokens_out") or 0)
            except (TypeError, ValueError):
                tok_raw = 0
        elif "tokens" in inner:
            try:
                tok_raw = int(inner["tokens"])
            except (TypeError, ValueError):
                tok_raw = 0
        tokens = max(tokens, tok_raw)
    return usd, tokens


def build_supervisor(
    *,
    project_root: Path | None = None,
    dispatcher: Dispatcher,
    classify_callable: Optional[Callable] = None,
    checkpoint_path: Path | None = None,
    critique_client: Optional[CritiqueClient] = None,
    profile: Optional[str] = None,
    force_pure_python: bool = False,
):
    """Build a compiled supervisor. Returns the compiled graph if LangGraph is
    installed; otherwise returns a callable that runs the graph step-by-step
    in pure Python (suitable for headless tests)."""
    packs = discover_squads(project_root)
    if not packs:
        raise RuntimeError("No squads discovered. Expected `squads/<name>/squad.yaml`.")

    # The immortal head is loaded once per supervisor build. Every postcheck
    # uses this snapshot; a hash change means the law changed and the
    # supervisor must be rebuilt (typically on next workflow run).
    constitution = load_constitution(project_root)
    judge_policy = load_policy(project_root)
    judge_trace_root = Path(project_root) if project_root else Path.cwd()
    # Best-effort attestation to the eights-daemon. No-ops cleanly when the
    # daemon is not registered at user scope (~/.claude.json mcpServers).
    # B8: durable payloads (attest / envelope_record / hitl_request /
    # evolution.propose) that fail transport are spooled to disk via the
    # shared PendingSpool; `node_intake` calls `replay_pending()` once per
    # workflow start so the spool drains the next time eights is healthy.
    eights = EightsAttestor(dispatcher=dispatcher)

    # Cerberus' venom registry is hydrated from cerberus.yaml at boot.
    # Capabilities not pre-registered raise VenomUnregistered when invoked
    # through `require_cerberus_pass`, so a missing file means *no* venom
    # is callable — the safe default per the manifesto.
    load_cerberus_venoms(project_root)

    # Inject squad packs into dispatcher for RBAC enforcement.
    if hasattr(dispatcher, "set_squad_packs"):
        dispatcher.set_squad_packs(packs)

    # Build the toolshed (meta-tool facade for large MCP servers).
    from .toolshed import build_default_shed
    toolshed = build_default_shed(dispatcher=dispatcher)

    # Node-scoped context trimming.
    from .node_context import build_node_context

    # Tool usage analytics (Phase D1).
    from .tool_analytics import ToolUsageTracker, analytics_path
    tool_tracker = ToolUsageTracker(packs=packs)
    if hasattr(dispatcher, "_tool_tracker"):
        dispatcher._tool_tracker = tool_tracker

    # ----- boundary enforcement helpers -----

    def _validate_and_redact_envelope(
        env_dict: dict,
        *,
        direction: str,
        squad_id: str | None = None,
    ) -> dict:
        """Validate envelope schema and redact text at a squad boundary.

        Called at dispatch (outbound to squad) and synthesis (inbound from squad).
        Invalid envelopes raise ValueError — caller decides whether to fail the
        task or surface to HITL.
        """
        try:
            validate_envelope(env_dict)
        except (ValueError, Exception) as exc:
            emit_trace(judge_trace_root, "boundary", "envelope_validation_failed", {
                "envelope_id": env_dict.get("id"),
                "envelope_type": env_dict.get("type"),
                "direction": direction,
                "squad_id": squad_id,
                "error": str(exc),
            })
            raise

        redacted = dict(env_dict)
        for text_field in ("objective", "summary", "instructions", "decision",
                           "rationale", "risk_assessment", "rollout_plan"):
            if text_field in redacted and isinstance(redacted[text_field], str):
                redacted[text_field] = redact_for_squad_boundary(redacted[text_field])

        emit_trace(judge_trace_root, env_dict.get("workflow_id", "boundary"),
                   "squad_boundary_crossing", {
                       "envelope_id": env_dict.get("id"),
                       "envelope_type": env_dict.get("type"),
                       "direction": direction,
                       "squad_id": squad_id,
                   })
        return redacted

    # ----- node implementations -----

    def _emit_node_context(state: HydraState, node_name: str) -> None:
        """Emit node context to the trace for debugging/audit."""
        ctx = build_node_context(
            node_name,
            selected_squads=getattr(state, "selected_squads", []),
            packs=packs,
            toolshed=toolshed,
        )
        emit_trace(judge_trace_root, state.workflow_id, "node_context", {
            "node": node_name,
            "tool_categories": ctx.tool_categories,
            "relevant_squads": ctx.relevant_squads,
            "instructions_len": len(ctx.instructions),
        })

    def node_intake(state: HydraState) -> dict:
        state.phase = "intake"
        _emit_node_context(state, "intake")
        state.bump_iteration()
        # B8: thread the workflow_id onto the attestor so any newly-spooled
        # payloads from this turn carry it, then drain any prior-workflow
        # spool entries while we have the dispatcher in scope. Replay is
        # best-effort — if the daemon is still down, the spool stays and
        # the next workflow will retry.
        eights.workflow_id = str(state.workflow_id)
        try:
            replay_summary = eights.replay_pending()
        except Exception:  # noqa: BLE001 — never crash intake on replay
            replay_summary = {"sent": 0, "failed": 0, "skipped": 0}
        if any(replay_summary.values()):
            emit_trace(
                judge_trace_root,
                state.workflow_id,
                "supervisor.eights_replay",
                replay_summary,
            )

        # --repo <id> extraction: parse an optional --repo token from the goal
        # text and set state.target_repo_id. An unknown id is a user error —
        # surface immediately via HITL rather than silently ignoring it. On
        # success, target_repo_id is set (or left unchanged if already set by
        # the caller, e.g. a test that pre-seeds HydraState). root_goal is
        # NOT mutated so the router and planner see the original text.
        from hydra_core.repo_registry import parse_repo_arg
        try:
            _repo_id, _cleaned = parse_repo_arg(state.root_goal)
        except ValueError as _repo_err:
            # Unknown --repo id: surface immediately so the operator sees the
            # problem rather than the dispatch silently targeting the wrong repo.
            state.phase = "surfaced"
            _hitl_payload: dict[str, Any] = {
                "workflow_id": str(state.workflow_id),
                "reason": "high_risk",
                "gate_node": "intake",
                "summary": f"--repo argument rejected: {_repo_err}",
                "options": ["abort"],
                "default_option": "abort",
            }
            emit_trace(
                judge_trace_root,
                state.workflow_id,
                "supervisor.bad_repo_arg",
                {"error": str(_repo_err)},
            )
            return {
                "phase": "surfaced",
                "pending_hitl": _hitl_payload,
                "last_event": f"bad --repo arg: {_repo_err}",
            }
        # Only set target_repo_id from the goal text if the caller has not
        # already injected one (e.g. via direct HydraState construction in
        # tests or a future structured API path).
        if _repo_id is not None and not state.target_repo_id:
            state.target_repo_id = _repo_id

        # Route the goal text. A pre-seeded non-empty `selected_squads`
        # (CLI `hydra run --squad ...` / operator force-select) wins over
        # the intent router: validate slugs against discovered packs and
        # skip classification. Unknown slugs are dropped with a trace event;
        # if nothing valid survives, fall back to the router.
        forced = [s for s in state.selected_squads if s in packs]
        unknown = [s for s in state.selected_squads if s not in packs]
        if unknown:
            emit_trace(
                judge_trace_root,
                state.workflow_id,
                "supervisor.force_select_unknown_squads",
                {"unknown": unknown, "known": sorted(packs)},
            )
        if forced:
            decision = RoutingDecision(
                squads=forced,
                confidence=1.0,
                rationale=f"operator force-select: {forced}",
            )
        else:
            decision = classify_intent(
                state.root_goal,
                packs,
                industries=tuple(getattr(state.budget, "industries", []) or []),
                classify_callable=classify_callable,
            )
        state.selected_squads = decision.squads

        tool_scope = compute_tool_scope(
            state.root_goal, decision.squads, packs, toolshed=toolshed,
        )
        emit_trace(judge_trace_root, state.workflow_id, "tool_scope", {
            "relevant_tools": list(tool_scope.relevant_tools)[:20],
            "relevant_categories": list(tool_scope.relevant_categories),
            "intent_keywords": list(tool_scope.intent_keywords),
            "tool_count": tool_scope.tool_count,
        })

        state.last_event = f"intake: chose {decision.squads} ({decision.rationale})"

        # Eights attestation: stamp the constitution hash into state and
        # record the receipt for audit. The local immortal_head check is
        # still the authoritative refusal gate; this is the shared-ledger
        # attestation. Falls through if the daemon is offline.
        update: dict[str, Any] = {
            "selected_squads": decision.squads,
            "phase": "planning",
            "last_event": state.last_event,
            "iteration_count": state.iteration_count,
            "constitution_hash": constitution.sha256,
        }
        receipt = eights.constitution_attest(constitution)
        if isinstance(receipt, dict):
            if isinstance(receipt.get("version"), str):
                update["constitution_version"] = receipt["version"]
            if isinstance(receipt.get("receipt"), str):
                update["constitution_receipt"] = receipt["receipt"]
        eights.ceiling_tick(workflow_id=str(state.workflow_id), node="intake")
        return update

    def node_planner(state: HydraState) -> dict:
        state.phase = "planning"

        # -----------------------------------------------------------------------
        # TASK-LIST LIFECYCLE (holistic — read this before editing):
        #
        # HydraState.tasks uses an APPEND reducer (_append in state.py line 15).
        # That means every dict key "tasks" in a node's return value is APPENDED
        # onto the existing state list by LangGraph — it is NOT a replace.
        #
        # Therefore node_planner must return ONLY net-new synthesised tasks in
        # its output dict (the fresh defaults added for squads with no pre-seeded
        # tasks).  Pre-seeded tasks are already in state.tasks and must NOT be
        # re-emitted; doing so doubles them.
        #
        # The gate evaluation (AC gate + high_risk) uses the FULL logical set:
        #   full_tasks = existing_tasks (already in state) + synthesised_tasks (new)
        # but only synthesised_tasks goes into the return dict["tasks"].
        #
        # Invariants enforced here:
        #   (i)  One source of truth: full_tasks = existing + synthesised (no dups).
        #   (ii) One shared per-task risk helper (_task_is_high_risk) drives BOTH
        #        requires_human_approval and the AC qualifying check.
        #   (iii) No task is duplicated: synthesised set covers only squads not
        #         already in pre_seeded_squads, deduped by task_id before return.
        #   (iv)  No task dropped: every pre-seeded task is in full_tasks; every
        #         selected squad without a pre-seeded task gets exactly one default.
        #   (v)   Gates evaluate full_tasks; only net-new go into return["tasks"].
        # -----------------------------------------------------------------------

        existing_tasks: list[TaskState] = list(state.tasks or [])
        # Dedup existing by task_id (guard against earlier double-commit).
        seen_ids: set[str] = set()
        deduped_existing: list[TaskState] = []
        for t in existing_tasks:
            tid = str(t.task_id)
            if tid not in seen_ids:
                seen_ids.add(tid)
                deduped_existing.append(t)
        existing_tasks = deduped_existing

        pre_seeded_squads: set[str] = {t.owner_squad for t in existing_tasks}

        # synthesised_tasks: ONLY the fresh defaults added by this planner run.
        # These are the tasks returned to the graph (append reducer).
        synthesised_tasks: list[TaskState] = []
        new_envelopes: list[dict] = []

        if "executive" in state.selected_squads:
            packet = CSuiteDecisionPacket(
                workflow_id=state.workflow_id,
                origin_squad="hydra",
                target_squad="executive",
                origin="BOARDROOM",
                objective=state.root_goal,
                proposed_tasks=[
                    ProposedTask(target_squad=s, description=state.root_goal)
                    for s in state.selected_squads
                ],
            )
            new_envelopes.append(packet.model_dump(mode="json"))

            # If NO executive task was pre-seeded, synthesise the decompose task.
            # Pre-seeded executive tasks are already in existing_tasks — do NOT
            # re-emit them (append reducer would duplicate).
            if "executive" not in pre_seeded_squads:
                synthesised_tasks.append(TaskState(
                    owner_squad="executive",
                    description="Decompose goal + budget split",
                    envelope_id=packet.id,
                ))

            # Fresh defaults for selected non-executive squads with no pre-seeded tasks.
            for s in state.selected_squads:
                if s == "executive" or s in pre_seeded_squads:
                    continue
                synthesised_tasks.append(TaskState(
                    owner_squad=s,
                    description=state.root_goal,
                ))
        else:
            # Non-executive branch: fresh defaults only for squads not already seeded.
            for s in state.selected_squads:
                if s in pre_seeded_squads:
                    continue
                synthesised_tasks.append(TaskState(
                    owner_squad=s,
                    description=state.root_goal,
                ))

        # Full logical task set for gate evaluation.
        # Pre-seeded are authoritative; synthesised are appended after.
        full_tasks: list[TaskState] = existing_tasks + synthesised_tasks

        # -----------------------------------------------------------------------
        # SHARED PER-TASK RISK HELPER
        # Used by BOTH (a) requires_human_approval / high_risk HITL and
        # (b) AC qualifying check.  Evaluates the task's OWN squad against packs
        # so tasks outside selected_squads are covered (e.g. a pre-seeded security
        # task whose squad was not in selected_squads still triggers HITL if its
        # pack has hitl_required=True).
        # Non-blanket: P2/P3 tasks whose squad has no hitl_required gate → False.
        # -----------------------------------------------------------------------
        def _task_has_valid_criteria(t: TaskState) -> bool:
            crit = t.acceptance_criteria
            return bool(crit and any(c.strip() for c in crit))

        def _task_is_high_risk(t: TaskState) -> bool:
            """True when the task is major (P0/P1) OR its squad has an hitl_required gate."""
            if t.priority in {"P0", "P1"}:
                return True
            squad_pack = packs.get(t.owner_squad)
            return (
                squad_pack is not None
                and squad_pack.entrypoint != "stub"
                and any(g.hitl_required for g in squad_pack.gates)
            )

        # (a) requires_human_approval: any high-risk task anywhere in full_tasks.
        high_risk = any(_task_is_high_risk(t) for t in full_tasks)
        state.requires_human_approval = high_risk or state.is_over_budget()

        # squad_gate_high_risk: True when any task's squad has an explicit
        # hitl_required gate (independent of task priority).  This is the
        # "frozen contract" discriminator: reason="high_risk" is the canonical
        # value keyed by the mesh console, and it must be preserved whenever a
        # squad-level HITL gate would have fired regardless of AC.
        # P0/P1 priority alone does NOT set this flag — those tasks qualify the
        # AC gate as "high-risk qualifying" but do not set the frozen-contract
        # squad-gate reason.
        def _squad_has_hitl_gate(t: "TaskState") -> bool:
            sp = packs.get(t.owner_squad)
            return (
                sp is not None
                and sp.entrypoint != "stub"
                and any(g.hitl_required for g in sp.gates)
            )

        squad_gate_high_risk = any(_squad_has_hitl_gate(t) for t in full_tasks)

        # (b) AC gate: any HIGH-RISK task (qualifying) is MISSING valid criteria.
        needs_ac_hitl = any(
            _task_is_high_risk(t) and not _task_has_valid_criteria(t)
            for t in full_tasks
        )

        # Merge: AC gate folds into a single pause.
        if needs_ac_hitl:
            state.requires_human_approval = True

        out: dict = {
            # APPEND REDUCER: emit only synthesised_tasks (not existing_tasks).
            # Pre-seeded tasks are already in state.tasks; re-emitting them
            # would duplicate every task_id.
            "tasks": synthesised_tasks,
            "envelopes": new_envelopes,
            "requires_human_approval": state.requires_human_approval,
            "phase": "approval" if state.requires_human_approval else "dispatch",
        }

        if state.requires_human_approval:
            # C2 (mesh-console-unification): the graph interrupts BEFORE the
            # `approval` node, so the HITL request must be built and filed
            # HERE (pre-interrupt) for the paused state to carry pending_hitl
            # and for TheEights' hitl_queue to see the gate. Previously the
            # request was only rendered inside node_approval — i.e. AFTER the
            # operator had already resumed — so the approval gate was
            # invisible to both /hydra:status state and the mesh HITL Center.
            #
            # REASON PRECEDENCE (WS9 regression fix):
            # The canonical `reason` field is a FROZEN CONTRACT keyed by
            # mesh-console consumers and test_mesh_console_surface.py:80.
            # Rule: `reason="high_risk"` whenever a squad-level hitl_required
            # gate is present (squad_gate_high_risk=True) — this is the frozen
            # contract the mesh console and approval gate consumers key off of.
            # `reason="acceptance_criteria"` is used ONLY when the gate fires
            # SOLELY because a major (P0/P1) task on a squad WITHOUT a
            # hitl_required gate is missing criteria — i.e. squad_gate_high_risk
            # is False (no squad gate independently demands the pause).
            # Missing-criteria information is always surfaced in the summary
            # regardless of which reason wins, so the operator is never blind
            # to missing AC even when `reason="high_risk"`.
            if needs_ac_hitl and not squad_gate_high_risk:
                # Pure AC gate: a major (P0/P1) task on a squad without a
                # hitl_required gate is missing acceptance criteria.
                # No frozen-contract reason to preserve — use the WS9 reason.
                hitl_summary = (
                    f"Major task dispatching to {state.selected_squads} "
                    f"for goal: {state.root_goal!r} — no structured acceptance criteria "
                    f"provided. Please confirm or supply acceptance criteria before "
                    f"dispatch proceeds."
                )
                hitl = HITLRequest(
                    workflow_id=state.workflow_id,
                    origin_squad="hydra",
                    target_squad="human",
                    reason="acceptance_criteria",
                    summary=hitl_summary,
                    options=["approve_with_criteria", "reject", "modify-budget"],
                    default_option="reject",
                )
            else:
                # Squad-level hitl_required gate is present (squad_gate_high_risk).
                # reason="high_risk" is the canonical frozen-contract value.
                # When the AC gate also contributed (needs_ac_hitl=True), append
                # missing-criteria info to the summary so the operator is still
                # informed even though the reason label stays "high_risk".
                base_summary = (
                    f"Approve dispatch of: {state.selected_squads} "
                    f"for goal: {state.root_goal}"
                )
                if needs_ac_hitl:
                    # Identify which tasks are qualifying and missing criteria.
                    missing_squads = [
                        t.owner_squad for t in full_tasks
                        if _task_is_high_risk(t) and not _task_has_valid_criteria(t)
                    ]
                    base_summary += (
                        f"; acceptance criteria missing for: {missing_squads}"
                    )
                hitl = HITLRequest(
                    workflow_id=state.workflow_id,
                    origin_squad="hydra",
                    target_squad="human",
                    reason="high_risk",
                    summary=base_summary,
                    options=["approve", "reject", "modify-budget"],
                    default_option="reject",
                )
            hitl_dict = hitl.model_dump(mode="json")
            hitl_dict["gate_node"] = "approval"  # C2: dedupe key half (workflow_id+gate_node)
            # Required-with-spool: transport failures land in
            # ~/.hydra/eights-pending/ and replay at the next node_intake.
            eights.hitl_request(hitl_dict, gate_node="approval")
            out["pending_hitl"] = hitl_dict

        return out

    def node_approval(state: HydraState) -> dict:
        # Runs only AFTER the operator resumes past the interrupt — the gate
        # itself is rendered+filed by node_planner (see above). This node is
        # post-resume bookkeeping: clear the pending gate (idempotent with
        # `hydra resume`, which also clears it before re-invoking).
        #
        # Clobber guard (Codex verdict_ZCsp2WBc3e item 2): only clear a gate
        # this node owns. If pending_hitl carries a DIFFERENT gate_node (a
        # foreign gate landed via replay/update_state between the operator's
        # clear and this continuation), leave it untouched so the newer gate
        # is never silently discarded.
        cur = state.pending_hitl
        if isinstance(cur, dict) and cur.get("gate_node") not in (None, "approval"):
            return {"phase": "approval"}
        return {
            "pending_hitl": None,
            "phase": "approval",
        }

    def _judge_envelope(
        state: HydraState,
        env: dict,
        *,
        is_post_synthesis: bool = False,
    ) -> list[dict]:
        """Apply route_judge + dispatch_judge to one envelope.

        Returns the list of JudgeVerdict dicts produced (empty if route=skip).
        Errors are caught and surfaced as a synthetic 'fail' verdict so the
        supervisor never silently swallows a judge failure.

        Policy gating: squads not in `policy.enabled_squads` fall back to the
        NoOp client (skeleton verdict) — useful for Phase-2 staged rollout.
        """
        origin = env.get("origin_squad")
        route = route_judge(
            env,
            origin_squad=origin,
            profile=profile,
            is_post_synthesis=is_post_synthesis,
        )
        if route.tier == "skip" or not route.rubric_ids:
            emit_trace(judge_trace_root, state.workflow_id, "judge.skipped", {
                "envelope_id": env.get("id"),
                "envelope_type": env.get("type"),
                "rationale": route.rationale,
            })
            return []

        # Squad gating: use NoOp for squads not yet rolled out.
        squad_enabled = judge_policy.squad_enabled(origin if not is_post_synthesis else None)
        use_client = critique_client if squad_enabled else NoOpCritiqueClient()

        out: list[dict] = []
        judge_vendor = (route.preferred_judge_vendors or ["gemini"])[0]
        for rubric_id in route.rubric_ids:
            emit_trace(judge_trace_root, state.workflow_id, "judge.invoked", {
                "envelope_id": env.get("id"),
                "envelope_type": env.get("type"),
                "rubric_id": rubric_id,
                "judge_vendor": judge_vendor,
                "tier": route.tier,
                "squad_enabled": squad_enabled,
                "is_post_synthesis": is_post_synthesis,
            })
            try:
                verdict = dispatch_judge(
                    envelope=env,
                    rubric_id=rubric_id,
                    judge_vendor=judge_vendor,
                    workflow_id=state.workflow_id,
                    generator_vendor=origin or "unknown",
                    client=use_client,
                )
                out.append(verdict.model_dump(mode="json"))
                emit_trace(judge_trace_root, state.workflow_id, "judge.verdict", {
                    "envelope_id": env.get("id"),
                    "rubric_id": rubric_id,
                    "outcome": verdict.outcome,
                    "judge_vendor": verdict.judge_vendor,
                    "score_json": verdict.score_json,
                })
            except Exception as e:
                from .judge.schemas import JudgeVerdict
                from uuid import UUID, uuid4
                target_id = env.get("id")
                target_id = UUID(target_id) if isinstance(target_id, str) else (target_id or uuid4())
                synth = JudgeVerdict(
                    workflow_id=state.workflow_id,
                    origin_squad="hydra-judge",
                    target_squad=origin,
                    target_envelope_id=target_id,
                    outcome="fail",
                    rubric_id=rubric_id,
                    judge_vendor=judge_vendor,
                    generator_vendor=origin or "unknown",
                    critique_md=f"judge dispatch error: {e}",
                    score_json={"_error": True},
                )
                out.append(synth.model_dump(mode="json"))
                emit_trace(judge_trace_root, state.workflow_id, "judge.verdict", {
                    "envelope_id": env.get("id"),
                    "rubric_id": rubric_id,
                    "outcome": "fail",
                    "error": str(e),
                })
        return out

    def _dispatch_best_of_n(
        state: HydraState,
        pack,
        task,
        artifacts: list[dict],
        verdicts_out: list[dict],
    ) -> list[dict]:
        """Produce N candidate outputs from a squad and Borda-rank them.

        Only invoked when pack.best_of_n >= 2 AND the squad is in
        policy.enabled_squads (real judging gated by Phase-2 allowlist —
        otherwise N>=2 would burn judge calls for no gain).

        Returns the WINNING envelopes (loser envelopes archived in artifacts).
        Falls back to single-shot dispatch if anything goes wrong.
        """
        from .judge.best_of_n import judge_and_rank

        n = pack.best_of_n
        squad_enabled = judge_policy.squad_enabled(pack.slug)
        client_for_bon = critique_client if squad_enabled else NoOpCritiqueClient()

        candidates: list[dict] = []
        per_candidate_winners: list[dict] = []
        for i in range(n):
            # WS9 Fix 2: thread task.model_tier onto best-of-N packets.
            payload = CSuiteDecisionPacket(
                workflow_id=state.workflow_id,
                origin_squad="hydra",
                target_squad=pack.slug,
                origin="BOARDROOM",
                # Vary the objective slightly per candidate so a deterministic
                # squad still produces N traceable artifacts. Real diversity
                # comes from the underlying responder's temperature/seed.
                objective=f"{task.description}\n\n[bon-candidate {i+1}/{n}]",
                target_repo_id=state.target_repo_id,
                model_tier=getattr(task, "model_tier", None),
            )
            try:
                result = execute_squad(state, pack, payload, dispatcher)
            except Exception as e:
                emit_trace(judge_trace_root, state.workflow_id, "judge.bon_candidate_error", {
                    "candidate": i, "error": str(e),
                })
                continue
            # Fix 2b: charge + gate after every best-of-N candidate.
            _cost_usd, _cost_tok = _extract_squad_cost(result)
            _block, _downgrade = charge_and_gate(state, _cost_usd, _cost_tok)
            if _cost_usd == 0.0 and _cost_tok == 0:
                emit_trace(judge_trace_root, state.workflow_id, "budget.cost_unavailable", {
                    "site": "best_of_n", "candidate": i, "squad": pack.slug,
                })
            if _downgrade and not state.budget_downgrade_active:
                state.budget_downgrade_active = True
                emit_trace(judge_trace_root, state.workflow_id, "budget.downgrade_tripwire", {
                    "site": "best_of_n", "candidate": i, "squad": pack.slug,
                    "percent_consumed": state.budget.percent_consumed,
                })
            if _block:
                # Budget exhausted mid best-of-N — surface to HITL immediately.
                # Fix 2a: must set state.phase + pending_hitl so that
                # node_dispatch (the caller) detects the surfaced condition and
                # returns the surface payload rather than continuing to dispatch
                # more tasks. A bare `break` was silently swallowed — the state
                # machine never halted and further squads could still be dispatched.
                state.budget_downgrade_active = True
                _bon_hitl: dict[str, Any] = {
                    "workflow_id": str(state.workflow_id),
                    "reason": "over_budget",
                    "gate_node": "dispatch",
                    "summary": (
                        f"Budget exhausted mid best-of-N candidate {i+1}/{n} "
                        f"for squad {pack.slug}: "
                        f"${state.budget.spent_usd:.4f} of "
                        f"${state.budget.budget_usd:.2f} spent."
                    ),
                    "options": ["approve_override", "abort"],
                    "default_option": "abort",
                    "spent_usd": state.budget.spent_usd,
                    "budget_usd": state.budget.budget_usd,
                }
                state.phase = "surfaced"
                state.pending_hitl = _bon_hitl
                eights.hitl_request(_bon_hitl, gate_node="dispatch")
                emit_trace(judge_trace_root, state.workflow_id, "budget.over_budget_surface", {
                    "site": "best_of_n", "candidate": i, "squad": pack.slug,
                    "spent_usd": state.budget.spent_usd,
                    "budget_usd": state.budget.budget_usd,
                })
                artifacts.extend(result.artifacts)
                break  # node_dispatch will check state.phase == "surfaced"
            artifacts.extend(result.artifacts)
            # Use the first envelope as the candidate the judge will score.
            if not result.envelopes:
                continue
            primary = result.envelopes[0].model_dump(mode="json")
            primary["_bon_candidate_index"] = i
            if result.host_pickup_pending:
                primary["_host_pickup_pending"] = True
            # Fix 2 (best-of-N _task_id): stamp originating task_id so
            # _reflexion_retry can resolve the exact task and source its
            # model_tier rather than falling back to first-same-squad.
            primary["_task_id"] = str(task.task_id)
            candidates.append(primary)
            per_candidate_winners.append(primary)

        # If every candidate is host-pickup-pending, skip ranking entirely —
        # there's nothing substantive to score yet. Return them all so the
        # downstream host fulfils each one.
        if candidates and all(c.get("_host_pickup_pending") for c in candidates):
            emit_trace(judge_trace_root, state.workflow_id, "judge.bon_all_pending", {
                "squad": pack.slug, "n": len(candidates),
            })
            return candidates

        if len(candidates) < 2:
            # Not enough candidates to rank — fall back to single output.
            emit_trace(judge_trace_root, state.workflow_id, "judge.bon_fallback", {
                "produced": len(candidates),
            })
            return candidates

        # Pick rubrics by envelope type. Cross-domain + a squad-fit one.
        bon_rubrics = ["constitution-alignment@1"]
        if pack.slug == "executive":
            bon_rubrics.append("board-decision-quality@1")
        elif pack.slug == "garland":
            bon_rubrics.extend(["brand-consistency@1", "audience-fit@1"])

        try:
            outcome = judge_and_rank(
                candidates,
                rubric_ids=bon_rubrics,
                workflow_id=state.workflow_id,
                judge_vendor="gemini",
                client=client_for_bon,
                generator_vendor=pack.slug,
            )
        except Exception as e:
            emit_trace(judge_trace_root, state.workflow_id, "judge.bon_rank_error", {
                "error": str(e),
            })
            return candidates

        emit_trace(judge_trace_root, state.workflow_id, "judge.borda", {
            "winner_id": outcome.winner_id,
            "leaderboard": outcome.leaderboard,
            "squad": pack.slug,
            "n": len(candidates),
        })
        # Persist the internal verdicts to state so audit + HITL escalation can
        # see them (best_of_n verdicts otherwise live only inside this branch).
        verdicts_out.extend(v.model_dump(mode="json") for v in outcome.verdicts)
        # Archive the loser envelopes to artifacts (episodic-memory ready).
        if outcome.losers:
            artifacts.append({
                "kind": "bon_losers",
                "squad": pack.slug,
                "loser_envelope_ids": [str(e.get("id")) for e in outcome.losers],
                "rationale": "Borda-ranked below winner; preserved per TheEights Kan-cell convention.",
            })
        # Tag the winner so the judge node knows it was already evaluated.
        winner = dict(outcome.winner_envelope)
        winner["_bon_winner"] = True
        return [winner]

    def node_dispatch(state: HydraState) -> dict:
        _emit_node_context(state, "dispatch")
        # Preemptive envelope-ceiling guard. The Claude Code sub-agent that
        # hosts this supervisor is one-shot-per-turn; if the planner emitted
        # too many envelopes for a single dispatch round, surface to HITL
        # before the dispatch loop burns the remaining context. The operator
        # then re-spawns the supervisor per phase_batch (planner contract) or
        # uses direct parallel Agent() fanout.
        if state.is_over_envelope_ceiling():
            state.phase = "surfaced"
            state.pending_hitl = {
                "workflow_id": str(state.workflow_id),
                "reason": "envelope_ceiling",
                "gate_node": "dispatch",  # C2 dedupe key half
                "summary": (
                    f"Envelope ceiling hit: {len(state.envelopes)} envelopes "
                    f"(ceiling {state.envelope_ceiling})"
                ),
                "options": ["split_phase", "abort"],
                "remediation": (
                    "Split phase across multiple supervisor invocations "
                    "(planner phase_batch_index) or use parallel Agent() dispatch."
                ),
                "envelope_count": len(state.envelopes),
                "envelope_ceiling": state.envelope_ceiling,
            }
            # C2: file the ceiling gate into TheEights' hitl_queue too
            eights.hitl_request(state.pending_hitl, gate_node="dispatch")
            emit_trace(
                judge_trace_root,
                state.workflow_id,
                "supervisor.envelope_ceiling_surface",
                {"count": len(state.envelopes), "ceiling": state.envelope_ceiling},
            )
            return {}
        # Fix 2c — pre-dispatch budget check. If we are already at or over
        # budget before dispatching any task this turn, surface immediately
        # rather than dispatching a squad that would push us further over.
        if should_block_for_budget(state):
            state.budget_downgrade_active = True
            _pre_hitl: dict[str, Any] = {
                "workflow_id": str(state.workflow_id),
                "reason": "over_budget",
                "gate_node": "dispatch",
                "summary": (
                    f"Pre-dispatch budget check: already at/over budget "
                    f"(${state.budget.spent_usd:.4f} of ${state.budget.budget_usd:.2f})."
                ),
                "options": ["approve_override", "abort"],
                "default_option": "abort",
                "spent_usd": state.budget.spent_usd,
                "budget_usd": state.budget.budget_usd,
            }
            eights.hitl_request(_pre_hitl, gate_node="dispatch")
            emit_trace(judge_trace_root, state.workflow_id, "budget.pre_dispatch_block", {
                "spent_usd": state.budget.spent_usd,
                "budget_usd": state.budget.budget_usd,
            })
            return {
                "phase": "surfaced",
                "pending_hitl": _pre_hitl,
                "budget_downgrade_active": True,
            }

        state.phase = "executing"
        # For each pending task, drive the squad. The dispatcher is responsible
        # for actually invoking MCP / skills / subprocesses.
        new_decisions: list[dict] = []
        artifacts: list[dict] = []
        bon_verdicts: list[dict] = []

        # Belt-and-suspenders dedup at the dispatch point: state.tasks uses an
        # append reducer (_append in state.py), so re-plan passes or replay can
        # accumulate duplicate task_ids. Dispatching the same task_id twice would
        # double-charge budget and produce redundant envelopes. Keep first
        # occurrence per task_id (preserves order and all identity fields).
        _seen_dispatch_ids: set[str] = set()
        _dispatch_tasks: list[TaskState] = []
        for _t in state.tasks:
            _tid = str(_t.task_id)
            if _tid not in _seen_dispatch_ids:
                _seen_dispatch_ids.add(_tid)
                _dispatch_tasks.append(_t)

        # WS8 SLICE 1 — shared payload factory used by BOTH the sequential loop
        # and the fleet path so construction is never duplicated.
        # WS9 Fix 2: thread task.model_tier onto the dispatch packet so
        # _via_mcp can route to the correct pp team/profile.
        def _build_payload(task: TaskState) -> CSuiteDecisionPacket:
            # WS8 SLICE 1: per-task repo wins over workflow-level target_repo_id.
            # task.target_repo_id is set by the planner (or /hydra:run --repo) to
            # enable distinct-repo fleet dispatch.  Falls back to the workflow root.
            pack_for_task = packs.get(task.owner_squad)
            _task_repo_id = getattr(task, "target_repo_id", None)
            return CSuiteDecisionPacket(
                workflow_id=state.workflow_id,
                origin_squad="hydra",
                target_squad=pack_for_task.slug if pack_for_task else task.owner_squad,
                origin="BOARDROOM",
                objective=task.description,
                target_repo_id=_task_repo_id if _task_repo_id is not None else state.target_repo_id,
                model_tier=getattr(task, "model_tier", None),
            )

        # WS8 Fix 5: build EVERY pending task's payload EXACTLY ONCE, up-front,
        # before the fleet/sequential branch decision. Both paths consume
        # _all_task_payloads; _build_payload is never called a second time.
        # A build failure for a task produces None in the map; that task falls
        # to the sequential path and fails there (pack missing or exception).
        _all_task_payloads: dict[int, Any] = {}
        for _pt in _dispatch_tasks:
            if _pt.status == "pending":
                try:
                    _all_task_payloads[id(_pt)] = _build_payload(_pt)
                except Exception:
                    pass  # None entry; sequential loop will mark task failed

        # WS8 SLICE 1: fleet gating predicate.
        # Eligibility: fleet_parallel flag AND >=2 pending tasks that are:
        #   (a) NOT best-of-N, (b) pack.entrypoint == "mcp" (engineering only),
        #   (c) have DISTINCT non-None task.target_repo_id values.
        # Non-mcp tasks (impersonation/claude-skill/stub) are never fleet-eligible
        # because they race on state.error_counters and ignore target_repo_id
        # (fleet.py engineering-only eligibility). They remain on the sequential path.
        _fleet_candidate_tasks: list[TaskState] = [
            t for t in _dispatch_tasks
            if t.status == "pending"
            and packs.get(t.owner_squad) is not None
            and not (packs[t.owner_squad].best_of_n and packs[t.owner_squad].best_of_n >= 2)
            and packs[t.owner_squad].entrypoint == "mcp"  # Fix 3+4: mcp-only
            and id(t) in _all_task_payloads  # skip tasks whose payload build failed
        ]
        # Alias: fleet candidates share the already-built payloads.
        _fleet_candidate_payloads: dict[int, Any] = {
            id(t): _all_task_payloads[id(t)] for t in _fleet_candidate_tasks
        }

        _distinct_non_none_repo_ids: set[str] = {
            str(_fleet_candidate_payloads[id(t)].target_repo_id)
            for t in _fleet_candidate_tasks
            if id(t) in _fleet_candidate_payloads
            and _fleet_candidate_payloads[id(t)].target_repo_id is not None
        }

        _use_fleet = (
            state.fleet_parallel
            and len(_fleet_candidate_tasks) >= 2
            and len(_distinct_non_none_repo_ids) >= 2
        )

        if _use_fleet:
            # WS8 SLICE 1: parallel fleet path.
            # Dispatcher factory: each worker gets a FRESH dispatcher with its OWN
            # asyncio event loop (Fix 2 -- no shared loop race).
            # MCPStdioDispatcher._run calls run_until_complete on self._loop; two
            # threads sharing one loop would raise "This event loop is already running".
            # Constructing per-worker MCPStdioDispatcher instances (same project_root
            # + verbose setting) gives each worker an independent _loop=None baseline.
            if hasattr(dispatcher, "project_root"):
                _pr = dispatcher.project_root
                _vb = getattr(dispatcher, "verbose", False)
                _dcls = dispatcher.__class__
                # RBAC: capture the allow-list map and active handoffs from the
                # original (already-configured) dispatcher so each worker dispatcher
                # enforces IDENTICAL tool allow-lists. A blank _squad_packs={} would
                # let fleet MCP calls bypass per-squad RBAC entirely.
                _squad_packs_snapshot: dict[str, Any] = dict(
                    getattr(dispatcher, "_squad_packs", {}) or {}
                )
                _handoffs_snapshot: list[dict[str, Any]] = list(
                    getattr(dispatcher, "_active_handoffs", []) or []
                )
                # _tool_tracker is MUTATED on every MCP call (appends to _calls
                # list on each invocation) so it MUST NOT be shared across worker
                # threads.  Each worker gets a FRESH tracker; the original tracker
                # is only referenced here to copy its _packs config (read-only).
                _orig_tracker: Any = getattr(dispatcher, "_tool_tracker", None)
                _tracker_packs: Any = (
                    getattr(_orig_tracker, "_packs", {}) if _orig_tracker is not None else {}
                )

                def _dispatcher_factory(
                    _cls: type = _dcls,
                    _root: Any = _pr,
                    _v: bool = _vb,
                    _sp: dict[str, Any] = _squad_packs_snapshot,
                    _ho: list[dict[str, Any]] = _handoffs_snapshot,
                    _tp: Any = _tracker_packs,
                ) -> Dispatcher:
                    d = _cls(project_root=_root, verbose=_v)
                    # RBAC: shared read-only state (packs + handoffs are not mutated
                    # during dispatch — only read by _check_tool_rbac).
                    if hasattr(d, "set_squad_packs"):
                        d.set_squad_packs(_sp)
                    if hasattr(d, "_active_handoffs"):
                        d._active_handoffs = list(_ho)
                    # Tool tracker: FRESH instance per worker (not the shared ref)
                    # so concurrent .record() calls never race on one list.
                    # The factory does NOT append to any shared list — pure
                    # construct+configure+return (no side effects on shared state).
                    if hasattr(d, "_tool_tracker") and _orig_tracker is not None:
                        from .tool_analytics import ToolUsageTracker
                        d._tool_tracker = ToolUsageTracker(packs=_tp)
                    return d
            else:
                # Stub/mock dispatchers have no asyncio loop; safe to share.
                def _dispatcher_factory(_d: Dispatcher = dispatcher) -> Dispatcher:  # type: ignore[misc]
                    return _d

            # Wrap stored payloads so dispatch_fleet never calls build_payload again.
            def _stored_payload_builder(task: TaskState) -> Any:
                return _fleet_candidate_payloads[id(task)]

            # WS8 SLICE 2: cancel-on-surfaced policy.
            # A "surfaced" result means the task requires HITL or has aborted;
            # continuing to spend on remaining tasks is wasteful.  Set the
            # cancel_event so not-yet-started workers skip dispatch immediately.
            # In-flight workers that already passed their entry-check complete
            # naturally (threads cannot be force-killed; this is documented).
            # Note: over-budget cancellation is handled by the post-join pass-2
            # budget gate (which fires after ALL results are available).  The
            # cancel-on-surfaced policy is a proactive guard that fires as soon
            # as any result comes back surfaced — before all workers are done.
            def _cancel_on_surfaced(result: SquadResult) -> bool:
                return result.status == "surfaced"

            _fleet_results, _fleet_worker_trackers = dispatch_fleet(
                state,
                _fleet_candidate_tasks,
                _dispatcher_factory,
                build_payload=_stored_payload_builder,
                packs=packs,
                max_concurrency=getattr(state, "fleet_max_concurrency", None),
                should_cancel=_cancel_on_surfaced,
            )
            # WS8 SLICE 2 Fix 4: mark that the fleet path actually ran so
            # node_synthesis can gate on this flag (not just distinct repo count).
            state.fleet_dispatched = True
            # Merge per-worker tool tracker calls into the original tracker.
            # Iterate in INPUT INDEX ORDER (0..n-1) so the merge is deterministic
            # regardless of thread completion order.  Writing to a distinct
            # pre-allocated slot (in dispatch_fleet) means no shared list.append
            # ever ran from a worker thread — race-free by design.
            _orig_tt = getattr(dispatcher, "_tool_tracker", None)
            if _orig_tt is not None:
                for _wt in _fleet_worker_trackers:
                    if _wt is not None:
                        _orig_tt._calls.extend(_wt._calls)
                        _wt._calls = []  # free memory; worker is done

            # Fix 6 (two-pass fleet merge):
            # PASS 1 -- charge ALL fleet results before making any budget decision.
            # Since all workers have already joined, every result is available.
            # Charging them one at a time (sequentially in the main thread) keeps
            # the ledger monotonic. We record which tasks blocked and which
            # triggered downgrade, but we do NOT surface HITL until pass 2.
            _fleet_any_block = False
            _fleet_last_blocking_squad = ""
            for fleet_task, result in zip(_fleet_candidate_tasks, _fleet_results):
                pack = packs.get(fleet_task.owner_squad)
                # Always extract and charge cost for EVERY result — even failed ones.
                # A failed paid MCP call may still have incurred spend; skipping the
                # charge produces ledger undercount and makes the HITL budget summary
                # inaccurate (Fix 6 gap: failed results must be charged before continue).
                _cost_usd, _cost_tok = _extract_squad_cost(result)
                if pack is not None:
                    _block, _downgrade = charge_and_gate(state, _cost_usd, _cost_tok)
                    if _cost_usd == 0.0 and _cost_tok == 0:
                        emit_trace(judge_trace_root, state.workflow_id, "budget.cost_unavailable", {
                            "site": "dispatch_fleet", "squad": pack.slug,
                        })
                    if _downgrade and not state.budget_downgrade_active:
                        state.budget_downgrade_active = True
                        emit_trace(judge_trace_root, state.workflow_id, "budget.downgrade_tripwire", {
                            "percent_consumed": state.budget.percent_consumed,
                            "spent_usd": state.budget.spent_usd,
                            "budget_usd": state.budget.budget_usd,
                            "squad": pack.slug,
                        })
                    if _block:
                        _fleet_any_block = True
                        _fleet_last_blocking_squad = pack.slug
                # ── Unconditional artifact + envelope collection (BEFORE any continue) ──
                # Artifacts are collected from EVERY result — done, failed, AND
                # cancelled — so no task's artifacts are dropped from state before
                # synthesis can list them.  A failed task may have produced artifacts
                # worth surfacing; a cancelled worker produces none but extend([]) is
                # harmless.  Envelopes are collected here too (when pack is known) so
                # the task_id tag and eights record are always written regardless of
                # the status branch below.
                artifacts.extend(result.artifacts)
                if pack is not None:
                    for produced in result.envelopes:
                        d = produced.model_dump(mode="json")
                        try:
                            d = _validate_and_redact_envelope(
                                d, direction="inbound_from_squad",
                                squad_id=pack.slug,
                            )
                        except (ValueError, Exception):
                            fleet_task.status = "failed"
                            state.error_counters[fleet_task.owner_squad] = (
                                state.error_counters.get(fleet_task.owner_squad, 0) + 1
                            )
                            continue
                        if result.host_pickup_pending:
                            d["_host_pickup_pending"] = True
                        d["_task_id"] = str(fleet_task.task_id)
                        new_decisions.append(d)
                        eights.envelope_record(d)

                # ── Status-based handling (continues are safe now that artifacts/
                #    envelopes are already collected above) ──
                # Invariant: a "failed" set by envelope validation above must
                # never be overwritten by ANY branch here — cancelled, failed,
                # or done/surfaced.  Apply the guard at EVERY assignment so the
                # rule is uniform and impossible to accidentally violate in a
                # future edit.
                if result.status == "cancelled":
                    # Cancelled tasks (WS8 SLICE 2): the worker never dispatched,
                    # so there are no envelopes or artifacts to collect.  Do NOT
                    # increment error_counters — cancellation is not an error.
                    if fleet_task.status != "failed":
                        fleet_task.status = "cancelled"
                    continue
                if pack is None or result.status == "failed":
                    if fleet_task.status != "failed":
                        fleet_task.status = "failed"
                    if pack is not None:
                        state.error_counters[fleet_task.owner_squad] = (
                            state.error_counters.get(fleet_task.owner_squad, 0) + 1
                        )
                    continue
                # Record the task status for done/surfaced/running results.
                # Do NOT clobber a "failed" status set by envelope validation.
                if fleet_task.status != "failed":
                    fleet_task.status = result.status

            # PASS 2 -- budget block decision, now that ALL spend is charged.
            # The ledger reflects the full fleet spend; the HITL summary is accurate.
            if _fleet_any_block:
                state.budget_downgrade_active = True
                emit_trace(judge_trace_root, state.workflow_id, "budget.over_budget_surface", {
                    "spent_usd": state.budget.spent_usd,
                    "budget_usd": state.budget.budget_usd,
                    "site": "dispatch_fleet",
                    "squad": _fleet_last_blocking_squad,
                })
                _hitl_fleet_budget: dict[str, Any] = {
                    "workflow_id": str(state.workflow_id),
                    "reason": "over_budget",
                    "gate_node": "dispatch",
                    "summary": (
                        f"Budget exhausted (fleet): ${state.budget.spent_usd:.4f} of "
                        f"${state.budget.budget_usd:.2f} (all {len(_fleet_results)} "
                        f"fleet results charged before surface)."
                    ),
                    "options": ["approve_override", "abort"],
                    "default_option": "abort",
                    "spent_usd": state.budget.spent_usd,
                    "budget_usd": state.budget.budget_usd,
                }
                eights.hitl_request(_hitl_fleet_budget, gate_node="dispatch")
                return {
                    "envelopes": new_decisions,
                    "artifacts": artifacts,
                    "verdicts": bon_verdicts,
                    "phase": "surfaced",
                    "pending_hitl": _hitl_fleet_budget,
                    "budget_downgrade_active": True,
                    "open_pp_runs": state.open_pp_runs,
                }
            # Fleet tasks now have status != "pending"; the sequential loop
            # below skips them. Non-mcp and best-of-N tasks are still pending.

        # --- Original sequential dispatch path (default; unchanged) ----------
        for task in _dispatch_tasks:
            if task.status != "pending":
                continue
            pack = packs.get(task.owner_squad)
            if pack is None:
                task.status = "failed"
                continue

            # Best-of-N branch (pack opt-in via squad.yaml `best_of_n: N`).
            if pack.best_of_n and pack.best_of_n >= 2:
                winners = _dispatch_best_of_n(
                    state, pack, task, artifacts, bon_verdicts
                )
                # Fix 2a: _dispatch_best_of_n sets state.phase="surfaced" when
                # budget is exhausted mid-N. Detect this and return the surface
                # payload immediately rather than continuing to dispatch tasks.
                if state.phase == "surfaced":
                    return {
                        "envelopes": new_decisions,
                        "artifacts": artifacts,
                        "verdicts": bon_verdicts,
                        "phase": "surfaced",
                        "pending_hitl": state.pending_hitl,
                        "budget_downgrade_active": state.budget_downgrade_active,
                        "open_pp_runs": state.open_pp_runs,
                    }
                if winners:
                    new_decisions.extend(winners)
                    task.status = "done"
                else:
                    task.status = "failed"
                continue

            # Standard single-shot dispatch.
            # Fix 5: consume the pre-built payload; never call _build_payload again.
            payload = _all_task_payloads.get(id(task))
            if payload is None:
                # Payload build failed up-front; fail the task now.
                task.status = "failed"
                state.error_counters[task.owner_squad] = (
                    state.error_counters.get(task.owner_squad, 0) + 1
                )
                continue
            try:
                result = execute_squad(state, pack, payload, dispatcher)
            except Exception as e:
                task.status = "failed"
                state.error_counters[task.owner_squad] = (
                    state.error_counters.get(task.owner_squad, 0) + 1
                )
                continue
            # Fix 2b: charge + gate via centralized helper.
            _cost_usd, _cost_tok = _extract_squad_cost(result)
            _block, _downgrade = charge_and_gate(state, _cost_usd, _cost_tok)
            if _cost_usd == 0.0 and _cost_tok == 0:
                emit_trace(judge_trace_root, state.workflow_id, "budget.cost_unavailable", {
                    "site": "dispatch", "squad": pack.slug,
                })
            if _downgrade and not state.budget_downgrade_active:
                state.budget_downgrade_active = True
                emit_trace(judge_trace_root, state.workflow_id, "budget.downgrade_tripwire", {
                    "percent_consumed": state.budget.percent_consumed,
                    "spent_usd": state.budget.spent_usd,
                    "budget_usd": state.budget.budget_usd,
                    "squad": pack.slug,
                })
            if _block:
                # >= 100%: surface to HITL, stop dispatching further tasks.
                state.budget_downgrade_active = True
                emit_trace(judge_trace_root, state.workflow_id, "budget.over_budget_surface", {
                    "spent_usd": state.budget.spent_usd,
                    "budget_usd": state.budget.budget_usd,
                    "squad": pack.slug,
                })
                task.status = result.status
                _hitl_over_budget: dict[str, Any] = {
                    "workflow_id": str(state.workflow_id),
                    "reason": "over_budget",
                    "gate_node": "dispatch",
                    "summary": (
                        f"Budget exhausted: ${state.budget.spent_usd:.4f} of "
                        f"${state.budget.budget_usd:.2f} spent after {pack.slug} dispatch."
                    ),
                    "options": ["approve_override", "abort"],
                    "default_option": "abort",
                    "spent_usd": state.budget.spent_usd,
                    "budget_usd": state.budget.budget_usd,
                }
                eights.hitl_request(_hitl_over_budget, gate_node="dispatch")
                return {
                    "envelopes": new_decisions,
                    "artifacts": artifacts,
                    "verdicts": bon_verdicts,
                    "phase": "surfaced",
                    "pending_hitl": _hitl_over_budget,
                    "budget_downgrade_active": True,
                    "open_pp_runs": state.open_pp_runs,
                }
            task.status = result.status
            # Validate and redact envelopes crossing the squad boundary back
            # into the supervisor. Invalid envelopes fail the task.
            for produced in result.envelopes:
                d = produced.model_dump(mode="json")
                try:
                    d = _validate_and_redact_envelope(
                        d, direction="inbound_from_squad",
                        squad_id=pack.slug,
                    )
                except (ValueError, Exception):
                    task.status = "failed"
                    state.error_counters[task.owner_squad] = (
                        state.error_counters.get(task.owner_squad, 0) + 1
                    )
                    continue
                if result.host_pickup_pending:
                    d["_host_pickup_pending"] = True
                # WS9 Fix 2: tag the envelope with the originating task_id so
                # _reflexion_retry can source model_tier from the EXACT task,
                # not just the first same-squad task.
                d["_task_id"] = str(task.task_id)
                new_decisions.append(d)
                eights.envelope_record(d)
            artifacts.extend(result.artifacts)
        return {
            "envelopes": new_decisions,
            "artifacts": artifacts,
            "verdicts": bon_verdicts,
            "phase": "judge_per_squad",
        }

    def _reflexion_retry(
        state: HydraState,
        original_env: dict,
        revise_verdict: dict,
        prior_retry_index: int,
    ) -> tuple[list[dict], list[dict]]:
        """Re-dispatch the source squad once with the critique appended.

        Returns (new_envelope_dicts, new_verdict_dicts). Empty when retry
        is not possible (unknown squad, missing pack, exec failure).
        """
        origin = original_env.get("origin_squad")
        # The envelope's origin_squad is the *producer* (e.g., "executive").
        # Hydra-built initial envelopes have origin_squad="hydra"; in that case
        # nothing meaningful to retry against.
        if not origin or origin in {"hydra", "hydra-judge"}:
            return [], []
        pack = packs.get(origin)
        if pack is None:
            return [], []

        verdict_obj = JudgeVerdict.model_validate(revise_verdict)
        # R3-tail post-mortem: per-workflow ceiling raise after operator HITL
        # approval of a `reflexion_override` request. Default 0 ⇒ no raise ⇒
        # the ×1 invariant default still applies.
        packet = package_retry(
            original_env, verdict_obj,
            prior_retry_index=prior_retry_index,
            max_retry_override=state.reflexion_override_granted_until or None,
        )
        if packet is None:
            return [], []

        # Build a Reflexion-augmented inbound. Embed the critique in the
        # objective so any squad executor sees it via the standard
        # `getattr(inbound, "objective", ...)` path.
        prior_objective = (
            original_env.get("objective")
            or original_env.get("summary")
            or state.root_goal
            or "(retry)"
        )
        retry_obj = (
            f"{prior_objective}\n\n"
            f"=== REFLEXION RETRY #{packet.retry_index} ===\n"
            f"Prior verdict on rubric {revise_verdict.get('rubric_id')}: revise.\n"
            f"Critique to address:\n{revise_verdict.get('critique_md', '')}\n"
        )
        # WS9 Fix 2 (multi-task-per-squad): source model_tier from the EXACT
        # originating task identified by _task_id, not just any same-squad task.
        # Fix A sourced from state.tasks by owner_squad — wrong when a squad has
        # multiple tasks with different tiers.  The dispatch loop now stamps
        # `_task_id` onto each produced envelope so we can look up the precise
        # TaskState here.  Fall back to owner_squad match if _task_id is absent
        # (e.g. envelopes produced by older code paths), then to None.
        _tagged_task_id = original_env.get("_task_id")
        if _tagged_task_id:
            _retry_task = next(
                (t for t in state.tasks if str(t.task_id) == _tagged_task_id),
                None,
            )
        else:
            # Legacy fallback: first same-squad task (pre-_task_id envelopes).
            _retry_task = next(
                (t for t in state.tasks if t.owner_squad == origin),
                None,
            )
        _retry_model_tier = getattr(_retry_task, "model_tier", None)
        retry_envelope = CSuiteDecisionPacket(
            workflow_id=state.workflow_id,
            origin_squad="hydra",
            target_squad=pack.slug,
            origin="BOARDROOM",
            objective=retry_obj,
            parent_id=original_env.get("id"),
            target_repo_id=state.target_repo_id,
            model_tier=_retry_model_tier,
        )

        try:
            result = execute_squad(state, pack, retry_envelope, dispatcher)
        except Exception as e:
            emit_trace(judge_trace_root, state.workflow_id, "judge.reflexion_error", {
                "origin": origin, "error": str(e),
            })
            return [], []
        # Fix 2b: charge + gate for reflexion retries.
        _cost_usd, _cost_tok = _extract_squad_cost(result)
        _block, _downgrade = charge_and_gate(state, _cost_usd, _cost_tok)
        if _cost_usd == 0.0 and _cost_tok == 0:
            emit_trace(judge_trace_root, state.workflow_id, "budget.cost_unavailable", {
                "site": "reflexion", "origin": origin,
            })
        if _downgrade and not state.budget_downgrade_active:
            state.budget_downgrade_active = True
            emit_trace(judge_trace_root, state.workflow_id, "budget.downgrade_tripwire", {
                "site": "reflexion", "origin": origin,
                "percent_consumed": state.budget.percent_consumed,
            })
        if _block:
            # Budget hit during reflexion — surface to HITL immediately.
            # Fix 2b: must set state.phase + pending_hitl so after_dispatch
            # routes to "postcheck" (halt) rather than "judge_per_squad".
            # A bare `return [], []` was silently swallowed — the caller
            # (node_judge_per_squad) continued accumulating retries and the
            # state machine never halted.
            state.budget_downgrade_active = True
            _reflexion_hitl: dict[str, Any] = {
                "workflow_id": str(state.workflow_id),
                "reason": "over_budget",
                "gate_node": "judge_per_squad",
                "summary": (
                    f"Budget exhausted during reflexion retry for squad {origin}: "
                    f"${state.budget.spent_usd:.4f} of "
                    f"${state.budget.budget_usd:.2f} spent."
                ),
                "options": ["approve_override", "abort"],
                "default_option": "abort",
                "spent_usd": state.budget.spent_usd,
                "budget_usd": state.budget.budget_usd,
            }
            state.phase = "surfaced"
            state.pending_hitl = _reflexion_hitl
            eights.hitl_request(_reflexion_hitl, gate_node="judge_per_squad")
            emit_trace(judge_trace_root, state.workflow_id, "budget.reflexion_blocked", {
                "origin": origin, "spent_usd": state.budget.spent_usd,
            })
            return [], []

        new_env_dicts: list[dict] = []
        for produced in result.envelopes:
            d = produced.model_dump(mode="json")
            try:
                d = _validate_and_redact_envelope(
                    d, direction="reflexion_retry_inbound",
                    squad_id=origin,
                )
            except (ValueError, Exception):
                continue
            d["_retry_index"] = packet.retry_index
            # Propagate _task_id through every retry generation so that a
            # retry-of-retry still resolves the exact originating task for
            # tier lookup (not the first-same-squad fallback).
            if _tagged_task_id:
                d["_task_id"] = _tagged_task_id
            new_env_dicts.append(d)

        emit_trace(judge_trace_root, state.workflow_id, "judge.reflexion", {
            "original_envelope_id": original_env.get("id"),
            "retry_index": packet.retry_index,
            "origin_squad": origin,
            "rubric_id": revise_verdict.get("rubric_id"),
            "new_envelope_count": len(new_env_dicts),
        })

        # Re-judge the retry output. Loop ceiling: MAX_RETRY_INDEX caps depth.
        new_verdicts: list[dict] = []
        for new_env in new_env_dicts:
            new_verdicts.extend(_judge_envelope(state, new_env, is_post_synthesis=False))
        return new_env_dicts, new_verdicts

    def node_judge_per_squad(state: HydraState) -> dict:
        """Judge each squad-produced envelope from the most recent dispatch.

        Four responsibilities:
          1. Score each unjudged envelope against its rubrics.
          2. On `revise` (Phase 3): package_retry → re-dispatch source squad
             once, then re-judge. Bounded by Reflexion ×N (default ×1; raised
             per-workflow only via operator-approved `reflexion_override` HITL).
          3. On `fail` with a HITL-severity rubric: surface to HITL.
          4. On `revise` BUT ceiling exhausted (R3-tail post-mortem,
             2026-05-21): surface a `reflexion_override` HITL instead of
             silently advancing. The operator can either approve the raise
             (sets `state.reflexion_override_granted_until` for next pass)
             or accept the partial output.
        """
        state.phase = "judge_per_squad"
        _emit_node_context(state, "judge_per_squad")
        already_judged = {v.get("target_envelope_id") for v in state.verdicts}
        new_verdicts: list[dict] = []
        retry_envelopes: list[dict] = []
        retry_verdicts: list[dict] = []
        breach: dict | None = None
        # R3-tail: envelopes whose `revise` verdict could not be retried
        # because the active Reflexion ceiling is exhausted. Collected here
        # and surfaced as one `reflexion_override` HITL at the end of the
        # node so a single workflow with multiple ceiling-bound envelopes
        # gets one HITL prompt, not N.
        ceiling_blocked: list[tuple[dict, dict]] = []  # (envelope, revise_verdict)
        active_ceiling = effective_max_retry_index(
            max_retry_override=state.reflexion_override_granted_until or None
        )

        # First: scan best_of_n verdicts already in state for HITL-severity
        # fails. These were emitted during dispatch on candidate envelopes
        # before this node ran.
        for prior in state.verdicts:
            if (prior.get("outcome") == "fail"
                    and judge_policy.is_hitl_severity(prior.get("rubric_id", ""))):
                breach = prior
                break

        for env in state.envelopes:
            if env.get("type") == "JUDGE_VERDICT":
                continue
            if env.get("id") in already_judged:
                continue
            # Skip envelopes Hydra itself authored as routing slips
            # (origin_squad="hydra"). These are inbound CSuiteDecisionPackets
            # the planner emits to brief a squad, not the squad's response.
            # Judging them adds no signal and forces the NoOp fallback
            # (because "hydra" isn't in enabled_squads).
            if env.get("origin_squad") == "hydra":
                continue
            # Skip host-pickup placeholders. The real response lands later
            # via a separate envelope from the Claude Code host.
            if env.get("_host_pickup_pending"):
                emit_trace(judge_trace_root, state.workflow_id, "judge.skipped", {
                    "envelope_id": env.get("id"),
                    "envelope_type": env.get("type"),
                    "rationale": "host_pickup_pending: real response awaited",
                })
                continue
            env_verdicts = _judge_envelope(state, env, is_post_synthesis=False)
            new_verdicts.extend(env_verdicts)

            # (3) HITL escalation per envelope.
            severity_fail = next(
                (v for v in env_verdicts
                 if v.get("outcome") == "fail"
                 and judge_policy.is_hitl_severity(v.get("rubric_id", ""))),
                None,
            )
            if severity_fail and breach is None:
                breach = severity_fail
                continue  # don't bother retrying a HITL-severity failure

            # (2) Reflexion retry on the first revise verdict.
            revise = next(
                (v for v in env_verdicts if v.get("outcome") == "revise"),
                None,
            )
            if revise is None:
                continue
            prior_retry = int(env.get("_retry_index", 0) or 0)
            if prior_retry >= active_ceiling:
                # (4) R3-tail: ceiling exhausted. Don't silently advance —
                # collect for the `reflexion_override` HITL emitted below.
                # Exception: if the envelope's origin squad is NOT enabled
                # for judging (NoOp client path), the `revise` came from the
                # pragmatic-pass guard downgrading NoOp's empty pass, not
                # from substantive critique. Suppress the HITL in that case
                # so staged-rollout squads don't block on synthetic revises.
                if judge_policy.squad_enabled(env.get("origin_squad")):
                    ceiling_blocked.append((env, revise))
                continue
            # Fix 2d: skip reflexion if budget is already at/over limit.
            if should_block_for_budget(state):
                emit_trace(judge_trace_root, state.workflow_id, "budget.reflexion_skipped", {
                    "reason": "over_budget", "spent_usd": state.budget.spent_usd,
                })
                continue
            r_envs, r_verdicts = _reflexion_retry(state, env, revise, prior_retry)
            retry_envelopes.extend(r_envs)
            retry_verdicts.extend(r_verdicts)
            # Fix 2b: _reflexion_retry sets state.phase="surfaced" when budget
            # is exhausted. Detect this and short-circuit the envelope loop —
            # there is no point judging further envelopes if we must halt.
            if state.phase == "surfaced":
                return {
                    "verdicts": new_verdicts + retry_verdicts,
                    "envelopes": retry_envelopes,
                    "phase": "surfaced",
                    "pending_hitl": state.pending_hitl,
                    "budget_downgrade_active": state.budget_downgrade_active,
                }

        # R3-tail: also scan the just-completed retry verdicts. If a retry
        # envelope's own re-judge came back `revise`, the next pass would need
        # to retry it again — but the retry envelope already has
        # `_retry_index = active_ceiling`, so the ceiling is exhausted.
        # Surface those as ceiling_blocked alongside the for-loop entries.
        # Same squad-enabled filter as above: ignore retry verdicts from
        # staged-rollout squads whose `revise` is a pragmatic-guard artefact.
        retry_envs_by_id = {str(e.get("id")): e for e in retry_envelopes}
        for rv in retry_verdicts:
            if rv.get("outcome") != "revise":
                continue
            target_id = str(rv.get("target_envelope_id"))
            target_env = retry_envs_by_id.get(target_id)
            if target_env is None:
                continue
            if not judge_policy.squad_enabled(target_env.get("origin_squad")):
                continue
            target_retry_idx = int(target_env.get("_retry_index", 0) or 0)
            if target_retry_idx >= active_ceiling:
                ceiling_blocked.append((target_env, rv))

        out: dict[str, Any] = {
            "verdicts": new_verdicts + retry_verdicts,
            "envelopes": retry_envelopes,
            "phase": "synthesis",
        }
        if breach:
            hitl = HITLRequest(
                workflow_id=state.workflow_id,
                origin_squad="hydra-judge",
                target_squad="human",
                reason="policy_breach",
                summary=(
                    f"Per-squad judge FAIL on rubric {breach.get('rubric_id')}. "
                    f"Critique: {(breach.get('critique_md') or '')[:240]}"
                ),
                options=["approve_override", "reject", "send_back_for_revision"],
                default_option="reject",
            )
            hitl_dict = hitl.model_dump(mode="json")
            hitl_dict["gate_node"] = "judge_per_squad"  # C2 dedupe key half
            eights.hitl_request(hitl_dict, gate_node="judge_per_squad")
            out["pending_hitl"] = hitl_dict
            out["phase"] = "surfaced"
            emit_trace(judge_trace_root, state.workflow_id, "judge.hitl_escalation", {
                "stage": "per_squad",
                "rubric_id": breach.get("rubric_id"),
                "envelope_id": breach.get("target_envelope_id"),
            })
        elif ceiling_blocked:
            # R3-tail post-mortem (2026-05-21): emit a single
            # `reflexion_override` HITL when at least one envelope's `revise`
            # verdict could not be retried because the active ceiling is
            # exhausted. The constitutional ×1 invariant is unchanged — the
            # operator's choices are (a) raise the ceiling for THIS workflow
            # only via `approve_override_raise_to_N`, (b) accept the partial
            # output via `accept_partial`, or (c) abort. Summary truncates
            # the first blocked envelope's critique for readability; the full
            # critiques are reachable via state.verdicts lookup by id.
            first_env, first_revise = ceiling_blocked[0]
            count = len(ceiling_blocked)
            blocked_ids = ", ".join(
                str(env.get("id", "?"))[:8] for env, _ in ceiling_blocked
            )
            summary = (
                f"Reflexion ceiling (={active_ceiling}) exhausted on "
                f"{count} envelope(s) [{blocked_ids}] still in 'revise'. "
                f"First critique: "
                f"{(first_revise.get('critique_md') or '')[:200]}"
            )[:240]
            hitl = HITLRequest(
                workflow_id=state.workflow_id,
                origin_squad="hydra-judge",
                target_squad="human",
                reason="reflexion_override",
                summary=summary,
                options=[
                    f"approve_override_raise_to_{active_ceiling + 1}",
                    f"approve_override_raise_to_{active_ceiling + 2}",
                    "accept_partial",
                    "abort",
                ],
                default_option="accept_partial",
            )
            hitl_dict = hitl.model_dump(mode="json")
            hitl_dict["gate_node"] = "judge_per_squad"  # C2 dedupe key half
            eights.hitl_request(hitl_dict, gate_node="judge_per_squad")
            out["pending_hitl"] = hitl_dict
            out["phase"] = "surfaced"
            emit_trace(judge_trace_root, state.workflow_id, "judge.reflexion_ceiling_hit", {
                "active_ceiling": active_ceiling,
                "blocked_count": count,
                "blocked_envelope_ids": [str(env.get("id")) for env, _ in ceiling_blocked],
                "rubric_ids": [v.get("rubric_id") for _, v in ceiling_blocked],
            })
        return out

    def node_synthesis(state: HydraState) -> dict:
        """Synthesize the workflow into a DecisionRecord.

        R3-tail post-mortem Fix 2.2 (2026-05-21): the `hydra-synthesizer`
        Claude Code subagent (described in `.claude/agents/hydra-synthesizer.md`)
        was previously not wired into the LangGraph — `node_synthesis` emitted
        a minimal boilerplate DecisionRecord and `node_judge_synthesis`
        validated against it. When the subagent dropped mid-action (R3-tail
        observed this), Claude had to take over orchestration manually.

        This enriched implementation does deterministically (no LLM call)
        what the subagent contract specifies:
          - Group envelopes by squad (non-fleet) OR by repo (fleet).
          - Preserve dissenting opinions verbatim (verdicts with
            outcome='revise' OR explicit dissenting_opinions field).
          - List every artifact id (no drops).
          - Call out budget burn + remaining headroom.
          - Note any HITL gates that fired.
          - Set sealed=False when mutually-exclusive verdicts exist.

        WS8 SLICE 2 — multi-repo fleet synthesis:
          - When the dispatched tasks span >=2 distinct target_repo_id values
            the rationale SECTIONS BY REPO (sorted deterministically by repo_id),
            not just by squad.  Grouping by squad collapses all engineering repos
            into one section, losing per-repo distinction.
          - Each repo section: status (done/failed/surfaced/cancelled), key
            outcome, per-repo dissents (tagged with repo_id, verbatim).
          - Cancelled repos are explicitly noted.
          - sealed=False if any repo failed/surfaced/has mutually-exclusive verdict.
          - Non-fleet runs (single repo / fleet_parallel=False) use the existing
            squad-grouped synthesis unchanged (no regression).

        Cathedral voice for user-facing output; plaza slugs remain in the
        envelope's structured fields. Per the manifesto: "no head speaks
        to the user without Hydra's synthesis."
        """
        _emit_node_context(state, "synthesis")
        cathedral_roster = ", ".join(
            crown_label_for_squad(s) for s in state.selected_squads
        ) or "(no heads convened)"

        # Group envelopes by origin_squad (skip Hydra's own routing slips).
        # Redact at synthesis boundary — envelopes from different squads are
        # merged here, so cross-squad text must be sanitized.
        squad_to_envs: dict[str, list[dict]] = {}
        for env in state.envelopes:
            origin = env.get("origin_squad") or "hydra"
            if origin == "hydra":
                continue
            try:
                redacted = _validate_and_redact_envelope(
                    env, direction="synthesis_merge", squad_id=origin,
                )
                squad_to_envs.setdefault(origin, []).append(redacted)
            except (ValueError, Exception):
                squad_to_envs.setdefault(origin, []).append(env)

        # ------------------------------------------------------------------ #
        # WS8 SLICE 2: detect whether this was a fleet run.
        # Fix 4: gate on state.fleet_dispatched (set True only when
        # dispatch_fleet was actually invoked), NOT merely on distinct repo
        # count.  A sequential multi-repo run has distinct repos but
        # fleet_dispatched=False -> uses existing per-squad synthesis.
        # ------------------------------------------------------------------ #
        distinct_task_repos: set[str] = {
            t.target_repo_id for t in state.tasks
            if t.target_repo_id is not None
        }
        _is_fleet_run = getattr(state, "fleet_dispatched", False) and len(distinct_task_repos) >= 2

        # Build a task_id -> target_repo_id lookup (for correlating envelopes
        # tagged with _task_id back to their repo when in fleet mode).
        _task_id_to_repo: dict[str, str] = {
            str(t.task_id): t.target_repo_id
            for t in state.tasks
            if t.target_repo_id is not None
        }
        # Build a task_id -> task status lookup for per-repo status.
        _task_id_to_status: dict[str, str] = {
            str(t.task_id): t.status for t in state.tasks
        }
        # Build repo -> task_ids mapping.
        _repo_to_task_ids: dict[str, list[str]] = {}
        for t in state.tasks:
            if t.target_repo_id is not None:
                _repo_to_task_ids.setdefault(t.target_repo_id, []).append(str(t.task_id))

        # ------------------------------------------------------------------ #
        # Preserve dissenting opinions verbatim (R3-tail contract: NEVER
        # paraphrase, NEVER truncate).
        # Fix 3: the complete original string is preserved — no [:480] slice,
        #   no .strip() on the content.
        # Fix 5: repo-tag fail-closed — if the envelope can't be correlated to
        #   a repo (no _task_id or task not in map), tag it [repo:unknown] so
        #   every fleet dissent carries an explicit, non-empty repo attribution.
        # Tag format: "[repo:<id>]\n" prepended as a prefix line so the
        #   verbatim critique text is unmodified.
        # Sources:
        #   1. Any verdict with outcome='revise' or 'fail' — dissent = critique.
        #   2. envelope.dissenting_opinions field (explicit dissent strings).
        # ------------------------------------------------------------------ #
        dissents: list[str] = []
        for v in state.verdicts:
            if v.get("outcome") in ("revise", "fail"):
                critique = v.get("critique_md") or ""
                if critique:
                    if _is_fleet_run:
                        target_id = str(v.get("target_envelope_id") or "")
                        target_env = next(
                            (e for e in state.envelopes if str(e.get("id")) == target_id),
                            None,
                        )
                        env_task_id = (target_env or {}).get("_task_id") or ""
                        repo_tag = _task_id_to_repo.get(env_task_id, "unknown")
                        # Prefix on its own line; critique is verbatim below.
                        repo_prefix = f"[repo:{repo_tag}]\n"
                    else:
                        repo_prefix = ""
                    # No truncation, no strip — verbatim as received (Fix 3).
                    # Fleet path: ONLY [repo:<id>] prefix + verbatim critique.
                    # [vendor@rubric] is NOT inserted into the fleet dissent string.
                    # Non-fleet path: restore original [vendor@rubric] prefix format.
                    if _is_fleet_run:
                        dissents.append(f"{repo_prefix}{critique}")
                    else:
                        dissents.append(
                            f"[{v.get('judge_vendor', '?')}@{v.get('rubric_id', '?')}] "
                            f"{critique}"
                        )
        for env in state.envelopes:
            for d in (env.get("dissenting_opinions") or []):
                if isinstance(d, str) and d:
                    if _is_fleet_run:
                        env_task_id = env.get("_task_id") or ""
                        repo_tag = _task_id_to_repo.get(env_task_id, "unknown")
                        # Prefix on its own line; d is verbatim (Fix 3 + Fix 5).
                        repo_prefix = f"[repo:{repo_tag}]\n"
                    else:
                        repo_prefix = ""
                    # No strip, no truncation — preserve the string as-is.
                    dissents.append(f"{repo_prefix}{d}")

        # Conflict detection: mutually-exclusive verdicts at the synthesis
        # stage = NOT sealed. Per hydra-synthesizer.md "When to Surface
        # Instead of Decide": if engineering says ship and security says
        # block, the synthesizer does NOT pick.
        has_mutually_exclusive = False
        outcomes_by_squad: dict[str, set[str]] = {}
        for v in state.verdicts:
            target_id = v.get("target_envelope_id")
            target_env = next((e for e in state.envelopes if str(e.get("id")) == str(target_id)), None)
            squad = (target_env or {}).get("origin_squad") or "?"
            outcomes_by_squad.setdefault(squad, set()).add(v.get("outcome") or "?")
        # Heuristic: any squad with both 'pass' AND 'fail' verdicts indicates
        # cross-rubric disagreement worth surfacing.
        for _sq, outs in outcomes_by_squad.items():
            if "pass" in outs and "fail" in outs:
                has_mutually_exclusive = True
                break

        # HITL trace: any prior HITL the workflow surfaced.
        hitl_count = len(state.hitl_history or [])

        # Artifact list: every artifact, no drops (Fix 2).
        # Convert raw artifact dicts to MemoryRef handles for DecisionRecord.
        # state.artifacts is a list[dict]; each dict may carry a "ref" key
        # (the pp_run_id) and a "kind" key.  We build a MemoryRef per artifact
        # using the ref as the key (fallback: the artifact's index as a string)
        # and tier="episodic" (all squad artifacts are episodic by convention).
        # Deterministic stable order: matches state.artifacts insertion order.
        from .schemas import MemoryRef
        all_artifacts: list[MemoryRef] = []
        for _i, _art in enumerate(state.artifacts or []):
            _ref_key = (
                _art.get("ref")
                or _art.get("run_id")
                or _art.get("id")
                or f"artifact-{_i}"   # stable positional fallback — never id()
            )
            _summary = _art.get("kind") or _art.get("summary") or None
            all_artifacts.append(MemoryRef(tier="episodic", key=str(_ref_key), summary=_summary))
        artifact_count = len(all_artifacts)

        budget_pct = (
            int(100 * state.budget.spent_usd / state.budget.budget_usd)
            if state.budget.budget_usd > 0 else 0
        )

        # ------------------------------------------------------------------ #
        # Build the rationale body.
        # Fleet path: section-per-repo, ordered deterministically by repo_id.
        # Non-fleet path: existing squad-grouped block (unchanged behaviour).
        # ------------------------------------------------------------------ #
        if _is_fleet_run:
            # Per-repo sections — sorted by repo_id for determinism.
            # For each repo: status of its tasks, outcome summary, per-repo
            # dissents (verbatim, tagged).  Cancelled repos noted explicitly.
            repo_section_lines: list[str] = []
            _any_repo_bad = False  # failed/surfaced/cancelled -> sealed=False

            for repo_id in sorted(distinct_task_repos):
                task_ids_for_repo = _repo_to_task_ids.get(repo_id, [])
                # Aggregate repo status: worst-case across its tasks.
                # Priority: surfaced > failed > cancelled > running > done > pending.
                _status_priority = {
                    "surfaced": 5, "failed": 4, "cancelled": 3,
                    "running": 2, "done": 1, "pending": 0,
                }
                repo_statuses = [
                    _task_id_to_status.get(tid, "pending") for tid in task_ids_for_repo
                ]
                repo_status = max(
                    repo_statuses,
                    key=lambda s: _status_priority.get(s, 0),
                    default="unknown",
                )
                if repo_status in ("failed", "surfaced", "cancelled"):
                    _any_repo_bad = True

                # Per-repo dissents: filter global dissents by repo tag.
                repo_dissent_lines = [
                    d for d in dissents if f"[repo:{repo_id}]" in d
                ]
                # Per-repo envelopes: correlate via _task_id tag.
                repo_env_count = sum(
                    1 for env in state.envelopes
                    if _task_id_to_repo.get(env.get("_task_id") or "", "") == repo_id
                    and (env.get("origin_squad") or "hydra") != "hydra"
                )

                section = [f"  ── repo: {repo_id} ──"]
                section.append(f"     status: {repo_status}")
                section.append(f"     envelopes: {repo_env_count}")
                if repo_status == "cancelled":
                    section.append("     CANCELLED: task did not dispatch (fleet was cancelled before this repo's worker started)")
                if repo_dissent_lines:
                    section.append("     dissents (verbatim):")
                    for dline in repo_dissent_lines:
                        section.append(f"       {dline}")
                repo_section_lines.extend(section)

            repo_block = "\n".join(repo_section_lines)

            # sealed=False if any repo bad OR mutually-exclusive verdicts.
            if _any_repo_bad:
                has_mutually_exclusive = True  # reuse sealed gate

            rationale_lines: list[str] = [
                f"Council: {cathedral_roster}.",
                f"Plaza slugs: {state.selected_squads}.",
                f"Fleet run: {len(distinct_task_repos)} repos dispatched in parallel.",
                f"",
                f"Per-repo results (sorted by repo_id):",
                repo_block,
                f"",
                f"Tasks: {[t.status for t in state.tasks]}.",
                f"Artifacts: {artifact_count} archived.",
                f"Budget: ${state.budget.spent_usd:.2f} of ${state.budget.budget_usd:.2f} "
                f"({budget_pct}% used; "
                f"${state.budget.usd_remaining:.2f} headroom).",
            ]
            if hitl_count > 0:
                rationale_lines.append(f"HITL: {hitl_count} gate(s) fired during workflow.")
            if has_mutually_exclusive:
                rationale_lines.append(
                    "CONFLICT or PARTIAL: at least one repo failed/surfaced/cancelled — "
                    "sealed=False, operator must reconcile before downstream consumers act."
                )
        else:
            # Non-fleet: existing squad-grouped breakdown (unchanged behaviour).
            squad_lines = []
            for squad, envs in sorted(squad_to_envs.items()):
                squad_lines.append(
                    f"  • {crown_label_for_squad(squad)} ({squad}): "
                    f"{len(envs)} envelope(s)"
                )
            squad_block = "\n".join(squad_lines) if squad_lines else "  (no squad envelopes)"

            rationale_lines = [
                f"Council: {cathedral_roster}.",
                f"Plaza slugs: {state.selected_squads}.",
                f"",
                f"Squad outputs:",
                squad_block,
                f"",
                f"Tasks: {[t.status for t in state.tasks]}.",
                f"Artifacts: {artifact_count} archived.",
                f"Budget: ${state.budget.spent_usd:.2f} of ${state.budget.budget_usd:.2f} "
                f"({budget_pct}% used; "
                f"${state.budget.usd_remaining:.2f} headroom).",
            ]
            if hitl_count > 0:
                rationale_lines.append(f"HITL: {hitl_count} gate(s) fired during workflow.")
            if has_mutually_exclusive:
                rationale_lines.append(
                    "CONFLICT: mutually-exclusive verdicts detected — sealed=False, "
                    "operator must reconcile before downstream consumers act on this record."
                )

        # Decision line: be honest about completeness.
        if has_mutually_exclusive:
            decision_line = f"Workflow synthesis for: {state.root_goal} (UNSEALED — operator reconciliation required)"
        elif dissents:
            decision_line = f"Workflow synthesis for: {state.root_goal} (with {len(dissents)} preserved dissent(s))"
        else:
            decision_line = f"Workflow synthesis for: {state.root_goal}"

        record = DecisionRecord(
            workflow_id=state.workflow_id,
            origin_squad="hydra",
            target_squad="human",
            decision=decision_line,
            rationale="\n".join(rationale_lines),
            dissenting_opinions=dissents,
            artifacts=all_artifacts,  # Fix 2: every artifact, no drops
            sealed=not has_mutually_exclusive,
        )
        record_dict = record.model_dump(mode="json")
        eights.envelope_record(record_dict)
        return {
            "envelopes": [record_dict],
            "phase": "judge_synthesis",
        }

    def node_judge_synthesis(state: HydraState) -> dict:
        """Judge the final Cathedral artifact before postcheck.

        Always cross_vendor + synthesis-coherence@1 (+ constitution-alignment@1).
        On a `fail` outcome for a high-severity rubric, set pending_hitl with
        reason=policy_breach so /hydra:approve can intervene.
        """
        state.phase = "judge_synthesis"
        record_env = next(
            (e for e in reversed(state.envelopes)
             if e.get("type") == "DECISION_RECORD"),
            None,
        )
        if record_env is None:
            return {"phase": "postcheck"}
        verdicts = _judge_envelope(state, record_env, is_post_synthesis=True)

        # HITL escalation: any fail on a high-severity rubric surfaces.
        breach = next(
            (v for v in verdicts
             if v.get("outcome") == "fail"
             and judge_policy.is_hitl_severity(v.get("rubric_id", ""))),
            None,
        )
        out: dict[str, Any] = {"verdicts": verdicts, "phase": "postcheck"}
        if breach:
            hitl = HITLRequest(
                workflow_id=state.workflow_id,
                origin_squad="hydra-judge",
                target_squad="human",
                reason="policy_breach",
                summary=(
                    f"Judge verdict FAIL on rubric {breach.get('rubric_id')}. "
                    f"Critique: {(breach.get('critique_md') or '')[:240]}"
                ),
                options=["approve_override", "reject", "send_back_for_revision"],
                default_option="reject",
            )
            hitl_dict = hitl.model_dump(mode="json")
            hitl_dict["gate_node"] = "judge_synthesis"  # C2 dedupe key half
            eights.hitl_request(hitl_dict, gate_node="judge_synthesis")
            out["pending_hitl"] = hitl_dict
            out["phase"] = "surfaced"
            emit_trace(judge_trace_root, state.workflow_id, "judge.hitl_escalation", {
                "rubric_id": breach.get("rubric_id"),
                "envelope_id": breach.get("target_envelope_id"),
            })
        return out

    def node_postcheck(state: HydraState) -> dict:
        _emit_node_context(state, "postcheck")
        verdict = enforce_governance(state, packs, constitution=constitution)
        if verdict.surfaced:
            state.phase = "surfaced"
            # B7 — release pp-harness locks for any open runs this workflow
            # started. On a clean "done" path we intentionally leave the
            # entries in place (pp owns those runs from start_run onward),
            # but on a surface the workflow has explicitly failed and any
            # outstanding pp run is now an orphaned lock — drain it.
            if state.open_pp_runs:
                try:
                    from .squad_node import abort_open_pp_runs
                    drained = abort_open_pp_runs(
                        state, dispatcher, reason=f"hydra_surface:{verdict.reason}"
                    )
                    emit_trace(
                        judge_trace_root,
                        state.workflow_id,
                        "supervisor.pp_runs_aborted",
                        {
                            "count_drained": len(drained),
                            "count_remaining": len(state.open_pp_runs),
                            "surface_reason": verdict.reason,
                        },
                    )
                    # WS3d — safe salvage: if any entries remain undrained, surface
                    # an operator HITL listing the run_ids/project_paths that still
                    # hold locks. NEVER auto force_unlock — that is an operator action.
                    if state.open_pp_runs:
                        _undrained_ids = [e.get("run_id", "?") for e in state.open_pp_runs]
                        _undrained_paths = [e.get("project_path", "?") for e in state.open_pp_runs]
                        _lock_hitl: dict[str, Any] = {
                            "workflow_id": str(state.workflow_id),
                            "reason": "lock_release_pending",
                            "gate_node": "postcheck",
                            "summary": (
                                f"{len(state.open_pp_runs)} pp run(s) could not be finalized "
                                f"during abort. Locks may still be held. "
                                f"run_ids={_undrained_ids}; "
                                f"project_paths={_undrained_paths}. "
                                "Use `pp_harness.force_unlock` manually to release each lock."
                            ),
                            "options": ["acknowledge"],
                            "default_option": "acknowledge",
                            "undrained_run_ids": _undrained_ids,
                            "undrained_project_paths": _undrained_paths,
                        }
                        eights.hitl_request(_lock_hitl, gate_node="postcheck")
                        # Fix 5: lock_release_pending MUST be the active gate so
                        # the operator actually sees it and can act on the orphaned
                        # locks. If another gate is already pending, preserve it in
                        # the lock_hitl metadata so nothing is lost, then overwrite.
                        if state.pending_hitl:
                            _lock_hitl["prior_gate"] = state.pending_hitl
                        state.pending_hitl = _lock_hitl
                        emit_trace(
                            judge_trace_root,
                            state.workflow_id,
                            "supervisor.pp_runs_lock_pending",
                            {
                                "undrained": _undrained_ids,
                                "paths": _undrained_paths,
                            },
                        )
                except Exception as e:  # noqa: BLE001 — never mask the original surface
                    emit_trace(
                        judge_trace_root,
                        state.workflow_id,
                        "supervisor.pp_runs_abort_failed",
                        {"error": repr(e), "surface_reason": verdict.reason},
                    )
        elif state.phase != "surfaced":
            # Only advance to "done" if the workflow was not already surfaced
            # before postcheck ran (e.g. intake rejected an unknown --repo and
            # routed here via the after_intake "halt" edge).  Clobbering an
            # already-surfaced phase would hide the pending_hitl and report
            # the workflow as successful.
            state.phase = "done"

        # Flush tool usage analytics to disk at workflow end.
        try:
            flushed = tool_tracker.flush_to_file(
                analytics_path(Path(project_root) if project_root else Path.cwd())
            )
            if flushed:
                report = tool_tracker.report(str(state.workflow_id))
                emit_trace(judge_trace_root, state.workflow_id, "tool_usage_report", {
                    "total_calls": report.total_calls,
                    "unique_tools": report.unique_tools,
                    "calls_by_server": report.calls_by_server,
                    "declared_but_unused": report.declared_but_unused[:10],
                    "used_but_undeclared": report.used_but_undeclared[:10],
                    "recommendations": report.recommendations,
                })
        except Exception:
            pass

        out: dict[str, Any] = {
            "phase": state.phase,
            "last_event": verdict.reason,
            "open_pp_runs": state.open_pp_runs,
            "budget_downgrade_active": state.budget_downgrade_active,
        }
        if state.pending_hitl is not None:
            out["pending_hitl"] = state.pending_hitl
        return out

    # ----- routing edges -----

    def after_intake(state: HydraState) -> str:
        """Route to postcheck (halt) when intake surfaced — e.g. bad --repo arg.
        Postcheck handles pending_hitl and reaches END cleanly via after_postcheck.
        """
        return "halt" if state.phase == "surfaced" else "planner"

    def after_planner(state: HydraState) -> str:
        return "approval" if state.requires_human_approval else "dispatch"

    def after_dispatch(state: HydraState) -> str:
        """Fix 2d — if dispatch surfaced a budget HITL, route to postcheck
        (halt) rather than proceeding to judge_per_squad / reflexion.
        Mirrors the after_intake halt pattern.
        """
        return "halt" if state.phase == "surfaced" else "judge_per_squad"

    def after_judge_per_squad(state: HydraState) -> str:
        """Fix 2b — if judge_per_squad surfaced a budget HITL (reflexion
        budget block), route to postcheck (halt) rather than synthesis.
        Without this edge, synthesis runs unconditionally even after
        node_judge_per_squad sets phase='surfaced' via _reflexion_retry.
        """
        return "halt" if state.phase == "surfaced" else "synthesis"

    def after_postcheck(state: HydraState) -> str:
        return END

    # ----- assemble graph -----

    if force_pure_python or not _HAS_LANGGRAPH:
        return _PurePythonRunner(
            packs=packs,
            steps=[
                ("intake", node_intake),
                ("planner", node_planner),
                ("approval", node_approval),
                ("dispatch", node_dispatch),
                ("judge_per_squad", node_judge_per_squad),
                ("synthesis", node_synthesis),
                ("judge_synthesis", node_judge_synthesis),
                ("postcheck", node_postcheck),
            ],
        )

    graph = StateGraph(HydraState)
    graph.add_node("intake", node_intake)
    graph.add_node("planner", node_planner)
    graph.add_node("approval", node_approval)
    graph.add_node("dispatch", node_dispatch)
    graph.add_node("judge_per_squad", node_judge_per_squad)
    graph.add_node("synthesis", node_synthesis)
    graph.add_node("judge_synthesis", node_judge_synthesis)
    graph.add_node("postcheck", node_postcheck)

    graph.set_entry_point("intake")
    graph.add_conditional_edges("intake", after_intake, {
        "planner": "planner",
        "halt": "postcheck",
    })
    graph.add_conditional_edges("planner", after_planner, {
        "approval": "approval",
        "dispatch": "dispatch",
    })
    graph.add_edge("approval", "dispatch")
    # Fix 2d: conditional edge from dispatch so a budget surface routes to
    # postcheck instead of proceeding to judge_per_squad / reflexion.
    graph.add_conditional_edges("dispatch", after_dispatch, {
        "judge_per_squad": "judge_per_squad",
        "halt": "postcheck",
    })
    # Fix 2b: conditional edge from judge_per_squad — when reflexion budget
    # block sets phase='surfaced', route to postcheck (halt) not synthesis.
    graph.add_conditional_edges("judge_per_squad", after_judge_per_squad, {
        "synthesis": "synthesis",
        "halt": "postcheck",
    })
    graph.add_edge("synthesis", "judge_synthesis")
    graph.add_edge("judge_synthesis", "postcheck")
    graph.add_conditional_edges("postcheck", after_postcheck, {END: END})

    # HYDRA_CHECKPOINT_DB override (C2): keeps the supervisor, the hydra_memory
    # read tools, and `hydra resume` pointed at the same store — and lets tests
    # run against a hermetic temp DB.
    _env_cp = os.environ.get("HYDRA_CHECKPOINT_DB")
    cp_path = checkpoint_path or (Path(_env_cp) if _env_cp else (Path.home() / ".hydra" / "checkpoints.db"))
    cp_path.parent.mkdir(parents=True, exist_ok=True)
    import sqlite3
    conn = sqlite3.connect(str(cp_path), check_same_thread=False)
    checkpointer = SqliteSaver(conn)

    return graph.compile(
        checkpointer=checkpointer,
        interrupt_before=["approval", "synthesis", "judge_synthesis"],
    )


def _reducer_channels() -> tuple[set[str], set[str]]:
    """Channels on HydraState that carry a LangGraph reducer annotation.

    Returns (append_channels, merge_dict_channels). Everything else is
    replace-by-default — including `selected_squads` (operator force-select
    must be replaceable) and `open_pp_runs` (must be able to shrink during
    drain; see state.py).
    """
    from .state import _append as _append_fn, _merge_dict as _merge_fn
    append_ch: set[str] = set()
    merge_ch: set[str] = set()
    for fname, field in HydraState.model_fields.items():
        for meta in getattr(field, "metadata", ()):
            if meta is _append_fn:
                append_ch.add(fname)
            elif meta is _merge_fn:
                merge_ch.add(fname)
    return append_ch, merge_ch


class _PurePythonRunner:
    """Fallback runner when LangGraph is not installed. Step-by-step execution,
    in-memory state. Useful for tests and the bootstrap dev loop."""
    def __init__(self, packs, steps):
        self.packs = packs
        self.steps = steps

    def invoke(self, initial: HydraState, *, stop_before: str | None = None) -> HydraState:
        # Mirror LangGraph channel semantics: append/merge ONLY where the
        # state model declares that reducer; otherwise replace. The previous
        # blanket list-append duplicated replace-channels like
        # `selected_squads` whenever a node both mutated state in place and
        # returned the same value in its patch.
        append_ch, merge_ch = _reducer_channels()
        s = initial
        for name, fn in self.steps:
            if stop_before == name:
                return s
            # The compiled-graph version skips `approval` unless planner set
            # requires_human_approval; mirror that here so the fallback runner
            # can reach later nodes.
            if name == "approval" and not s.requires_human_approval:
                continue
            patch = fn(s) or {}
            for k, v in patch.items():
                if hasattr(s, k):
                    cur = getattr(s, k)
                    if k in append_ch and isinstance(cur, list) and isinstance(v, list):
                        setattr(s, k, [*cur, *v])
                    elif k in merge_ch and isinstance(cur, dict) and isinstance(v, dict):
                        setattr(s, k, {**cur, **v})
                    else:
                        setattr(s, k, v)
            if s.phase in ("done", "surfaced"):
                return s
        return s
