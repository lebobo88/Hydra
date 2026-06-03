"""Integration tests for the Senate (Curia Crown) legal-compliance wiring.

After the Senate pack shipped externally (2026-06-03), these tests pin the
Hydra-side contract: the legal-compliance squad loads with all 12 jurists,
the consilium sub-agents declare their parent jurists, the gatekeepers carry
HITL triggers, the router fires on legal + Roman-jurist keywords, the
cathedral overlay renders the Curia, the legal rubrics resolve in the judge
registry, and the senate MCP shim serves the pack.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from hydra_core.heads import crown_label_for_squad, load_aliases
from hydra_core.router import classify_intent
from hydra_core.squad_loader import discover_squads
from hydra_core.version import is_deprecated


REPO_ROOT = Path(__file__).resolve().parents[1]
SENATE_ROOT = REPO_ROOT.parent / "Senate"


@pytest.fixture(scope="module")
def packs():
    return discover_squads(REPO_ROOT)


# --- squad-loader: the Curia roster ------------------------------------------

def test_legal_squad_loads_with_twelve_jurists(packs):
    senate = packs["legal-compliance"]
    assert senate.entrypoint == "claude-skill"
    assert "Senate" in (senate.source_pack or "")
    assert len(senate.agents) == 12


def test_jurists_carry_mythic_names(packs):
    senate = packs["legal-compliance"]
    mythic_by_plaza = {a.slug: a.mythic for a in senate.agents}
    assert mythic_by_plaza["general-counsel"] == "Papinian"
    assert mythic_by_plaza["citation-verifier"] == "Tribonian"
    assert mythic_by_plaza["privacy-counsel"] == "Angerona"
    assert mythic_by_plaza["litigation-counsel"] == "Cicero"


def test_consilium_sub_agents_declare_parent_jurists(packs):
    senate = packs["legal-compliance"]
    parents = {a.slug: a.parent for a in senate.agents if a.parent}
    assert parents == {
        "employment-counsel": "contract-counsel",
        "tax-counsel": "mna-counsel",
        "export-controls": "regulatory-counsel",
    }


def test_papinian_is_gatekeeper_on_opus(packs):
    senate = packs["legal-compliance"]
    gc = next(a for a in senate.agents if a.slug == "general-counsel")
    assert gc.authority == "gatekeeper"
    assert gc.model_tier == "opus"


def test_hitl_triggered_jurists(packs):
    # Angerona (privacy), Tribonian (fabrication breach), Janus (sanctions)
    senate = packs["legal-compliance"]
    by_slug = {a.slug: a for a in senate.agents}
    for slug in ("privacy-counsel", "citation-verifier", "export-controls"):
        assert by_slug[slug].hitl_trigger is True, slug
        assert by_slug[slug].authority == "gatekeeper", slug


def test_senate_envelope_contract(packs):
    senate = packs["legal-compliance"]
    assert "HANDOFF" in senate.accepts
    assert "C_SUITE_DECISION_PACKET" in senate.accepts
    assert "DECISION_RECORD" in senate.emits
    assert "HITL_REQUEST" in senate.emits


# --- gates --------------------------------------------------------------------

def test_senate_declares_six_gates_with_always_on_pair(packs):
    senate = packs["legal-compliance"]
    rubric_ids = {g.rubric_id for g in senate.gates}
    assert {
        "citation-integrity@1", "aba-512-ethics@1",
        "gdpr-art-25-privacy-by-design@1", "eu-ai-act-classification@1",
        "open-source-license-compatibility@1", "compliance-coverage@1",
    }.issubset(rubric_ids)


def test_citation_integrity_and_ethics_gates_require_hitl(packs):
    senate = packs["legal-compliance"]
    by_rubric = {g.rubric_id: g for g in senate.gates}
    assert by_rubric["citation-integrity@1"].hitl_required is True
    assert by_rubric["aba-512-ethics@1"].hitl_required is True


# --- legal-compliance is active, not a stub -----------------------------------

def test_senate_is_active_not_deprecated(packs):
    senate = packs.get("legal-compliance")
    assert senate is not None
    assert senate.deprecated_after is None
    assert not is_deprecated(senate.deprecated_after)
    assert senate.entrypoint != "stub"


def test_iolaus_allows_dispatch_to_active_senate(packs):
    from hydra_core.iolaus import pre_dispatch, SpawnLedger
    from hydra_core.schemas import CSuiteDecisionPacket
    from uuid import uuid4

    senate = packs["legal-compliance"]
    env = CSuiteDecisionPacket(
        workflow_id=uuid4(), origin_squad="hydra",
        target_squad="legal-compliance", origin="BOARDROOM",
        objective="contract review",
    )
    verdict = pre_dispatch(senate, env, ledger=SpawnLedger())
    assert verdict is not None


# --- router fires on legal + mythic keywords -----------------------------------

def test_router_picks_legal_on_jurist_names(packs):
    decision = classify_intent("Ask Papinian and Tribonian to verify this opinion", packs)
    assert "legal-compliance" in decision.squads


def test_router_picks_legal_on_senate_keyword(packs):
    decision = classify_intent("Convene the senate on the vendor contract redline", packs)
    assert "legal-compliance" in decision.squads


def test_router_picks_legal_on_operational_vocabulary(packs):
    decision = classify_intent(
        "We need a DPIA and an indemnification clause review for the EU rollout",
        packs,
    )
    assert "legal-compliance" in decision.squads


# --- cathedral overlay ---------------------------------------------------------

def test_curia_heads_yaml_overlay_loads_all_twelve_jurists():
    aliases = load_aliases(REPO_ROOT)
    expected = {
        "general-counsel": "Papinian", "contract-counsel": "Gaius",
        "regulatory-counsel": "Ulpian", "privacy-counsel": "Angerona",
        "ip-counsel": "Minerva", "mna-counsel": "Scaevola",
        "litigation-counsel": "Cicero", "governance-counsel": "Cato",
        "citation-verifier": "Tribonian", "employment-counsel": "Paulus",
        "tax-counsel": "Modestinus", "export-controls": "Janus",
    }
    for plaza, mythic in expected.items():
        assert aliases[plaza].mythic == mythic, plaza
        assert aliases[plaza].crown == "curia", plaza


def test_crown_label_for_legal_squad_is_curia_crown():
    assert crown_label_for_squad("legal-compliance") == "the Curia Crown"


def test_no_mythic_name_collision_with_other_crowns():
    # Themis belongs to the executive CLO; the Curia must not reuse any
    # mythic name from another crown.
    aliases = load_aliases(REPO_ROOT)
    curia = {a.mythic for a in aliases.values() if a.crown == "curia"}
    others = {a.mythic for a in aliases.values() if a.crown != "curia"}
    assert not curia & others, f"collisions: {curia & others}"
    assert "Themis" not in curia


# --- judge plane: legal rubrics resolve -----------------------------------------

def test_legal_rubrics_registered_in_judge_registry():
    from hydra_core.judge.registry import get_rubric
    for rid in (
        "citation-integrity@1", "aba-512-ethics@1",
        "gdpr-art-25-privacy-by-design@1", "eu-ai-act-classification@1",
        "open-source-license-compatibility@1", "compliance-coverage@1",
    ):
        r = get_rubric(rid)
        assert r.score_dimensions, rid


def test_judge_router_binds_always_on_legal_rubrics():
    from hydra_core.judge.router import route_judge
    route = route_judge(
        {"type": "HANDOFF", "target_squad": "legal-compliance",
         "payload": {"objective": "review the vendor MSA"}},
        origin_squad="hydra",
    )
    assert route.tier == "cross_vendor"
    assert "citation-integrity@1" in route.rubric_ids
    assert "aba-512-ethics@1" in route.rubric_ids
    assert "compliance-coverage@1" in route.rubric_ids


def test_judge_router_adds_topic_conditional_legal_rubrics():
    from hydra_core.judge.router import route_judge
    route = route_judge(
        {"type": "HANDOFF", "target_squad": "legal-compliance",
         "payload": {"objective": "GDPR DPIA for an AI system using GPL code"}},
        origin_squad="hydra",
    )
    assert "gdpr-art-25-privacy-by-design@1" in route.rubric_ids
    assert "eu-ai-act-classification@1" in route.rubric_ids
    assert "open-source-license-compatibility@1" in route.rubric_ids


# --- claude-skill shim selection ------------------------------------------------

def test_skill_pack_shim_registry_maps_legal_to_senate():
    from hydra_core.squad_node import _SKILL_PACK_SHIMS
    shim = _SKILL_PACK_SHIMS["legal-compliance"]
    assert shim["server"] == "senate"
    assert shim["prefix"] == "senate"
    assert shim["default_cmd"] == "/senate"
    # garland mapping unchanged (legacy default)
    assert _SKILL_PACK_SHIMS["garland"]["server"] == "rlm_creative"


def test_via_claude_skill_dispatches_senate_tools(packs):
    """The generalized claude-skill executor must call senate.* tools (not
    rlm.*) for the legal-compliance squad, and label the artifact legal."""
    from uuid import uuid4
    from hydra_core.schemas import Handoff
    from hydra_core.squad_node import _via_claude_skill
    from hydra_core.state import HydraState

    calls: list[tuple[str, str]] = []

    class FakeDispatcher:
        def call_mcp(self, server, tool, args, *, squad_id=None):
            calls.append((server, tool))
            if tool.endswith("command.list"):
                return {"status": "done",
                        "result": {"commands": [{"name": "senate.md"}]}}
            if tool.endswith("output.write"):
                return {"status": "done",
                        "result": {"relative": "output/legal/test.md"}}
            return {"status": "done", "result": {}}

        def spawn_subprocess(self, cmd, env=None):  # pragma: no cover
            raise NotImplementedError

        def emit_claude_prompt(self, prompt, *, agent=None):  # pragma: no cover
            raise NotImplementedError

        def invoke_claude_skill(self, skill, args):
            return {"status": "host_pickup_required", "summary": "queued"}

    senate = packs["legal-compliance"]
    env = Handoff(
        workflow_id=uuid4(), origin_squad="hydra",
        target_squad="legal-compliance",
        granted_tools=[], granted_memory_scopes=[],
        payload_envelope_id=uuid4(),
    )
    state = HydraState(workflow_id=env.workflow_id)
    result = _via_claude_skill(state, senate, env, FakeDispatcher())

    servers = {s for s, _ in calls}
    assert servers == {"senate"}
    tools = {t for _, t in calls}
    assert "senate.command.list" in tools
    assert "senate.output.write" in tools
    assert result.host_pickup_pending is True
    assert result.artifacts[0]["kind"] == "legal_output"
    ref_keys = [r.key for r in result.envelopes[0].artifacts]
    assert any(k.startswith("senate:output:") for k in ref_keys)


# --- mcp shim serves the pack ----------------------------------------------------

def test_senate_shim_handlers_serve_roster_and_ping():
    if not SENATE_ROOT.is_dir():
        pytest.skip("Senate repo not checked out next to Hydra")
    import os
    os.environ.setdefault("HYDRA_SENATE_ROOT", str(SENATE_ROOT))
    from mcp_servers.senate.server import _tool_handlers
    handlers = _tool_handlers()
    ping = handlers["senate.ping"]({})
    assert ping["ok"] is True and ping["exists"] is True
    roster = handlers["senate.roster.list"]({})
    names = {a["name"] for a in roster["agents"]}
    assert "general-counsel" in names
    assert len(names) == 12
    gc = handlers["senate.agent.get"]({"slug": "general-counsel"})
    assert "Papinian" in gc.get("content", "")


def test_senate_static_tool_catalog_registered():
    from hydra_core.toolshed import SENATE_TOOLS, build_default_shed
    assert "senate.ping" in SENATE_TOOLS
    shed = build_default_shed()
    assert "senate" in shed.servers
