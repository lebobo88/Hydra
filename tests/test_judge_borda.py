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
        judge_vendor="agy",
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


def test_string_valued_score_dimension_is_excluded_not_cast():
    """Verification finding (cross-vendor judge, follow-up round): a judge
    reporting a score dimension as a STRING (e.g. a hostile/buggy vendor
    sending `"correctness": "NaN"`) is NOT cast to float here --
    `_verdict_score`'s `isinstance(val, (int, float))` filter excludes it
    from the sum entirely BEFORE any `float()` call, so the string-bypass
    this thread closes elsewhere does not apply to this cast site. This is
    a verification/regression-lock test, not a fix: `score_json` is an
    untyped dict (pydantic does not coerce a string in an `Any`-shaped
    field), so a hostile string genuinely reaches this function unchanged --
    it is simply never cast. Pinned here so a future refactor of
    `_verdict_score` that removes the isinstance guard is caught."""
    wf = uuid4()
    a, b = str(uuid4()), str(uuid4())
    verdicts = [
        # `a`'s hostile string score dimension is excluded -> only "real"=1 counts.
        _v(a, "r@1", {"correctness": "NaN", "real": 1}, wf),
        _v(b, "r@1", {"real": 5}, wf),
    ]
    winner, board = borda_winner([a, b], verdicts)
    assert winner == b  # b's real=5 beats a's real=1; the string never inflates/poisons a's score


def test_bool_valued_score_dimension_does_not_inflate_ranking():
    """Cross-vendor judge finding (follow-up round, HIGH): `bool` is an
    `int` subclass in Python, so `isinstance(val, (int, float))` alone lets
    a boolean dimension through -- `score_json` is the judge's own
    UNTRUSTED response with arbitrary keys, so a hostile/buggy judge
    reporting `"looks_good": True` would previously add `float(True) ==
    1.0` to the sum, inflating a candidate's Borda ranking for free.
    Excluded explicitly (mirroring `squad_node._rank_key`'s own
    `not isinstance(v, bool)` guard) -- proven here by SELECTION, not by
    inspecting the summed score directly."""
    wf = uuid4()
    a, b = str(uuid4()), str(uuid4())
    verdicts = [
        # `a`'s only REAL score (real=1) is strictly lower than b's genuine
        # real=1.5; a's bogus boolean dimension, IF counted, would push a's
        # total to 2 (1 bool + 1 real) -- strictly ABOVE b's 1.5, flipping
        # the winner. Chosen so the outcome is a clean strict inequality
        # flip either way, never a tie broken by (random) candidate id.
        _v(a, "r@1", {"looks_good": True, "real": 1}, wf),
        _v(b, "r@1", {"real": 1.5}, wf),
    ]
    winner, _ = borda_winner([a, b], verdicts)
    assert winner == b  # the bool must not let a's lower real score win


def test_ordinary_numeric_scores_control_unaffected():
    """Control: ordinary int/float score dimensions (no bool, no string,
    no non-finite) rank and select exactly as before."""
    wf = uuid4()
    a, b = str(uuid4()), str(uuid4())
    verdicts = [
        _v(a, "r@1", {"correctness": 4, "adherence": 3.5}, wf),
        _v(b, "r@1", {"correctness": 1, "adherence": 1.0}, wf),
    ]
    winner, _ = borda_winner([a, b], verdicts)
    assert winner == a
