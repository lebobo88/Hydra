"""Typed state object for the Hydra supervisor graph.

LangGraph reduces state with the `Annotated[..., reducer]` pattern. We use that
for collections (tasks, messages, artifacts) and replace-by-default for scalars.
"""
from __future__ import annotations

import os
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

    # B8: companion counters for cost provenance. ``estimated_usd`` is the
    # portion of ``spent_usd`` that came from ``pricing.price_call`` rather
    # than a directly reported ``cost_usd`` (source="estimated" in
    # ``record_cost``) — a companion, never a substitute: ``spent_usd``
    # keeps its existing meaning and the 0.8/1.0 tripwires read it unchanged.
    # ``unmeasured_stages`` counts stages that reported neither a cost nor
    # usable tokens (source="unmeasured") — logged, never blocking.
    estimated_usd: float = 0.0
    unmeasured_stages: int = 0

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
    status: Literal["pending", "running", "blocked", "done", "failed", "surfaced", "cancelled", "deferred_to_host"] = "pending"
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
    # Optional repo-relative engineering target under target_repo_id.
    # Example: target_repo_id="mc-test", target_repo_subpath="test-5".
    # Validated by hydra_core.repo_registry.normalize_repo_subpath.
    target_repo_subpath: Optional[str] = None
    # pp team / profile selection for engineering dispatch.  The planner sets
    # these (or they ride in on a forwarded DEV_TASK) so node_dispatch's
    # _build_payload can stamp them onto the CSuiteDecisionPacket and _via_mcp
    # can route to the correct pair-programmer team/profile.  None = squad
    # default / auto-detect.  Preserved across planner rebuilds and retries.
    pp_team: Optional[str] = None
    pp_profile: Optional[str] = None
    # P0 planning substrate: task_ids this task depends on, and the Plan
    # step / revision it was materialized from (None = task predates
    # planning, or was created outside a Plan). P1 (plan_deps_satisfied,
    # below) reads ``depends_on`` to gate attended task selection, and it
    # does so UNCONDITIONALLY -- not only while a plan barrier is active
    # (see the call site in cli.py for why). This is a no-op for existing
    # workflows only because nothing in the codebase populates
    # ``depends_on`` yet; the moment a planner starts setting it, dependency
    # ordering takes effect.
    depends_on: list[str] = Field(default_factory=list)
    plan_step_id: Optional[str] = None
    plan_revision: int = 0
    # P5c: `--modify-plan` seeds a fresh "planning" task carrying the
    # operator's revision critique instead of a plan STEP -- this task has
    # no `plan_step_id`. `plan_critique` is the critique's full, untruncated
    # text (read from the `--critique-ref` file/MemoryRef by
    # `hydra_core.cli._read_plan_critique`, never routed through the
    # `_OPTION_RE`-bounded `--option` string). `supersedes_plan_envelope_id`
    # names the prior `Plan.id` this revision replaces, mirroring
    # `hydra_core.schemas.Plan.supersedes` (a UUID there; a str here since a
    # TaskState is not itself an envelope and need not round-trip through
    # envelope validation). Both additive-only Optional fields -- an
    # existing checkpoint loads fine with both None.
    plan_critique: Optional[str] = None
    supersedes_plan_envelope_id: Optional[str] = None


