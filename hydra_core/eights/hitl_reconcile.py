"""HITL lifecycle reconciliation against TheEights' shared ledger (E2-17).

Hydra files a `hydra_gate` HITL request with TheEights on every gate it
raises, but nothing ever closed those rows: a workflow that reached a
terminal phase (resumed, rejected, aborted, reaped) left its ticket pending
forever. The 2026-09-01 E2E trace measured 858 pending rows across 763
distinct workflows, all of them terminal — the shared ledger was unusable as
an operator work queue.

This module holds the pure lookup/matching logic so the CLI wiring stays
thin and testable with a stub dispatcher:

  * :func:`row_workflow_id` / :func:`row_gate_node` — read the frozen
    `hydra_gate` payload contract (see ``EightsAttestor.hitl_request``).
  * :func:`resolve_for_workflow` — close one workflow's pending rows on a
    terminal transition (resume resolution, abort, reap).
  * :func:`reconcile` — the sweep behind ``hydra eights-hitl-reconcile``.

Every call here is fail-soft: an unreachable daemon yields
``unavailable: True`` and resolves nothing. Nothing in this module deletes
or mutates local state.
"""
from __future__ import annotations

import json
from typing import Any, Callable, Iterable, Optional

# Mirrors `hydra_core.cli._TERMINAL_PHASES`; kept local so this module stays
# importable without pulling the CLI in.
TERMINAL_PHASES = frozenset({"done", "surfaced"})


def _payload(row: dict) -> dict:
    """The row's payload as a dict (tolerating a JSON-string transport)."""
    payload = row.get("payload")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            return {}
    return payload if isinstance(payload, dict) else {}


def row_workflow_id(row: dict) -> str:
    """Workflow id a `hydra_gate` row belongs to ('' when unattributable)."""
    wf = _payload(row).get("workflow_id")
    if wf:
        return str(wf)
    return str(row.get("run_id") or "")


def row_gate_node(row: dict) -> str:
    """Gate identity of a `hydra_gate` row ('' for pre-C2 rows)."""
    return str(_payload(row).get("gate_node") or "")


def resolve_rows(
    attestor: Any,
    rows: Iterable[dict],
    *,
    note: str,
    decision: str = "rejected",
) -> dict[str, int]:
    """Resolve every row, counting successes and daemon refusals."""
    resolved = 0
    failed = 0
    for row in rows:
        request_id = row.get("request_id")
        if not request_id:
            continue
        out = attestor.hitl_resolve(
            request_id=str(request_id),
            decision=decision,
            note=note,
            workflow_id=row_workflow_id(row) or None,
        )
        if out is None:
            failed += 1
        else:
            resolved += 1
    return {"resolved": resolved, "failed": failed}


def resolve_for_workflow(
    attestor: Any,
    workflow_id: str,
    *,
    note: str,
    decision: str = "rejected",
    rows: Optional[list[dict]] = None,
    gate_node: Optional[str] = None,
) -> dict[str, Any]:
    """Close the pending ledger rows belonging to one workflow.

    ``rows`` lets a caller sweeping many workflows (``hydra reap --apply``)
    pay for a single ``hitl.list`` instead of one per workflow. When
    ``gate_node`` is given, only that gate's rows are closed — the same
    gate-identity scoping ``_prune_spooled_hitl_requests`` uses, so a
    different unresolved gate in the same workflow survives.
    """
    if rows is None:
        rows = attestor.hitl_list()
    if rows is None:
        return {"pending": 0, "resolved": 0, "failed": 0, "unavailable": True}
    wf = str(workflow_id)
    matched = [r for r in rows if row_workflow_id(r) == wf]
    if gate_node:
        matched = [r for r in matched if row_gate_node(r) == gate_node]
    out = resolve_rows(attestor, matched, note=note, decision=decision)
    out["pending"] = len(matched)
    out["unavailable"] = False
    return out


def reconcile(
    attestor: Any,
    phase_lookup: Callable[[str], Optional[str]],
    *,
    apply: bool = False,
    limit: Optional[int] = None,
) -> dict[str, Any]:
    """Sweep the pending queue against Hydra's own workflow state.

    ``phase_lookup(workflow_id)`` returns the workflow's phase, or ``None``
    when Hydra has no checkpoint for it (an orphan from a wiped checkpoint
    db, a foreign run id, or an unattributable row). Terminal and unknown
    rows are the zombies; active rows are left strictly alone.

    Returns ``{pending, terminal, unknown, active, resolved, failed, mode}``.
    With ``apply=False`` nothing is written and ``resolved`` is 0.
    """
    rows = attestor.hitl_list()
    if rows is None:
        return {
            "error": "eights_unreachable",
            "pending": 0, "terminal": 0, "unknown": 0,
            "active": 0, "resolved": 0, "failed": 0,
        }
    if limit is not None and limit >= 0:
        rows = rows[:limit]

    terminal: list[tuple[dict, str]] = []
    unknown: list[dict] = []
    active = 0
    for row in rows:
        wf = row_workflow_id(row)
        phase = phase_lookup(wf) if wf else None
        if phase is None:
            unknown.append(row)
        elif phase in TERMINAL_PHASES:
            terminal.append((row, phase))
        else:
            active += 1

    resolved = 0
    failed = 0
    if apply:
        for row, phase in terminal:
            out = resolve_rows(
                attestor, [row], note=f"workflow terminal: {phase}",
            )
            resolved += out["resolved"]
            failed += out["failed"]
        if unknown:
            out = resolve_rows(
                attestor, unknown, note="workflow terminal: unknown",
            )
            resolved += out["resolved"]
            failed += out["failed"]

    return {
        "pending": len(rows),
        "terminal": len(terminal),
        "unknown": len(unknown),
        "active": active,
        "resolved": resolved,
        "failed": failed,
    }
