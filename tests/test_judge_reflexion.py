"""Unit tests for the Reflexion ×1 bridge."""
from __future__ import annotations

from uuid import uuid4

from hydra_core.judge.reflexion import MAX_RETRY_INDEX, package_retry
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