class HydraState(BaseModel):
    """Persistent supervisor-graph state. Checkpointed by LangGraph per workflow_id."""

    workflow_id: UUID = Field(default_factory=uuid4)
    tenant_id: str = "default"
    root_goal: str = ""
    target_repo_id: Optional[str] = None  # allow-listed repo_id for engineering dispatch targeting (None = workflow project_root)
    target_repo_subpath: Optional[str] = None  # optional repo-relative engineering subdir under target_repo_id
    # Pre-seeded multi-repo targets for the structured API path (CLI --repos /
    # hydra.workflow.plan repos=). Mirrors the goal-text --repos/--fleet token
    # but arrives directly on state instead of being folded into root_goal.
    # node_intake merges this into the fleet-wiring path and validates every
    # id against the allow-list regardless of how it arrived (WS1-B).
    target_repo_ids: list[str] = Field(default_factory=list)
    # WS1-E: where target_repo_id/target_repo_ids resolved FROM, for the
    # plan/step "resolved_target" ergonomic (operator-visible provenance) and
    # for the "checkpoint inheritance" case (a value already set on a resumed
    # checkpoint is never overwritten -- see node_intake's `if not
    # state.target_repo_source` guards). One of: "explicit_param" (structured
    # --repo/--repos CLI flag or hydra.workflow.plan/launch repo=/repos=
    # param, pre-seeded onto state before node_intake runs), "goal_text_flag"
    # (an explicit --repo/--repos token found in goal-text prose),
    # "goal_text_inferred" (MU5 conservative cue-based inference), or None
    # when no target has resolved.
    target_repo_source: Optional[str] = None
    # P3 plan-triage substrate: the operator's `--risk`/`risk=` hint (CLI
    # `hydra run --risk` / `hydra plan --risk`, or the hydra.workflow.plan
    # / hydra.workflow.launch MCP `risk` param). Previously recorded only on
    # the workflow_start/workflow_plan trace event with a comment noting
    # "there is no dedicated HydraState risk field yet" (see cli.py); this is
    # that field. Defaults to "medium" so a workflow with no operator hint
    # triages the same as before this field existed.
    risk_tolerance: Literal["low", "medium", "high"] = "medium"
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

    # F9 — resumable HITL gate node marker (fable-audit-2 Phase 2).
    # Set by the originating HITL gate to tell after_dispatch /
    # after_judge_per_squad to route to a dedicated hitl_gate_* node
    # (interrupt_before-paused) rather than to postcheck/halt.
    # Cleared by the hitl_gate node itself when the operator resumes.
    # None = legacy behaviour (route to halt on surfaced).  Additive-only
    # change: existing persisted checkpoints load fine (defaults to None).
    hitl_return_node: str | None = None

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

    # Attended (host-bridged) execution: task_ids whose engineering stage the
    # host has driven to a terminal outcome via `hydra step`/`submit-host-result`.
    # The `tasks` channel uses an _append reducer, so an out-of-graph
    # update_state cannot flip a task's status in place (it would APPEND a stale
    # duplicate). This replace-by-default list is the authoritative "don't
    # re-pick this engineering task" signal for _next_engineering_task.
    attended_completed_task_ids: list[str] = Field(default_factory=list)

    # MU15: task_ids whose attended stage finalized with final_status="complete"
    # (a subset of attended_completed_task_ids). Used by enforce_governance to
    # skip the deferred_to_host / surfaced governance checks for tasks the host
    # drove to a successful outcome. Only "complete" cursors enter this list —
    # surfaced/aborted attended outcomes intentionally stay out so governance
    # can still surface workflows whose attended tasks did not finish cleanly.
    # Replace-by-default (no _append reducer) so update_state can grow the list.
    attended_done_task_ids: list[str] = Field(default_factory=list)

    # E2-30: one record per attended task the host drove to a terminal outcome
    # (engineering stage OR claude-skill/impersonation squad cursor). This is
    # the ONLY durable trace of an attended squad's output on the checkpointed
    # state — the in-graph dispatch path never ran for these tasks, so
    # `envelopes`/`artifacts` stay empty until `hydra finalize` materialises
    # these records into squad DECISION_RECORDs + artifact rows and resumes the
    # graph at `synthesis`. Replace-by-default so out-of-graph update_state can
    # grow it (the same reason attended_completed_task_ids is not appended).
    # Shape: {task_id, owner_squad, run_id, status, artifact_ref?, summary}.
    attended_results: list[dict[str, Any]] = Field(default_factory=list)

    # E2-30 idempotency marker: the DECISION_RECORD id `hydra finalize` produced
    # for this workflow. Set once the attended synthesis leg has run; a second
    # finalize returns {status:"already_finalized"} with this id instead of
    # re-synthesizing (which would double-write episodic rows).
    attended_finalized_record_id: Optional[str] = None

    # P0 planning substrate. Plain replace-by-default fields, no reducers: a
    # planning re-run REPLACES the prior plan snapshot rather than
    # accumulating history. P1 (plan_barrier_active, below) now reads
    # ``plan_status`` to gate dispatch while a plan is
    # authoring/drafted/judged/rejected; it is a no-op for existing
    # workflows only because nothing yet drives ``plan_status`` away from
    # its default "none".
    plan_status: Literal[
        "none", "skipped", "authoring", "drafted", "judged",
        "approved", "rejected", "bypassed",
    ] = "none"
    plan_rigor: Optional[str] = None
    plan_rigor_source: Optional[str] = None
    # P3: operator override input (pre-seeded the way --squad pre-seeds
    # selected_squads, via `hydra plan --rigor` / hydra.workflow.plan
    # rigor=). node_planner reads this once to set plan_rigor/plan_rigor_source
    # ("operator_flag") instead of the computed triage value, and records a
    # hitl_history downgrade event when the override is stricter-to-looser
    # than the computed rigor. Left populated afterwards (an input value, not
    # a derived one) so replay reproduces the same override.
    plan_rigor_override: Optional[Literal["trivial", "standard", "major"]] = None
    plan_envelope_id: Optional[UUID] = None
    plan_ref: Optional[dict[str, Any]] = None
    plan_revision: int = 0
    plan_approved_at: Optional[datetime] = None
    plan_artifact_location: Optional[str] = None

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


