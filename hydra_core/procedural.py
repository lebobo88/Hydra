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

# F36: Risk classification for each ProceduralKind.
# low    → approve() commits locally with an explicit posture comment.
# medium → approve() routes through eights evolution; fail-soft to 'pending'
#          when eights is unreachable (never silently commit).
# high   → approve() routes through eights evolution; fail CLOSED (→ 'rejected')
#          when eights is unreachable.
# critical → like high; reserved for future tightening.
_PROCEDURAL_RISK_CLASS: dict[str, Literal["low", "medium", "high", "critical"]] = {
    "routing_heuristic": "low",
    "prompt_rewrite": "medium",
    "policy_adjustment": "high",
    "deprecation_proposal": "high",
    "memory_pruning": "low",
}


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
    attestor: Optional[Any] = None,
) -> Optional[ProceduralUpdate]:
    """Commit a pending update. Returns the updated record, or None if the
    update is missing or not in 'pending'.

    F36 — risk_class gating:
      low    (routing_heuristic, memory_pruning):
               Local commit immediately. Posture: low-risk heuristic update;
               no governance escalation required by constitution §5.
      medium (prompt_rewrite):
               Route through eights evolution contract via `attestor`.
               Commit only after an eights verdict (status=='approved'/'committed').
               If eights is unreachable (attestor=None or transport error),
               fail-soft → leave status='pending' (never silently commit).
      high   (policy_adjustment, deprecation_proposal):
               Route through eights evolution contract via `attestor`.
               If eights is unreachable, fail CLOSED → set status='rejected'.
               A high-risk update that cannot get an eights verdict MUST NOT
               commit; the operator must retry when TheEights is healthy.
    """
    s = store or _DEFAULT_STORE
    u = s.get(update_id)
    if u is None or u.status != "pending":
        return None

    risk = _PROCEDURAL_RISK_CLASS.get(u.kind, "low")

    if risk == "low":
        # Low-risk: local commit. Explicit posture comment per F36: routing
        # heuristics and memory pruning are low-blast-radius; the constitution
        # §5 governance gate runs only at propose() time for these kinds.
        # POSTURE: low-risk local commit — no eights escalation.
        u.status = "committed"
        u.decided_at = datetime.now(timezone.utc).isoformat()
        u.decided_by = approved_by
        s.put(u)
        return u

    # medium or high: must route through eights evolution contract.
    # For medium: fail-soft to 'pending' when eights unreachable.
    # For high:   fail CLOSED to 'rejected' when eights unreachable.
    if attestor is None:
        if risk == "medium":
            # Fail-soft: leave as pending so operator can retry when eights is healthy.
            u.rationale = (
                (u.rationale + " | " if u.rationale else "")
                + f"F36: {risk} risk kind requires eights verdict; "
                "attestor not provided — leaving pending (fail-soft)."
            ).strip(" |")
            s.put(u)
            return u
        else:  # high / critical
            u.status = "rejected"
            u.decided_at = datetime.now(timezone.utc).isoformat()
            u.decided_by = "governance.fail_closed"
            u.rationale = (
                (u.rationale + " | " if u.rationale else "")
                + f"F36: {risk} risk kind requires eights verdict; "
                "attestor not provided — fail CLOSED."
            ).strip(" |")
            s.put(u)
            return u

    # Attestor provided — route through eights evolution.
    resource_id = f"procedural:{u.kind}:{u.id}"
    eights_verdict: Optional[dict] = None

    try:
        # Step 1: register the resource (idempotent).
        attestor.evolution_register(
            resource_kind=u.kind,
            resource_id=resource_id,
            body=u.body,
            summary=u.summary,
        )
        # Step 2: propose the evolution and read back the verdict.
        eights_verdict = attestor.evolution_propose(
            resource_id=resource_id,
            summary=u.summary,
            body=u.body,
            proposed_by=approved_by,
            workflow_id=u.workflow_id,
        )
    except Exception:  # noqa: BLE001 — fail per risk class on transport error
        eights_verdict = None

    # Interpret verdict: commit only on explicit approved/committed + successful
    # evolution_commit round-trip (F36 full round-trip requirement). Generic acks
    # ("ok", "done") are not explicit approvals — they keep status pending so
    # operators can retry and obtain a proper verdict.
    verdict_status = ""
    if isinstance(eights_verdict, dict):
        verdict_status = str(
            eights_verdict.get("status")
            or eights_verdict.get("verdict")
            or ""
        ).lower()

    if verdict_status in ("approved", "committed"):
        # Full round-trip: call evolution_commit with the proposal_id returned by
        # evolution_propose. Without a confirmed commit receipt we do NOT promote
        # to 'committed' (F36: evolution_commit is never optional for medium+).
        proposal_id = str(
            eights_verdict.get("proposal_id") or ""  # type: ignore[union-attr]
        ) if isinstance(eights_verdict, dict) else ""
        commit_ok = False
        if proposal_id:
            try:
                commit_receipt = attestor.evolution_commit(
                    resource_id=resource_id,
                    proposal_id=proposal_id,
                )
                commit_ok = isinstance(commit_receipt, dict) and str(
                    commit_receipt.get("status") or ""
                ).lower() in ("committed", "ok", "done")
            except Exception:  # noqa: BLE001 — treat commit failure as eights down
                commit_ok = False

        if commit_ok:
            u.status = "committed"
            u.decided_at = datetime.now(timezone.utc).isoformat()
            u.decided_by = approved_by
            u.rationale = (
                (u.rationale + " | " if u.rationale else "")
                + f"F36: eights verdict={verdict_status}; evolution_commit confirmed"
            ).strip(" |")
            s.put(u)
            return u

        # evolution_commit unavailable or did not confirm (proposal_id missing
        # or commit call failed). Resolve by risk class.
        if risk == "medium":
            u.rationale = (
                (u.rationale + " | " if u.rationale else "")
                + f"F36: medium risk; eights verdict={verdict_status} but "
                "evolution_commit did not confirm — leaving pending (fail-soft)."
            ).strip(" |")
            s.put(u)
            return u
        else:  # high / critical
            u.status = "rejected"
            u.decided_at = datetime.now(timezone.utc).isoformat()
            u.decided_by = "governance.fail_closed"
            u.rationale = (
                (u.rationale + " | " if u.rationale else "")
                + f"F36: {risk} risk; eights verdict={verdict_status} but "
                "evolution_commit did not confirm — fail CLOSED."
            ).strip(" |")
            s.put(u)
            return u

    if verdict_status in ("ok", "done"):
        # Generic acknowledgement — not an explicit approval. Stay pending so
        # operators can retry with a proper approved/committed verdict (F36).
        u.rationale = (
            (u.rationale + " | " if u.rationale else "")
            + f"F36: eights ack={verdict_status!r} (generic, not approved/committed); "
            "staying pending."
        ).strip(" |")
        s.put(u)
        return u

    # Eights either unavailable (None verdict) or returned an unrecognised status.
    if risk == "medium":
        # Fail-soft: stay pending so operator can retry.
        u.rationale = (
            (u.rationale + " | " if u.rationale else "")
            + f"F36: medium risk; eights verdict={verdict_status or 'unavailable'} "
            "— leaving pending (fail-soft)."
        ).strip(" |")
        s.put(u)
        return u
    else:  # high / critical
        u.status = "rejected"
        u.decided_at = datetime.now(timezone.utc).isoformat()
        u.decided_by = "governance.fail_closed"
        u.rationale = (
            (u.rationale + " | " if u.rationale else "")
            + f"F36: {risk} risk; eights verdict={verdict_status or 'unavailable'} "
            "— fail CLOSED."
        ).strip(" |")
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
