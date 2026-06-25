"""WS9 — model_tier definitions and normalisation helper.

VALID_MODEL_TIERS is the authoritative set of tier tokens Hydra recognises.
"deep" is an alias for "fable" — both route to the deep-reasoning-team in pp.

normalize_tier:
  - Accepts a raw string (case-insensitive).
  - Returns the canonical lowercase token on success.
  - Returns None when the input is None.
  - Raises ValueError for a non-None token that is not in VALID_MODEL_TIERS.
    Callers that intercept this must return a failed SquadResult rather than
    silently ignoring an unknown tier — fail-closed is the contract.

Fable / deep routing:
  Reaching the Fable (deep-reasoning) team in pair-programmer requires the
  caller to explicitly pass model_tier="fable" or model_tier="deep".  There
  is NO automatic escalation path — Fable is operator/flag-driven only.
  See squad_node._via_mcp for the dispatch implementation.
"""
from __future__ import annotations

# Canonical lowercase token set.
VALID_MODEL_TIERS: frozenset[str] = frozenset({
    "haiku",
    "sonnet",
    "opus",
    "fable",
    "deep",   # alias for fable — both route to deep-reasoning-team
})

# Tokens that route to pp's deep-reasoning-team.
FABLE_TIERS: frozenset[str] = frozenset({"fable", "deep"})

# Capability ordering (low → high). Used to enforce the same-vendor judging rule:
# a same-vendor judge must run at the SAME or a HIGHER tier than the generator —
# a weaker model rubber-stamping a stronger one's output is not a real check.
_TIER_RANK: dict[str, int] = {"haiku": 0, "sonnet": 1, "opus": 2, "fable": 3, "deep": 3}


def tier_rank(tier: str | None) -> int | None:
    """Capability rank of a tier (higher = more capable), or None if unknown/None."""
    if tier is None:
        return None
    return _TIER_RANK.get(tier.strip().lower())


def is_same_or_higher_tier(judge_tier: str | None, generator_tier: str | None) -> bool:
    """True when ``judge_tier`` is at least as capable as ``generator_tier``.

    The same-vendor judging rule: a same-vendor judge MUST run at the same or a
    higher tier than the generator. Unknown/None tiers are PERMISSIVE (return
    True) — we cannot prove a violation without both ranks, and must not block on
    a tier we don't recognise (fail-open for observability, not enforcement).
    """
    jr, gr = tier_rank(judge_tier), tier_rank(generator_tier)
    if jr is None or gr is None:
        return True
    return jr >= gr


def normalize_tier(tier: str | None) -> str | None:
    """Return the canonical lowercase tier token, or None if tier is None.

    Raises ValueError for a non-None token that is not in VALID_MODEL_TIERS.
    Callers MUST treat ValueError as a hard rejection (return failed SquadResult),
    not a warning — unknown tiers are fail-closed.
    """
    if tier is None:
        return None
    canonical = tier.strip().lower()
    if canonical not in VALID_MODEL_TIERS:
        raise ValueError(
            f"Unknown model_tier={tier!r}. "
            f"Valid tokens: {sorted(VALID_MODEL_TIERS)}"
        )
    return canonical
