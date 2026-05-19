"""Governance plane — HITL gates, budget enforcement, loop ceilings, redaction."""
from __future__ import annotations

import re
from dataclasses import dataclass

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


def redact_for_squad_boundary(text: str, *, allow_pii: bool = False) -> str:
    if allow_pii:
        return text
    out = text
    for p in _PII_PATTERNS:
        out = p.sub("[REDACTED]", out)
    return out


# --------- postcheck verdict ---------

@dataclass
class GovernanceVerdict:
    surfaced: bool
    reason: str


def enforce_governance(
    state: HydraState,
    packs: dict[str, SquadPack],
) -> GovernanceVerdict:
    """Final check before marking a workflow `done`. Returns surfaced=True
    when a gate failed and HITL must intervene."""
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
