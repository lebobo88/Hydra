"""Governance plane — HITL gates, budget enforcement, loop ceilings, redaction.

The constitution gate (see `immortal_head`) is the outermost ring of governance:
no budget, loop, or squad-failure check is permitted to override a refusal that
flows from CONSTITUTION.md. Verdict precedence in `enforce_governance` reflects
that — constitution check runs first.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from .immortal_head import (
    AlignmentVerdict,
    ConstitutionSnapshot,
    load_constitution,
    verify_intent,
)
from .schemas import HydraEnvelope
from .squad_loader import SquadPack
from .state import HydraState


# --------- budget ---------

def record_cost(state: HydraState, usd: float, tokens: int) -> None:
    state.budget.spent_usd += usd
    state.budget.spent_tokens += tokens


def should_downgrade_model(state: HydraState, threshold: float = 0.8) -> bool:
    return state.budget.percent_consumed >= threshold


def should_block_for_budget(state: HydraState) -> bool:
    # >= so that exactly-100% (spent == budget) also blocks. A workflow that
    # has spent every dollar must not dispatch further squad work.
    return state.budget.spent_usd >= state.budget.budget_usd


def charge_and_gate(
    state: HydraState,
    cost_usd: float,
    cost_tokens: int,
) -> tuple[bool, bool]:
    """Record cost then evaluate both budget gates in one call.

    Returns (block, downgrade):
      block     — True when spent >= budget_usd (>= 100% => stop dispatching)
      downgrade — True when percent_consumed >= 80% (WS9 tier tripwire)

    Callers at every execute_squad site use this so the logic is not
    duplicated across best-of-N, single-shot dispatch, and reflexion.
    Sequential (non-fleet) path only — do NOT replace with charge_and_gate_repo.
    """
    record_cost(state, cost_usd, cost_tokens)
    block = should_block_for_budget(state)
    downgrade = should_downgrade_model(state)
    return block, downgrade


def charge_and_gate_repo(
    state: HydraState,
    repo_id: str | None,
    cost_usd: float,
    cost_tokens: int,
) -> tuple[bool, bool, bool]:
    """WS8 SLICE 4 — per-repo fleet budget isolation.

    Record cost (global ledger, unchanged) then evaluate:
      - repo_over:    this repo has exceeded its per-repo allocation.
      - global_block: GLOBAL ledger hit 100% (the only fleet-wide stop).
      - downgrade:    global percent_consumed >= 80% (WS9 tier tripwire).

    Returns (repo_over, global_block, downgrade).

    ISOLATION SEMANTICS:
      repo_over True does NOT imply global_block.  A repo spending past its
      equal-split allocation is FLAGGED for the operator breakdown but does NOT
      block the rest of the fleet.  Only global_block triggers a fleet-wide HITL.

    NOTE on future multi-task-per-repo:
      A repo_over event COULD cancel that repo's not-yet-started tasks.
      With one-task-per-repo there are none, so no per-repo cancellation fires.
      Design the data (repo_over flagging, per-repo spend tracking) so this
      extension is addable without schema changes.  Per-repo over must NEVER be
      wired into the fleet-WIDE cancel_event — that would break isolation.
    """
    record_cost(state, cost_usd, cost_tokens)

    # Per-repo spend tracking.
    # If repo_id is None OR the repo has no per-repo allocation (not a fleet
    # repo), the charge is routed to the reserved "(unattributed)" bucket so
    # that sum(repo_spend.values()) == global spent_usd and the HITL breakdown
    # can reconcile.  The "(unattributed)" key is never in repo_budgets, so it
    # can never trigger repo_over — it is a reconciliation bucket only.
    _UNATTRIBUTED = "(unattributed)"
    if repo_id is not None and repo_id in state.budget.repo_budgets:
        # Attributed spend: repo is a known fleet repo with an allocation.
        state.budget.repo_spend[repo_id] = (
            state.budget.repo_spend.get(repo_id, 0.0) + cost_usd
        )
    else:
        # Unattributed: repo_id is None, or it is not a fleet repo.
        # Route into the explicit unattributed bucket for reconciliation.
        state.budget.repo_spend[_UNATTRIBUTED] = (
            state.budget.repo_spend.get(_UNATTRIBUTED, 0.0) + cost_usd
        )

    # Per-repo over: only flagged for repos WITH an allocation.
    # "(unattributed)" is never in repo_budgets so this is always False for it.
    repo_over = (
        repo_id is not None
        and repo_id in state.budget.repo_budgets
        and state.budget.repo_spend[repo_id] >= state.budget.repo_budgets[repo_id]
    )

    global_block = should_block_for_budget(state)
    downgrade = should_downgrade_model(state)
    return repo_over, global_block, downgrade


# --------- loop ceiling ---------

def should_circuit_break(state: HydraState, node: str, max_consecutive: int = 3) -> bool:
    return state.error_counters.get(node, 0) >= max_consecutive


# --------- redaction ---------

_PII_PATTERNS = [
    re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),                     # SSN
    re.compile(r"\b\d{16}\b"),                                 # credit card
    re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"),               # email
    re.compile(r"\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b"),  # phone
]


# Stage-5 MCP-attack patterns. Per the April 2025 MCP security analysis
# cited in the manifesto, the three known categories are prompt injection,
# lookalike tools, and cross-tool exfiltration. We redact rather than
# refuse here — the venom registry's `require_cerberus_pass` is the layer
# that refuses; redaction is the sanitization that runs at every squad
# boundary regardless of whether a capability is invoked.
_MCP_ATTACK_PATTERNS = [
    re.compile(r"ignore (?:all )?previous (?:instructions|prompts|rules)", re.IGNORECASE),
    re.compile(r"disregard (?:the )?(?:above|prior) (?:instructions|prompt|system)", re.IGNORECASE),
    re.compile(r"you are (?:now |actually )?(?:no longer|a different)", re.IGNORECASE),
    # Cross-tool exfiltration: read+post in one breath
    re.compile(r"(read|list|dump|export).{0,40}(post|send|upload|webhook|curl|wget)", re.IGNORECASE),
    # Base64 obfuscation near a network verb
    re.compile(r"base64.{0,30}(curl|wget|fetch|http)", re.IGNORECASE),
]


def redact_for_squad_boundary(
    text: str,
    *,
    allow_pii: bool = False,
    scan_mcp_attacks: bool = True,
) -> str:
    """Redact PII and optionally neutralize MCP-attack shapes from a string
    crossing a squad boundary. PII is replaced with `[REDACTED]`; MCP-attack
    patterns are replaced with `[REDACTED-INJECTION]` so the attempt itself
    is visible in audit but the payload is defanged."""
    out = text
    if not allow_pii:
        for p in _PII_PATTERNS:
            out = p.sub("[REDACTED]", out)
    if scan_mcp_attacks:
        for p in _MCP_ATTACK_PATTERNS:
            out = p.sub("[REDACTED-INJECTION]", out)
    return out


# --------- constitution gate (immortal head) ---------

def enforce_constitution(
    payload: str | dict | object,
    snapshot: Optional[ConstitutionSnapshot] = None,
) -> AlignmentVerdict:
    """Run a proposed action or envelope through the immortal-head gate.

    Thin wrapper over `immortal_head.verify_intent` so governance callers can
    reach all gating verdicts from one module. Use this before:
      - committing a procedural-memory update,
      - executing a venom-class capability,
      - finalizing a workflow's postcheck.
    """
    return verify_intent(payload, snapshot=snapshot)


# --------- postcheck verdict ---------

@dataclass
class GovernanceVerdict:
    surfaced: bool
    reason: str


def enforce_governance(
    state: HydraState,
    packs: dict[str, SquadPack],
    *,
    constitution: Optional[ConstitutionSnapshot] = None,
) -> GovernanceVerdict:
    """Final check before marking a workflow `done`. Returns surfaced=True
    when a gate failed and HITL must intervene.

    Order of precedence (highest first):
      1. Constitution gate — refusals flow from CONSTITUTION.md.
      2. Loop ceiling.
      3. Budget.
      4. Failed tasks.
      5. Surfaced tasks (stub / HITL).
    """
    snap = constitution or load_constitution()
    constitution_payload = {
        "goal": state.root_goal,
        "envelopes": state.envelopes,
        "last_event": state.last_event or "",
    }
    verdict = enforce_constitution(constitution_payload, snapshot=snap)
    if not verdict.aligned:
        return GovernanceVerdict(
            surfaced=True,
            reason=f"constitution_breach: {verdict.rationale}",
        )
    if state.is_looping():
        return GovernanceVerdict(
            surfaced=True,
            reason=f"loop ceiling tripped (iter={state.iteration_count}, depth={state.depth})",
        )
    if state.is_over_envelope_ceiling():
        return GovernanceVerdict(
            surfaced=True,
            reason=(
                f"envelope_ceiling tripped "
                f"(envelopes={len(state.envelopes)}, ceiling={state.envelope_ceiling})"
            ),
        )
    mcp_tripped, mcp_server = state.any_mcp_over_ceiling()
    if mcp_tripped:
        return GovernanceVerdict(
            surfaced=True,
            reason=(
                f"mcp_disconnect:{mcp_server} "
                f"(failures={state.mcp_failures_for(mcp_server)}, ceiling={state.mcp_failure_ceiling})"
            ),
        )
    if state.is_over_budget():
        return GovernanceVerdict(
            surfaced=True,
            reason=f"over budget (${state.budget.spent_usd:.2f} > ${state.budget.budget_usd:.2f})",
        )
    failed = [t for t in state.tasks if t.status == "failed"]
    if failed:
        return GovernanceVerdict(
            surfaced=True,
            reason=f"{len(failed)} task(s) failed: {[t.owner_squad for t in failed]}",
        )
    surfaced = [t for t in state.tasks if t.status == "surfaced"]
    if surfaced:
        return GovernanceVerdict(
            surfaced=True,
            reason=f"{len(surfaced)} task(s) surfaced (stub or HITL): {[t.owner_squad for t in surfaced]}",
        )
    return GovernanceVerdict(surfaced=False, reason="all gates passed")
