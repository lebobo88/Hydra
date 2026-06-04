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

from pydantic import BaseModel, Field, field_validator

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
    budget_usd: Optional[float] = None
    token_limit: Optional[int] = None
    deadline_ts: Optional[datetime] = None
    risk_tolerance: Literal["low", "medium", "high"] = "medium"
    priority: Literal["P0", "P1", "P2", "P3"] = "P2"
    industries: list[str] = Field(default_factory=list)


class HydraEnvelope(BaseModel):
    """Base envelope shared by every cross-squad message."""
    id: UUID = Field(default_factory=uuid4)
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
    model_type: Literal["diffusion", "nerf", "video_llm", "tts", "music"]
    resolution: str = "1920x1080"
    fps: int = 24
    style_refs: list[MemoryRef] = Field(default_factory=list)
    output_bucket: str
    max_render_cost_usd: float = 200.0


# ---------- governance ----------

class HITLRequest(HydraEnvelope):
    type: Literal["HITL_REQUEST"] = "HITL_REQUEST"
    reason: Literal["budget_approval", "prod_deploy", "high_risk", "policy_breach",
                    "campaign_signoff", "schema_conflict", "loop_ceiling",
                    "constitution_breach", "reflexion_override"]
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
    "SUPPORT_TICKET": SupportTicket,
    "PORTABLE_CONTEXT": PortableContext,
    "VOC_REPORT": VocReport,
}


def _register_judge_verdict() -> None:
    """Register the JudgeVerdict envelope. Called by `hydra_core.judge` at
    package init to avoid the schemas ↔ judge import cycle (judge.schemas
    imports HydraEnvelope from this module).
    """
    from .judge.schemas import JudgeVerdict  # local import
    SCHEMA_REGISTRY["JUDGE_VERDICT"] = JudgeVerdict


def validate_envelope(obj: dict[str, Any]) -> HydraEnvelope:
    """Validate any envelope dict. Raises pydantic.ValidationError on failure."""
    t = obj.get("type")
    if t not in SCHEMA_REGISTRY:
        raise ValueError(f"Unknown envelope type: {t!r}. Known: {list(SCHEMA_REGISTRY)}")
    return SCHEMA_REGISTRY[t].model_validate(obj)
