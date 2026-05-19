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

from .governance import enforce_governance, GovernanceVerdict
from .heads import cathedral_name, crown_label_for_squad, heads_in_crown
from .immortal_head import load_constitution
from .router import classify_intent
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
        return {
            "selected_squads": decision.squads,
            "phase": "planning",
            "last_event": state.last_event,
            "iteration_count": state.iteration_count,
        }

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

    def node_dispatch(state: HydraState) -> dict:
        state.phase = "executing"
        # For each pending task, drive the squad. The dispatcher is responsible
        # for actually invoking MCP / skills / subprocesses.
        new_decisions: list[dict] = []
        artifacts: list[dict] = []
        for task in list(state.tasks):
            if task.status != "pending":
                continue
            pack = packs.get(task.owner_squad)
            if pack is None:
                task.status = "failed"
                continue
            # Build a minimal envelope from the goal — supervisor stages will
            # produce more specific envelopes later in the lifecycle.
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
            new_decisions.extend(e.model_dump(mode="json") for e in result.envelopes)
            artifacts.extend(result.artifacts)
        return {
            "envelopes": new_decisions,
            "artifacts": artifacts,
            "phase": "synthesis",
        }

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
        return {
            "envelopes": [record.model_dump(mode="json")],
            "phase": "postcheck",
        }

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

    if not _HAS_LANGGRAPH:
        return _PurePythonRunner(
            packs=packs,
            steps=[
                ("intake", node_intake),
                ("planner", node_planner),
                ("approval", node_approval),
                ("dispatch", node_dispatch),
                ("synthesis", node_synthesis),
                ("postcheck", node_postcheck),
            ],
        )

    graph = StateGraph(HydraState)
    graph.add_node("intake", node_intake)
    graph.add_node("planner", node_planner)
    graph.add_node("approval", node_approval)
    graph.add_node("dispatch", node_dispatch)
    graph.add_node("synthesis", node_synthesis)
    graph.add_node("postcheck", node_postcheck)

    graph.set_entry_point("intake")
    graph.add_edge("intake", "planner")
    graph.add_conditional_edges("planner", after_planner, {
        "approval": "approval",
        "dispatch": "dispatch",
    })
    graph.add_edge("approval", "dispatch")
    graph.add_edge("dispatch", "synthesis")
    graph.add_edge("synthesis", "postcheck")
    graph.add_conditional_edges("postcheck", after_postcheck, {END: END})

    cp_path = checkpoint_path or (Path.home() / ".hydra" / "checkpoints.db")
    cp_path.parent.mkdir(parents=True, exist_ok=True)
    import sqlite3
    conn = sqlite3.connect(str(cp_path), check_same_thread=False)
    checkpointer = SqliteSaver(conn)

    return graph.compile(
        checkpointer=checkpointer,
        interrupt_before=["approval", "synthesis"],
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
