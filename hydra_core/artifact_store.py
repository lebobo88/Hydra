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


_ATTENDED_SUFFIXES = frozenset({".md", ".json", ".txt"})


def write_attended_artifact(
    project_root: Path | str,
    workflow_id: str,
    relative: str,
    content: str,
) -> MemoryRef:
    """Generic attended artifact store (E2-35).

    Squads whose pack has no ``NATIVE_PACKS`` entry (customer-support, and
    every other claude-skill pack Hydra does not own a Claude plugin for)
    previously had their attended artifact dropped on the floor: the only
    persist path was :func:`write_native_artifact`, which raises for an
    unregistered slug. This store is always available -- it writes beneath the
    per-workflow attended tree Hydra already owns -- so a squad result can
    never be reported ``complete`` with no durable artifact behind it.

    Layout: ``<project_root>/.hydra/<workflow_id>/attended/artifacts/<relative>``.
    Escapes and non-text artifacts are rejected exactly as in the native store.
    """
    wf = str(workflow_id)
    if not wf or wf in {".", ".."} or any(c in wf for c in "/\\"):
        raise ArtifactStoreError(f"invalid workflow id {workflow_id!r}")
    root = (Path(project_root) / ".hydra" / wf / "attended" / "artifacts").resolve()
    candidate = (root / relative).resolve()
    if not candidate.is_relative_to(root):
        raise ArtifactStoreError("artifact path escapes attended artifact root")
    if candidate.suffix.lower() not in _ATTENDED_SUFFIXES:
        raise ArtifactStoreError("attended artifact must be a text artifact")
    candidate.parent.mkdir(parents=True, exist_ok=True)
    candidate.write_text(content, encoding="utf-8")
    rel = candidate.relative_to(root).as_posix()
    return MemoryRef(
        tier="episodic",
        key=f"attended:artifacts:{wf}/{rel}",
        summary=rel,
    )
