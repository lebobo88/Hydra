"""Unit tests for hydra_core.judge.borda — rank aggregation determinism."""
from __future__ import annotations

from uuid import UUID, uuid4

from hydra_core.judge.borda import borda_winner
from hydra_core.judge.schemas import JudgeVerdict


def _v(target: str, rubric: str, score: dict, workflow_id: UUID) -> JudgeVerdict:
    return JudgeVerdict(
        workflow_id=workflow_id,
        origin_squad="hydra-judge",
        target_envelope_id=UUID(target),
        outcome="pass",
        rubric_id=rubric,
        judge_vendor="gemini",
        critique_md="x" * 100,
        score_json=score,
    )


def test_single_candidate_wins_trivially():
    wf = uuid4()
    cid = str(uuid4())
    winner, board = borda_winner([cid], [_v(cid, "r@1", {"a": 4}, wf)])
    assert winner == cid


def test_three_candidates_clear_winner():
    wf = uuid4()
    a, b, c = str(uuid4()), str(uuid4()), str(uuid4())
    verdicts = [
        _v(a, "board-decision-quality@1", {"objective_clarity": 5, "risk_treatment": 4}, wf),
        _v(b, "board-decision-quality@1", {"objective_clarity": 3, "risk_treatment": 2}, wf),
        _v(c, "board-decision-quality@1", {"objective_clarity": 1, "risk_treatment": 1}, wf),
    ]
    winner, board = borda_winner([a, b, c], verdicts)
    assert winner == a
    # Borda: a=2, b=1, c=0
    points = dict(board)
    assert points[a] > points[b] > points[c]


def test_multi_rubric_aggregation():
    wf = uuid4()
    a, b = str(uuid4()), str(uuid4())
    # a wins rubric1, b wins rubric2 — tied total
    verdicts = [
        _v(a, "r1@1", {"x": 5}, wf),
        _v(b, "r1@1", {"x": 1}, wf),
        _v(a, "r2@1", {"y": 1}, wf),
        _v(b, "r2@1", {"y": 5}, wf),
    ]
    winner, board = borda_winner([a, b], verdicts)
    # Tied at 1 borda point each → lexicographic tiebreak
    points = dict(board)
    assert points[a] == points[b] == 1
    assert winner == min(a, b)


def test_deterministic_tiebreak():
    wf = uuid4()
    a, b = "11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"
    verdicts = [
        _v(a, "r@1", {"x": 3}, wf),
        _v(b, "r@1", {"x": 3}, wf),
    ]
    winner, _ = borda_winner([a, b], verdicts)
    assert winner == a  # lexicographic
    winner2, _ = borda_winner([b, a], verdicts)
    assert winner2 == a  # order-of-candidates does NOT change result


def test_underscore_score_keys_ignored():
    wf = uuid4()
    a, b = str(uuid4()), str(uuid4())
    verdicts = [
        _v(a, "r@1", {"_skeleton": True, "real": 1}, wf),
        _v(b, "r@1", {"_skeleton": True, "real": 5}, wf),
    ]
    winner, _ = borda_winner([a, b], verdicts)
    assert winner == b
