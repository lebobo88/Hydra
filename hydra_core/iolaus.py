"""Iolaus — the cauterizer.

Heracles' nephew applied burning brands to each severed neck so the Hydra
could not regrow heads it had lost. In the orchestrator, Iolaus is the
lifecycle hook layer: every squad dispatch flows through `pre_dispatch`
(before invoke) and `post_dispatch` (after invoke). The hooks:

  - refuse dispatch of a squad past its `deprecated_after` date,
  - refuse a duplicate spawn of the same squad for the same envelope in the
    same workflow (the "cut one, two grow" failure mode made explicit),
  - emit a `LifecycleEvent` to telemetry on every entry and exit.

The spawn ledger is per-workflow. Callers can either pass an explicit
`SpawnLedger` (recommended for tests and for non-LangGraph hosts) or rely on
the module-level registry keyed by workflow_id (the default for the supervisor
graph in this repo).
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from .schemas import HydraEnvelope
from .squad_loader import SquadPack
from .version import DoubleSpawnRefused, SquadDeprecated, is_deprecated


# --- lifecycle events --------------------------------------------------------

@dataclass(frozen=True)
class LifecycleEvent:
    """Single audit record from an Iolaus hook."""
    kind: str  # "pre_dispatch" | "post_dispatch" | "refused_deprecated" | "refused_duplicate"
    workflow_id: str
    envelope_id: str
    squad_slug: str
    squad_version: str
    timestamp: str
    detail: str = ""

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "workflow_id": self.workflow_id,
            "envelope_id": self.envelope_id,
            "squad_slug": self.squad_slug,
            "squad_version": self.squad_version,
            "timestamp": self.timestamp,
            "detail": self.detail,
        }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# --- spawn ledger ------------------------------------------------------------

class SpawnLedger:
    """Per-workflow record of (squad, envelope_id) pairs already dispatched.

    A second dispatch with the same pair is refused with `DoubleSpawnRefused`.
    This is the explicit answer to the Hydra's first failure mode — cut one
    head, two grow back. Heads regenerate only when Iolaus permits it.
    """

    def __init__(self) -> None:
        self._seen: set[tuple[str, str]] = set()
        self._lock = threading.Lock()

    def claim(self, slug: str, envelope_id: str) -> bool:
        """Return True if this is the first claim; False if already dispatched."""
        key = (slug, envelope_id)
        with self._lock:
            if key in self._seen:
                return False
            self._seen.add(key)
            return True

    def release(self, slug: str, envelope_id: str) -> None:
        """Forget a claim (e.g. after explicit teardown). Rarely needed."""
        with self._lock:
            self._seen.discard((slug, envelope_id))

    def contains(self, slug: str, envelope_id: str) -> bool:
        with self._lock:
            return (slug, envelope_id) in self._seen


_LEDGERS: dict[str, SpawnLedger] = {}
_LEDGERS_LOCK = threading.Lock()


def ledger_for(workflow_id: str | UUID) -> SpawnLedger:
    """Return the module-level ledger for a workflow_id, creating if missing."""
    wid = str(workflow_id)
    with _LEDGERS_LOCK:
        led = _LEDGERS.get(wid)
        if led is None:
            led = SpawnLedger()
            _LEDGERS[wid] = led
        return led


def reset_ledger(workflow_id: str | UUID) -> None:
    """Drop a workflow's ledger. Call from replay / forensic flows only."""
    with _LEDGERS_LOCK:
        _LEDGERS.pop(str(workflow_id), None)


# --- hooks -------------------------------------------------------------------

@dataclass
class IolausVerdict:
    allowed: bool
    event: LifecycleEvent
    reason: str = ""


def pre_dispatch(
    pack: SquadPack,
    envelope: HydraEnvelope,
    *,
    allow_archived: bool = False,
    ledger: Optional[SpawnLedger] = None,
    now: Optional[object] = None,
) -> IolausVerdict:
    """Gate before a squad invocation.

    Raises `SquadDeprecated` if the pack is past its deprecation date and
    `allow_archived` is False. Raises `DoubleSpawnRefused` if the same
    (squad, envelope) pair already ran in this workflow.

    Returns an `IolausVerdict` with the lifecycle event on success.
    """
    led = ledger if ledger is not None else ledger_for(envelope.workflow_id)
    wid = str(envelope.workflow_id)
    eid = str(envelope.id)

    if is_deprecated(pack.deprecated_after, now=now) and not allow_archived:
        evt = LifecycleEvent(
            kind="refused_deprecated",
            workflow_id=wid,
            envelope_id=eid,
            squad_slug=pack.slug,
            squad_version=str(pack.version),
            timestamp=_now_iso(),
            detail=f"deprecated_after={pack.deprecated_after}",
        )
        raise SquadDeprecated(
            slug=pack.slug,
            version=str(pack.version),
            deprecated_after=pack.deprecated_after,
            reason="post-deprecation dispatch refused by Iolaus",
        )

    if not led.claim(pack.slug, eid):
        LifecycleEvent(  # constructed for symmetry; raise carries the detail
            kind="refused_duplicate",
            workflow_id=wid,
            envelope_id=eid,
            squad_slug=pack.slug,
            squad_version=str(pack.version),
            timestamp=_now_iso(),
            detail="duplicate spawn refused",
        )
        raise DoubleSpawnRefused(slug=pack.slug, envelope_id=eid, workflow_id=wid)

    evt = LifecycleEvent(
        kind="pre_dispatch",
        workflow_id=wid,
        envelope_id=eid,
        squad_slug=pack.slug,
        squad_version=str(pack.version),
        timestamp=_now_iso(),
        detail=f"entrypoint={pack.entrypoint}",
    )
    return IolausVerdict(allowed=True, event=evt, reason="pre_dispatch ok")


def post_dispatch(
    pack: SquadPack,
    envelope: HydraEnvelope,
    *,
    status: str,
    detail: str = "",
) -> LifecycleEvent:
    """Record the close of a dispatch. Always called after a successful
    `pre_dispatch`. Failure-path callers should still emit this with
    `status="failed"` so the trace records the cauterized stump.
    """
    return LifecycleEvent(
        kind="post_dispatch",
        workflow_id=str(envelope.workflow_id),
        envelope_id=str(envelope.id),
        squad_slug=pack.slug,
        squad_version=str(pack.version),
        timestamp=_now_iso(),
        detail=f"status={status}; {detail}".strip("; "),
    )
