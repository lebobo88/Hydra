"""Squad-registry discovery.

A squad is *any directory* under `squads/` (project-local) or `~/.hydra/squads/`
(user-global) that contains a `squad.yaml`. Hydra discovers them at supervisor
construction time. No code change required to add a squad.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Literal, Optional

import yaml

from .version import Version, parse_deprecated_after


SQUAD_DIR_NAMES = ("squads",)
USER_SQUAD_DIR = Path.home() / ".hydra" / "squads"


@dataclass(frozen=True)
class AgentSpec:
    slug: str
    role: str = ""
    authority: str = "advisory"          # advisory | execute | gatekeeper
    model_hint: Optional[str] = None
    # Optional inline cathedral overlay (Stage 4 heads.yaml has the canonical
    # path; this lets a squad.yaml declare the alias directly when there is
    # no separate overlay file).
    mythic: Optional[str] = None
    model_tier: Optional[str] = None     # "opus" | "sonnet" | "haiku" | None
    agent_file: Optional[str] = None     # relative path to the Claude Code agent definition
    parent: Optional[str] = None         # slug of the parent head (for sub-agents)
    hitl_trigger: bool = False           # True → this agent's gate always surfaces HITL
    notes: Optional[str] = None


@dataclass(frozen=True)
class ToolSpec:
    name: str
    mcp_server: Optional[str] = None
    # Privilege is conventionally one of read | write | execute | destructive
    # but the Garland Crown declares composite values like "read+write" for
    # the eights-memory tool; we accept any string and let consumers parse.
    privilege: str = "read"
    notes: Optional[str] = None


@dataclass(frozen=True)
class GateSpec:
    rubric_id: Optional[str] = None
    hitl_required: bool = False
    when: Optional[str] = None           # CEL-ish predicate, evaluated by governance


@dataclass(frozen=True)
class SquadPack:
    slug: str
    name: str
    description: str
    source_pack: Optional[str] = None        # filesystem path or None
    entrypoint: Literal["mcp", "subprocess", "agent-impersonation",
                        "claude-skill", "claude-native", "stub"] = "stub"
    industries: tuple[str, ...] = ()
    agents: tuple[AgentSpec, ...] = ()
    tools: tuple[ToolSpec, ...] = ()
    accepts: tuple[str, ...] = ()
    emits: tuple[str, ...] = ()
    gates: tuple[GateSpec, ...] = ()
    invoke: dict[str, Any] = None            # entrypoint-specific config
    version: Version = field(default_factory=lambda: Version(1, 0, 0))
    deprecated_after: Optional[date] = None
    # Best-of-N opt-in. 0 / None / 1 → no best-of-N. N≥2 → produce N candidate
    # outputs, judge each, Borda-rank, return the winner. See
    # `hydra_core.judge.best_of_n`.
    best_of_n: int = 0

    def can_accept(self, envelope_type: str) -> bool:
        return envelope_type in self.accepts or "*" in self.accepts


def resolve_agent_file_path(pack_root: Path, declared_rel_path: str) -> Path:
    """Resolve declared agent_file path with dual-path legacy/modern fallback."""
    primary = pack_root / declared_rel_path
    if primary.is_file():
        return primary
    if declared_rel_path.startswith("plugins/rlm-creative/agents/"):
        legacy_rel = ".claude/agents/" + declared_rel_path.removeprefix("plugins/rlm-creative/agents/")
        legacy = pack_root / legacy_rel
        if legacy.is_file():
            return legacy
    elif declared_rel_path.startswith(".claude/agents/"):
        modern_rel = "plugins/rlm-creative/agents/" + declared_rel_path.removeprefix(".claude/agents/")
        modern = pack_root / modern_rel
        if modern.is_file():
            return modern
    raise FileNotFoundError(f"Agent file '{declared_rel_path}' not found in pack at {pack_root}")


def _coerce_pack(slug: str, data: dict[str, Any], pack_root: Path | None = None) -> SquadPack:
    agent_specs = []
    seen_slugs: set[str] = set()
    for a in data.get("agents", []):
        spec_dict = dict(a)
        if pack_root and spec_dict.get("agent_file"):
            try:
                resolved = resolve_agent_file_path(pack_root, spec_dict["agent_file"])
                spec_dict["agent_file"] = resolved.relative_to(pack_root).as_posix()
            except FileNotFoundError:
                pass  # Allow overlay specs before repo checkout exists
        spec = AgentSpec(**spec_dict)
        if spec.slug in seen_slugs:
            raise ValueError(f"Duplicate agent slug '{spec.slug}' in squad '{slug}'")
        seen_slugs.add(spec.slug)
        agent_specs.append(spec)
    agents = tuple(agent_specs)
    tools = tuple(ToolSpec(**t) for t in data.get("tools", []))
    gates = tuple(GateSpec(**g) for g in data.get("gates", []))
    return SquadPack(
        slug=slug,
        name=data.get("name", slug),
        description=data.get("description", ""),
        source_pack=data.get("source_pack"),
        entrypoint=data.get("entrypoint", "stub"),
        industries=tuple(data.get("industries", [])),
        agents=agents,
        tools=tools,
        accepts=tuple(data.get("accepts", [])),
        emits=tuple(data.get("emits", [])),
        gates=gates,
        invoke=data.get("invoke", {}) or {},
        version=Version.parse(str(data.get("version", "1.0.0"))),
        deprecated_after=parse_deprecated_after(data.get("deprecated_after")),
        best_of_n=int(data.get("best_of_n", 0) or 0),
    )


def discover_squads(project_root: Path | None = None) -> dict[str, SquadPack]:
    """Discover squad packs. Resolution order: project → user → built-in."""
    project_root = project_root or Path.cwd()
    packs: dict[str, SquadPack] = {}

    search_dirs: list[Path] = []
    for name in SQUAD_DIR_NAMES:
        search_dirs.append(project_root / name)
    search_dirs.append(USER_SQUAD_DIR)

    for d in search_dirs:
        if not d.exists():
            continue
        for child in sorted(d.iterdir()):
            if not child.is_dir():
                continue
            yml = child / "squad.yaml"
            if not yml.exists():
                continue
            try:
                data = yaml.safe_load(yml.read_text(encoding="utf-8")) or {}
            except yaml.YAMLError as e:
                raise ValueError(f"Malformed squad.yaml at {yml}: {e}") from e
            slug = child.name
            if slug not in packs:  # project shadow wins
                packs[slug] = _coerce_pack(slug, data, pack_root=child)
    return packs


def route_envelope_to_squad(packs: dict[str, SquadPack], envelope_type: str) -> list[str]:
    """Return all squads that can accept a given envelope type."""
    return [s for s, p in packs.items() if p.can_accept(envelope_type)]
