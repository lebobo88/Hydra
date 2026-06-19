"""Regression guard: rlm-gaming must not siphon routes from other squads.

When the rlm-gaming squad pack was added, its keyword fingerprint included
generic ambient words (narrative, dialogue, level, economy, balance, season,
progression, difficulty, mechanic) that overlap garland/executive/marketing
vocabulary. Because the router's flat scorer co-selects any squad scoring
>= top_score * 0.6, a single ambient hit was enough to pull rlm-gaming into —
or ahead of — a route meant for another squad. This locked the contract so the
collision cannot silently return: non-game goals must NOT pick rlm-gaming, while
genuine game goals still must.

See hydra_core/router.py (_KEYWORDS["rlm-gaming"]).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from hydra_core.router import classify_intent
from hydra_core.squad_loader import discover_squads

_PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def packs():
    return discover_squads(_PROJECT_ROOT)


# Goals that belong to OTHER squads but historically tripped rlm-gaming via an
# ambient keyword. rlm-gaming must be absent from the routing decision.
@pytest.mark.parametrize(
    "goal",
    [
        "write the dialogue and narrative for our marketing video",
        "balance the quarterly budget and investment strategy for the board",
        "design the next level of our go-to-market positioning and channel mix",
        "refine the brand narrative and creative copy for the campaign",
        "model the unit economy and pricing for the new subscription tier",
        # second-pass ambient words pruned after codex judge review:
        "drive unity and alignment across the org this quarter",
        "publish the architecture blueprint for the platform roadmap",
        "improve monetization of our SaaS subscription and reduce churn",
        # combat words qualified after codex second-pass probing:
        "align with my boss on the quarterly budget strategy",
        "document the customer encounter and support escalation",
        "analyze public enemy messaging risk for the brand campaign",
    ],
)
def test_non_game_goal_does_not_route_to_rlm_gaming(packs, goal):
    if "rlm-gaming" not in packs:
        pytest.skip("rlm-gaming pack not discovered in this checkout")
    decision = classify_intent(goal, packs)
    assert "rlm-gaming" not in decision.squads, (
        f"rlm-gaming wrongly selected for non-game goal {goal!r}: "
        f"{decision.squads} (rationale: {decision.rationale})"
    )


# Recall guard: genuine game goals must still select rlm-gaming. These carry
# several unambiguous game terms, so pruning the ambient words costs no recall.
@pytest.mark.parametrize(
    "goal",
    [
        "design the boss encounter and gacha loot for our roguelite RPG vertical slice",
        "plan the netcode and matchmaking for our multiplayer FPS with anti-cheat",
        "scope the season pass and battle pass for our live service game economy",
        # leaner recall cases — guard the gaps codex flagged from pruning:
        "balance the economy for our MMO",
        "tune the boss fight difficulty in our soulslike",
    ],
)
def test_game_goal_still_routes_to_rlm_gaming(packs, goal):
    if "rlm-gaming" not in packs:
        pytest.skip("rlm-gaming pack not discovered in this checkout")
    decision = classify_intent(goal, packs)
    assert "rlm-gaming" in decision.squads, (
        f"rlm-gaming NOT selected for game goal {goal!r}: "
        f"{decision.squads} (rationale: {decision.rationale})"
    )
