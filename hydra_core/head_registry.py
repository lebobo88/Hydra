"""Head registry: map (squad slug, head slug) to a plugin-scoped agent.

Read-only resolution helper for ``claude-native`` squad packs.  It changes no
dispatch path; it only answers "which agent file backs this head, and what is
its ``plugin:agent`` name?".

Resolution, first rule that yields an existing file wins:

1. ``override`` -- ``NativePack.lead_agent`` applies to exactly one head: the
   head whose slug equals it, else the head whose ``agent_file`` stem equals
   it.  A tie within the winning tier applies it to none and is reported in
   ``error``.
2. ``agent_file_stem`` -- the stem of the head's declared ``agent_file``,
   resolved through ``squad_loader.resolve_agent_file_path`` (handles nested
   and legacy/modern paths).
3. ``slug`` -- ``<agents_dir>/<head slug>.md`` (non-recursive).

Otherwise the head is ``unresolved``.  Independently of the rule that picked
the agent, ``stale_agent_file`` is set whenever the declared ``agent_file``
cannot be resolved.

Never raises
------------
For any ``packs`` / ``pack_root``, :func:`resolve_head` returns a
:class:`HeadResolution` and :func:`resolve_all_heads` returns one
``HeadResolution`` per head of every ``claude-native`` pack it is given.  A
filesystem or configuration failure (``OSError``, ``ValueError``, a malformed or wrongly
shaped ``squad.yaml`` -- including the ``AttributeError`` / ``TypeError`` that
default discovery raises for it -- an unavailable pack checkout, an unreadable
agent file) is recorded in ``error``, never raised.  ``KeyboardInterrupt``,
``SystemExit`` and other ``BaseException``-only classes are NOT
caught, and neither are programming errors: ``TypeError`` / ``AttributeError``
from bad *arguments* passed to these functions.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal, Mapping

from .native_packs import native_pack, native_pack_root
from .squad_loader import SquadPack, discover_squads, resolve_agent_file_path

Rule = Literal["override", "agent_file_stem", "slug", "unresolved"]


@dataclass(frozen=True)
class HeadResolution:
    squad: str
    head: str
    plugin: str
    agent_name: str
    qualified_name: str
    agent_path: Path | None
    rule: Rule
    frontmatter_name: str | None = None
    stale_agent_file: str | None = None
    error: str | None = None


# native_pack_root -> repo_registry.resolve_repo_path raises ValueError (unknown
# repo, not a git root, git failed) and OSError (resolve()/subprocess failures);
# RuntimeError is a symlink loop from Path.resolve() on older Pythons.
_ROOT_ERRORS = (ValueError, OSError, RuntimeError)
_ROOT_ERROR_PREFIX = "pack checkout unavailable"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _frontmatter(path: Path) -> tuple[str | None, str | None]:
    """Single read of the agent file: ``(frontmatter_name, read_error)``."""
    from .cli import _read_agent_frontmatter  # lazy: avoid module-level cli import

    return _read_agent_frontmatter(path)


def _unresolved(squad: str, head: str, plugin: str, error: str,
                stale: str | None = None) -> HeadResolution:
    return HeadResolution(
        squad=squad, head=head, plugin=plugin, agent_name=head,
        qualified_name=f"{plugin}:{head}", agent_path=None, rule="unresolved",
        stale_agent_file=stale, error=error,
    )


def _lead_heads(pack: SquadPack, lead: str) -> tuple[set[str], set[str]]:
    """Return ``(winners, tied)`` head slugs for the override.

    Exactly one head per pack may receive the override.  Tier 1: the head whose
    slug equals ``lead``.  Only when no head has that slug, tier 2: the head whose
    ``agent_file`` stem equals ``lead``.  If several heads tie inside the winning
    tier none wins; they are returned in ``tied`` so the caller can report the
    ambiguity and let them resolve by the later rules.
    """
    by_slug = {a.slug for a in pack.agents if a.slug == lead}
    if by_slug:
        return (by_slug, set()) if len(by_slug) == 1 else (set(), by_slug)
    by_stem = {a.slug for a in pack.agents if a.agent_file and Path(a.agent_file).stem == lead}
    if len(by_stem) == 1:
        return by_stem, set()
    return set(), by_stem


def resolve_head(
    squad_slug: str,
    head_slug: str,
    *,
    packs: Mapping[str, SquadPack] | None = None,
    pack_root: Path | None = None,
) -> HeadResolution:
    """Resolve one head.

    Never raises: always returns a :class:`HeadResolution`.  Filesystem and
    configuration failures are recorded in ``error`` (see the module docstring
    for the exact contract and its exclusions).
    """
    try:
        native = native_pack(squad_slug)
    except ValueError as exc:
        return _unresolved(squad_slug, head_slug, "", str(exc))
    plugin = native.plugin

    if packs is None:
        try:
            packs = discover_squads(_repo_root())
        # discover_squads parses operator-written YAML and can raise arbitrary
        # Exception subclasses (e.g. OverflowError for `best_of_n: .inf`). Four
        # reviews each found another class, so the operator approved a broad
        # handler at this one site. BaseException-only classes (KeyboardInterrupt,
        # SystemExit, GeneratorExit) are deliberately not caught and propagate.
        except Exception as exc:
            return _unresolved(squad_slug, head_slug, plugin,
                               f"squad discovery failed: {type(exc).__name__}: {exc}")
    pack = packs.get(squad_slug)
    if pack is None:
        return _unresolved(squad_slug, head_slug, plugin, f"squad {squad_slug!r} not found in packs")
    spec = next((a for a in pack.agents if a.slug == head_slug), None)
    if spec is None:
        return _unresolved(squad_slug, head_slug, plugin,
                           f"head {head_slug!r} not declared by squad {squad_slug!r}")

    if pack_root is None:
        try:
            pack_root = native_pack_root(squad_slug)
        except _ROOT_ERRORS as exc:  # an unresolved checkout is data, not a crash
            return _unresolved(squad_slug, head_slug, plugin,
                               f"{_ROOT_ERROR_PREFIX}: {exc}")
    agents_dir = Path(pack_root) / "plugins" / plugin / "agents"

    declared = spec.agent_file

    # The declaration is checked independently of which rule picks the agent, so a
    # stale declared path is reported whichever rule resolved the head.
    declared_path: Path | None = None
    stale: str | None = None
    fs_errors: list[str] = []  # filesystem failures, reported in ``error``
    if declared:
        try:
            declared_path = resolve_agent_file_path(Path(pack_root), declared)
        except FileNotFoundError:
            stale = declared
        except OSError as exc:  # PermissionError, EIO, ENAMETOOLONG, ...
            stale = declared
            fs_errors.append(f"declared agent_file {declared}: {type(exc).__name__}: {exc}")

    # (1) override: exactly one head per pack is the lead (see _lead_heads).
    lead = native.lead_agent
    winners, tied = _lead_heads(pack, lead)
    ambiguity: str | None = None
    if tied:
        ambiguity = (f"lead_agent {lead!r} is ambiguous in squad {squad_slug!r}: heads "
                     f"{sorted(tied)} all declare an agent_file stem {lead!r}; "
                     f"override not applied")

    def errors(*extra: str) -> str | None:
        parts = [ambiguity] if ambiguity and head_slug in tied else []
        parts.extend(fs_errors)
        parts.extend(extra)
        return "; ".join(parts) or None

    def candidate_is_file(path: Path) -> bool:
        try:
            return path.is_file()
        except OSError as exc:  # PermissionError can escape on some platforms
            fs_errors.append(f"{path}: {type(exc).__name__}: {exc}")
            return False

    def done(name: str, path: Path, rule: Rule) -> HeadResolution:
        fm_name, read_err = _frontmatter(path)
        if read_err:  # keep rule and agent_path; report the read failure
            fs_errors.append(f"{path}: {read_err}")
        return HeadResolution(
            squad=squad_slug, head=head_slug, plugin=plugin, agent_name=name,
            qualified_name=f"{plugin}:{name}", agent_path=path, rule=rule,
            frontmatter_name=fm_name, stale_agent_file=stale,
            error=errors(),
        )

    if head_slug in winners:
        lead_path = agents_dir / f"{lead}.md"
        if candidate_is_file(lead_path):
            return done(lead, lead_path, "override")

    # (2) stem of the declared agent_file.
    if declared_path is not None:
        return done(declared_path.stem, declared_path, "agent_file_stem")

    # (3) head slug.
    slug_path = agents_dir / f"{head_slug}.md"
    if candidate_is_file(slug_path):
        return done(head_slug, slug_path, "slug")

    msg = errors(f"no agent file for head {head_slug!r} under {agents_dir}")
    return _unresolved(squad_slug, head_slug, plugin, msg or "", stale)


def resolve_all_heads(
    packs: Mapping[str, SquadPack],
    *,
    pack_root_for: Callable[[str], Path] | None = None,
) -> list[HeadResolution]:
    """Resolve every head of every ``claude-native`` pack.

    Never raises: returns one :class:`HeadResolution` per head of every
    ``claude-native`` pack, recording failures in ``error`` (see the module
    docstring for the exact contract and its exclusions).
    """
    root_for = pack_root_for or native_pack_root
    out: list[HeadResolution] = []
    for slug, pack in packs.items():
        if pack.entrypoint != "claude-native":
            continue
        try:
            root = Path(root_for(slug))
        except _ROOT_ERRORS as exc:
            plugin = ""
            try:
                np = native_pack(slug)
                plugin = np.plugin
            except ValueError:
                pass
            err = f"{_ROOT_ERROR_PREFIX}: {exc}"
            out.extend(_unresolved(slug, a.slug, plugin, err) for a in pack.agents)
            continue
        out.extend(resolve_head(slug, a.slug, packs=packs, pack_root=root) for a in pack.agents)
    return out
