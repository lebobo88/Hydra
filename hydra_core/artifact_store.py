"""Safe local persistence for native Claude Code squad artifacts."""
from __future__ import annotations

from pathlib import Path

from .native_packs import native_pack, native_pack_root
from .schemas import MemoryRef


class ArtifactStoreError(ValueError):
    pass


def write_native_artifact(slug: str, relative: str, content: str) -> MemoryRef:
    """Write beneath the pack's declared output root, rejecting every escape."""
    spec = native_pack(slug)
    root = (native_pack_root(slug) / spec.output_root).resolve()
    candidate = (root / relative).resolve()
    if not candidate.is_relative_to(root):
        raise ArtifactStoreError(f"artifact path escapes {slug} output root")
    if candidate.suffix.lower() not in {".md", ".json", ".txt"}:
        raise ArtifactStoreError("native artifact must be a text artifact")
    candidate.parent.mkdir(parents=True, exist_ok=True)
    candidate.write_text(content, encoding="utf-8")
    rel = candidate.relative_to(root).as_posix()
    return MemoryRef(tier="episodic", key=f"{spec.plugin}:output:{rel}", summary=rel)
