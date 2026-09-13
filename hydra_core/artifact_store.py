"""Safe local persistence for native Claude Code squad artifacts."""
from __future__ import annotations

import ntpath
import re
from collections.abc import Iterable
from pathlib import Path

from .native_packs import native_pack, native_pack_root
from .schemas import MemoryRef


class ArtifactStoreError(ValueError):
    pass


# C0 controls (0x00-0x1F, including NUL/newline/CR/tab) and C1 controls
# (0x7F-0x9F, including DEL). A path containing any of these is refused
# outright rather than passed through to the filesystem -- some platforms
# happen to reject a subset of these bytes in a path (Windows rejects an
# embedded newline or CR with an OSError), but that is the platform
# rescuing a missing guard, not the guard doing its job. On a filesystem
# that permits them the write would otherwise silently succeed.
_CONTROL_CHARS = frozenset(chr(c) for c in range(0x00, 0x20)) | frozenset(
    chr(c) for c in range(0x7F, 0xA0)
)

# Explicit, conservative bounds on `relative`, checked before any Path is
# constructed. Without these, whether an over-long name is refused depends
# entirely on the platform's own path-length ceiling (Windows MAX_PATH is
# ~260 characters and raises a raw FileNotFoundError deep in the write call);
# on a filesystem with a longer limit -- or a differently-configured Windows
# host with long-path support enabled -- the same write would silently
# succeed. Pinning our own, smaller bound makes the refusal this module's
# decision rather than an accident of the host filesystem.
_MAX_RELATIVE_LENGTH = 1024
_MAX_COMPONENT_LENGTH = 255  # the conventional single-component limit (NTFS, ext4, APFS)


def _validate_relative(relative: object) -> str:
    """Syntactically validate a repo-relative path *before* any path
    construction or filesystem access.

    ``relative`` is documented (here and on :func:`write_repo_artifact`) as
    repo-relative and control-character-free. Without this gate, a
    malformed value -- an absolute path, an empty string, an embedded
    control character -- flows straight into ``Path`` construction and a
    filesystem call, and surfaces as a platform-specific ``OSError`` or
    ``ValueError`` instead of the ``ArtifactStoreError`` every other
    rejection in this module raises. Worse, an *absolute* ``relative`` is
    silently accepted by ``root / relative`` (the join is a no-op for an
    absolute right-hand side), so it can pass every downstream containment
    check purely by coincidence of where it happens to point -- the same
    repo-relative ambiguity already closed for ``allowed_roots``.
    """
    if not isinstance(relative, str):
        raise ArtifactStoreError(
            f"relative must be a string, got {type(relative).__name__}"
        )
    if not relative.strip():
        raise ArtifactStoreError("relative must not be empty or whitespace-only")
    if any(ch in _CONTROL_CHARS for ch in relative):
        raise ArtifactStoreError(f"relative {relative!r} contains a control character")
    drive, _ = ntpath.splitdrive(relative)
    if drive or relative[0] in ("/", "\\"):
        raise ArtifactStoreError(
            f"relative {relative!r} must be repo-relative, not absolute"
        )
    if len(relative) > _MAX_RELATIVE_LENGTH:
        raise ArtifactStoreError(
            f"relative path exceeds {_MAX_RELATIVE_LENGTH} characters "
            f"({len(relative)})"
        )
    for component in re.split(r"[\\/]+", relative):
        if len(component) > _MAX_COMPONENT_LENGTH:
            raise ArtifactStoreError(
                f"relative path component {component!r} exceeds "
                f"{_MAX_COMPONENT_LENGTH} characters ({len(component)})"
            )
    return relative


