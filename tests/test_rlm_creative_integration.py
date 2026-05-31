"""Integration tests for the RLM-Creative Garland Crown wiring.

After RLM-Creative shipped externally on 2026-05-19, these tests pin the
Hydra-side contract: the garland squad loads with all 13 agents, the
Garland sub-agents declare their Helios parent, the C2PA governance head
carries the HITL trigger, and the router fires on Muse keywords.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from hydra_core.heads import cathedral_name, crown_label_for_squad, load_aliases
from hydra_core.router import classify_intent
from hydra_core.squad_loader import discover_squads
from hydra_core.version import is_deprecated


REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def packs():
    return discover_squads(REPO_ROOT)


# --- squad-loader: AgentSpec / ToolSpec accept new fields --------------------

def test_creative_squad_loads_with_thirteen_agents(packs):
    garland = packs["garland"]
    assert garland.entrypoint == "claude-skill"
    assert "RLM-Creative" in (garland.source_pack or "")
    assert len(garland.agents) == 13


def test_creative_agents_carry_mythic_names(packs):
    garland = packs["garland"]
    mythic_by_plaza = {a.slug: a.mythic for a in garland.agents}
    assert mythic_by_plaza["brand-strategist"] == "Calliope"
    assert mythic_by_plaza["photo-cinema"] == "Helios"
    assert mythic_by_plaza["copywriter"] == "Erato"


def test_helios_sub_agents_have_photo_cinema_parent(packs):
    garland = packs["garland"]
    sub_agents = [a for a in garland.agents if a.parent == "photo-cinema"]
    sub_slugs = {a.slug for a in sub_agents}
    assert sub_slugs == {
        "video-synth", "audio-foley", "music-score",
        "dialogue-mix", "governance-c2pa",
    }


def test_governance_c2pa_is_hitl_triggered(packs):
    garland = packs["garland"]
    gov = next(a for a in garland.agents if a.slug == "governance-c2pa")
    assert gov.hitl_trigger is True
    assert gov.model_tier == "opus"


def test_calliope_and_helios_are_gatekeepers_on_opus(packs):
    garland = packs["garland"]
    by_slug = {a.slug: a for a in garland.agents}
    for slug in ("brand-strategist", "photo-cinema"):
        assert by_slug[slug].authority == "gatekeeper"
        assert by_slug[slug].model_tier == "opus"


def test_creative_tools_accept_composite_privilege_string(packs):
    garland = packs["garland"]
    eights_tool = next(t for t in garland.tools if t.name == "eights-memory")
    assert "read" in eights_tool.privilege and "write" in eights_tool.privilege


def test_creative_emits_and_accepts_creative_envelopes(packs):
    garland = packs["garland"]
    assert "CREATIVE_BRIEF" in garland.accepts
    assert "SHOT_LIST" in garland.emits
    assert "ASSET_JOB" in garland.emits


# --- four Garland gates ------------------------------------------------------

def test_creative_squad_declares_four_garland_gates(packs):
    garland = packs["garland"]
    rubric_ids = {g.rubric_id for g in garland.gates}
    assert {"brand-consistency", "ip-clearance", "media-cost-cap",
            "brand-safety"}.issubset(rubric_ids)


def test_ip_clearance_and_media_cost_cap_require_hitl(packs):
    garland = packs["garland"]
    by_rubric = {g.rubric_id: g for g in garland.gates}
    assert by_rubric["ip-clearance"].hitl_required is True
    assert by_rubric["media-cost-cap"].hitl_required is True


# --- garland is the active creative squad ------------------------------------
# garland graduated from a retired placeholder stub into the active
# RLM-Creative squad (claude-skill entrypoint, full Muse roster — see the
# router + cathedral-overlay tests below). It must NOT be flagged deprecated.
# The deprecation MECHANISM itself is covered by synthetic fixtures in
# tests/test_iolaus.py, so these assertions track garland's live state.

def test_garland_is_active_not_deprecated(packs):
    garland = packs.get("garland")
    assert garland is not None, "garland should be present in the registry"
    assert garland.deprecated_after is None
    assert not is_deprecated(garland.deprecated_after)
    assert garland.entrypoint != "stub"


def test_iolaus_allows_dispatch_to_active_garland(packs):
    from hydra_core.iolaus import pre_dispatch, SpawnLedger
    from hydra_core.schemas import CSuiteDecisionPacket
    from uuid import uuid4

    garland = packs["garland"]
    env = CSuiteDecisionPacket(
        workflow_id=uuid4(), origin_squad="hydra",
        target_squad="garland", origin="BOARDROOM",
        objective="creative work",
    )
    # Active squad → pre_dispatch returns a verdict without raising.
    verdict = pre_dispatch(garland, env, ledger=SpawnLedger())
    assert verdict is not None


# --- router fires on the new Muse keywords -----------------------------------

def test_router_picks_creative_on_muse_names(packs):
    decision = classify_intent("Send this to Calliope and Helios for review", packs)
    assert "garland" in decision.squads


def test_router_picks_creative_on_garland_keyword(packs):
    decision = classify_intent("Convene the garland crew on the rebrand", packs)
    assert "garland" in decision.squads


def test_router_picks_creative_on_creative_crew_phrase(packs):
    decision = classify_intent("Run the creative crew on Q3 campaign", packs)
    assert "garland" in decision.squads


# --- cathedral overlay -------------------------------------------------------

def test_creative_heads_yaml_overlay_loads_all_eight_muses():
    aliases = load_aliases(REPO_ROOT)
    expected = {
        "brand-strategist": "Calliope", "copywriter": "Erato",
        "content-strategist": "Polyhymnia", "social-community": "Terpsichore",
        "paid-acquisition": "Euterpe", "pr-earned": "Clio",
        "seo-discovery": "Urania", "photo-cinema": "Helios",
    }
    for plaza, mythic in expected.items():
        assert aliases[plaza].mythic == mythic
        assert aliases[plaza].crown == "garland"


def test_crown_label_for_creative_squad_is_garland_crown():
    assert crown_label_for_squad("garland") == "the Garland Crown"


# --- mcp config --------------------------------------------------------------

def test_user_scope_points_rlm_creative_at_new_root():
    """Regression guard: HYDRA_RLM_ROOT must point at RLM-Creative (the new
    Garland pack), not at the deprecated RLM-CLI-Starter. After the
    2026-05-21 user-scope migration, this lives in ~/.claude.json — skip when
    not present (e.g., CI without the user config).
    """
    import os
    from pathlib import Path
    user_cfg = Path(os.path.expanduser("~/.claude.json"))
    if not user_cfg.exists():
        import pytest
        pytest.skip("~/.claude.json not present in this environment")
    cfg = json.loads(user_cfg.read_text(encoding="utf-8"))
    rlm = cfg.get("mcpServers", {}).get("rlm_creative")
    if not rlm:
        import pytest
        pytest.skip("rlm_creative not registered at user scope")
    root = rlm["env"]["HYDRA_RLM_ROOT"]
    assert "RLM-Creative" in root
    assert "RLM-CLI-Starter" not in root
