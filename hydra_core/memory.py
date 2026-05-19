"""Memory fabric.

Three tiers per the architecture doc:
  - ephemeral: in-prompt rolling window (no persistence)
  - episodic : append-only SQLite log of every envelope, tool call, verdict
  - semantic : vector store (Chroma by default; pluggable)

Agents never receive raw blobs — only `MemoryRef` handles. This module is the
authority for resolving handles back to content.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional
from uuid import UUID

from .eights import ALL_CELLS, Cell, validate_cells
from .eights.classifier import classify
from .schemas import MemoryRef


DEFAULT_DIR = Path.home() / ".hydra"
EPISODIC_DB = DEFAULT_DIR / "episodic.db"


# --------- episodic ---------

@dataclass
class EpisodicRow:
    key: str
    workflow_id: str
    kind: str
    payload_json: str
    created_at: str
    cells: list[Cell]


def _ensure_episodic(db: Path = EPISODIC_DB) -> sqlite3.Connection:
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS episodic (
          key         TEXT PRIMARY KEY,
          workflow_id TEXT NOT NULL,
          kind        TEXT NOT NULL,
          payload_json TEXT NOT NULL,
          created_at  TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_episodic_wf ON episodic(workflow_id);
        CREATE INDEX IF NOT EXISTS ix_episodic_kind ON episodic(kind);
        """
    )
    # Stage-3 migration: add the cells column (TheEights tag vocabulary).
    # SQLite has no IF NOT EXISTS for ALTER, so we probe pragma_table_info.
    have_cells = any(
        row[1] == "cells"
        for row in conn.execute("PRAGMA table_info(episodic)").fetchall()
    )
    if not have_cells:
        conn.execute("ALTER TABLE episodic ADD COLUMN cells TEXT NOT NULL DEFAULT '[]'")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_episodic_cells ON episodic(cells)")
    return conn


def append_episodic(
    workflow_id: UUID | str,
    kind: str,
    payload: dict[str, Any],
    *,
    key: Optional[str] = None,
    db: Path = EPISODIC_DB,
    cells: Optional[list[str]] = None,
    origin_squad: Optional[str] = None,
) -> MemoryRef:
    """Append a payload to episodic. If `cells` is None, the rules-first
    classifier infers them from `kind` (treated as envelope_type) and `payload`."""
    key = key or f"ep:{workflow_id}:{kind}:{datetime.now(timezone.utc).timestamp():.6f}"
    if cells is None:
        inferred = classify(envelope_type=kind, origin_squad=origin_squad, payload=payload)
    else:
        inferred = validate_cells(cells)
    cells_json = json.dumps(inferred)
    with _ensure_episodic(db) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO episodic (key, workflow_id, kind, payload_json, created_at, cells) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (key, str(workflow_id), kind, json.dumps(payload),
             datetime.now(timezone.utc).isoformat(), cells_json),
        )
        conn.commit()
    return MemoryRef(tier="episodic", key=key, summary=f"{kind}@{workflow_id}", cells=inferred)


def tag_episodic(
    key: str,
    cells: list[str],
    *,
    db: Path = EPISODIC_DB,
    replace: bool = False,
) -> list[Cell]:
    """Attach cells to an existing episodic row. Default merges with the
    existing tag set; pass `replace=True` to overwrite."""
    validated = validate_cells(cells)
    with _ensure_episodic(db) as conn:
        row = conn.execute("SELECT cells FROM episodic WHERE key=?", (key,)).fetchone()
        if not row:
            return []
        if replace:
            merged = validated
        else:
            existing = validate_cells(json.loads(row[0] or "[]"))
            merged = list(existing)
            for c in validated:
                if c not in merged:
                    merged.append(c)
        conn.execute("UPDATE episodic SET cells=? WHERE key=?", (json.dumps(merged), key))
        conn.commit()
    return merged


def resolve_episodic(key: str, *, db: Path = EPISODIC_DB) -> dict[str, Any] | None:
    with _ensure_episodic(db) as conn:
        row = conn.execute(
            "SELECT workflow_id, kind, payload_json, created_at, cells FROM episodic WHERE key=?",
            (key,),
        ).fetchone()
    if not row:
        return None
    return {
        "key": key, "workflow_id": row[0], "kind": row[1],
        "payload": json.loads(row[2]), "created_at": row[3],
        "cells": json.loads(row[4] or "[]"),
    }