# P1 plan-barrier predicates, defined ONCE here and imported everywhere else —
# a divergent second definition is the documented trap from the
# worktree-relocation incident.
#
# P5b: the flag gates WRITERS, not READERS -- deliberately. There are exactly
# TWO places in the engine that write `plan_status` while deciding whether the
# plan phase is even active -- i.e. that can move it OFF its default "none":
# `node_planner`'s `_plan_gate_active` check in supervisor.py (seeds
# "authoring"), and the ingest PLAN branch in `hydra_core/ingest.py` (seeds
# "drafted", gated on `_plan_phase_enabled()`). Both MUST be flag-gated --
# that is the entire safety argument this asymmetry rests on. Everything
# downstream of them (`node_plan_judge`/`node_plan_gate`, which write
# "judged"/"approved") writes unconditionally, but only ever runs because one
# of the two gated writers above already moved `plan_status` off "none" --
# an ungated writer anywhere in that pair would transitively make "judged"/
# "approved" reachable with the flag off too. (A prior revision of this
# branch shipped the ingest writer ungated; a cross-vendor judge caught it
# before merge -- see `test_p5b_plan_lifecycle.py::TestTask1AllowLists`'s
# flag-off refusal test.) `plan_barrier_active` and `plan_deps_satisfied`
# below, and every caller of them (the four selectors in cli.py,
# node_dispatch's sequential loop, `after_dispatch`), read `plan_status`
# UNCONDITIONALLY -- with no `HYDRA_PLAN_PHASE` check anywhere in the read
# path. This is intentional, not an oversight: it means flipping the flag OFF
# mid-flight can never release the barrier and let unplanned work dispatch
# out from under an in-progress plan -- the barrier, once raised, only ever
# comes down through the plan's own lifecycle (approved/rejected/bypassed),
# never through an environment variable. Reading this the other way around
# -- "the flag being off should make the barrier inert everywhere, including
# here" -- gets the safety direction backwards; see
# `test_p5b_plan_lifecycle.py`'s locked-in regression tests for the property
# this asymmetry buys.
#
# P5c adds TWO more writers, both in `hydra_core/cli.py`'s resume handler,
# and both scoped to `gate_node == "plan_gate"` rather than written
# unconditionally -- unlike the two seeding writers above, these run inside
# a handler (the resume/reject path) that also serves EVERY OTHER gate in
# the engine, so an unscoped write here would raise or move the barrier from
# an action that has nothing to do with planning:
#   * `--force-dispatch` past `plan_gate` writes `plan_status="bypassed"`.
#     `"bypassed"` is deliberately NOT a member of `_PLAN_BARRIER_STATES` --
#     it cannot raise the barrier by construction, so this write is safe even
#     if the scoping were ever dropped by accident. It is still scoped, so
#     the next reader does not have to re-derive that safety argument.
#   * `--reject` at `plan_gate` writes `plan_status="rejected"`, which IS a
#     barrier member -- unlike `bypassed`, an unscoped write here WOULD raise
#     the barrier from an ordinary rejection of ANY gate (budget, high_risk,
#     constitution, ...), with `HYDRA_PLAN_PHASE` off and no flag-gated code
#     anywhere able to ever clear it. This is exactly the total-dispatch-
#     freeze class of bug the flag exists to prevent, reachable from a
#     routine operator reject -- the scoping to `plan_gate` is load-bearing,
#     not cosmetic. See `tests/test_p5c_plan_operator_surfaces.py`.
_PLAN_BARRIER_STATES = frozenset({"authoring", "drafted", "judged", "rejected"})


