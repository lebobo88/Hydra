"""TheEights pending-call spool — B8 replay queue.

When the eights-daemon is unreachable, `EightsAttestor._call` would silently
return None and the attestation / proposal / envelope-record / hitl-request
payload was lost. The bootstrap session surfaced this twice:

  * 5 evolution proposals filed via AgentSmith returned ``degraded: true,
    reason: "eights-mcp-unavailable"`` and were never replayed when the
    daemon came back.
  * 12 documented proposals (``docs/eights/2026-05-20-evolution-proposals.md``)
    sat as a Markdown document because no machine-readable spool existed.

This module spools each failed payload to disk as ``<spool_root>/<uuid>.json``
and exposes a `replay(send_fn)` that drains the spool when the daemon is back.
The supervisor's `node_intake` calls `replay()` once per workflow start, so
the spool naturally drains the next time a workflow runs on the same project
with eights healthy.

Design constraints:
  * Disk-backed, JSON, one file per payload. Crash-safe — partial writes
    use atomic ``os.replace``.
  * Append-only on the write path; replay deletes drained files. No mutation
    of in-flight files.
  * Fail-soft: a corrupt JSON file does NOT block draining the rest of the
    spool — it is logged and left in place for an operator to inspect.
  * No constitution gate here — that runs at the original ``propose()``
    call (see ``hydra_core/procedural.py``). The spool is a *transport*
    retry, not an authority gate.
"""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable


DEFAULT_SPOOL_ROOT = Path.home() / ".hydra" / "eights-pending"
DEFAULT_DEAD_LETTER_ROOT = Path.home() / ".hydra" / "eights-pending-dead"

# E2-26: the spool root is operator state. `HYDRA_EIGHTS_SPOOL` was already
# honoured by the CLI (`hydra doctor`, `hydra eights-replay`) but NOT by the
# `PendingSpool()` default used by `EightsAttestor`, so a test run wrote real
# failure payloads into the operator's live `~/.hydra/eights-pending`. Both
# roots now resolve through the environment at *construction* time (not
# import time) so an in-test `monkeypatch.setenv` takes effect too.
#
#   HYDRA_EIGHTS_SPOOL       -> spool root       (pre-existing name)
#   HYDRA_EIGHTS_DEAD_LETTER -> dead-letter root (added by E2-26)


def resolve_spool_root() -> Path:
    """Spool root: ``HYDRA_EIGHTS_SPOOL`` env override, else the default."""
    return Path(os.environ.get("HYDRA_EIGHTS_SPOOL") or DEFAULT_SPOOL_ROOT)


def resolve_dead_letter_root() -> Path:
    """Dead-letter root: ``HYDRA_EIGHTS_DEAD_LETTER`` override, else default."""
    return Path(
        os.environ.get("HYDRA_EIGHTS_DEAD_LETTER") or DEFAULT_DEAD_LETTER_ROOT
    )


