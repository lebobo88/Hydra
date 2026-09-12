"""Pure plan-rigor classifier.

P3 of the planning-phase substrate: classifies a plan into trivial / standard
/ major so a later phase can scale gate strictness — mirroring pair-programmer's
`heuristicTriage` (see `mcp_servers/pp_harness` / `triage_request`), but scoped
to Hydra's 14-squad envelope decomposition rather than a single code request.

This module is PURE by hard requirement, not style: `HydraState.plan_rigor`
is recomputed on `/hydra:replay`, so any impurity here (MCP calls, clock
reads, randomness, filesystem, env vars beyond the module constants below)
makes replay non-deterministic. Do NOT call pair-programmer's
`triage_request` from here — that is an MCP round-trip, breaks the
`HYDRA_TEST_NO_DAEMONS=1` hermeticity the suite depends on, is scoped to code
requests rather than a 14-squad decomposition, and is not replay-deterministic.

Do NOT import `hydra_core.dispatcher` or `hydra_core.host_bridge` here — see
`tests/test_plan_triage.py::test_module_imports_no_dispatcher_or_host_bridge`.
"""
from __future__ import annotations

from typing import Any, Literal, Sequence

from .escalation import goal_escalates

Rigor = Literal["trivial", "standard", "major"]

# Module constants -- deliberately not env-backed (env reads would break
# purity/determinism on replay across environments).
_MAJOR_BUDGET_USD = 100.0
_TRIVIAL_BUDGET_USD = 10.0
_TRIVIAL_GOAL_CHARS = 200
# Budget floor above which a squad-level hitl_required gate alone escalates
# a plan to major (a governed action worth real money, not a toy run).
_HITL_GATE_BUDGET_FLOOR_USD = 25.0


def _squad_has_hitl_gate(pack: Any) -> bool:
    """True if a squad pack declares an unconditional hitl_required gate.

    Mirrors the `_squad_has_hitl_gate` / `_task_is_high_risk` predicate in
    `hydra_core.supervisor.node_planner` (stub packs never gate).
    """
    if pack is None:
        return False
    if getattr(pack, "entrypoint", None) == "stub":
        return False
    gates = getattr(pack, "gates", ()) or ()
    return any(getattr(g, "hitl_required", False) for g in gates)


def triage_plan(
    *,
    goal: str,
    selected_squads: list[str],
    task_priorities: Sequence[Literal["P0", "P1", "P2", "P3"]],
    budget_usd: float,
    risk_tolerance: str,
    packs: dict[str, Any],
    repo_count: int = 1,
) -> tuple[Rigor, str]:
    """Classify a plan's rigor level.

    Args:
        goal: the operator's root goal text.
        selected_squads: squad slugs routed for this plan.
        task_priorities: the priority ("P0".."P3") of every task in the
            plan's full logical task set (pre-seeded + synthesised).
        budget_usd: the workflow's budget cap.
        risk_tolerance: "low" | "medium" | "high".
        packs: squad-slug -> SquadPack registry (as discovered by
            `hydra_core.squad_loader.discover_squads`), used to check for an
            unconditional `hitl_required` gate on a selected squad.
        repo_count: distinct target repos for this plan (>=2 means fleet).

    Returns:
        (rigor, reason) -- `reason` names the single deciding signal, so a
        later plan artifact can show *why* a plan was classified.
    """
    n_squads = len(selected_squads)
    has_p0 = "P0" in task_priorities
    has_p0_or_p1 = any(p in ("P0", "P1") for p in task_priorities)
    escalates = goal_escalates(goal)
    any_hitl_gate = any(_squad_has_hitl_gate(packs.get(s)) for s in selected_squads)

    # --- major: any one trigger is sufficient ---
    if n_squads >= 3:
        return "major", f"{n_squads} selected squads (>= 3)"
    if has_p0:
        return "major", "a task in the plan is priority P0"
    if repo_count >= 2:
        return "major", f"fleet dispatch across {repo_count} repos (>= 2)"
    if budget_usd >= _MAJOR_BUDGET_USD:
        return "major", f"budget_usd {budget_usd} >= {_MAJOR_BUDGET_USD}"
    if any_hitl_gate and budget_usd >= _HITL_GATE_BUDGET_FLOOR_USD:
        return "major", (
            "a selected squad's pack declares a hitl_required gate and "
            f"budget_usd {budget_usd} >= {_HITL_GATE_BUDGET_FLOOR_USD}"
        )
    if escalates:
        return "major", "goal text matches an escalation keyword"
    if risk_tolerance == "high":
        return "major", "risk_tolerance is high"

    # --- trivial: every condition must hold ---
    if (
        n_squads == 1
        and not has_p0_or_p1
        and budget_usd < _TRIVIAL_BUDGET_USD
        and not any_hitl_gate
        and len(goal) < _TRIVIAL_GOAL_CHARS
        and not escalates
    ):
        return "trivial", (
            "exactly one selected squad, no P0/P1 task, budget_usd "
            f"{budget_usd} < {_TRIVIAL_BUDGET_USD}, no hitl_required gate, "
            f"goal under {_TRIVIAL_GOAL_CHARS} chars, no escalation keyword"
        )

    return "standard", "no major trigger fired and not all trivial conditions held"
