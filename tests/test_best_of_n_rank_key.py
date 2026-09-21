"""``hydra_core.squad_node._rank_key`` is the sole ranking key for
``sorted(eligible, key=lambda s: s["rank"], reverse=True)`` in the best-of-N
selection loop -- the comparison that decides WHICH CANDIDATE'S CODE gets
merged. A genuine float NaN/Infinity rubric-score dimension (the judge
model's own untrusted response) used to poison that key: every comparison
against a NaN is False, so the candidate's position in the sort became
effectively arbitrary -- and silently, because a run with a poisoned rank
looks exactly like an ordinary run that chose a winner.

`judge.dispatcher.dispatch_judge` already refuses to even construct a
`JudgeVerdict` carrying a non-finite score (`find_non_finite_field` on the
verdict, raised BEFORE the verdict is ever returned to a caller -- see that
module for the live evidence). This module's independent best-of-N loop
never routed its own `score` dict through that or any other guard, so the
same defect class survived here, on the path with the sharpest consequence
in this whole thread.

These tests assert the SELECTION OUTCOME (which candidate's critique/code
"wins"), not merely that `_rank_key`'s return value is finite.

Follow-up round correction: the first fix EXCLUDED a non-finite dimension
from the mean. That was itself wrong -- excluding raises the mean of the
remainder, so a poisoned dimension could IMPROVE a candidate's rank. See
the "Finding 3" tests below for the corrected fix (a non-finite dimension
now counts AGAINST the candidate at a score floor, and every dimension is
clamped into a bounded range so the outcome/smoke dominance invariant holds
for negative and huge inputs too).
"""
from __future__ import annotations

from typing import Any

import pytest

from hydra_core.squad_node import _drive_pp_stage_loop, _rank_key


# ---------------------------------------------------------------------------
# Unit level: _rank_key itself, and the exact sort expression the real
# best-of-N loop uses (`sorted(eligible, key=lambda s: s["rank"],
# reverse=True)`), reproduced verbatim against a `scored`-shaped list.
# ---------------------------------------------------------------------------

def _select_winner(scored: list[dict[str, Any]]) -> dict[str, Any]:
    """The exact selection expression `_drive_best_of_loop` uses once Borda
    is unavailable/inconclusive (duplicate/missing attempt ids): the
    top-ranked candidate after a reverse sort by `rank`."""
    ranked = sorted(scored, key=lambda s: s["rank"], reverse=True)
    return ranked[0]


def test_nan_dimension_does_not_win_over_a_genuinely_better_candidate():
    """The candidate under test.

    Candidate A: same outcome/smoke as B, but one rubric dimension is a
    genuine float NaN (a hostile/buggy judge report) and NOTHING ELSE --
    after exclusion it has no usable dimension at all (mean = 0.0).
    Candidate B: the same outcome/smoke, a real positive score.
    B must win -- A's poisoned/absent score must not out-rank a real one.
    """
    rank_a = _rank_key("pass", {"correctness": float("nan")}, "pass")
    rank_b = _rank_key("pass", {"correctness": 0.9}, "pass")
    scored = [
        {"ci": 1, "att": "att_T", "outcome": "pass", "rank": rank_a,
         "smoke": "pass", "critique": "candidate-A (poisoned)"},
        {"ci": 2, "att": "att_T", "outcome": "pass", "rank": rank_b,
         "smoke": "pass", "critique": "candidate-B (genuine, better)"},
    ]
    winner = _select_winner(scored)
    assert winner["ci"] == 2
    assert winner["critique"] == "candidate-B (genuine, better)"


def test_nan_dimension_candidate_still_loses_when_listed_first():
    """Same as above but with the poisoned candidate FIRST in `scored` --
    this is exactly the arrangement that exposed the arbitrary-sort bug
    (Python's stable sort leaves an indeterminate (NaN) comparison's
    relative position largely governed by input order), so it is the
    sharpest regression guard: order must never decide the winner."""
    rank_a = _rank_key("pass", {"correctness": float("nan")}, "pass")
    rank_b = _rank_key("pass", {"correctness": 0.9}, "pass")
    scored = [
        {"ci": 1, "att": "att_T", "outcome": "pass", "rank": rank_a,
         "smoke": "pass", "critique": "candidate-A (poisoned, listed first)"},
        {"ci": 2, "att": "att_T", "outcome": "pass", "rank": rank_b,
         "smoke": "pass", "critique": "candidate-B (genuine, better)"},
    ]
    winner = _select_winner(scored)
    assert winner["ci"] == 2