@dataclass
class SpooledCall:
    """A single eights-daemon call payload that failed to reach the daemon.

    Persisted as JSON in the spool root. Reconstructed via :py:meth:`load`.
    """

    id: str
    tool: str
    args: dict[str, Any]
    spooled_at: str  # ISO-8601 UTC
    attempts: int = 0
    workflow_id: str | None = None
    reason: str = ""

    def to_json(self) -> str:
        return json.dumps(
            {
                "id": self.id,
                "tool": self.tool,
                "args": self.args,
                "spooled_at": self.spooled_at,
                "attempts": self.attempts,
                "workflow_id": self.workflow_id,
                "reason": self.reason,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )

    @classmethod
    def from_json(cls, raw: str) -> "SpooledCall":
        d = json.loads(raw)
        return cls(
            id=str(d["id"]),
            tool=str(d["tool"]),
            args=dict(d.get("args") or {}),
            spooled_at=str(d.get("spooled_at") or ""),
            attempts=int(d.get("attempts") or 0),
            workflow_id=d.get("workflow_id"),
            reason=str(d.get("reason") or ""),
        )


class PendingSpool:
    """File-backed spool of eights-daemon calls that failed transport.

    Concurrency model: a single supervisor turn owns the spool while it runs.
    Multiple workflows MAY share a spool root — replay() handles missing
    files gracefully so two workflows draining the same spool race-safely.
    """

    def __init__(
        self,
        root: Path | str | None = None,
        *,
        dead_letter_root: Path | str | None = None,
    ) -> None:
        self.root = Path(root) if root is not None else resolve_spool_root()
        self.dead_letter_root = (
            Path(dead_letter_root)
            if dead_letter_root is not None
            else resolve_dead_letter_root()
        )
        # Lazy mkdir — the spool only materializes when something is spooled
        # so a clean install with healthy eights never creates the directory.

    # --- write ------------------------------------------------------------

    def spool(
        self,
        *,
        tool: str,
        args: dict[str, Any],
        workflow_id: str | None = None,
        reason: str = "",
    ) -> SpooledCall:
        """Persist a failed call to disk. Returns the spooled record.

        Atomic write: stage to ``<id>.json.partial`` then ``os.replace``
        to ``<id>.json``. A crash mid-write leaves the .partial file in
        place — replay() ignores files not ending in ``.json``.
        """
        self.root.mkdir(parents=True, exist_ok=True)
        sc = SpooledCall(
            id=str(uuid.uuid4()),
            tool=tool,
            args=dict(args or {}),
            spooled_at=datetime.now(timezone.utc).isoformat(),
            workflow_id=workflow_id,
            reason=reason,
        )
        final_path = self.root / f"{sc.id}.json"
        partial_path = self.root / f"{sc.id}.json.partial"
        partial_path.write_text(sc.to_json(), encoding="utf-8")
        os.replace(partial_path, final_path)
        return sc

    # --- read -------------------------------------------------------------

    def count(self) -> int:
        """How many calls are currently spooled. 0 when the dir doesn't exist."""
        if not self.root.is_dir():
            return 0
        return sum(1 for _ in self._iter_pending_files())

    def list_pending(self) -> list[SpooledCall]:
        """Load every spooled call (sorted by spooled_at). Corrupt entries
        are skipped silently — they remain on disk for inspection."""
        out: list[SpooledCall] = []
        if not self.root.is_dir():
            return out
        for path in self._iter_pending_files():
            try:
                out.append(SpooledCall.from_json(path.read_text(encoding="utf-8")))
            except Exception:  # noqa: BLE001 — fail-soft on bad files
                continue
        out.sort(key=lambda c: c.spooled_at)
        return out

    # --- drain ------------------------------------------------------------

    def replay(
        self,
        send_fn: Callable[[SpooledCall], Any],
        *,
        max_replays: int | None = None,
        max_attempts: int = 5,
        max_age_hours: float = 24.0,
    ) -> dict[str, int]:
        """Drain the spool by invoking ``send_fn(spooled_call)`` per record.

        send_fn must return a truthy value on success or raise / return None
        on failure. On success the spool file is deleted. On failure the
        file is left in place for the next replay attempt until ``attempts``
        exceeds ``max_attempts``. Entries older than ``max_age_hours`` or
        entries that exceed ``max_attempts`` are moved to the dead-letter
        directory instead of retrying forever.

        Returns a summary dict::

            {"sent": N, "failed": M, "skipped": K,
             "dead_lettered": D, "dead_lettered_expired": E}

        ``skipped`` counts files that were corrupt or already deleted by
        a concurrent replay, plus entries dead-lettered without a replay
        attempt because they expired their TTL.

        ``dead_lettered`` counts every entry moved to the dead-letter
        directory during THIS run — both TTL expiries and entries that
        exhausted ``max_attempts``. ``dead_lettered_expired`` is the TTL
        subset, so an operator can tell "the backlog aged out" (fixable by
        raising ``--max-age-hours`` / replaying the dead-letter dir) from
        "the daemon keeps rejecting these". E2-3: these moves used to be
        invisible — an outage longer than the TTL silently converted the
        whole backlog into unreplayable dead letters.
        """
        sent = failed = skipped = 0
        dead_lettered = dead_lettered_expired = 0
        if not self.root.is_dir():
            return {
                "sent": sent,
                "failed": failed,
                "skipped": skipped,
                "dead_lettered": dead_lettered,
                "dead_lettered_expired": dead_lettered_expired,
            }

        replayed = 0
        for path in self._iter_pending_files():
            try:
                raw = path.read_text(encoding="utf-8")
                sc = SpooledCall.from_json(raw)
            except FileNotFoundError:
                skipped += 1
                continue
            except Exception:  # noqa: BLE001
                skipped += 1
                continue
            if self._is_expired(sc, max_age_hours=max_age_hours):
                try:
                    self._dead_letter(path, sc)
                except FileNotFoundError:
                    skipped += 1
                    continue
                except Exception:  # noqa: BLE001 — fail-soft; preserve for inspection
                    skipped += 1
                    continue
                skipped += 1
                dead_lettered += 1
                dead_lettered_expired += 1
                continue
            if max_replays is not None and replayed >= max_replays:
                break
            replayed += 1
            try:
                result = send_fn(sc)
            except Exception:  # noqa: BLE001 — leave on disk for next replay
                failed += 1
                if self._record_failure(path, sc, max_attempts=max_attempts):
                    dead_lettered += 1
                continue
            if not result:
                failed += 1
                if self._record_failure(path, sc, max_attempts=max_attempts):
                    dead_lettered += 1
                continue
            try:
                path.unlink()
                sent += 1
            except FileNotFoundError:
                skipped += 1
        return {
            "sent": sent,
            "failed": failed,
            "skipped": skipped,
            "dead_lettered": dead_lettered,
            "dead_lettered_expired": dead_lettered_expired,
        }

    def dead_letter_count(self) -> int:
        """How many entries sit in the dead-letter dir. 0 when it doesn't exist."""
        if not self.dead_letter_root.is_dir():
            return 0
        return sum(
            1 for p in self.dead_letter_root.iterdir() if p.suffix == ".json"
        )

    def requeue_dead_letters(self, *, limit: int | None = None) -> int:
        """Move up to ``limit`` dead-letter entries back into the pending spool.

        ``attempts`` is reset to 0 on the way back so a re-queued entry gets a
        full retry budget. Returns the number of entries actually re-queued.
        Corrupt dead letters are left in place for inspection (fail-soft).
        """
        if not self.dead_letter_root.is_dir():
            return 0
        self.root.mkdir(parents=True, exist_ok=True)
        requeued = 0
        for path in sorted(
            p for p in self.dead_letter_root.iterdir() if p.suffix == ".json"
        ):
            if limit is not None and requeued >= limit:
                break
            try:
                sc = SpooledCall.from_json(path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001 — leave corrupt files for the operator
                continue
            sc.attempts = 0
            target = self.root / path.name
            if target.exists():
                target = self.root / f"{sc.id}-{uuid.uuid4().hex}.json"
            try:
                self._write_call(path, sc)
                os.replace(path, target)
            except OSError:
                continue
            requeued += 1
        return requeued

    # --- internals --------------------------------------------------------

    def _iter_pending_files(self) -> Iterable[Path]:
        """Iterate pending files in stable spooled-at order."""
        if not self.root.is_dir():
            return iter(())
        return iter(sorted(p for p in self.root.iterdir() if p.suffix == ".json"))

    def _record_failure(self, path: Path, sc: SpooledCall, *, max_attempts: int) -> bool:
        """Persist the incremented attempt count. Returns True if dead-lettered."""
        sc.attempts += 1
        if sc.attempts > max_attempts:
            self._write_call(path, sc)
            self._dead_letter(path, sc)
            return True
        self._write_call(path, sc)
        return False

    def _write_call(self, path: Path, sc: SpooledCall) -> None:
        partial_path = path.with_suffix(f"{path.suffix}.partial")
        partial_path.write_text(sc.to_json(), encoding="utf-8")
        os.replace(partial_path, path)

    def _dead_letter(self, path: Path, sc: SpooledCall) -> None:
        self.dead_letter_root.mkdir(parents=True, exist_ok=True)
        target = self.dead_letter_root / path.name
        if target.exists():
            target = self.dead_letter_root / f"{sc.id}-{uuid.uuid4().hex}.json"
        os.replace(path, target)

    def _is_expired(self, sc: SpooledCall, *, max_age_hours: float) -> bool:
        if max_age_hours <= 0:
            return False
        try:
            spooled_at = datetime.fromisoformat(sc.spooled_at)
        except ValueError:
            return False
        if spooled_at.tzinfo is None:
            spooled_at = spooled_at.replace(tzinfo=timezone.utc)
        age_limit = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
        return spooled_at < age_limit
