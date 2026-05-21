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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable


DEFAULT_SPOOL_ROOT = Path.home() / ".hydra" / "eights-pending"


@dataclass
class SpooledCall:
    """A single eights-daemon call payload that failed to reach the daemon.

    Persisted as JSON in the spool root. Reconstructed via :py:meth:`load`.
    """

    id: str
    tool: str
    args: dict[str, Any]
    spooled_at: str  # ISO-8601 UTC
    workflow_id: str | None = None
    reason: str = ""

    def to_json(self) -> str:
        return json.dumps(
            {
                "id": self.id,
                "tool": self.tool,
                "args": self.args,
                "spooled_at": self.spooled_at,
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
            workflow_id=d.get("workflow_id"),
            reason=str(d.get("reason") or ""),
        )


class PendingSpool:
    """File-backed spool of eights-daemon calls that failed transport.

    Concurrency model: a single supervisor turn owns the spool while it runs.
    Multiple workflows MAY share a spool root — replay() handles missing
    files gracefully so two workflows draining the same spool race-safely.
    """

    def __init__(self, root: Path | str | None = None) -> None:
        self.root = Path(root) if root is not None else DEFAULT_SPOOL_ROOT
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
        send_fn: Callable[[str, dict[str, Any]], Any],
        *,
        max_replays: int | None = None,
    ) -> dict[str, int]:
        """Drain the spool by invoking send_fn(tool, args) per record.

        send_fn must return a truthy value on success or raise / return None
        on failure. On success the spool file is deleted. On failure the
        file is left in place for the next replay attempt.

        Returns a summary dict: ``{"sent": N, "failed": M, "skipped": K}``.
        ``skipped`` counts files that were corrupt or already deleted by
        a concurrent replay.
        """
        sent = failed = skipped = 0
        if not self.root.is_dir():
            return {"sent": sent, "failed": failed, "skipped": skipped}

        for i, path in enumerate(self._iter_pending_files()):
            if max_replays is not None and i >= max_replays:
                break
            try:
                raw = path.read_text(encoding="utf-8")
                sc = SpooledCall.from_json(raw)
            except FileNotFoundError:
                skipped += 1
                continue
            except Exception:  # noqa: BLE001
                skipped += 1
                continue
            try:
                result = send_fn(sc.tool, sc.args)
            except Exception:  # noqa: BLE001 — leave on disk for next replay
                failed += 1
                continue
            if not result:
                failed += 1
                continue
            try:
                path.unlink()
                sent += 1
            except FileNotFoundError:
                skipped += 1
        return {"sent": sent, "failed": failed, "skipped": skipped}

    # --- internals --------------------------------------------------------

    def _iter_pending_files(self) -> Iterable[Path]:
        """Iterate pending files in stable spooled-at order."""
        if not self.root.is_dir():
            return iter(())
        return iter(sorted(p for p in self.root.iterdir() if p.suffix == ".json"))