def test_ranking_is_deterministic_and_ordered_with_a_poisoned_dimension_present():
    """A poisoned dimension must not merely fail to win by accident -- the
    FULL ordering must be deterministic and correctly reflect the genuine
    scores, run repeatedly (guards against relying on incidental sort
    stability of a single run)."""
    rank_poisoned = _rank_key("pass", {"correctness": float("inf")}, "pass")
    rank_mid = _rank_key("pass", {"correctness": 0.5}, "pass")
    rank_high = _rank_key("pass", {"correctness": 0.9}, "pass")
    scored = [
        {"ci": 1, "rank": rank_poisoned, "att": "a"},
        {"ci": 2, "rank": rank_mid, "att": "b"},
        {"ci": 3, "rank": rank_high, "att": "c"},
    ]
    for _ in range(5):
        ranked = sorted(scored, key=lambda s: s["rank"], reverse=True)
        assert [s["ci"] for s in ranked] == [3, 2, 1]


def test_candidate_with_no_usable_dimension_neither_wins_nor_loses_unfairly():
    """Decision (2): every dimension unusable (all non-finite/non-numeric)
    contributes `mean = 0.0` -- the same NEUTRAL default this function
    already used for "no score dimensions reported at all". It must not
    let a candidate win purely because its rank happens to be a poisoned
    value (already covered above), NOR must the 0.0 default itself act as
    a fabricated advantage: a candidate with a genuine positive score
    (same outcome/smoke) must still beat an all-unusable candidate, and an
    all-unusable candidate must still beat a WORSE outcome/smoke
    regardless of its neutral 0.0 rubric contribution."""
    rank_all_unusable = _rank_key(
        "pass", {"correctness": float("nan"), "adherence": "not-a-number"}, "pass",
    )
    rank_genuine_positive = _rank_key("pass", {"correctness": 0.1}, "pass")
    rank_worse_outcome = _rank_key("revise", {"correctness": 9.0}, "pass")

    # A genuine (even small) positive score beats an all-unusable candidate
    # at the same outcome/smoke.
    assert rank_genuine_positive > rank_all_unusable
    # The all-unusable candidate's neutral 0.0 still correctly loses to
    # nothing worse than its own outcome tier -- but it still beats a
    # WORSE outcome regardless of that outcome's high (but irrelevant,
    # since verdict dominates) rubric score.
    assert rank_all_unusable > rank_worse_outcome
    # And it is exactly equal to a candidate that has literally no score
    # dimensions at all -- the pre-existing, unrelated-to-this-fix behavior.
    assert rank_all_unusable == _rank_key("pass", {}, "pass")


def test_ordinary_finite_scores_control_unaffected():
    """The control that matters most: every normal best-of-N run (no
    non-finite dimension anywhere) ranks and selects exactly as before."""
    rank_a = _rank_key("pass", {"correctness": 0.7, "adherence": 0.8}, "pass")
    rank_b = _rank_key("pass", {"correctness": 0.2, "adherence": 0.3}, "pass")
    scored = [
        {"ci": 1, "rank": rank_a, "critique": "A"},
        {"ci": 2, "rank": rank_b, "critique": "B"},
    ]
    winner = _select_winner(scored)
    assert winner["ci"] == 1
    assert winner["critique"] == "A"
    # Exact pre-existing arithmetic: base*1000 + smoke*100 + mean.
    assert rank_a == pytest.approx(2000.0 + 100.0 + 0.75)


