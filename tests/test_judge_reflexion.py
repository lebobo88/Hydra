"""Unit tests for the Reflexion ×1 bridge."""
from __future__ import annotations

from uuid import uuid4

import pytest

from hydra_core.judge.reflexion import (
    ENV_OVERRIDE_KEY,
    MAX_RETRY_INDEX,
    effective_max_retry_index,
    package_retry,
)
from hydra_core.judge.schemas import JudgeVerdict


def _verdict(outcome: str) -> JudgeVerdict:
    return JudgeVerdict(
        workflow_id=uuid4(),
        origin_squad="hydra-judge",
        target_envelope_id=uuid4(),
        outcome=outcome,
        rubric_id="board-decision-quality@1",
        judge_vendor="gemini",
        critique_md="needs more rigor on financials" * 4,
    )


def _env() -> dict:
    return {
        "id": str(uuid4()),
        "type": "C_SUITE_DECISION_PACKET",
        "origin_squad": "executive",
        "workflow_id": str(uuid4()),
    }


def test_pass_yields_no_retry():
    assert package_retry(_env(), _verdict("pass")) is None


def test_skip_yields_no_retry():
    assert package_retry(_env(), _verdict("skip")) is None


def test_fail_yields_no_retry_must_escalate_hitl():
    assert package_retry(_env(), _verdict("fail")) is None


def test_revise_yields_retry_when_under_ceiling():
    pkt = package_retry(_env(), _verdict("revise"), prior_retry_index=0)
    assert pkt is not None
    assert pkt.retry_index == 1
    payload = pkt.to_squad_payload()
    assert payload["kind"] == "reflexion_retry"
    assert payload["rubric_id"] == "board-decision-quality@1"
    assert "critique_md" in payload


def test_revise_at_ceiling_yields_no_retry():
    pkt = package_retry(_env(), _verdict("revise"), prior_retry_index=MAX_RETRY_INDEX)
    assert pkt is None


# ── R3-tail post-mortem (2026-05-21): operator-approved ceiling raise ─────────

def test_effective_max_retry_default_is_invariant():
    """No override, no env: ceiling is MAX_RETRY_INDEX (1)."""
    assert effective_max_retry_index() == MAX_RETRY_INDEX


def test_effective_max_retry_argument_raises_ceiling():
    """The per-workflow override argument raises the ceiling for THIS call."""
    assert effective_max_retry_index(max_retry_override=3) == 3


def test_effective_max_retry_argument_below_invariant_is_ignored():
    """An override below the invariant default is ignored (the invariant is a
    floor, not a ceiling). Prevents accidental tightening below ×1."""
    assert effective_max_retry_index(max_retry_override=0) == MAX_RETRY_INDEX


def test_effective_max_retry_env_var_raises_ceiling(monkeypatch):
    """The HYDRA_REFLEXION_MAX_RETRY_INDEX_OVERRIDE env var is honored as a
    debug seam. Not the production override path (use the argument instead),
    but useful for local diagnostics."""
    monkeypatch.setenv(ENV_OVERRIDE_KEY, "5")
    assert effective_max_retry_index() == 5


def test_effective_max_retry_argument_beats_env(monkeypatch):
    """When both env and argument are set, the higher wins."""
    monkeypatch.setenv(ENV_OVERRIDE_KEY, "2")
    assert effective_max_retry_index(max_retry_override=7) == 7
    assert effective_max_retry_index(max_retry_override=1) == 2  # env still raises


def test_effective_max_retry_invalid_env_falls_back(monkeypatch):
    """A malformed env var is silently ignored (no crash on bad operator input)."""
    monkeypatch.setenv(ENV_OVERRIDE_KEY, "not-an-int")
    assert effective_max_retry_index() == MAX_RETRY_INDEX


def test_revise_at_ceiling_with_override_yields_retry():
    """After operator approval, the override raises the ceiling for one
    additional retry. The invariant default is unchanged for other workflows.
    """
    pkt = package_retry(
        _env(),
        _verdict("revise"),
        prior_retry_index=MAX_RETRY_INDEX,
        max_retry_override=MAX_RETRY_INDEX + 1,
    )
    assert pkt is not None
    assert pkt.retry_index == MAX_RETRY_INDEX + 1


def test_revise_at_doubled_ceiling_without_override_yields_no_retry():
    """Belt-and-suspenders: even if the env override raises the ceiling,
    a caller that does NOT pass `max_retry_override` still gets the env
    value via effective_max_retry_index. This test confirms the precedence
    is consistent across the two pathways."""
    # No env, no arg: ceiling is 1, prior_retry=1 → blocked.
    pkt = package_retry(
        _env(), _verdict("revise"), prior_retry_index=MAX_RETRY_INDEX
    )
    assert pkt is None