def _validate_allowed_roots(allowed_roots: object) -> tuple[str, ...]:
    """Syntactically validate ``allowed_roots`` *before* any ``Path`` is built.

    ``root / allowed`` -- the very first thing the old code did with each
    entry -- crashes with a raw ``TypeError`` for a non-string entry (an
    ``int``, ``None``, or a ``pathlib.Path``), pre-empting every downstream
    guard. A ``Path`` entry is an especially easy mistake: it is an
    idiomatic, non-hostile way to spell a path in Python, and it deserves a
    clear ``ArtifactStoreError`` naming the expected type, not a crash.
    ``allowed_roots`` is documented as strings; this module does not accept
    a ``Path`` here (unlike accepting one would, silently, for every future
    caller) -- it is refused with a message that says so explicitly.

    A bare string is also refused: ``allowed_roots="docs/plans"`` looks like
    a single-entry shorthand but iterates character-by-character, which is
    never what a caller intends.
    """
    if isinstance(allowed_roots, str) or not isinstance(allowed_roots, Iterable):
        raise ArtifactStoreError(
            "allowed_roots must be a non-string iterable of strings, got "
            f"{allowed_roots!r} of type {type(allowed_roots).__name__}"
        )
    materialized = tuple(allowed_roots)
    for entry in materialized:
        if not isinstance(entry, str):
            raise ArtifactStoreError(
                "allowed_roots entries must be strings, got "
                f"{entry!r} of type {type(entry).__name__}"
            )
    return materialized


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

    Guards, in order: every ``allowed_roots`` entry must be repo-relative
    (never absolute -- POSIX-absolute, Windows drive-absolute, drive-relative
    root, or UNC -- since an absolute entry makes "relative to repo_root"
    ambiguous) and must resolve strictly *inside* ``repo_root`` -- not to
    ``repo_root`` itself, which would erase the subtree boundary this
    function exists to enforce; the resolved candidate path must stay under
    ``repo_root``; it must additionally resolve under at least one of
    ``allowed_roots``; and its suffix must be one of ``allowed_suffixes``.
    Any violation raises :class:`ArtifactStoreError`, matching the style of
    the other two writers in this module.

    Text-only, like the other two writers here (``content: str``, no binary
    path) -- an image referenced by a rendered plan is written by the
    renderer's caller through its own path, never through this store.

    ``relative`` is repo-relative and control-character-free: it is
    validated syntactically (non-empty, not whitespace-only, no absolute
    spelling, no C0/C1 control character, within the length bounds) before
    any path is constructed or any filesystem call is made -- see
    :func:`_validate_relative`. ``allowed_roots`` is validated the same way,
    before it is ever joined onto a path -- see :func:`_validate_allowed_roots`.

    Every remaining filesystem operation (directory creation, the write
    itself) is wrapped so that whatever the underlying OS does -- a
    too-long full path, a permissions error, any other ``OSError`` -- is
    re-raised as :class:`ArtifactStoreError` with the original chained as
    the cause. The contract is total: a caller of this writer sees exactly
    one exception type for every rejection, never a platform-specific one.
    """
    _validate_relative(relative)
    allowed_roots = _validate_allowed_roots(allowed_roots)
    root = Path(repo_root).resolve()
    candidate = (root / relative).resolve()
    if not candidate.is_relative_to(root):
        raise ArtifactStoreError("artifact path escapes repo root")

    allowed_root_paths = [(root / allowed).resolve() for allowed in allowed_roots]
    for allowed, allowed_path in zip(allowed_roots, allowed_root_paths):
        # allowed_roots is documented as repo-relative. An absolute entry
        # (POSIX-absolute "/x", Windows drive-absolute "C:\x", a
        # drive-relative root "\x", a bare drive-relative "C:foo", or a UNC
        # "\\server\share") makes "relative to repo_root" ambiguous and is
        # exactly how an intended subtree (e.g. the absolute spelling of
        # docs/plans) can slip past the containment check below by already
        # being an absolute path the join+resolve leaves untouched.
        if allowed and (ntpath.splitdrive(allowed)[0] or allowed[0] in ("/", "\\")):
            raise ArtifactStoreError(
                f"allowed_roots entry {allowed!r} must be repo-relative, not absolute"
            )
        # allowed_roots is caller-controlled (a hardcoded default everywhere
        # today, but the parameter is public). Resolving "<allowed>" relative
        # to root and never checking the RESULT is still under root means an
        # allowed_roots of ("..",) -- or any other out-of-tree value -- turns
        # the allow-list into free rein over the parent tree for anything with
        # an allowed suffix. Every configured root must itself live under
        # repo_root before it can allow-list a destination path.
        if not allowed_path.is_relative_to(root):
            raise ArtifactStoreError(
                f"allowed_roots entry {allowed!r} resolves outside repo_root"
            )
        # An allowed_roots entry that resolves to repo_root ITSELF (".", "",
        # "./", "docs/..", or any other spelling that normalises to the
        # root) is the containment check's own trivial case: repo_root is
        # always "relative to" repo_root, so `allowed_path == root` used to
        # be accepted here -- which erases the subtree boundary this
        # function exists to create and, combined with ".html" being an
        # allowed suffix, reaches straight into the repo's source tree. A
        # proper subtree boundary must be a strict subset of repo_root.
        if allowed_path == root:
            raise ArtifactStoreError(
                f"allowed_roots entry {allowed!r} resolves to repo_root itself"
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

    # Every guard above is this module's own opinion, checked before any
    # filesystem call. What remains -- directory creation and the write --
    # is still at the mercy of the OS (a too-long full path even after the
    # relative-length bound above, since repo_root itself can be long; a
    # permissions error; a full disk). Whatever OSError the platform raises
    # here is re-raised as ArtifactStoreError so the writer's error contract
    # stays total regardless of platform, with the original exception
    # chained as the cause and the target path named in the message.
    try:
        candidate.parent.mkdir(parents=True, exist_ok=True)
        candidate.write_text(content, encoding="utf-8")
    except OSError as exc:
        raise ArtifactStoreError(
            f"failed to write repo artifact at {candidate}: {exc}"
        ) from exc
    rel = candidate.relative_to(root).as_posix()
    return MemoryRef(tier="episodic", key=f"repo:artifact:{rel}", summary=rel)
