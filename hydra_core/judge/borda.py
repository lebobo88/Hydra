"""Borda count for best-of-N candidate ranking.

Mirrors pair-programmer's `daemon/src/orchestrator/best-of-n.ts:27`. Each judge
verdict provides a ranking (via numeric scores); Borda aggregates ranks across
all judges/rubrics to pick a winner that resists verbosity bias.

Hydra uses this in `deliberation.py` when a squad emits N independent drafts and
the squad pack declares `best_of_n: N` (N≥3).
"""
from __future__ import annotations

from typing import Sequence

from .schemas import JudgeVerdict


def _verdict_score(v: JudgeVerdict) -> float:
    """Sum of numeric score dimensions (ignoring underscore-prefixed metadata).

    Cross-vendor judge finding (follow-up round, HIGH): `score_json` is the
    judge model's own UNTRUSTED response with arbitrary keys -- a hostile
    or buggy judge can add a non-underscore-prefixed BOOLEAN dimension
    (e.g. `"looks_good": True`). `bool` is an `int` subclass in Python, so
    `isinstance(val, (int, float))` alone lets it through, and `float(True)
    == 1.0` inflates the sum exactly like a real score point -- the same
    class of exposure `squad_node._rank_key` already guards against via its
    own `not isinstance(v, bool)` check, mirrored here.
    """
    return sum(
        float(val)
        for k, val in (v.score_json or {}).items()
        if not k.startswith("_")
        and isinstance(val, (int, float))
        and not isinstance(val, bool)
    )


def borda_winner(
    candidates: Sequence[str],
    verdicts: Sequence[JudgeVerdict],
) -> tuple[str, list[tuple[str, int]]]:
    """Rank-aggregate verdicts and return (winner_id, [(candidate_id, borda_points), ...]).

    Algorithm:
      - Group verdicts by rubric.
      - Within each rubric, rank candidates by total numeric score (desc).
      - Award Borda points: top gets N-1, next N-2, ..., last 0.
      - Sum points across rubrics. Highest total wins.
      - Ties broken by lexicographic candidate_id (deterministic).

    `candidates` is the ordered list of candidate envelope IDs (string form).
    Each verdict's `target_envelope_id` must match one of them.
    """
    if not candidates:
        raise ValueError("borda_winner requires at least one candidate")
    if len(candidates) == 1:
        return candidates[0], [(candidates[0], 0)]

    cand_ids = [str(c) for c in candidates]
    points: dict[str, int] = {c: 0 for c in cand_ids}

    by_rubric: dict[str, list[JudgeVerdict]] = {}
    for v in verdicts:
        by_rubric.setdefault(v.rubric_id, []).append(v)

    for rubric_id, vs in by_rubric.items():
        # Map candidate_id -> aggregated score for this rubric (sum across any
        # multiple verdicts that hit the same candidate under the same rubric).
        scores: dict[str, float] = {c: 0.0 for c in cand_ids}
        seen = False
        for v in vs:
            tid = str(v.target_envelope_id)
            if tid in scores:
                scores[tid] += _verdict_score(v)
                seen = True
        if not seen:
            continue
        # Rank desc by score, deterministic tiebreak by candidate id.
        ranked = sorted(cand_ids, key=lambda c: (-scores[c], c))
        n = len(ranked)
        for rank, cid in enumerate(ranked):
            points[cid] += (n - 1 - rank)

    leaderboard = sorted(points.items(), key=lambda kv: (-kv[1], kv[0]))
    return leaderboard[0][0], leaderboard