def plan_max_revisions() -> int:
    """HYDRA_PLAN_MAX_REVISIONS -- the `--modify-plan` revision ceiling.

    Default 2. Shared by `node_plan_judge` (supervisor.py, which reads it to
    decide what options the plan_gate advertises) and `hydra_core.cli`'s
    `--modify-plan` handler (which enforces it before seeding another
    revision) so the ceiling is defined exactly once -- see
    `plan_revision_ceiling_reached` below for why a hand-duplicated second
    copy of the comparison itself would be worse than a hand-duplicated env
    read.
    """
    raw = os.environ.get("HYDRA_PLAN_MAX_REVISIONS", "")
    if not raw:
        return 2
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 2
    return value if value > 0 else 2


def plan_revision_ceiling_reached(plan_revision: int, max_revisions: int) -> bool:
    """True once the operator has used every `--modify-plan` revision the
    ceiling allows.

    `plan_revision` starts at 1 for the first authored plan (the initial
    draft is not itself a "revision"); each `--modify-plan` call increments
    it by one, so the number of revisions actually consumed is
    ``plan_revision - 1``. Defined ONCE here -- both `node_plan_judge` (to
    decide whether the gate offers `modify-plan` at all) and the CLI's
    `--modify-plan` handler (to refuse a request that would exceed the
    ceiling) call this instead of re-deriving the comparison, so the two
    can never drift the way a prior phase's hand-duplicated decision did
    (see `_ingest_item_should_release_claim`'s docstring in cli.py for that
    history).
    """
    return max(0, plan_revision - 1) >= max_revisions


def plan_barrier_active(state) -> bool:
    """True while a plan is mid-authoring/judging/rejected and dispatch should
    hold non-planning work. ``getattr`` with a "none" default so a checkpoint
    written before this field existed is never blocked."""
    return str(getattr(state, "plan_status", "none") or "none") in _PLAN_BARRIER_STATES


def plan_deps_satisfied(state, task) -> bool:
    """True when every task_id in ``task.depends_on`` has been driven to a
    genuinely-done outcome.

    Empty ``depends_on`` is always satisfied. A dependency is satisfied when
    its id appears in ``state.attended_done_task_ids`` (attended cursors that
    finalized with final_status="complete" — see HydraState.attended_done_task_ids)
    or when the corresponding TaskState has ``status == "done"`` (in-graph
    dispatch path). Deliberately NOT attended_completed_task_ids: that list
    also includes "surfaced" and "aborted" outcomes, and releasing a dependent
    onto a surfaced upstream is the E2-23 bug in a new costume.
    """
    deps = list(getattr(task, "depends_on", None) or [])
    if not deps:
        return True
    done_ids = set(getattr(state, "attended_done_task_ids", None) or [])
    status_by_id = {
        str(t.task_id): getattr(t, "status", None)
        for t in getattr(state, "tasks", None) or []
    }
    for dep in deps:
        dep = str(dep)
        if dep in done_ids:
            continue
        if status_by_id.get(dep) == "done":
            continue
        return False
    return True


class PoisonedStateError(Exception):
    """Raised by the checkpoint-deserialization choke point (see
    ``make_checkpoint_serde``) when a persisted checkpoint's ``channel_values``
    contains a non-finite float (``NaN``/``Infinity``/``-Infinity``) anywhere
    in its structure.

    Twelve prior cross-vendor rounds each patched ONE more node/edge/`as_node`
    jump that could reach `synthesis`/postcheck without re-running the
    non-finite scan a sibling node already had -- each fix closed one path
    and the next round found another. This exception is raised from the ONE
    place every one of those paths is structurally forced to pass through:
    `HydraState`/`TaskState`/`BudgetLedger` (and therefore `envelopes`,
    `verdicts`, `artifacts`, `plan_ref`, `attended_results`, and any other
    collection state carries) do not exist in a Python process until
    LangGraph's checkpointer deserializes them off disk via this module's
    serde -- there is no second, unwrapped route to the same bytes (see
    `make_checkpoint_serde`'s docstring for the exhaustive
    `grep -rn "SqliteSaver("` confirming exactly two construction sites, both
    already required to route through this function).

    ``field`` is the dotted/bracketed path `find_non_finite_field` reports
    (e.g. ``"$.verdicts[2].score_json.value"``); callers surface it verbatim
    in the same `unjudgeable`-shaped message the rest of the codebase already
    uses (see `judge.dispatcher._unjudgeable_verdict`,
    `cli._cmd_finalize`'s pre-materialization scan) so an operator sees one
    consistent vocabulary regardless of which path caught the poison.
    """

    def __init__(self, field: str) -> None:
        self.field = field
        super().__init__(
            f"checkpoint contains a non-finite value at {field}; "
            "refusing to deserialize poisoned state"
        )


