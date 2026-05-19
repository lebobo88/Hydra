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
    return state.budget.spent_usd > state.budget.budget_usd


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