# ---------------------------------------------------------------------------
# Finding 3 (follow-up round, HIGH): an EARLIER version of this fix simply
# EXCLUDED a non-finite dimension from the mean -- but excluding RAISES the
# mean of the remainder, so a poisoned dimension could IMPROVE a candidate's
# rank (a candidate benefits from its worst dimension being unusable). Fixed
# by counting a non-finite dimension AGAINST the candidate at the score
# floor (included in the mean) instead of dropping it, and by clamping every
# dimension into a bounded range so the outcome/smoke dominance invariant
# holds for negative and huge inputs too (the "outcome/smoke dominate
# absolutely" claim was false as previously written: `min(mean, 999)`
# bounded the mean above but not below).
# ---------------------------------------------------------------------------

def test_poisoned_dimension_cannot_improve_a_candidates_rank():
    """The inversion this finding is about: a candidate reporting an
    ADDITIONAL poisoned dimension alongside a genuine one must never rank
    BETTER than if it had reported the genuine dimension alone (excluding
    a poisoned dimension would raise the mean of the remainder and do
    exactly that)."""
    rank_with_poison = _rank_key("pass", {"a": 0.9, "b": float("nan")}, "pass")
    rank_without_extra_dim = _rank_key("pass", {"a": 0.9}, "pass")
    assert rank_with_poison <= rank_without_extra_dim
    # And strictly worse against a genuine second dimension, in EITHER
    # selection ordering.
    rank_genuine_second_dim = _rank_key("pass", {"a": 0.9, "b": 0.3}, "pass")
    for scored in (
        [{"ci": "poisoned", "rank": rank_with_poison},
         {"ci": "genuine", "rank": rank_genuine_second_dim}],
        [{"ci": "genuine", "rank": rank_genuine_second_dim},
         {"ci": "poisoned", "rank": rank_with_poison}],
    ):
        assert _select_winner(scored)["ci"] == "genuine"


def test_negative_and_huge_scores_cannot_break_the_outcome_invariant():
    """The docstring's stated invariant -- a `fail` can never outrank a
    `revise`/`pass`, regardless of score magnitude -- verified for
    UNBOUNDED negative, unbounded positive, and poisoned inputs together,
    not just the documented 0-1/0-10 rubric range."""
    huge_fail = _rank_key("fail", {"a": 1e12}, "pass")
    tiny_revise = _rank_key("revise", {"a": -1e12}, "skipped")
    huge_revise = _rank_key("revise", {"a": 1e12}, "pass")
    tiny_pass = _rank_key("pass", {"a": -1e12}, "skipped")
    poisoned_pass = _rank_key("pass", {"a": float("inf")}, "skipped")

    assert tiny_revise > huge_fail        # revise ALWAYS beats fail
    assert tiny_pass > huge_revise        # pass ALWAYS beats revise
    assert poisoned_pass > huge_revise    # a poisoned pass still beats any revise
    # A negative score does not drag a pass below a revise, nor let an
    # all-floored candidate look worse than it should relative to a WORSE
    # outcome tier.
    assert _rank_key("pass", {"a": -9999.0}, "skipped") > \
        _rank_key("revise", {"a": 9999.0}, "pass")


def test_all_dimensions_unusable_equals_all_dimensions_floored():
    """An all-unusable candidate (no numeric dimension at all) and a
    candidate whose only dimension came back non-finite (floored to 0, but
    still counted) both land at the SAME neutral contribution -- neither
    is treated as if it had a real positive score, and neither is punished
    beyond the floor."""
    assert _rank_key("pass", {}, "pass") == _rank_key(
        "pass", {"a": float("nan")}, "pass",
    )


# ---------------------------------------------------------------------------
# End-to-end through the real best-of-N drive loop: a poisoned judge report
# on one candidate does not change which candidate's code actually merges.
# ---------------------------------------------------------------------------

class _BestOfCandidateDispatcher:
    """Scripted dispatcher (mirrors test_drive_pp_loop.py's
    `_ScriptedDispatcher`) that returns a DIFFERENT critique response per
    candidate, keyed by the candidate's worktree path -- the real drive
    loop always sets `cwd=candidate.worktree_path` on the critique call."""

    def __init__(self, responses: dict, critique_by_cwd: dict[str, dict]):
        self.responses = responses
        self.critique_by_cwd = critique_by_cwd
        self.calls: list[tuple[str, str, dict]] = []
        self.drive_pp_loop = True

    def call_mcp(self, server: str, tool: str, args: dict, *, squad_id=None):
        self.calls.append((server, tool, args))
        if (server, tool) == ("pp_codex", "critique"):
            cwd = args.get("cwd")
            if cwd in self.critique_by_cwd:
                return self.critique_by_cwd[cwd]
        return self.responses.get((server, tool), {"status": "done", "result": {}})