def make_checkpoint_serde() -> Any:
    """Return a JsonPlusSerializer with hydra_core.state types registered,
    wrapped so every checkpoint READ is scanned for non-finite floats before
    any caller (node, conditional edge, ``as_node`` jump, or CLI finalize
    path) can observe the deserialized value.

    This suppresses the 'Deserializing unregistered type' deprecation warning
    that langgraph emits when it deserializes Pydantic models (BudgetLedger,
    TaskState, HydraState) whose modules are not in the explicit allowlist.

    Pass the return value as ``serde=`` to SqliteSaver at every construction
    site (supervisor.build_supervisor + hydra_memory._load_state_values) --
    confirmed by ``grep -rn "SqliteSaver("`` to be the ONLY two places this
    process ever constructs a checkpoint reader/writer. Every
    ``get_state``/``update_state``/``invoke`` call on the compiled graph (and
    every ``hydra-mem.workflow_status`` style read-only tool) reads the prior
    checkpoint through ``SqliteSaver.get_tuple``, which calls
    ``serde.loads_typed`` on the WHOLE checkpoint dict (see
    ``SqliteSaver.put``'s use of ``self.serde.dumps_typed(checkpoint)`` for
    the symmetric write) -- there is no code path in this repository that
    reads a persisted checkpoint's ``channel_values`` without going through
    that call. Scanning here, once, therefore covers a hostile/legacy
    ``as_node`` jump exactly the same as an ordinary node re-entry: the
    poisoned bytes cannot become a live Python object at all without first
    passing this gate.

    Chosen over a "state-entry validator" graph node (the other candidate
    choke point): LangGraph's ``update_state(..., as_node=...)`` is
    EXPLICITLY designed to apply a patch and re-enter the graph at an
    arbitrary node without running any node function in between -- that is
    precisely the mechanism `cli._cmd_finalize` uses (`as_node=
    "judge_per_squad"`) and precisely the mechanism the FAIL this round
    describes exploits. A validator implemented as a graph node is, by
    construction, one more node an `as_node=` jump can route around; a
    validator implemented as a Python-level wrapper around
    `invoke`/`update_state`/`get_state` would need to be called at every one
    of dozens of scattered call sites across `cli.py`/`supervisor.py`/the MCP
    servers, and a future call site can always forget it (this is exactly
    the "twelve rounds, twelve near-misses" failure mode already observed).
    The serde is the one object both of those layers are built ON TOP of, so
    there is nothing beneath it left to bypass.

    Returns None when langgraph / JsonPlusSerializer is not importable so the
    caller can fall back to the bare SqliteSaver(conn) construction (the
    ``_PurePythonRunner`` dev/test fallback never persists a checkpoint at
    all, so there is nothing for this choke point to scan there).
    """
    try:
        from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer  # type: ignore
    except ImportError:  # pragma: no cover — langgraph absent
        return None

    from .strict_json import find_non_finite_field

    class _ScanningCheckpointSerde(JsonPlusSerializer):
        """``JsonPlusSerializer`` whose ``loads_typed`` refuses a checkpoint
        whose deserialized ``channel_values`` (or any other top-level key —
        the whole checkpoint dict is scanned, not an enumerated field list,
        so a FUTURE state field carrying a payload is covered without a code
        change here) contains a non-finite float anywhere in its structure.

        Scans the deserialized VALUE, not the raw bytes: this runs after
        ``JsonPlusSerializer``'s own msgpack/json decode has already
        reconstructed real Python objects (dicts, lists, Pydantic models via
        ``model_dump``), so nested Pydantic model fields are visible to
        ``find_non_finite_field`` the same way a plain dict's are.
        """

        def loads_typed(self, data: tuple[str, bytes]) -> Any:
            value = super().loads_typed(data)
            bad_field = find_non_finite_field(value)
            if bad_field is not None:
                raise PoisonedStateError(bad_field)
            return value

    return _ScanningCheckpointSerde(
        allowed_msgpack_modules=[HydraState, TaskState, BudgetLedger],
    )
