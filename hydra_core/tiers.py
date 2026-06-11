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
