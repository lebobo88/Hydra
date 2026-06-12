"""Typed state object for the Hydra supervisor graph.

LangGraph reduces state with the `Annotated[..., reducer]` pattern. We use that
for collections (tasks, messages, artifacts) and replace-by-default for scalars.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any, Literal, Optional
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


def _append(left: list, right: list) -> list:
    """LangGraph reducer: append-only for lists."""
    return [*left, *right]


def _merge_dict(left: dict, right: dict) -> dict:
    return {**left, **right}


class BudgetLedger(BaseModel):
    budget_usd: float = 50.0
    token_limit: int = 200_000
    spent_usd: float = 0.0
    spent_tokens: int = 0

    # WS8 SLICE 4 — per-repo fleet budget isolation.
    # repo_budgets: equal-split allocation per fleet repo (set by allocate_repos).
    # repo_spend:   accumulated spend per fleet repo (updated by charge_and_gate_repo).
    # These are populated only in fleet mode; empty dicts are safe for sequential runs.
    repo_budgets: dict[str, float] = Field(default_factory=dict)
    repo_spend: dict[str, float] = Field(default_factory=dict)

    @property
    def usd_remaining(self) -> float:
        return max(self.budget_usd - self.spent_usd, 0.0)

    @property
    def percent_consumed(self) -> float:
        return (self.spent_usd / self.budget_usd) if self.budget_usd else 0.0

    def allocate_repos(self, repo_ids: list[str]) -> None:
        """Equal-split the global budget_usd across distinct fleet repos.

        REPLACES any prior per-repo allocation + resets per-repo spend so a
        fresh fleet run always starts with a clean per-repo ledger.  The global
        ledger (spent_usd / spent_tokens) is NOT touched — only the per-repo
        attribution is reset.

        Guards:
          - Negative budget_usd raises ValueError — this is a misconfiguration.
          - Zero budget_usd is valid: every repo allocation is 0.0, and repo_over
            fires immediately on any spend (correct — the budget is exhausted).
          - Empty repo_ids is a no-op; neither dict is mutated.

        PRECISION CONTRACT — integer micro-unit split:
          Allocations are exact to the micro-dollar (1e-6 USD).  budget_usd is
          converted to integer micro-dollars (round(budget_usd * 1_000_000)).
          Integer divmod gives an exact micro-sum with zero accumulation error.
          Remainder micro-units are distributed one-per-repo to the FIRST `rem`
          repos (fair; still exact at micro-level).

          Contract: sum(int(r * 1e6) for r in repo_budgets.values())
                      == round(budget_usd * 1_000_000)   [exact integer equality]

          The float sum may differ from budget_usd by at most 1e-6 (one micro-
          dollar) due to the final / 1_000_000 division introducing sub-micro
          float noise.  This is the inherent precision of the micro-unit
          accounting unit and is negligible for USD-range budgets.

          A repo CAN receive 0.0 only when budget_usd is smaller than 1 micro
          per repo (i.e. budget < n / 1_000_000).  This is documented and tested;
          in practice Hydra budgets are in the dollar range.
        """
        if self.budget_usd < 0:
            raise ValueError(
                f"budget_usd must be non-negative, got {self.budget_usd}"
            )
        distinct = list(dict.fromkeys(repo_ids))  # preserve first-seen order, drop dups
        if not distinct:
            return
        n = len(distinct)
        total_micro = round(self.budget_usd * 1_000_000)
        base_micro, rem = divmod(total_micro, n)
        new_budgets: dict[str, float] = {}
        for i, rid in enumerate(distinct):
            # First `rem` repos get one extra micro-unit each.
            micro = base_micro + (1 if i < rem else 0)
            new_budgets[rid] = micro / 1_000_000

        # FULL REPLACE — wipe any stale entries from a prior call so the
        # HITL breakdown never shows repos that are no longer in the fleet,
        # and sum(repo_budgets.values()) stays == budget_usd.
        self.repo_budgets = new_budgets
        # Reset per-repo spend so the new fleet starts with a clean slate.
        # Global spent_usd is intentionally preserved (it tracks total cost
        # across the workflow lifetime, not just this fleet run).
        self.repo_spend = {}

    def repo_remaining(self, rid: str) -> float:
        """Return remaining budget for a specific repo.

        Returns math.inf when the repo has no per-repo allocation (i.e. it is
        not a fleet repo or allocate_repos was not called yet).  This matches
        the semantics used by charge_and_gate_repo: a repo with no allocation
        never triggers repo_over.
        """
        import math
        alloc = self.repo_budgets.get(rid)
        if alloc is None:
            return math.inf
        return alloc - self.repo_spend.get(rid, 0.0)


class TaskState(BaseModel):
    task_id: UUID = Field(default_factory=uuid4)
    owner_squad: str
    description: str
    # WS8 SLICE 2: "cancelled" added for fleet tasks that were not dispatched
    # because cancel_event was set before their worker started.
    status: Literal["pending", "running", "blocked", "done", "failed", "surfaced", "cancelled"] = "pending"
    envelope_id: Optional[UUID] = None      # the message that triggered it
    result_envelope_id: Optional[UUID] = None
    retries: int = 0
    priority: Literal["P0", "P1", "P2", "P3"] = "P2"
    # WS9: model_tier hint propagated from the dispatch envelope or operator flag.
    # Valid tokens: "haiku" | "sonnet" | "opus" | "fable" | "deep".
    # "fable"/"deep" route engineering work to pp's deep-reasoning-team.
    # None means "use squad default" — existing behaviour is unchanged.
    model_tier: Optional[str] = None
    # WS9: structured acceptance criteria for this task.  When a task is
    # major/high-risk and this list is absent (or empty), the planner gates
    # on HITL with reason="acceptance_criteria" before dispatch.
    acceptance_criteria: Optional[list[str]] = None
    # WS8 SLICE 1 — per-task repo targeting.
    # When set, this task is dispatched to a specific allow-listed repo (distinct
    # from the workflow-level state.target_repo_id).  node_dispatch's _build_payload
    # picks task.target_repo_id first; falls back to state.target_repo_id when None.
    # This is what makes the fleet's distinct-repo predicate work in production:
    # a campaign that targets multiple repos sets per-task target_repo_id on each
    # TaskState at planning time (or via the /hydra:run --repo <id> routing).
    # Preserved across planner rebuilds (node_planner carries existing tasks
    # through the dedup path unchanged) and retries (_reflexion_retry does not
    # overwrite this field).
    target_repo_id: Optional[str] = None


class HydraState(BaseModel):
    """Persistent supervisor-graph state. Checkpointed by LangGraph per workflow_id."""

    workflow_id: UUID = Field(default_factory=uuid4)
    tenant_id: str = "default"
    root_goal: str = ""
    target_repo_id: Optional[str] = None  # allow-listed repo_id for engineering dispatch targeting (None = workflow project_root)
    phase: Literal[
        "intake", "planning", "approval", "dispatch",
        "executing", "judge_per_squad", "synthesis", "judge_synthesis",
        "postcheck", "done", "surfaced"
    ] = "intake"

    # Routing
    selected_squads: list[str] = Field(default_factory=list)
    current_node: Optional[str] = None

    # Work
    tasks: Annotated[list[TaskState], _append] = Field(default_factory=list)
    envelopes: Annotated[list[dict[str, Any]], _append] = Field(default_factory=list)
    artifacts: Annotated[list[dict[str, Any]], _append] = Field(default_factory=list)

    # Memory handles
    episodic_refs: Annotated[list[str], _append] = Field(default_factory=list)
    semantic_queries: Annotated[list[dict[str, Any]], _append] = Field(default_factory=list)

    # Judge verdicts (cross-model second-opinions). Append-only.
    verdicts: Annotated[list[dict[str, Any]], _append] = Field(default_factory=list)

    # Governance
    budget: BudgetLedger = Field(default_factory=BudgetLedger)
    iteration_count: int = 0
    depth: int = 0
    loop_ceiling: int = 25
    depth_ceiling: int = 5
    # Preemptive in-flight ceilings (checked before damage accumulates, not
    # only at postcheck). envelope_ceiling caps total envelopes accumulated
    # in a single supervisor invocation — context exhaustion guard for the
    # Claude Code sub-agent that hosts the supervisor for one tool round.
    # mcp_failure_ceiling caps per-server consecutive _mcp_call_safe failures
    # before the run surfaces to HITL with reason=mcp_disconnect:<server>.
    envelope_ceiling: int = 30
    mcp_failure_ceiling: int = 3
    error_counters: Annotated[dict[str, int], _merge_dict] = Field(default_factory=dict)

    requires_human_approval: bool = False
    pending_hitl: Optional[dict[str, Any]] = None
    hitl_history: Annotated[list[dict[str, Any]], _append] = Field(default_factory=list)

    # WS-AUTH: operator-capability token minted by apply_approval() on each
    # HITL approval event.  Downstream dispatch nodes can present this token
    # to verify that the human operator authorised the gated action.
    # None until the first approval in this workflow.
    operator_capability: Optional[dict[str, Any]] = None

    # FS-4 — budget downgrade tripwire. Set True when spent_usd/budget_usd >= 80%
    # and dispatching is still legal (< 100%). WS9 tier-propagation consumes this
    # flag to downgrade the model tier passed to squads; this module only sets it.
    budget_downgrade_active: bool = False

    # Per-workflow Reflexion ceiling raise (R3-tail post-mortem, 2026-05-21).
    # Default 0 means "no raise — use MAX_RETRY_INDEX". Set by the operator
    # approval handler for a `reflexion_override` HITL request to the new
    # ceiling value (e.g. 2 to allow Reflexion ×2 on the next pass through
    # `node_judge_per_squad`). Scoped to this workflow only — the
    # constitutional ×1 invariant is unchanged for other workflows.
    reflexion_override_granted_until: int = 0

    # Trace
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: Optional[datetime] = None
    last_event: Optional[str] = None

    # TheEights binding (Phase 6). Populated at intake by calling
    # `eights.constitution.attest`; refusal here aborts the workflow.
    constitution_hash: Optional[str] = None
    constitution_version: Optional[str] = None
    constitution_receipt: Optional[str] = None

    # WS8 SLICE 1 — parallel fleet dispatch.
    # fleet_parallel: when True, node_dispatch fans out tasks across DISTINCT
    # target repos in parallel via hydra_core.fleet.dispatch_fleet.  Default
    # False preserves the original sequential behaviour — zero regression risk.
    # fleet_max_concurrency: per-workflow worker cap passed to dispatch_fleet.
    # Clamped to [1, FLEET_MAX_CAP=8] inside fleet.py.
    fleet_parallel: bool = False
    fleet_max_concurrency: int = 4
    # WS8 SLICE 2 — set True by node_dispatch ONLY when dispatch_fleet was
    # actually invoked this run.  node_synthesis uses this (not just the count
    # of distinct repos) to decide between fleet-synthesis and non-fleet-synthesis
    # so a sequential multi-repo run never accidentally gets fleet sections.
    fleet_dispatched: bool = False

    # B7 — pp-harness lock release on supervisor crash.
    # Tracks pp_harness runs that this workflow started but has not yet
    # finalized. `_via_mcp` registers a run after `start_run` returns
    # a run_id (lock acquired); `node_postcheck` drains the list by
    # calling `pp_harness.finalize_run(status="aborted")` on each entry
    # ONLY when the workflow surfaces (state.phase == "surfaced"). On
    # the normal "done" path entries are intentionally left in place so
    # operators have an audit trail of which pp runs this workflow
    # kicked off (pp itself owns those runs from that point forward).
    # Each entry: {"run_id": "...", "project_path": "..."}.
    # Replace-by-default (no _append reducer) so `abort_open_pp_runs`
    # can shrink the list — append semantics would defeat draining.
    open_pp_runs: list[dict[str, str]] = Field(default_factory=list)

    def bump_iteration(self) -> None:
        self.iteration_count += 1

    def is_over_budget(self) -> bool:
        return self.budget.spent_usd >= self.budget.budget_usd

    def is_looping(self) -> bool:
        return self.iteration_count >= self.loop_ceiling or self.depth >= self.depth_ceiling

    def is_over_envelope_ceiling(self) -> bool:
        return len(self.envelopes) >= self.envelope_ceiling

    def mcp_failures_for(self, server: str) -> int:
        return self.error_counters.get(f"mcp_failure:{server}", 0)

    def any_mcp_over_ceiling(self) -> tuple[bool, Optional[str]]:
        for key, count in self.error_counters.items():
            if key.startswith("mcp_failure:") and count >= self.mcp_failure_ceiling:
                return True, key.split(":", 1)[1]
        return False, None
