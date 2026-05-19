"""Per-workflow JSONL trace + lightweight OTEL-style spans.

Compatible with pair-programmer's trace format so PP's debug tooling can read
Hydra traces without modification.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional


def trace_path(project_root: Path, workflow_id: str | uuid.UUID) -> Path:
    p = Path(project_root) / ".hydra" / str(workflow_id) / "trace.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


@dataclass
class Span:
    name: str
    span_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    parent_id: Optional[str] = None
    start_ns: int = field(default_factory=time.time_ns)
    end_ns: Optional[int] = None
    attributes: dict[str, Any] = field(default_factory=dict)

    @property
    def duration_ms(self) -> float:
        if self.end_ns is None:
            return 0.0
        return (self.end_ns - self.start_ns) / 1e6


def _write(path: Path, record: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, default=str) + os.linesep)


def emit(
    project_root: Path,
    workflow_id: str | uuid.UUID,
    kind: str,
    payload: dict[str, Any],
) -> None:
    _write(
        trace_path(project_root, workflow_id),
        {"ts": datetime.now(timezone.utc).isoformat(),
         "kind": kind, "workflow_id": str(workflow_id), **payload},
    )


@contextmanager
def span(
    project_root: Path,
    workflow_id: str | uuid.UUID,
    name: str,
    *,
    parent_id: Optional[str] = None,
    attributes: Optional[dict[str, Any]] = None,
) -> Iterator[Span]:
    s = Span(name=name, parent_id=parent_id, attributes=dict(attributes or {}))
    try:
        yield s
    finally:
        s.end_ns = time.time_ns()
        emit(project_root, workflow_id, "span", {
            "name": s.name,
            "span_id": s.span_id,
            "parent_id": s.parent_id,
            "duration_ms": s.duration_ms,
            "attributes": s.attributes,
        })
