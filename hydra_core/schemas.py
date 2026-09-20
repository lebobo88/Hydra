"""Cross-squad message schemas.

Every artifact that crosses a squad boundary in Hydra is one of these. Validation
runs at every edge in the supervisor graph (`schema-validate` PreToolUse hook).

Schema lineage maps to `Enterprise Master AI Orchestration System Architecture.md`:
  - CSuiteDecisionPacket  →  PRD  →  ArchRFC  →  DevTask
  - CreativeBrief         →  ShotList  →  AssetJob
  - HITLRequest           →  (approval / rejection / mutation)
  - DecisionRecord        →  immutable consensus artifact
  - Handoff               →  cross-squad delegation with explicit privilege grant
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal, Optional
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, field_validator, model_validator

from .eights import Cell


# ---------- shared primitives ----------

class MemoryRef(BaseModel):
    """A handle, not a blob. Agents resolve via the memory MCP server.

    `cells` is the TheEights tag vocabulary (Qian/Kun/Zhen/Xun/Kan/Li/Gen/Dui).
    Empty list means untagged — backwards compatible with pre-Stage-3 writes.
    See `hydra_core.eights` for the cell vocabulary.
    """
    tier: Literal["ephemeral", "episodic", "semantic", "profile"]
    key: str
    summary: Optional[str] = None
    cells: list[Cell] = Field(default_factory=list)


class Constraints(BaseModel):
    # AgentSmith's checkPlan validator (X2) requires strict RFC 8259 JSON —
    # NaN/Infinity/-Infinity are not valid JSON tokens even though Python's
    # `json` module accepts them by default (`allow_nan=True`). `allow_
    # inf_nan=False` rejects those three inputs at construction time while
    # still accepting `None` and any ordinary finite float; no other
    # constraint (e.g. non-negativity) is added.
    #
    # Cross-vendor judge finding (item 6/6, MEDIUM): "at construction time"
    # is the operative limit. `allow_inf_nan=False` is a pydantic FIELD
    # VALIDATOR, and pydantic v2 field validators run on `Model(...)` /
    # `model_validate(...)` / `model_validate_json(...)` -- they do NOT run
    # on `model_copy(update=...)` (an in-place field swap, no validation at
    # all by default) or `model_construct(...)` (explicitly skips ALL
    # validation, including this one). Both are real code paths in this
    # codebase: `Constraints.model_construct(budget_usd=float("nan"))`
    # succeeds silently, and so does
    # `some_plan.model_copy(update={"constraints": <hostile>})`.
    #
    # Boundaries that DO revalidate a value that reached here via one of
    # those two bypasses (so the invariant still holds end to end even
    # though the field validator alone cannot enforce it universally):
    #   - `hydra_core.ingest.dispatch_ingested_envelopes` (~line 510):
    #     round-trips every caller-supplied TYPED envelope through
    #     `type(env).model_validate(env.model_dump(mode="json"))` BEFORE it
    #     reaches the PLAN-write / judge-serialize code below, specifically
    #     to catch a `model_copy`/`model_construct` bypass with pydantic's
    #     own field-naming error at the ingest boundary rather than deeper
    #     inside a writer.
    #   - `hydra_core.strict_json.dumps_strict` / `find_non_finite_field`
    #     (this module's sibling): inspects the ACTUAL runtime value, not
    #     how it was constructed, so it catches a bypassed non-finite float
    #     regardless of provenance -- the last-resort backstop every
    #     envelope/plan-to-JSON-text writer (the judge dispatcher, the plan
    #     HTML/JSON artifact renderers, the MCP persistence writes) routes
    #     through.
    #   - `hydra_core.plan_artifact._sum_step_budgets` /
    #     `sum_finite_budgets`: guards against a SEPARATE failure mode
    #     (float overflow from summing two already-finite values), not
    #     against a bypassed non-finite input, but documented alongside
    #     since it sits on the same read path.
    # A value that reaches a downstream consumer WITHOUT transiting any of
    # these three boundaries (e.g. a raw dict loaded straight from an old
    # checkpoint and handed directly to a judge call) is exactly the
    # "legacy envelope" scenario `judge.dispatcher.dispatch_judge` treats as
    # `unjudgeable` rather than crashing or silently degrading to `skip`
    # (item 1/6) -- there is no field-validator boundary to catch it earlier.
    budget_usd: Optional[float] = Field(default=None, allow_inf_nan=False)
    token_limit: Optional[int] = None
    deadline_ts: Optional[datetime] = None
    risk_tolerance: Literal["low", "medium", "high"] = "medium"
    priority: Literal["P0", "P1", "P2", "P3"] = "P2"
    industries: list[str] = Field(default_factory=list)


class HydraEnvelope(BaseModel):
    """Base envelope shared by every cross-squad message."""
    id: UUID = Field(default_factory=uuid4)
    # E2-34: the identifier the ORIGINATING system used, when it was not a
    # UUID. A host-run pack may label its envelope "devtask-hydra-heads-166fc7ee";
    # `hydra_core.ingest.normalize_pack_envelope` mints a real UUID for `id` and
    # preserves the original here so the pack's own reference still resolves and
    # the trace stays correlatable. None when the pack supplied a valid UUID.
    external_id: Optional[str] = None
    type: str
    origin_squad: str
    target_squad: Optional[str] = None
    workflow_id: UUID
    parent_id: Optional[UUID] = None
    context_refs: list[MemoryRef] = Field(default_factory=list)
    constraints: Constraints = Field(default_factory=Constraints)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    # R3-tail post-mortem Fix 2.1 (2026-05-21): paths the receiving squad
    # MUST NOT touch in its produced diff. Project-relative paths; glob
    # patterns are NOT supported (literal-string equality). Receivers
    # pre-flight-check their diff against this list and refuse to commit
    # if any path matches.
    # R3-tail δ tail-fix-4 demonstrated this prevents regressions: when
    # the operator explicitly told test-strategist NOT to touch
    # `apps/web/lib/idempotency.ts`, the surgical patches stayed
    # surgical. Earlier rounds without it had regressions because the
    # engineer kept re-touching files that earlier fixes had stabilized.
    do_not_touch: list[str] = Field(default_factory=list)


# ---------- executive squad ----------

class ProposedTask(BaseModel):
    task_id: UUID = Field(default_factory=uuid4)
    target_squad: str
    description: str
    success_metrics: list[str] = Field(default_factory=list)
    priority: Literal["P0", "P1", "P2", "P3"] = "P2"
    estimated_budget_usd: Optional[float] = None


class CSuiteDecisionPacket(HydraEnvelope):
    type: Literal["C_SUITE_DECISION_PACKET"] = "C_SUITE_DECISION_PACKET"
    origin: Literal["CEO", "CFO", "CMO", "CTO", "CRO", "CAIO", "BOARDROOM"]
    objective: str
    proposed_tasks: list[ProposedTask] = Field(default_factory=list)
    approvals_required: list[str] = Field(default_factory=list)  # e.g. ["human:CFO"]
    dissenting_opinions: list[str] = Field(default_factory=list)
    notes: Optional[str] = None
    # allow-listed repo_id for engineering dispatch targeting (None = workflow project_root)
    target_repo_id: Optional[str] = None
    # Optional repo-relative engineering target under target_repo_id.
    target_repo_subpath: Optional[str] = None
    # WS9: model_tier hint from the operator or planner task.  Propagated by
    # node_dispatch onto each CSuiteDecisionPacket so _via_mcp can read it via
    # getattr(inbound, "model_tier", None) — mirroring the target_repo_id pattern.
    # Valid tokens: "haiku" | "sonnet" | "opus" | "fable" | "deep".
    # None = use squad default.
    model_tier: Optional[str] = None
    # pp team / profile selection for engineering dispatch.  Propagated by
    # node_dispatch from TaskState so `_via_mcp` can route to the correct
    # pair-programmer team (e.g. "game-feature-team") and project profile.
    # None = fall back to the engineering squad.yaml default / auto-detect.
    # Read via getattr(inbound, "pp_team", None) — same pattern as model_tier.
    pp_team: Optional[str] = None
    pp_profile: Optional[str] = None


# ---------- engineering squad ----------

class UserStory(BaseModel):
    id: str
    as_a: str
    i_want: str
    so_that: str


class PRD(HydraEnvelope):
    type: Literal["PRD"] = "PRD"
    source_goal_id: UUID
    summary: str
    user_personas: list[str] = Field(default_factory=list)
    user_stories: list[UserStory] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)
    non_functional_requirements: list[str] = Field(default_factory=list)


class ProposedChange(BaseModel):
    component: str
    change_type: Literal["new", "modify", "deprecate"]
    details: str


class ArchRFC(HydraEnvelope):
    type: Literal["ARCH_RFC"] = "ARCH_RFC"
    related_prd: Optional[UUID] = None
    proposed_changes: list[ProposedChange] = Field(default_factory=list)
    risk_assessment: str
    rollout_plan: str
    requires_approvals: list[str] = Field(default_factory=list)


class DevTask(HydraEnvelope):
    type: Literal["DEV_TASK"] = "DEV_TASK"
    owner: Literal["frontend", "backend", "fullstack", "devops", "data"]
    repo: str
    branch: str
    instructions: str
    files_touched: list[str] = Field(default_factory=list)
    test_plan: list[str] = Field(default_factory=list)
    status: Literal["pending", "in_progress", "done", "blocked", "surfaced"] = "pending"
    pr_url: Optional[str] = None
    # allow-listed repo_id for engineering dispatch targeting (None = workflow
    # project_root). When an orchestrator emits a DEV_TASK whose `repo` names an
    # allow-listed repo, the dispatcher mirrors it here so _via_mcp resolves the
    # real path (the `repo` free-text field is NOT used for path resolution).
    target_repo_id: Optional[str] = None
    # Optional repo-relative engineering target under target_repo_id.
    target_repo_subpath: Optional[str] = None
    # pp team / profile selection. An orchestrator squad (e.g. rlm-gaming) sets
    # these when handing implementation to engineering so the run uses the right
    # pair-programmer team (e.g. "game-feature-team") and project profile.
    # None = engineering squad.yaml default / auto-detect in `_via_mcp`.
    pp_team: Optional[str] = None
    pp_profile: Optional[str] = None


# ---------- garland squad ----------

class CreativeBrief(HydraEnvelope):
    type: Literal["CREATIVE_BRIEF"] = "CREATIVE_BRIEF"
    campaign_id: UUID
    objective: str
    target_audience: str
    key_messages: list[str] = Field(default_factory=list)
    channels: list[str] = Field(default_factory=list)
    brand_constraints: list[str] = Field(default_factory=list)
    assets_required: list[str] = Field(default_factory=list)


class Shot(BaseModel):
    shot_id: str
    description: str
    camera_angle: Literal["wide", "closeup", "medium", "aerial", "pov"] = "medium"
    focal_length_mm: int = 35
    duration_sec: float = 3.0
    lighting_notes: Optional[str] = None


class ShotList(HydraEnvelope):
    type: Literal["SHOT_LIST"] = "SHOT_LIST"
    brief_id: UUID
    shots: list[Shot] = Field(default_factory=list)


class AssetJob(HydraEnvelope):
    type: Literal["ASSET_JOB"] = "ASSET_JOB"
    shotlist_id: Optional[UUID] = None
    # `mesh` and `rig` are executed by Garland's existing blender-model and
    # blender-rig workers. Their detailed DCC/rig contracts are persisted as
    # MemoryRefs in the inherited `context_refs`, never invented as an inline
    # payload field.
    model_type: Literal["diffusion", "nerf", "video_llm", "tts", "music", "mesh", "rig"]
    resolution: str = "1920x1080"
    fps: int = 24
    style_refs: list[MemoryRef] = Field(default_factory=list)
    output_bucket: str
    max_render_cost_usd: float = 200.0
    provenance_required: bool = False


# ---------- governance ----------

class HITLRequest(HydraEnvelope):
    type: Literal["HITL_REQUEST"] = "HITL_REQUEST"
    reason: Literal["budget_approval", "prod_deploy", "high_risk", "policy_breach",
                    "campaign_signoff", "schema_conflict", "loop_ceiling",
                    "constitution_breach", "reflexion_override",
                    "acceptance_criteria", "lock_release_pending",
                    "mcp_disconnect", "over_budget", "envelope_ceiling",
                    "plan_approval", "unjudgeable_envelope", "unjudgeable_plan"]
    # `unjudgeable_envelope` / `unjudgeable_plan`: cross-vendor judge finding
    # (item 1/6, CRITICAL) -- filed by `node_judge_per_squad`,
    # `node_judge_synthesis`, and `node_plan_judge` (supervisor.py) when an
    # envelope/plan failed STRICT SERIALIZATION (outcome="unjudgeable"), a
    # genuine data defect distinct from a routed `skip` or a `policy_breach`
    # quality verdict. `unjudgeable_plan` intentionally offers only
    # `["abort"]` (never `acknowledge`) -- `node_plan_gate` materialises
    # tasks on ANY non-terminal resume regardless of chosen option, so an
    # `acknowledge` there would silently behave like `approve`.
    # `reflexion_override`: emitted by `node_judge_per_squad` when an envelope's
    # `revise` verdict cannot be retried because the Reflexion ×1 ceiling is
    # exhausted. Operator approval raises `state.reflexion_override_granted_until`
    # for this workflow only; the constitutional ×1 default is unchanged. Added
    # in the R3-tail post-mortem (2026-05-21) to replace ad-hoc LLM-mediated
    # ceiling overrides with a structured HITL audit trail. See
    # `hydra_core.judge.reflexion.effective_max_retry_index`.
    summary: str
    options: list[str]
    default_option: Optional[str] = None
    expires_at: Optional[datetime] = None


class DecisionRecord(HydraEnvelope):
    """Immutable consensus artifact. Append-only in episodic memory."""
    type: Literal["DECISION_RECORD"] = "DECISION_RECORD"
    decision: str
    rationale: str
    dissenting_opinions: list[str] = Field(default_factory=list)
    artifacts: list[MemoryRef] = Field(default_factory=list)
    sealed: bool = True


class Handoff(HydraEnvelope):
    """Explicit cross-squad delegation. Carries the privilege grant."""
    type: Literal["HANDOFF"] = "HANDOFF"
    granted_tools: list[str] = Field(default_factory=list)
    granted_memory_scopes: list[str] = Field(default_factory=list)
    payload_envelope_id: UUID  # the actual artifact being handed off
    expires_at: Optional[datetime] = None


# ---------- planning ----------

class PlanStep(BaseModel):
    """One decomposed unit of work inside a `Plan`.

    `step_id` is a readable slug (e.g. "wire-auth-middleware"), NOT a UUID —
    steps must stay stable across plan revisions so markdown diffs between
    `plan_revision`s line up on the same identifiers.
    """
    step_id: str
    target_squad: str
    envelope_type: str
    description: str
    acceptance_criteria: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)  # other step_ids
    priority: Literal["P0", "P1", "P2", "P3"] = "P2"
    model_tier: Optional[str] = None
    target_repo_id: Optional[str] = None
    target_repo_subpath: Optional[str] = None
    # See Constraints.budget_usd above — same strict-JSON rationale.
    estimated_budget_usd: Optional[float] = Field(default=None, allow_inf_nan=False)
    taxonomy_section: Optional[str] = None
    rationale: Optional[str] = None

    @field_validator("envelope_type")
    @classmethod
    def _known_type(cls, v: str) -> str:
        # NOTE: SCHEMA_REGISTRY is defined further down in this module.
        # Pydantic validators run at CALL time (not import time), so this
        # module-level name resolves fine by the time any PlanStep is
        # constructed. Do NOT snapshot the registry into a frozenset here —
        # "JUDGE_VERDICT" is registered lazily via `_register_judge_verdict`
        # after package init, and a snapshot taken now would miss it.
        if v not in SCHEMA_REGISTRY:
            raise ValueError(
                f"Unknown envelope_type: {v!r}. Known: {list(SCHEMA_REGISTRY)}"
            )
        return v


class Plan(HydraEnvelope):
    """A decomposition of a goal into dependency-ordered `PlanStep`s.

    Cyclic or dangling dependencies are rejected at construction time — a
    cyclic plan cannot enter the system, because `validate_envelope` is
    nothing more than a registry lookup + `model_validate`.
    """
    type: Literal["PLAN"] = "PLAN"
    rigor: Literal["trivial", "standard", "major"]
    goal_restatement: str
    # REQUIRED: TheEights' `extractSummary` probes `objective|summary|
    # description|goal` in that order when minting a semantic memory row.
    # A Plan without `summary` gets no semantic memory row at all.
    summary: str
    steps: list[PlanStep] = Field(default_factory=list)
    non_goals: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    artifact_path: Optional[str] = None
    # P5b: >= 1, never 0 or negative. This value gets copied verbatim onto
    # every TaskState materialised from this plan's steps (node_plan_gate),
    # and the four attended-selectors filter stale work with
    # `getattr(t, "plan_revision", 0) and t.plan_revision != state.plan_revision`
    # -- a leading truthiness test that treats 0 as "not a plan step at all"
    # (TaskState.plan_revision's own default). An unconstrained Plan let
    # plan_revision=0 through construction, which would stamp every step task
    # 0 and make that truthiness test exempt a superseded revision's steps
    # FOREVER -- the exact append-only stale-task bug the whole "materialise
    # on approval only" design exists to close, reachable through a
    # perfectly valid envelope. A negative value is a different failure: it
    # is truthy and can never equal state.plan_revision, so every step would
    # be filtered permanently and the operator's approval would silently
    # dispatch nothing. Neither failure raises anywhere else in the pipeline
    # -- this is the one place that can catch it, at construction.
    plan_revision: int = Field(default=1, ge=1)
    supersedes: Optional[UUID] = None
    authored_by: list[str] = Field(default_factory=list)
    dissents: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_dag(self) -> "Plan":
        seen: set[str] = set()
        for step in self.steps:
            if step.step_id in seen:
                raise ValueError(
                    f"Duplicate step_id in Plan: {step.step_id!r}"
                )
            seen.add(step.step_id)

        known_ids = seen
        for step in self.steps:
            if step.step_id in step.depends_on:
                raise ValueError(
                    f"Step {step.step_id!r} depends on itself"
                )
            for dep in step.depends_on:
                if dep not in known_ids:
                    raise ValueError(
                        f"Step {step.step_id!r} depends on unknown step_id "
                        f"{dep!r} (dangling dependency)"
                    )

        # Kahn's algorithm: repeatedly drain nodes with in-degree 0. Any node
        # left over once the queue is exhausted is part of a cycle.
        in_degree: dict[str, int] = {step.step_id: 0 for step in self.steps}
        dependents: dict[str, list[str]] = {step.step_id: [] for step in self.steps}
        for step in self.steps:
            for dep in step.depends_on:
                in_degree[step.step_id] += 1
                dependents[dep].append(step.step_id)

        queue = [sid for sid, deg in in_degree.items() if deg == 0]
        drained: set[str] = set()
        while queue:
            sid = queue.pop()
            drained.add(sid)
            for nxt in dependents[sid]:
                in_degree[nxt] -= 1
                if in_degree[nxt] == 0:
                    queue.append(nxt)

        remaining = known_ids - drained
        if remaining:
            # Removing this validator (or the Kahn drain above) makes a
            # cyclic Plan, e.g. steps a->b->a, construct without error —
            # that is exactly the property this test proves.
            raise ValueError(
                f"Cyclic dependency detected among steps: {sorted(remaining)}"
            )
        return self


# ---------- customer-support squad (Xenia) ----------


class ActiveObject(BaseModel):
    type: str
    ref: str
    state: str


class SentimentSnapshot(BaseModel):
    current: Literal["positive", "neutral", "negative", "hostile"]
    trajectory: Literal["improving", "stable", "worsening"]


class ActionAttempted(BaseModel):
    action: str
    by: str
    executed: bool
    result: Optional[str] = None


class PortableContextPayload(BaseModel):
    """The structured state token from the portable-context-token skill.

    Minted by Iris; updated by every head on state change.  `customer_ref`
    is ALWAYS opaque (`customer:<hash>`).  Raw identity MUST NOT appear
    here (constitution Article IV).
    """
    ctx_id: str                                  # CTX-<ticket-id>-<rev>
    ticket_id: str
    customer_ref: str                            # customer:<hash> — never raw PII
    goal: str
    active_objects: list[ActiveObject] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    sentiment: Optional[SentimentSnapshot] = None
    history_digest: str = ""
    actions_attempted: list[ActionAttempted] = Field(default_factory=list)
    minted_by: str = "iris"
    minted_at: Optional[datetime] = None
    updated_by: Optional[str] = None
    rev: int = 1


class SupportTicket(HydraEnvelope):
    """First-class support ticket envelope.

    Carries a normalised inbound support request — from a channel adapter,
    an operator paste, or a HANDOFF lift — into the customer-support squad.
    `portable_context` is optional; it is populated when the ticket
    originates from an in-progress session that already has a context token.

    BACKWARD COMPAT: HANDOFF-tunneled portable_context remains valid; the
    HANDOFF payload envelope id may point at one of these or any other
    artifact type.
    """
    type: Literal["SUPPORT_TICKET"] = "SUPPORT_TICKET"
    ticket_id: str
    customer_ref: str                            # customer:<hash> — never raw PII
    subject: str
    body: str
    priority: Literal["P0", "P1", "P2", "P3"] = "P2"
    intent: Optional[str] = None
    channel: Optional[str] = None               # e.g. "email", "chat", "voice", "api"
    portable_context: Optional[PortableContextPayload] = None


class PortableContext(HydraEnvelope):
    """Portable-context token as a first-class envelope.

    Allows the token to travel as a standalone artifact between squads,
    rather than only inside a HANDOFF payload.  The inner `payload` is the
    same PortableContextPayload schema used in SupportTicket.

    BACKWARD COMPAT: HANDOFF-tunneled portable_context is still accepted;
    this envelope is additive.
    """
    type: Literal["PORTABLE_CONTEXT"] = "PORTABLE_CONTEXT"
    payload: PortableContextPayload


class VocTheme(BaseModel):
    theme: str
    count: int
    trend: Optional[str] = None                 # e.g. "+12% vs prior period"
    sentiment_trajectory: Optional[str] = None
    representative_quote_redacted: Optional[str] = None
    kb_gap: bool = False


class VocReport(HydraEnvelope):
    """Voice-of-Customer report envelope.

    Produced by Echo (Soteria sub-agent) and delivered upward to the
    executive layer via this first-class type.  All fields are aggregates
    and opaque refs — raw customer identity MUST NOT appear here
    (constitution Article IV).
    """
    type: Literal["VOC_REPORT"] = "VOC_REPORT"
    period: dict[str, str]                       # {"from": "ISO-8601", "to": "ISO-8601"}
    coverage: str                                # e.g. "47 tickets, 2026-05-01 to 2026-06-01"
    themes: list[VocTheme] = Field(default_factory=list)
    escalation_patterns: Optional[str] = None
    delight_signals: Optional[str] = None
    recommendations: list[str] = Field(default_factory=list)


# ---------- discriminator union for routing ----------

AnyEnvelope = (
    CSuiteDecisionPacket | PRD | ArchRFC | DevTask
    | CreativeBrief | ShotList | AssetJob
    | HITLRequest | DecisionRecord | Handoff
    | Plan
    | SupportTicket | PortableContext | VocReport
)


SCHEMA_REGISTRY: dict[str, type[HydraEnvelope]] = {
    "C_SUITE_DECISION_PACKET": CSuiteDecisionPacket,
    "PRD": PRD,
    "ARCH_RFC": ArchRFC,
    "DEV_TASK": DevTask,
    "CREATIVE_BRIEF": CreativeBrief,
    "SHOT_LIST": ShotList,
    "ASSET_JOB": AssetJob,
    "HITL_REQUEST": HITLRequest,
    "DECISION_RECORD": DecisionRecord,
    "HANDOFF": Handoff,
    "PLAN": Plan,
    "SUPPORT_TICKET": SupportTicket,
    "PORTABLE_CONTEXT": PortableContext,
    "VOC_REPORT": VocReport,
}

# M7: COCKPIT_WRITE is an internal audit event type (not a cross-squad pydantic
# envelope — workflow_id is a string, not a UUID, and the envelope is emitted
# by the cockpit bridge, never routed across squad boundaries). Listed here so
# validate_envelope does not raise on known-good types and so the toolshed /
# AgentSmith catalog sees it as a first-class name.
_OPAQUE_KNOWN_TYPES: frozenset[str] = frozenset({"COCKPIT_WRITE"})


def _register_judge_verdict() -> None:
    """Register the JudgeVerdict envelope. Called by `hydra_core.judge` at
    package init to avoid the schemas ↔ judge import cycle (judge.schemas
    imports HydraEnvelope from this module).
    """
    from .judge.schemas import JudgeVerdict  # local import
    SCHEMA_REGISTRY["JUDGE_VERDICT"] = JudgeVerdict


def validate_envelope(obj: dict[str, Any]) -> HydraEnvelope:
    """Validate any envelope dict. Raises pydantic.ValidationError on failure.

    Opaque known types (e.g. COCKPIT_WRITE) that carry a non-UUID workflow_id
    or other deviations from HydraEnvelope's strict contract are allowed
    through as a passthrough envelope — they are internal audit events, never
    routed across squad boundaries.
    """
    t = obj.get("type")
    if t in _OPAQUE_KNOWN_TYPES:
        # Known internal type — return a minimal passthrough HydraEnvelope so
        # callers that only inspect `.type` work without pydantic validation
        # blowing up on the non-UUID workflow_id or missing fields.
        from uuid import UUID as _UUID, uuid4 as _uuid4
        raw_wf = obj.get("workflow_id")
        try:
            wf_uuid = _UUID(str(raw_wf)) if raw_wf else _uuid4()
        except (ValueError, AttributeError):
            wf_uuid = _uuid4()
        return HydraEnvelope(
            id=obj.get("id", str(_uuid4())),
            type=t,
            origin_squad=str(obj.get("origin_squad") or obj.get("actor") or "hydra"),
            workflow_id=wf_uuid,
        )
    if t not in SCHEMA_REGISTRY:
        raise ValueError(f"Unknown envelope type: {t!r}. Known: {list(SCHEMA_REGISTRY)}")
    return SCHEMA_REGISTRY[t].model_validate(obj)