def _e2e_responses(n_candidates: int = 2) -> dict:
    cands = [{"candidate_index": i, "judge_position": i,
              "attempt_slot_id": f"slot{i}", "worktree_path": f"/tmp/c{i}",
              "worktree_mode": "copy"} for i in range(1, n_candidates + 1)]
    return {
        ("pp_harness", "start_run"): {"status": "done", "result": {"run_id": "run_T"}},
        ("pp_harness", "start_stage"): {"status": "done", "result": {"stage_id": "st_T"}},
        ("pp_harness", "gate_eligible_judges"): {"status": "done", "result": {
            "required_cross_vendor": False, "rubric_id": "rfc-2119-normative"}},
        ("pp_harness", "start_best_of_stage"): {"status": "done", "result": {
            "stage_id": "st_BO", "candidates": cands, "shuffle_seed": 1}},
        ("pp_codex", "generate"): {"status": "done", "result": {
            "text": "edited foo.py\n{\"status\": \"pass\", \"reason\": \"exit 0\"}",
            "model": "codex-1",
            "tokens_in": 5, "tokens_out": 7, "cost_usd": 0.02, "wall_ms": 100}},
        ("pp_harness", "archive_artifact"): {"status": "done", "result": {"path": ".harness/x"}},
        # Deliberately the SAME attempt id for every candidate: forces the
        # winner resolution to fall through to the top-RANKED candidate
        # (a duplicate id can't uniquely match Borda's "winner", by design)
        # -- exactly the code path this fix protects.
        ("pp_harness", "record_attempt"): {"status": "done", "result": {"attempt_id": "att_T"}},
        ("pp_harness", "borda_count"): {"status": "done", "result": {
            "winner": "att_T", "scores": []}},
        ("pp_harness", "record_verdict"): {"status": "done", "result": {}},
        ("pp_harness", "finalize_stage"): {"status": "done", "result": {}},
        ("pp_harness", "finalize_run"): {"status": "done", "result": {"status": "complete"}},
        ("pp_harness", "archive_winner_and_losers"): {"status": "done", "result": {
            "merge_status": "merged", "winner_diff_path": "code/winner.diff",
            "losers_archived": n_candidates - 1}},
        ("pp_harness", "teardown_candidates"): {"status": "done", "result": {
            "teardown_status": "ok"}},
    }


def test_e2e_best_of_n_poisoned_judge_score_does_not_change_the_winner(monkeypatch):
    """Candidate 1 (evaluated/listed FIRST -- the arrangement that exposed
    the arbitrary-sort bug) gets a judge report with a non-finite rubric
    dimension and nothing else. Candidate 2 gets a genuine, clearly better
    score. Candidate 2's code must be the one that merges, both before and
    after this fix is proven irrelevant to ordering by the unit tests above
    -- this proves it through the REAL drive loop end to end."""
    monkeypatch.setenv("HYDRA_BEST_OF_N", "2")
    monkeypatch.setattr("hydra_core.squad_node._run_smoke",
                        lambda *_a, **_k: ("pass", "ok"))

    critique_by_cwd = {
        "/tmp/c1": {"status": "done", "result": {"parsed": {
            "outcome": "pass", "critique_md": "candidate-1-poisoned",
            "score": {"correctness": float("nan")},
        }}},
        "/tmp/c2": {"status": "done", "result": {"parsed": {
            "outcome": "pass", "critique_md": "candidate-2-genuine-better",
            "score": {"correctness": 0.9},
        }}},
    }
    disp = _BestOfCandidateDispatcher(_e2e_responses(2), critique_by_cwd)
    out = _drive_pp_stage_loop(
        disp, run_id="run_T", project_path="/tmp/proj", request_text="do it")

    assert out["final_status"] == "complete"
    assert out["critique"] == "candidate-2-genuine-better"
