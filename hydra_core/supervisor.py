"""LangGraph supervisor graph for Hydra.

Phase machine:
    intake → planning → approval(?) → dispatch → executing → synthesis → postcheck → done

`interrupt_before` is set on approval, dispatch (when high-risk), and synthesis,
so HITL via Claude Code's `/hydra:approve <workflow_id>` resumes the run.

The graph is built lazily — discovers squads from the registry and adds one
squad-node per pack so the graph is self-describing.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Optional
from uuid import uuid4

from .eights.attestation import EightsAttestor
from .governance import enforce_governance, GovernanceVerdict
from .heads import cathedral_name, crown_label_for_squad, heads_in_crown
from .immortal_head import load_constitution
from .judge import dispatch_judge, route_judge, load_policy
from .judge.dispatcher import CritiqueClient, NoOpCritiqueClient
from .judge.reflexion import MAX_RETRY_INDEX, package_retry
from .judge.schemas import JudgeVerdict
from .router import classify_intent
from .telemetry import emit as emit_trace
from .venom import load_cerberus_venoms
from .schemas import (
    CSuiteDecisionPacket,
    DecisionRecord,
    HITLRequest,
    HydraEnvelope,
    ProposedTask,
)
from .squad_loader import SquadPack, discover_squads
from .squad_node import Dispatcher, execute_squad
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
    # daemon is not registered in `.mcp.json` (it usually isn't yet).
    eights = EightsAttestor(dispatcher=dispatcher)

    # Cerberus' venom registry is hydrated from cerberus.yaml at boot.
    # Capabilities not pre-registered raise VenomUnregistered when invoked
    # through `require_cerberus_pass`, so a missing file means *no* venom
    # is callable — the safe default per the manifesto.
    load_cerberus_venoms(project_root)

    # ----- node implementations -----

    def node_intake(state: HydraState) -> dict:
        state.phase = "intake"
        state.bump_iteration()
        # Route the goal text
        decision = classify_intent(
            state.root_goal,
            packs,
            industries=tuple(getattr(state.budget, "industries", []) or []),
            classify_callable=classify_callable,
        )
        state.selected_squads = decision.squads
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
        # If executive squad is in play, ask it to decompose. Otherwise build
        # a flat task list one-per-selected-squad.
        new_tasks: list[TaskState] = []
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
            new_tasks.append(TaskState(
                owner_squad="executive",
                description="Decompose goal + budget split",
                envelope_id=packet.id,
            ))
        else:
            for s in state.selected_squads:
                new_tasks.append(TaskState(owner_squad=s, description=state.root_goal))

        high_risk = any(
            packs[s].entrypoint != "stub" and any(g.hitl_required for g in packs[s].gates)
            for s in state.selected_squads if s in packs
        )
        state.requires_human_approval = high_risk or state.is_over_budget()

        return {
            "tasks": new_tasks,
            "envelopes": new_envelopes,
            "requires_human_approval": state.requires_human_approval,
            "phase": "approval" if state.requires_human_approval else "dispatch",
        }

    def node_approval(state: HydraState) -> dict:
        # Render HITL request; LangGraph will `interrupt_before` and surface
        # the request to the operator via `/hydra:status`.
        hitl = HITLRequest(
            workflow_id=state.workflow_id,
            origin_squad="hydra",
            target_squad="human",
            reason="high_risk",
            summary=f"Approve dispatch of: {state.selected_squads} for goal: {state.root_goal}",
            options=["approve", "reject", "modify-budget"],
            default_option="reject",
        )
        return {
            "pending_hitl": hitl.model_dump(mode="json"),
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
            payload = CSuiteDecisionPacket(
                workflow_id=state.workflow_id,
                origin_squad="hydra",
                target_squad=pack.slug,
                origin="BOARDROOM",
                # Vary the objective slightly per candidate so a deterministic
                # squad still produces N traceable artifacts. Real diversity
                # comes from the underlying responder's temperature/seed.
                objective=f"{task.description}\n\n[bon-candidate {i+1}/{n}]",
            )
            try:
                result = execute_squad(state, pack, payload, dispatcher)
            except Exception as e:
                emit_trace(judge_trace_root, state.workflow_id, "judge.bon_candidate_error", {
                    "candidate": i, "error": str(e),
                })
                continue
            artifacts.extend(result.artifacts)
            # Use the first envelope as the candidate the judge will score.
            if not result.envelopes:
                continue
            primary = result.envelopes[0].model_dump(mode="json")
            primary["_bon_candidate_index"] = i
            if result.host_pickup_pending:
                primary["_host_pickup_pending"] = True
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
        elif pack.slug == "creative":
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
        # Preemptive envelope-ceiling guard. The Claude Code sub-agent that
        # hosts this supervisor is one-shot-per-turn; if the planner emitted
        # too many envelopes for a single dispatch round, surface to HITL
        # before the dispatch loop burns the remaining context. The operator
        # then re-spawns the supervisor per phase_batch (planner contract) or
        # uses direct parallel Agent() fanout.
        if state.is_over_envelope_ceiling():
            state.phase = "surfaced"
            state.pending_hitl = {
                "reason": "envelope_ceiling",
                "remediation": (
                    "Split phase across multiple supervisor invocations "
                    "(planner phase_batch_index) or use parallel Agent() dispatch."
                ),
                "envelope_count": len(state.envelopes),
                "envelope_ceiling": state.envelope_ceiling,
            }
            emit_trace(
                state.workflow_id,
                "supervisor.envelope_ceiling_surface",
                {"count": len(state.envelopes), "ceiling": state.envelope_ceiling},
            )
            return {}
        state.phase = "executing"
        # For each pending task, drive the squad. The dispatcher is responsible
        # for actually invoking MCP / skills / subprocesses.
        new_decisions: list[dict] = []
        artifacts: list[dict] = []
        bon_verdicts: list[dict] = []
        for task in list(state.tasks):
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
                if winners:
                    new_decisions.extend(winners)
                    task.status = "done"
                else:
                    task.status = "failed"
                continue

            # Standard single-shot dispatch.
            payload = CSuiteDecisionPacket(
                workflow_id=state.workflow_id,
                origin_squad="hydra",
                target_squad=pack.slug,
                origin="BOARDROOM",
                objective=task.description,
            )
            try:
                result = execute_squad(state, pack, payload, dispatcher)
            except Exception as e:
                task.status = "failed"
                state.error_counters[task.owner_squad] = (
                    state.error_counters.get(task.owner_squad, 0) + 1
                )
                continue
            task.status = result.status
            # Propagate host-pickup-pending onto each dumped envelope so the
            # judge node can skip placeholder artifacts that have no substance
            # to score (Claude Code subagent will fulfil out of band).
            for produced in result.envelopes:
                d = produced.model_dump(mode="json")
                if result.host_pickup_pending:
                    d["_host_pickup_pending"] = True
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
        packet = package_retry(
            original_env, verdict_obj, prior_retry_index=prior_retry_index
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
        retry_envelope = CSuiteDecisionPacket(
            workflow_id=state.workflow_id,
            origin_squad="hydra",
            target_squad=pack.slug,
            origin="BOARDROOM",
            objective=retry_obj,
            parent_id=original_env.get("id"),
        )

        try:
            result = execute_squad(state, pack, retry_envelope, dispatcher)
        except Exception as e:
            emit_trace(judge_trace_root, state.workflow_id, "judge.reflexion_error", {
                "origin": origin, "error": str(e),
            })
            return [], []

        new_env_dicts: list[dict] = []
        for produced in result.envelopes:
            d = produced.model_dump(mode="json")
            # Tag the envelope with its retry depth so we don't loop on it.
            d["_retry_index"] = packet.retry_index
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

        Three responsibilities:
          1. Score each unjudged envelope against its rubrics.
          2. On `revise` (Phase 3): package_retry → re-dispatch source squad
             once, then re-judge. Bounded by Reflexion ×1.
          3. On `fail` with a HITL-severity rubric: surface to HITL.
        """
        state.phase = "judge_per_squad"
        already_judged = {v.get("target_envelope_id") for v in state.verdicts}
        new_verdicts: list[dict] = []
        retry_envelopes: list[dict] = []
        retry_verdicts: list[dict] = []
        breach: dict | None = None

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
            if prior_retry >= MAX_RETRY_INDEX:
                continue
            r_envs, r_verdicts = _reflexion_retry(state, env, revise, prior_retry)
            retry_envelopes.extend(r_envs)
            retry_verdicts.extend(r_verdicts)

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
            eights.hitl_request(hitl_dict)
            out["pending_hitl"] = hitl_dict
            out["phase"] = "surfaced"
            emit_trace(judge_trace_root, state.workflow_id, "judge.hitl_escalation", {
                "stage": "per_squad",
                "rubric_id": breach.get("rubric_id"),
                "envelope_id": breach.get("target_envelope_id"),
            })
        return out

    def node_synthesis(state: HydraState) -> dict:
        # Cathedral voice for user-facing output; plaza slugs remain in the
        # envelope's structured fields. Per the manifesto: "no head speaks
        # to the user without Hydra's synthesis."
        cathedral_roster = ", ".join(
            crown_label_for_squad(s) for s in state.selected_squads
        ) or "(no heads convened)"
        record = DecisionRecord(
            workflow_id=state.workflow_id,
            origin_squad="hydra",
            target_squad="human",
            decision=f"Workflow synthesis for: {state.root_goal}",
            rationale=(
                f"Council: {cathedral_roster}. "
                f"Plaza slugs: {state.selected_squads}. "
                f"Tasks: {[t.status for t in state.tasks]}. "
                f"Budget spent ${state.budget.spent_usd:.2f} of ${state.budget.budget_usd:.2f}."
            ),
            artifacts=[],
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
            eights.hitl_request(hitl_dict)
            out["pending_hitl"] = hitl_dict
            out["phase"] = "surfaced"
            emit_trace(judge_trace_root, state.workflow_id, "judge.hitl_escalation", {
                "rubric_id": breach.get("rubric_id"),
                "envelope_id": breach.get("target_envelope_id"),
            })
        return out

    def node_postcheck(state: HydraState) -> dict:
        verdict = enforce_governance(state, packs, constitution=constitution)
        if verdict.surfaced:
            state.phase = "surfaced"
        else:
            state.phase = "done"
        return {
            "phase": state.phase,
            "last_event": verdict.reason,
        }

    # ----- routing edges -----

    def after_planner(state: HydraState) -> str:
        return "approval" if state.requires_human_approval else "dispatch"

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
    graph.add_edge("intake", "planner")
    graph.add_conditional_edges("planner", after_planner, {
        "approval": "approval",
        "dispatch": "dispatch",
    })
    graph.add_edge("approval", "dispatch")
    graph.add_edge("dispatch", "judge_per_squad")
    graph.add_edge("judge_per_squad", "synthesis")
    graph.add_edge("synthesis", "judge_synthesis")
    graph.add_edge("judge_synthesis", "postcheck")
    graph.add_conditional_edges("postcheck", after_postcheck, {END: END})

    cp_path = checkpoint_path or (Path.home() / ".hydra" / "checkpoints.db")
    cp_path.parent.mkdir(parents=True, exist_ok=True)
    import sqlite3
    conn = sqlite3.connect(str(cp_path), check_same_thread=False)
    checkpointer = SqliteSaver(conn)

    return graph.compile(
        checkpointer=checkpointer,
        interrupt_before=["approval", "synthesis", "judge_synthesis"],
    )


class _PurePythonRunner:
    """Fallback runner when LangGraph is not installed. Step-by-step execution,
    in-memory state. Useful for tests and the bootstrap dev loop."""
    def __init__(self, packs, steps):
        self.packs = packs
        self.steps = steps

    def invoke(self, initial: HydraState, *, stop_before: str | None = None) -> HydraState:
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
                    if isinstance(cur, list) and isinstance(v, list):
                        setattr(s, k, [*cur, *v])
                    elif isinstance(cur, dict) and isinstance(v, dict):
                        setattr(s, k, {**cur, **v})
                    else:
                        setattr(s, k, v)
            if s.phase in ("done", "surfaced"):
                return s
        return s