def list_episodic(workflow_id: UUID | str, *, db: Path = EPISODIC_DB) -> list[dict[str, Any]]:
    with _ensure_episodic(db) as conn:
        rows = conn.execute(
            "SELECT key, kind, payload_json, created_at, cells FROM episodic "
            "WHERE workflow_id=? ORDER BY created_at",
            (str(workflow_id),),
        ).fetchall()
    return [
        {"key": k, "kind": ki, "payload": json.loads(p), "created_at": c,
         "cells": json.loads(cs or "[]")}
        for (k, ki, p, c, cs) in rows
    ]


def query_by_cell(
    cell: Cell | str,
    *,
    limit: int = 50,
    workflow_id: Optional[UUID | str] = None,
    db: Path = EPISODIC_DB,
) -> list[dict[str, Any]]:
    """Return episodic rows tagged with `cell`, newest first. Optional
    workflow_id scopes the query. Matches via JSON containment (LIKE on the
    JSON-encoded cells column — fine for small N; swap for a join table if
    cardinality climbs)."""
    cell_str = str(cell).strip().lower()
    if cell_str not in ALL_CELLS:
        return []
    like = f'%"{cell_str}"%'
    with _ensure_episodic(db) as conn:
        if workflow_id is not None:
            rows = conn.execute(
                "SELECT key, workflow_id, kind, payload_json, created_at, cells "
                "FROM episodic WHERE cells LIKE ? AND workflow_id=? "
                "ORDER BY created_at DESC LIMIT ?",
                (like, str(workflow_id), int(limit)),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT key, workflow_id, kind, payload_json, created_at, cells "
                "FROM episodic WHERE cells LIKE ? "
                "ORDER BY created_at DESC LIMIT ?",
                (like, int(limit)),
            ).fetchall()
    return [
        {"key": k, "workflow_id": w, "kind": ki, "payload": json.loads(p),
         "created_at": c, "cells": json.loads(cs or "[]")}
        for (k, w, ki, p, c, cs) in rows
    ]


# --------- semantic (Chroma-pluggable; no hard dep) ---------

class SemanticIndex:
    """In-memory cosine fallback if no vector backend is available.
    Replace with Chroma / Qdrant / pgvector in production by subclassing."""

    def __init__(self, name: str):
        self.name = name
        self._docs: list[tuple[str, str, list[float]]] = []  # (key, text, emb)

    def add(self, key: str, text: str, embedding: list[float]) -> MemoryRef:
        self._docs.append((key, text, embedding))
        return MemoryRef(tier="semantic", key=f"{self.name}:{key}", summary=text[:80])

    def search(self, embedding: list[float], k: int = 5) -> list[MemoryRef]:
        def cos(a: list[float], b: list[float]) -> float:
            num = sum(x * y for x, y in zip(a, b))
            da = sum(x * x for x in a) ** 0.5
            db = sum(y * y for y in b) ** 0.5
            return num / (da * db) if da and db else 0.0
        scored = sorted(
            ((cos(embedding, e), key, text) for key, text, e in self._docs),
            reverse=True,
        )[:k]
        return [MemoryRef(tier="semantic", key=f"{self.name}:{k}", summary=t[:80])
                for (_score, k, t) in scored]


_INDEX_REGISTRY: dict[str, SemanticIndex] = {}


def get_index(name: str) -> SemanticIndex:
    if name not in _INDEX_REGISTRY:
        _INDEX_REGISTRY[name] = SemanticIndex(name)
    return _INDEX_REGISTRY[name]


# --------- handle resolution ---------

def resolve(ref: MemoryRef, *, db: Path = EPISODIC_DB) -> dict[str, Any] | None:
    if ref.tier == "episodic":
        return resolve_episodic(ref.key, db=db)
    if ref.tier == "semantic":
        # In a real deployment, look up the doc by id.
        return {"key": ref.key, "summary": ref.summary}
    return {"key": ref.key, "summary": ref.summary}
