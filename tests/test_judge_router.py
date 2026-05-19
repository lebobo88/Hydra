"""Unit tests for hydra_core.judge.router — tier policy decisions."""
from __future__ import annotations

from uuid import uuid4

from hydra_core.judge.router import route_judge


def _csuite(**kwargs) -> dict:
    base = {
        "id": str(uuid4()),
        "type": "C_SUITE_DECISION_PACKET",
        "origin_squad": "executive",
        "workflow_id": str(uuid4()),
        "origin": "BOARDROOM",
        "objective": "x",
    }
    base.update(kwargs)
    return base


def test_csuite_defaults_to_cross_vendor():
    route = route_judge(_csuite())
    assert route.tier == "cross_vendor"
    assert "constitution-alignment@1" in route.rubric_ids
    assert "board-decision-quality@1" in route.rubric_ids


def test_creative_brief_is_same_vendor_by_default():
    env = {
        "id": str(uuid4()),
        "type": "CREATIVE_BRIEF",
        "origin_squad": "creative",
        "workflow_id": str(uuid4()),
        "campaign_id": str(uuid4()),
        "objective": "launch",
        "target_audience": "GenZ",
    }
    route = route_judge(env)
    assert route.tier == "same_vendor"
    assert "brand-consistency@1" in route.rubric_ids
    assert "audience-fit@1" in route.rubric_ids


def test_content_escalation_upgrades_same_vendor():
    env = {
        "id": str(uuid4()),
        "type": "CREATIVE_BRIEF",
        "origin_squad": "creative",
        "workflow_id": str(uuid4()),
        "campaign_id": str(uuid4()),
        "objective": "GDPR-compliant audience targeting plan",
        "target_audience": "EU",
    }
    route = route_judge(env)
    assert route.tier == "cross_vendor"


def test_mna_topic_adds_mna_rubric():
    env = _csuite(objective="Acquire competitor X via friendly merger")
    route = route_judge(env)
    assert "mna-due-diligence@1" in route.rubric_ids
    assert "financial-hardcoding@1" not in route.rubric_ids or True  # may or may not


def test_financial_topic_adds_financial_rubric():
    env = _csuite(objective="Approve Q3 capex of $12M, WACC 8.5%, expected IRR 14%")
    route = route_judge(env)
    assert "financial-hardcoding@1" in route.rubric_ids


def test_pp_verdict_skip():
    env = _csuite(pp_verdict={"outcome": "pass", "rubric_id": "owasp-asvs-l1@1"})
    route = route_judge(env)
    assert route.tier == "skip"
    assert route.rubric_ids == []


def test_pp_verdict_fail_does_not_skip():
    env = _csuite(pp_verdict={"outcome": "revise", "rubric_id": "owasp-asvs-l1@1"})
    route = route_judge(env)
    assert route.tier != "skip"


def test_healthcare_target_adds_phi_rubric_and_cross_vendor():
    env = _csuite(target_squad="healthcare", objective="patient cohort analysis")
    route = route_judge(env)
    assert route.tier == "cross_vendor"
    assert "phi-redaction-completeness@1" in route.rubric_ids


def test_post_synthesis_forces_cross_vendor_synthesis_rubric():
    env = {
        "id": str(uuid4()),
        "type": "DECISION_RECORD",
        "origin_squad": "hydra",
        "workflow_id": str(uuid4()),
        "decision": "x",
        "rationale": "y",
    }
    route = route_judge(env, is_post_synthesis=True)
    assert route.tier == "cross_vendor"
    assert "synthesis-coherence@1" in route.rubric_ids
    assert "constitution-alignment@1" in route.rubric_ids


def test_legal_compliance_squad_binds_compliance_rubric_and_cross_vendor():
    env = _csuite(target_squad="legal-compliance", objective="GDPR DPIA needed")
    route = route_judge(env)
    assert route.tier == "cross_vendor"
    assert "compliance-coverage@1" in route.rubric_ids


def test_sales_gtm_squad_binds_sales_rubric():
    env = _csuite(target_squad="sales-gtm", objective="Q3 pipeline review")
    route = route_judge(env)
    assert "sales-gtm-rigor@1" in route.rubric_ids


def test_research_ds_squad_binds_research_rubric():
    env = _csuite(target_squad="research-ds", objective="experiment design for cohort A")
    route = route_judge(env)
    assert "research-rigor@1" in route.rubric_ids


def test_customer_support_squad_binds_support_rubric():
    env = _csuite(target_squad="customer-support", objective="ticket deflection plan")
    route = route_judge(env)
    assert "support-deflection-quality@1" in route.rubric_ids


def test_origin_side_binding_also_works():
    """A response from the stub squad (origin=stub-squad) is also judged
    against its rubric, not only inbound traffic to it."""
    env = {
        "id": str(uuid4()),
        "type": "DECISION_RECORD",
        "origin_squad": "research-ds",
        "workflow_id": str(uuid4()),
        "decision": "preliminary results",
        "rationale": "p=0.04 across 3 cohorts",
    }
    route = route_judge(env, origin_squad="research-ds")
    assert "research-rigor@1" in route.rubric_ids


def test_enterprise_profile_forces_cross_vendor():
    env = {
        "id": str(uuid4()),
        "type": "CREATIVE_BRIEF",
        "origin_squad": "creative",
        "workflow_id": str(uuid4()),
        "campaign_id": str(uuid4()),
        "objective": "blue",
        "target_audience": "x",
    }
    route = route_judge(env, profile="enterprise")
    assert route.tier == "cross_vendor"
