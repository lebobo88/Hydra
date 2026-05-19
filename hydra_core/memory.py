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
    return conn


def append_episodic(
    workflow_id: UUID | str,
    kind: str,
    payload: dict[str, Any],
    *,
    key: Optional[str] = None,
    db: Path = EPISODIC_DB,
) -> MemoryRef:
    key = key or f"ep:{workflow_id}:{kind}:{datetime.now(timezone.utc).timestamp():.6f}"
    with _ensure_episodic(db) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO episodic VALUES (?, ?, ?, ?, ?)",
            (key, str(workflow_id), kind, json.dumps(payload), datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
    return MemoryRef(tier="episodic", key=key, summary=f"{kind}@{workflow_id}")


def resolve_episodic(key: str, *, db: Path = EPISODIC_DB) -> dict[str, Any] | None:
    with _ensure_episodic(db) as conn:
        row = conn.execute(
            "SELECT workflow_id, kind, payload_json, created_at FROM episodic WHERE key=?",
            (key,),
        ).fetchone()
    if not row:
        return None
    return {
        "key": key, "workflow_id": row[0], "kind": row[1],
        "payload": json.loads(row[2]), "created_at": row[3],
    }


def list_episodic(workflow_id: UUID | str, *, db: Path = EPISODIC_DB) -> list[dict[str, Any]]:
    with _ensure_episodic(db) as conn:
        rows = conn.execute(
            "SELECT key, kind, payload_json, created_at FROM episodic WHERE workflow_id=? ORDER BY created_at",
            (str(workflow_id),),
        ).fetchall()
    return [
        {"key": k, "kind": ki, "payload": json.loads(p), "created_at": c}
        for (k, ki, p, c) in rows
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
