"""The Ouroboros loop — Hydra's self-improvement cycle.

Knud Thomsen's *Ouroboros Model* (arXiv:0805.2815) names this as
"a self-referential recursive process with alternating phases of data
acquisition and evaluation… contradictions between anticipations based on
previous experience and actual current data are highlighted."

In Hydra terms: every N decisions, re-read episodic outcomes per cell,
update what worked (Dui) and what hurt (Kan), and *propose* (not commit)
procedural updates. The constitution gate sits at the queue door; the
user is the final approver. The Ouroboros bites its tail under the eye
of the immortal head.

A deterministic stub `default_summarize_cell()` is provided so the cycle
is testable without an LLM. Callers wire a real summarizer (LLM, vector
search, anomaly detector) through the `summarize_cell` parameter.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional

from .eights import ALL_CELLS, Cell, CELL_SPECS
from .immortal_head import ConstitutionSnapshot, load_constitution
from .memory import EPISODIC_DB, query_by_cell
from .procedural import ProceduralStore, ProposalResult, propose


# --- summarizer interface ----------------------------------------------------

CellSummarizer = Callable[[Cell, list[dict]], "CellSummary"]


@dataclass
class CellSummary:
    cell: Cell
    n_rows: int
    headline: str
    proposed_updates: list[dict] = field(default_factory=list)
    # each proposed update: {"kind","summary","body","cells"}


def default_summarize_cell(cell: Cell, rows: list[dict]) -> CellSummary:
    """Deterministic stub. Counts rows, picks the most recent payload's first
    key as the headline, proposes a single 'memory_pruning' update if the
    cell holds more than 200 rows. Useful for tests and for sanity-checking
    the cycle wiring before plugging in an LLM."""
    if not rows:
        return CellSummary(cell=cell, n_rows=0, headline=f"{cell} is empty.")
    spec = CELL_SPECS[cell]
    headline = f"{spec.name} ({spec.chinese} {spec.trigram}): {len(rows)} episodic row(s)."
    proposed: list[dict] = []
    if len(rows) > 200:
        proposed.append({
            "kind": "memory_pruning",
            "summary": f"Prune oldest episodic rows in {spec.name} cell beyond 200.",
            "body": (
                f"Cell '{cell}' holds {len(rows)} rows. Recommend retaining the most "
                "recent 200 and archiving the remainder."
            ),
            "cells": [cell],
        })
    return CellSummary(cell=cell, n_rows=len(rows), headline=headline, proposed_updates=proposed)


# --- cycle -------------------------------------------------------------------

@dataclass
class ReflectionReport:
    started_at: str
    finished_at: str
    constitution_hash: str
    summaries: list[CellSummary]
    proposals: list[ProposalResult]

    @property
    def n_proposed(self) -> int:
        return len(self.proposals)

    @property
    def n_refused(self) -> int:
        return sum(1 for p in self.proposals if p.update.status == "refused")

    @property
    def n_queued(self) -> int:
        return sum(1 for p in self.proposals if p.update.status == "pending")


def run_reflection_cycle(
    *,
    cells: Optional[Iterable[Cell]] = None,
    window: int = 100,
    workflow_id: Optional[str] = None,
    summarize_cell: CellSummarizer = default_summarize_cell,
    store: Optional[ProceduralStore] = None,
    constitution: Optional[ConstitutionSnapshot] = None,
    db: Path = EPISODIC_DB,
) -> ReflectionReport:
    """Run one Ouroboros cycle.

    For each cell, pull the most-recent `window` rows, hand them to
    `summarize_cell`, and pipe any proposed updates into the procedural
    queue (gated by the constitution).

    No procedural update is *committed* here — only *proposed*. The user
    (or Iris, in a later stage) approves through `procedural.approve()`.
    """
    cells_to_visit = tuple(cells) if cells else ALL_CELLS
    snap = constitution or load_constitution()
    started = datetime.now(timezone.utc).isoformat()

    summaries: list[CellSummary] = []
    proposals: list[ProposalResult] = []

    for cell in cells_to_visit:
        rows = query_by_cell(cell, limit=window, workflow_id=workflow_id, db=db)
        summary = summarize_cell(cell, rows)
        summaries.append(summary)
        for prop in summary.proposed_updates:
            result = propose(
                kind=prop.get("kind", "routing_heuristic"),
                summary=prop.get("summary", ""),
                body=prop.get("body", ""),
                proposed_by="ouroboros",
                workflow_id=workflow_id,
                cells=prop.get("cells", [cell]),
                store=store,
                constitution=snap,
            )
            proposals.append(result)

    return ReflectionReport(
        started_at=started,
        finished_at=datetime.now(timezone.utc).isoformat(),
        constitution_hash=snap.sha256,
        summaries=summaries,
        proposals=proposals,
    )
