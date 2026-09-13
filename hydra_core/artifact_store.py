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
    # Keep this set exactly {.md, .json, .txt}. A third writer,
    # write_repo_artifact, now exists for tracked-repo output (e.g. rendered
    # plan HTML/JSON under docs/plans) with its own allow-list -- do not fold
    # its suffixes in here.
    if candidate.suffix.lower() not in {".md", ".json", ".txt"}:
        raise ArtifactStoreError("native artifact must be a text artifact")
    candidate.parent.mkdir(parents=True, exist_ok=True)
    candidate.write_text(content, encoding="utf-8")
    rel = candidate.relative_to(root).as_posix()
    return MemoryRef(tier="episodic", key=f"{spec.plugin}:output:{rel}", summary=rel)


# Keep this frozenset exactly {.md, .json, .txt}. A third writer,
# write_repo_artifact, now exists for tracked-repo output (e.g. rendered plan
# HTML/JSON under docs/plans) with its own allow-list -- do not fold its
# suffixes in here.
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


def write_repo_artifact(
    repo_root: Path | str,
    relative: str,
    content: str,
    *,
    allowed_roots: tuple[str, ...] = ("docs/plans",),
    allowed_suffixes: frozenset[str] = frozenset({".md", ".json", ".txt", ".html"}),
) -> MemoryRef:
    """Write a text artifact directly into a tracked subtree of a repo.

    A repo-bound writer that can write anywhere in a repo is a
    code-modification primitive, and this module is deliberately not one:
    :func:`write_native_artifact` and :func:`write_attended_artifact` both
    write beneath a side-channel state directory (a native pack's declared
    output root, or Hydra's own ``.hydra/<workflow_id>/attended`` tree) --
    never into the repo's own tracked source tree. This writer is the one
    deliberate exception, and it earns that exception only by being pinned to
    an allow-listed subtree (``docs/plans`` by default) that holds rendered,
    git-diffable artifacts (e.g. plan HTML/JSON) rather than engine source.
    Callers MUST NOT widen ``allowed_roots`` to cover source directories --
    that would turn this into exactly the code-modification primitive this
    module exists to avoid.

    Guards, in order: the resolved candidate path must stay under
    ``repo_root``; it must additionally resolve under at least one of
    ``allowed_roots`` (each interpreted relative to ``repo_root``); and its
    suffix must be one of ``allowed_suffixes``. Any violation raises
    :class:`ArtifactStoreError`, matching the style of the other two writers
    in this module.

    Text-only, like the other two writers here (``content: str``, no binary
    path) -- an image referenced by a rendered plan is written by the
    renderer's caller through its own path, never through this store.
    """
    root = Path(repo_root).resolve()
    candidate = (root / relative).resolve()
    if not candidate.is_relative_to(root):
        raise ArtifactStoreError("artifact path escapes repo root")

    allowed_root_paths = [(root / allowed).resolve() for allowed in allowed_roots]
    for allowed, allowed_path in zip(allowed_roots, allowed_root_paths):
        # allowed_roots is caller-controlled (a hardcoded default everywhere
        # today, but the parameter is public). Resolving "<allowed>" relative
        # to root and never checking the RESULT is still under root means an
        # allowed_roots of ("..",) -- or any other out-of-tree value -- turns
        # the allow-list into free rein over the parent tree for anything with
        # an allowed suffix. Every configured root must itself live under
        # repo_root before it can allow-list a destination path.
        if not (allowed_path == root or allowed_path.is_relative_to(root)):
            raise ArtifactStoreError(
                f"allowed_roots entry {allowed!r} resolves outside repo_root"
            )
    if not any(
        candidate.is_relative_to(allowed_root)
        for allowed_root in allowed_root_paths
    ):
        raise ArtifactStoreError(
            f"artifact path {relative!r} is not under an allowed root "
            f"{list(allowed_roots)!r}"
        )

    if candidate.suffix.lower() not in allowed_suffixes:
        raise ArtifactStoreError("repo artifact suffix not in allow-list")

    candidate.parent.mkdir(parents=True, exist_ok=True)
    candidate.write_text(content, encoding="utf-8")
    rel = candidate.relative_to(root).as_posix()
    return MemoryRef(tier="episodic", key=f"repo:artifact:{rel}", summary=rel)
