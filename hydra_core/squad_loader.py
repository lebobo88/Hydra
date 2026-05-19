"""Squad-registry discovery.

A squad is *any directory* under `squads/` (project-local) or `~/.hydra/squads/`
(user-global) that contains a `squad.yaml`. Hydra discovers them at supervisor
construction time. No code change required to add a squad.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Optional

import yaml


SQUAD_DIR_NAMES = ("squads",)
USER_SQUAD_DIR = Path.home() / ".hydra" / "squads"


@dataclass(frozen=True)
class AgentSpec:
    slug: str
    role: str
    authority: str = "advisory"          # advisory | execute | gatekeeper
    model_hint: Optional[str] = None


@dataclass(frozen=True)
class ToolSpec:
    name: str
    mcp_server: Optional[str] = None
    privilege: Literal["read", "write", "execute", "destructive"] = "read"


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
                        "claude-skill", "stub"] = "stub"
    industries: tuple[str, ...] = ()
    agents: tuple[AgentSpec, ...] = ()
    tools: tuple[ToolSpec, ...] = ()
    accepts: tuple[str, ...] = ()
    emits: tuple[str, ...] = ()
    gates: tuple[GateSpec, ...] = ()
    invoke: dict[str, Any] = None            # entrypoint-specific config

    def can_accept(self, envelope_type: str) -> bool:
        return envelope_type in self.accepts or "*" in self.accepts


def _coerce_pack(slug: str, data: dict[str, Any]) -> SquadPack:
    agents = tuple(AgentSpec(**a) for a in data.get("agents", []))
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
                packs[slug] = _coerce_pack(slug, data)
    return packs


def route_envelope_to_squad(packs: dict[str, SquadPack], envelope_type: str) -> list[str]:
    """Return all squads that can accept a given envelope type."""
    return [s for s, p in packs.items() if p.can_accept(envelope_type)]
