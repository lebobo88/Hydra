"""Unit tests for hydra_core.judge.dispatcher — mocked critique client."""
from __future__ import annotations

from uuid import uuid4

import pytest

from hydra_core.judge.dispatcher import (
    JudgeDispatchError,
    MIN_CRITIQUE_CHARS,
    NoOpCritiqueClient,
    dispatch_judge,
)


def _env() -> dict:
    return {
        "id": str(uuid4()),
        "type": "C_SUITE_DECISION_PACKET",
        "origin_squad": "executive",
        "workflow_id": str(uuid4()),
        "origin": "BOARDROOM",
        "objective": "fund Q3 plan",
    }


class _ScriptedClient:
    def __init__(self, response: dict, raises: Exception | None = None):
        self.response = response
        self.raises = raises
        self.calls: list[dict] = []

    def critique(self, *, vendor, artifact_text, rubric_md):
        self.calls.append({"vendor": vendor, "rubric_md": rubric_md, "artifact_text": artifact_text})
        if self.raises:
            raise self.raises
        return self.response


def test_noop_client_returns_pass_with_skeleton_marker():
    wf = uuid4()
    verdict = dispatch_judge(
        envelope=_env(),
        rubric_id="board-decision-quality@1",
        judge_vendor="gemini",
        workflow_id=wf,
        client=NoOpCritiqueClient(),
    )
    # Pragmatic-pass guard: NoOp has _skeleton only, so substantive scores=0 →
    # downgrades to "revise".
    assert verdict.outcome == "revise"
    assert "pragmatic-pass guard tripped" in verdict.critique_md


def test_real_pass_with_substantive_scores_remains_pass():
    wf = uuid4()
    client = _ScriptedClient({
        "outcome": "pass",
        "critique_md": "Solid memo. " * 20,  # well over 80 chars
        "score_json": {"objective_clarity": 5, "risk_treatment": 4},
    })
    verdict = dispatch_judge(
        envelope=_env(),
        rubric_id="board-decision-quality@1",
        judge_vendor="gemini",
        workflow_id=wf,
        client=client,
    )
    assert verdict.outcome == "pass"
    assert verdict.score_json["objective_clarity"] == 5


def test_short_critique_pass_gets_downgraded():
    wf = uuid4()
    client = _ScriptedClient({
        "outcome": "pass",
        "critique_md": "ok",  # < MIN_CRITIQUE_CHARS
        "score_json": {"objective_clarity": 5},
    })
    verdict = dispatch_judge(
        envelope=_env(),
        rubric_id="board-decision-quality@1",
        judge_vendor="gemini",
        workflow_id=wf,
        client=client,
    )
    assert verdict.outcome == "revise"


def test_empty_scores_pass_gets_downgraded():
    wf = uuid4()
    client = _ScriptedClient({
        "outcome": "pass",
        "critique_md": "x" * (MIN_CRITIQUE_CHARS + 10),
        "score_json": {},
    })
    verdict = dispatch_judge(
        envelope=_env(),
        rubric_id="board-decision-quality@1",
        judge_vendor="gemini",
        workflow_id=wf,
        client=client,
    )
    assert verdict.outcome == "revise"


def test_client_exception_surfaces_as_dispatch_error():
    wf = uuid4()
    client = _ScriptedClient({}, raises=RuntimeError("MCP unreachable"))
    with pytest.raises(JudgeDispatchError):
        dispatch_judge(
            envelope=_env(),
            rubric_id="board-decision-quality@1",
            judge_vendor="gemini",
            workflow_id=wf,
            client=client,
        )


def test_rubric_body_is_passed_to_judge():
    wf = uuid4()
    client = _ScriptedClient({
        "outcome": "revise",
        "critique_md": "needs work " * 10,
        "score_json": {"x": 1},
    })
    dispatch_judge(
        envelope=_env(),
        rubric_id="constitution-alignment@1",
        judge_vendor="gemini",
        workflow_id=wf,
        client=client,
    )
    assert len(client.calls) == 1
    assert "Constitution Alignment Rubric" in client.calls[0]["rubric_md"]
    assert "<untrusted-artifact>" in client.calls[0]["artifact_text"]


def test_unknown_rubric_raises():
    wf = uuid4()
    with pytest.raises(KeyError):
        dispatch_judge(
            envelope=_env(),
            rubric_id="not-a-real-rubric@99",
            judge_vendor="gemini",
            workflow_id=wf,
            client=NoOpCritiqueClient(),
        )
