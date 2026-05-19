"""Best-of-N orchestration for executive + creative squads.

Wraps the existing `deliberation.deliberate()` cycle: produce N independent
DeliberationOutcomes, judge each, Borda-rank, return the winner. Losers are
returned alongside so callers can archive them to episodic memory tagged with
the `Kan` (dissent) cell per TheEights taxonomy.

This module is content-agnostic — it works for any artifact list, not only
deliberation outcomes. The supervisor's executive/creative squad nodes will
opt-in via the `best_of_n: N` field in `squads/<slug>/squad.yaml`.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Sequence
from uuid import UUID

from .borda import borda_winner
from .dispatcher import CritiqueClient, dispatch_judge
from .schemas import JudgeVendor, JudgeVerdict


@dataclass
class BestOfNOutcome:
    winner_envelope: dict[str, Any]
    winner_id: str
    leaderboard: list[tuple[str, int]]
    verdicts: list[JudgeVerdict]
    losers: list[dict[str, Any]]


def judge_and_rank(
    candidates: Sequence[dict[str, Any]],
    *,
    rubric_ids: Sequence[str],
    workflow_id: UUID,
    judge_vendor: JudgeVendor = "gemini",
    client: CritiqueClient | None = None,
    generator_vendor: str = "unknown",
) -> BestOfNOutcome:
    """Judge N candidate envelopes against the rubrics and return the winner.

    Each rubric is applied to each candidate (so total judge calls = N × R).
    Borda count aggregates across rubrics.

    Raises ValueError if `candidates` is empty.
    """
    if not candidates:
        raise ValueError("judge_and_rank requires at least one candidate")

    verdicts: list[JudgeVerdict] = []
    cand_ids: list[str] = []
    for env in candidates:
        cand_ids.append(str(env.get("id")))
        for rubric_id in rubric_ids:
            v = dispatch_judge(
                envelope=env,
                rubric_id=rubric_id,
                judge_vendor=judge_vendor,
                workflow_id=workflow_id,
                generator_vendor=generator_vendor,
                client=client,
            )
            verdicts.append(v)

    winner_id, leaderboard = borda_winner(cand_ids, verdicts)
    by_id = {str(env.get("id")): env for env in candidates}
    winner = by_id[winner_id]
    losers = [env for cid, env in by_id.items() if cid != winner_id]

    return BestOfNOutcome(
        winner_envelope=winner,
        winner_id=winner_id,
        leaderboard=leaderboard,
        verdicts=verdicts,
        losers=losers,
    )


def best_of_n_run(
    *,
    n: int,
    produce: Callable[[int], dict[str, Any]],
    rubric_ids: Sequence[str],
    workflow_id: UUID,
    judge_vendor: JudgeVendor = "gemini",
    client: CritiqueClient | None = None,
    generator_vendor: str = "unknown",
) -> BestOfNOutcome:
    """High-level helper: invoke `produce(i)` N times to generate candidates,
    then judge_and_rank.

    `produce(i)` receives the candidate index (0..N-1) so the caller can seed
    diversity (different temperatures, different head subsets, etc.).
    """
    if n < 1:
        raise ValueError(f"best_of_n_run requires n>=1, got {n}")
    candidates = [produce(i) for i in range(n)]
    return judge_and_rank(
        candidates,
        rubric_ids=rubric_ids,
        workflow_id=workflow_id,
        judge_vendor=judge_vendor,
        client=client,
        generator_vendor=generator_vendor,
    )
