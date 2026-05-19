"""Smoke tests — no network, no LLMs. Verify the core skeleton works."""
from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from hydra_core.schemas import (
    CSuiteDecisionPacket, Constraints, MemoryRef, PRD, UserStory, validate_envelope,
)
from hydra_core.squad_loader import discover_squads
from hydra_core.router import classify_intent
from hydra_core.state import HydraState
from hydra_core.governance import enforce_governance, redact_for_squad_boundary
from hydra_core.memory import append_episodic, resolve_episodic


HYDRA_ROOT = Path(__file__).resolve().parents[1]


def test_registry_discovers_eight_squads():
    packs = discover_squads(HYDRA_ROOT)
    assert set(packs) >= {
        "executive", "engineering", "creative",
        "legal-compliance", "healthcare", "sales-gtm",
        "research-ds", "customer-support",
    }


def test_router_picks_engineering_on_code_keywords():
    packs = discover_squads(HYDRA_ROOT)
    d = classify_intent("Refactor the payments microservice API to add idempotency keys", packs)
    assert "engineering" in d.squads
    assert d.confidence >= 0.25


def test_router_picks_executive_on_strategy_keywords():
    packs = discover_squads(HYDRA_ROOT)
    d = classify_intent("Refresh our 3-year strategy and update OKRs", packs)
    assert "executive" in d.squads


def test_router_picks_creative_on_campaign_keywords():
    packs = discover_squads(HYDRA_ROOT)
    d = classify_intent("Produce a video press kit for the brand launch", packs)
    assert "creative" in d.squads


def test_schema_validate_roundtrip():
    wf = uuid4()
    prd = PRD(
        workflow_id=wf,
        origin_squad="hydra",
        source_goal_id=uuid4(),
        summary="thing",
        user_stories=[UserStory(id="s1", as_a="x", i_want="y", so_that="z")],
        constraints=Constraints(budget_usd=10, priority="P1"),
    )
    dumped = prd.model_dump(mode="json")
    restored = validate_envelope(dumped)
    assert isinstance(restored, PRD)
    assert restored.summary == "thing"


def test_governance_surfaces_loop():
    state = HydraState(root_goal="x")
    state.iteration_count = 99
    state.loop_ceiling = 25
    packs = discover_squads(HYDRA_ROOT)
    verdict = enforce_governance(state, packs)
    assert verdict.surfaced
    assert "loop" in verdict.reason.lower()


def test_redaction_strips_email():
    out = redact_for_squad_boundary("email me at user@example.com please")
    assert "user@example.com" not in out


def test_episodic_roundtrip(tmp_path, monkeypatch):
    db = tmp_path / "ep.db"
    monkeypatch.setattr("hydra_core.memory.EPISODIC_DB", db)
    wf = uuid4()
    ref = append_episodic(wf, "test", {"hello": "world"}, db=db)
    got = resolve_episodic(ref.key, db=db)
    assert got is not None
    assert got["payload"]["hello"] == "world"
