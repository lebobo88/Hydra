"""Tests for hydra_core.judge.best_of_n — judge_and_rank + best_of_n_run."""
from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from hydra_core.judge.best_of_n import best_of_n_run, judge_and_rank


class _ScoredClient:
    """Returns scores keyed by an `_id` field embedded in the artifact text.

    Lets us hand-craft which candidate wins.
    """
    def __init__(self, score_table: dict[str, dict[str, float]]):
        self.score_table = score_table
        self.calls: list[str] = []

    def critique(self, *, vendor, artifact_text, rubric_md):
        # The artifact_text contains the envelope JSON; pull out the id field.
        chosen_id = None
        for cid in self.score_table:
            if cid in artifact_text:
                chosen_id = cid
                break
        if chosen_id is None:
            chosen_id = next(iter(self.score_table))
        self.calls.append(chosen_id)
        return {
            "outcome": "pass",
            "critique_md": "thorough analysis " * 10,
            "score_json": dict(self.score_table[chosen_id]),
        }


def _envelope(eid: str) -> dict:
    return {
        "id": eid,
        "type": "C_SUITE_DECISION_PACKET",
        "origin_squad": "executive",
        "workflow_id": str(uuid4()),
        "origin": "BOARDROOM",
        "objective": f"candidate {eid}",
    }


def test_judge_and_rank_picks_highest_scorer():
    a, b, c = (
        "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        "cccccccc-cccc-cccc-cccc-cccccccccccc",
    )
    client = _ScoredClient({
        a: {"objective_clarity": 3, "risk_treatment": 2},
        b: {"objective_clarity": 5, "risk_treatment": 5},  # winner
        c: {"objective_clarity": 1, "risk_treatment": 1},
    })
    outcome = judge_and_rank(
        [_envelope(a), _envelope(b), _envelope(c)],
        rubric_ids=["board-decision-quality@1"],
        workflow_id=uuid4(),
        client=client,
    )
    assert outcome.winner_id == b
    assert len(outcome.losers) == 2
    assert len(outcome.verdicts) == 3
    assert all(v.outcome == "pass" for v in outcome.verdicts)


def test_judge_and_rank_rejects_empty():
    with pytest.raises(ValueError):
        judge_and_rank([], rubric_ids=["x@1"], workflow_id=uuid4())


def test_best_of_n_run_invokes_producer_n_times():
    seen = []

    def produce(i: int) -> dict:
        eid = f"{i:08d}-0000-0000-0000-000000000000"
        seen.append(i)
        return _envelope(eid)

    client = _ScoredClient({
        f"{i:08d}-0000-0000-0000-000000000000": {"objective_clarity": 5 - i}
        for i in range(3)
    })
    outcome = best_of_n_run(
        n=3,
        produce=produce,
        rubric_ids=["board-decision-quality@1"],
        workflow_id=uuid4(),
        client=client,
    )
    assert seen == [0, 1, 2]
    # i=0 has the highest score, so its id should win.
    assert outcome.winner_id.startswith("00000000")


def test_best_of_n_run_rejects_n_zero():
    with pytest.raises(ValueError):
        best_of_n_run(n=0, produce=lambda i: _envelope("x"), rubric_ids=["x@1"], workflow_id=uuid4())
