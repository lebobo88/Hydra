"""Procedural memory spine.

The third tier of TheEights (the crossing point of the lemniscate). Where
episodic memory remembers *what happened* and semantic memory remembers
*what is true*, procedural memory remembers *how to act*: routing
heuristics, prompt rewrites, "next time, try X first."

Every procedural update flows through the immortal-head gate before it
enters the queue. A proposed rewrite that contradicts the constitution
is refused at admission, not at commit — the queue itself does not hold
unconstitutional drafts.

State machine:
    propose() → pending → approve() → committed
                       ↘ reject()  ↘ rejected
                       ↘ enforce_constitution() refuses → never queued

The queue is in-memory by default; production deployments wire the
`ProceduralStore` interface to a database. The default `InMemoryStore`
is sufficient for tests and for the bootstrap dev loop.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal, Optional, Protocol
from uuid import UUID, uuid4

from .eights import Cell, validate_cells
from .governance import enforce_constitution
from .immortal_head import AlignmentVerdict, ConstitutionSnapshot


# --- domain types ------------------------------------------------------------

ProceduralKind = Literal[
    "routing_heuristic",     # "for goal X, prefer squad Y first"
    "prompt_rewrite",        # "replace head H's system prompt with …"
    "policy_adjustment",     # "raise budget tripwire from 80% → 75%"
    "deprecation_proposal",  # "retire squad/agent Z"
    "memory_pruning",        # "drop episodic rows older than N days in cell C"
]


ProceduralStatus = Literal["pending", "committed", "rejected", "refused"]


@dataclass
class ProceduralUpdate:
    """A proposed change to the system's *how-to-act* substrate."""
    id: UUID = field(default_factory=uuid4)
    kind: ProceduralKind = "routing_heuristic"
    summary: str = ""
    body: str = ""                          # the actual change (text, json, diff)
    proposed_by: str = "reflection"         # which head / cycle proposed it
    workflow_id: Optional[str] = None       # the workflow that surfaced the lesson
    cells: list[Cell] = field(default_factory=list)
    status: ProceduralStatus = "pending"
    rationale: str = ""                     # why approve/reject; from constitution if refused
    constitution_hash: Optional[str] = None  # which constitution snapshot was in force
    proposed_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    decided_at: Optional[str] = None
    decided_by: Optional[str] = None        # "user" | "iris" | "auto"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "kind": self.kind,
            "summary": self.summary,
            "body": self.body,
            "proposed_by": self.proposed_by,
            "workflow_id": self.workflow_id,
            "cells": list(self.cells),
            "status": self.status,
            "rationale": self.rationale,
            "constitution_hash": self.constitution_hash,
            "proposed_at": self.proposed_at,
            "decided_at": self.decided_at,
            "decided_by": self.decided_by,
        }


# --- store ------------------------------------------------------------------

class ProceduralStore(Protocol):
    def put(self, update: ProceduralUpdate) -> None: ...
    def get(self, update_id: UUID) -> Optional[ProceduralUpdate]: ...
    def list(self, *, status: Optional[ProceduralStatus] = None) -> list[ProceduralUpdate]: ...


class InMemoryStore:
    """Thread-safe in-process queue. Production wires a SQLite or row-store."""

    def __init__(self) -> None:
        self._by_id: dict[UUID, ProceduralUpdate] = {}
        self._lock = threading.Lock()

    def put(self, update: ProceduralUpdate) -> None:
        with self._lock:
            self._by_id[update.id] = update

    def get(self, update_id: UUID) -> Optional[ProceduralUpdate]:
        with self._lock:
            return self._by_id.get(update_id)

    def list(self, *, status: Optional[ProceduralStatus] = None) -> list[ProceduralUpdate]:
        with self._lock:
            items = list(self._by_id.values())
        if status is not None:
            items = [u for u in items if u.status == status]
        return sorted(items, key=lambda u: u.proposed_at)


_DEFAULT_STORE = InMemoryStore()


def default_store() -> ProceduralStore:
    return _DEFAULT_STORE


# --- API --------------------------------------------------------------------

@dataclass
class ProposalResult:
    update: ProceduralUpdate
    verdict: AlignmentVerdict

    @property
    def accepted_to_queue(self) -> bool:
        return self.update.status == "pending"


def propose(
    *,
    kind: ProceduralKind,
    summary: str,
    body: str,
    proposed_by: str = "reflection",
    workflow_id: Optional[str] = None,
    cells: Optional[list[str]] = None,
    store: Optional[ProceduralStore] = None,
    constitution: Optional[ConstitutionSnapshot] = None,
) -> ProposalResult:
    """Submit a procedural update. Runs the constitution gate first; aligned
    drafts enter the queue with status='pending', refused drafts go to the
    store with status='refused' so the refusal itself is recorded."""
    snap = constitution
    payload = {"kind": kind, "summary": summary, "body": body, "proposed_by": proposed_by}
    verdict = enforce_constitution(payload, snapshot=snap)

    update = ProceduralUpdate(
        kind=kind,
        summary=summary,
        body=body,
        proposed_by=proposed_by,
        workflow_id=workflow_id,
        cells=validate_cells(cells or []),
        status="pending" if verdict.aligned else "refused",
        rationale=verdict.rationale,
        constitution_hash=(snap.sha256 if snap else None),
    )
    if not verdict.aligned:
        update.decided_at = datetime.now(timezone.utc).isoformat()
        update.decided_by = "constitution"

    (store or _DEFAULT_STORE).put(update)
    return ProposalResult(update=update, verdict=verdict)


def approve(
    update_id: UUID,
    *,
    approved_by: str = "user",
    store: Optional[ProceduralStore] = None,
) -> Optional[ProceduralUpdate]:
    """Commit a pending update. Returns the updated record, or None if the
    update is missing or not in 'pending'."""
    s = store or _DEFAULT_STORE
    u = s.get(update_id)
    if u is None or u.status != "pending":
        return None
    u.status = "committed"
    u.decided_at = datetime.now(timezone.utc).isoformat()
    u.decided_by = approved_by
    s.put(u)
    return u


def reject(
    update_id: UUID,
    *,
    rejected_by: str = "user",
    reason: str = "",
    store: Optional[ProceduralStore] = None,
) -> Optional[ProceduralUpdate]:
    """Drop a pending update. Returns the updated record, or None if missing
    or not in 'pending'."""
    s = store or _DEFAULT_STORE
    u = s.get(update_id)
    if u is None or u.status != "pending":
        return None
    u.status = "rejected"
    u.decided_at = datetime.now(timezone.utc).isoformat()
    u.decided_by = rejected_by
    if reason:
        u.rationale = (u.rationale + " | " + reason).strip(" |")
    s.put(u)
    return u


def pending(store: Optional[ProceduralStore] = None) -> list[ProceduralUpdate]:
    """All pending updates awaiting human (or Iris) approval."""
    return (store or _DEFAULT_STORE).list(status="pending")
