"""Integration tests for the RLM-Creative Garland Crown wiring.

After RLM-Creative shipped externally on 2026-05-19, these tests pin the
Hydra-side contract: the creative squad loads with all 13 agents, the
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
    creative = packs["creative"]
    assert creative.entrypoint == "claude-skill"
    assert "RLM-Creative" in (creative.source_pack or "")
    assert len(creative.agents) == 13


def test_creative_agents_carry_mythic_names(packs):
    creative = packs["creative"]
    mythic_by_plaza = {a.slug: a.mythic for a in creative.agents}
    assert mythic_by_plaza["brand-strategist"] == "Calliope"
    assert mythic_by_plaza["photo-cinema"] == "Helios"
    assert mythic_by_plaza["copywriter"] == "Erato"


def test_helios_sub_agents_have_photo_cinema_parent(packs):
    creative = packs["creative"]
    sub_agents = [a for a in creative.agents if a.parent == "photo-cinema"]
    sub_slugs = {a.slug for a in sub_agents}
    assert sub_slugs == {
        "video-synth", "audio-foley", "music-score",
        "dialogue-mix", "governance-c2pa",
    }


def test_governance_c2pa_is_hitl_triggered(packs):
    creative = packs["creative"]
    gov = next(a for a in creative.agents if a.slug == "governance-c2pa")
    assert gov.hitl_trigger is True
    assert gov.model_tier == "opus"


def test_calliope_and_helios_are_gatekeepers_on_opus(packs):
    creative = packs["creative"]
    by_slug = {a.slug: a for a in creative.agents}
    for slug in ("brand-strategist", "photo-cinema"):
        assert by_slug[slug].authority == "gatekeeper"
        assert by_slug[slug].model_tier == "opus"


def test_creative_tools_accept_composite_privilege_string(packs):
    creative = packs["creative"]
    eights_tool = next(t for t in creative.tools if t.name == "eights-memory")
    assert "read" in eights_tool.privilege and "write" in eights_tool.privilege


def test_creative_emits_and_accepts_creative_envelopes(packs):
    creative = packs["creative"]
    assert "CREATIVE_BRIEF" in creative.accepts
    assert "SHOT_LIST" in creative.emits
    assert "ASSET_JOB" in creative.emits


# --- four Garland gates ------------------------------------------------------

def test_creative_squad_declares_four_garland_gates(packs):
    creative = packs["creative"]
    rubric_ids = {g.rubric_id for g in creative.gates}
    assert {"brand-consistency", "ip-clearance", "media-cost-cap",
            "brand-safety"}.issubset(rubric_ids)


def test_ip_clearance_and_media_cost_cap_require_hitl(packs):
    creative = packs["creative"]
    by_rubric = {g.rubric_id: g for g in creative.gates}
    assert by_rubric["ip-clearance"].hitl_required is True
    assert by_rubric["media-cost-cap"].hitl_required is True


# --- garland stub is retired -------------------------------------------------

def test_garland_stub_is_deprecated(packs):
    garland = packs.get("garland")
    assert garland is not None, "stub should still be present in the registry"
    assert garland.deprecated_after is not None
    assert is_deprecated(garland.deprecated_after), (
        "stub should be past its deprecated_after date so Iolaus refuses dispatch"
    )


def test_iolaus_refuses_dispatch_to_retired_garland_stub(packs):
    from hydra_core.iolaus import pre_dispatch, SpawnLedger
    from hydra_core.schemas import CSuiteDecisionPacket
    from uuid import uuid4
    from hydra_core.version import SquadDeprecated

    garland = packs["garland"]
    env = CSuiteDecisionPacket(
        workflow_id=uuid4(), origin_squad="hydra",
        target_squad="garland", origin="BOARDROOM",
        objective="creative work",
    )
    with pytest.raises(SquadDeprecated):
        pre_dispatch(garland, env, ledger=SpawnLedger())


# --- router fires on the new Muse keywords -----------------------------------

def test_router_picks_creative_on_muse_names(packs):
    decision = classify_intent("Send this to Calliope and Helios for review", packs)
    assert "creative" in decision.squads


def test_router_picks_creative_on_garland_keyword(packs):
    decision = classify_intent("Convene the garland crew on the rebrand", packs)
    assert "creative" in decision.squads


def test_router_picks_creative_on_creative_crew_phrase(packs):
    decision = classify_intent("Run the creative crew on Q3 campaign", packs)
    assert "creative" in decision.squads


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
    assert crown_label_for_squad("creative") == "the Garland Crown"


# --- mcp config --------------------------------------------------------------

def test_mcp_json_points_rlm_creative_at_new_root():
    raw = (REPO_ROOT / ".mcp.json").read_text(encoding="utf-8")
    cfg = json.loads(raw)
    rlm = cfg["mcpServers"]["rlm-creative"]
    root = rlm["env"]["HYDRA_RLM_ROOT"]
    assert "RLM-Creative" in root
    assert "RLM-CLI-Starter" not in root
