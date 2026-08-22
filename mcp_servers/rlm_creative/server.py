"""RLM Creative — thin MCP shim over the RLM-CLI-Starter Claude Code pack.

Exposes the RLM skill / command / agent surface as read-only introspection
plus a sandboxed output writer matching the squad.yaml
`output_dir: RLM/output/{phase}/{topic}-{date}.md` convention.

Tools:
  rlm.skill.list / rlm.skill.get(name)
  rlm.command.list / rlm.command.get(name)   — plugin command discovery
  rlm.agent.list / rlm.agent.get(slug)       — plugin agent discovery
  rlm.output.write(phase, topic, content|body, domain, scopes)
                                             — contract-validated RLM output
  rlm.output.read(path)
  rlm.ping
"""
from __future__ import annotations

import sys
import re
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parents[2]))

from mcp_servers._pack_shim import (  # noqa: E402
    list_dir, read_markdown, read_output, resolve_root, run_server, write_output,
)


_VALID_PHASES = frozenset({"launch", "photo", "brand", "brief", "pr", "paid", "seo", "governance"})
_CONTROLLED_SCOPES = frozenset({
    "public", "team:garland-crew", "team:helios-sub", "sensitive:ip",
    "sensitive:client-confidential", "assetlib:approved", "render:4k",
    "render:hdr", "audio:5.1",
})
_KEBAB_TOPIC = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


AUTHORITATIVE_AGENT_MAP: dict[str, str] = {
    "plugins/rlm-creative/agents/calliope.md": "brand-strategist",
    "plugins/rlm-creative/agents/erato.md": "copywriter",
    "plugins/rlm-creative/agents/polyhymnia.md": "content-strategist",
    "plugins/rlm-creative/agents/terpsichore.md": "social-community",
    "plugins/rlm-creative/agents/euterpe.md": "paid-acquisition",
    "plugins/rlm-creative/agents/clio.md": "pr-earned",
    "plugins/rlm-creative/agents/urania.md": "seo-discovery",
    "plugins/rlm-creative/agents/helios.md": "photo-cinema",
    "plugins/rlm-creative/agents/helios-crew/video-synth.md": "video-synth",
    "plugins/rlm-creative/agents/helios-crew/audio-foley.md": "audio-foley",
    "plugins/rlm-creative/agents/helios-crew/music-score.md": "music-score",
    "plugins/rlm-creative/agents/helios-crew/dialogue-mix.md": "dialogue-mix",
    "plugins/rlm-creative/agents/helios-crew/blender-model.md": "blender-model",
    "plugins/rlm-creative/agents/helios-crew/blender-rig.md": "blender-rig",
    "plugins/rlm-creative/agents/helios-crew/governance-c2pa.md": "governance-c2pa",
}

# Validate static bijection properties at module load time
assert len(AUTHORITATIVE_AGENT_MAP) == 15, "Authoritative roster must have exactly 15 agents"
assert len(set(AUTHORITATIVE_AGENT_MAP.values())) == 15, "Authoritative slugs must be strictly unique"
assert len({k.lower() for k in AUTHORITATIVE_AGENT_MAP}) == 15, "Case collision detected in agent paths"
assert len({v.lower() for v in AUTHORITATIVE_AGENT_MAP.values()}) == 15, "Case collision detected in agent slugs"

GARLAND_REQUIRED_AGENTS = frozenset(AUTHORITATIVE_AGENT_MAP.values())


def _agent_catalog(root: Path) -> list[dict[str, Any]]:
    """Return all plugin agents matching the authoritative catalog."""
    catalog = []
    for rel_path, slug in AUTHORITATIVE_AGENT_MAP.items():
        p = root / rel_path
        if p.is_file():
            catalog.append({"name": slug, "path": rel_path, "is_dir": False})
    return catalog


def _tool_handlers():
    root = resolve_root("HYDRA_RLM_ROOT", str(_HERE.parents[2].parent / "RLM-Creative"), fallback_env="HYDRA_RLM_CREATIVE_ROOT")
    if not root.is_dir():
        raise FileNotFoundError(f"RLM-Creative repository root not found at: {root}")

    # Exact-set validation at server startup
    agent_catalog = _agent_catalog(root)
    found_slugs = {agent["name"] for agent in agent_catalog}
    if missing := (GARLAND_REQUIRED_AGENTS - found_slugs):
        raise FileNotFoundError(f"Missing required Garland agent files in {root}: {sorted(missing)}")

    agent_root = root / "plugins" / "rlm-creative" / "agents"
    if agent_root.is_dir():
        all_disk_paths = [p.relative_to(root).as_posix() for p in agent_root.rglob("*.md")]
        lower_disk = [p.lower() for p in all_disk_paths]
        if len(lower_disk) != len(set(lower_disk)):
            raise ValueError("Disk-side case collision detected in agent directory")
        if extra_files := (set(all_disk_paths) - set(AUTHORITATIVE_AGENT_MAP.keys())):
            raise ValueError(f"Unexpected extra agent markdown files in plugin: {sorted(extra_files)}")

    def skill_list(args: dict[str, Any]) -> dict[str, Any]:
        return {"root": str(root),
                "skills": list_dir(root, "plugins/rlm-creative/skills", only_dirs=True)}

    def skill_get(args: dict[str, Any]) -> dict[str, Any]:
        return read_markdown(root, f"plugins/rlm-creative/skills/{args.get('name','')}/SKILL.md")

    def command_list(args: dict[str, Any]) -> dict[str, Any]:
        return {"root": str(root),
                "commands": list_dir(root, "plugins/rlm-creative/commands", suffix=".md")}

    def command_get(args: dict[str, Any]) -> dict[str, Any]:
        return read_markdown(root, f"plugins/rlm-creative/commands/{args.get('name','')}.md")

    def agent_list(args: dict[str, Any]) -> dict[str, Any]:
        return {"root": str(root), "agents": _agent_catalog(root)}

    def agent_get(args: dict[str, Any]) -> dict[str, Any]:
        slug = str(args.get("slug", ""))
        match = next((agent for agent in _agent_catalog(root) if agent["name"] == slug), None)
        if match is None:
            return {"error": "not_found", "slug": slug}
        return read_markdown(root, match["path"])

    def output_write(args: dict[str, Any]) -> dict[str, Any]:
        phase = args.get("phase")
        topic = args.get("topic")
        content = args.get("content", args.get("body", ""))
        scopes = args.get("scopes")
        errors: list[str] = []
        if args.get("domain") != "creative":
            errors.append("domain must be 'creative'")
        if phase not in _VALID_PHASES:
            errors.append("phase must be one of: " + ", ".join(sorted(_VALID_PHASES)))
        if not isinstance(topic, str) or not _KEBAB_TOPIC.fullmatch(topic):
            errors.append("topic must be a non-empty kebab-case string")
        if not isinstance(scopes, list) or not any(scope in _CONTROLLED_SCOPES for scope in scopes):
            errors.append("scopes must include at least one controlled creative scope tag")
        if not isinstance(content, str):
            errors.append("content or body must be a string")
        if errors:
            return {"error": "invalid_output_contract", "details": errors}
        result = write_output(root, f"RLM/output/{phase}", topic, content)
        return {**result, "domain": "creative", "scopes": scopes}

    def output_read(args: dict[str, Any]) -> dict[str, Any]:
        return read_output(root, args.get("path", ""))

    def ping(args: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True, "root": str(root), "exists": root.exists()}

    return {
        "rlm.skill.list": skill_list,
        "rlm.skill.get": skill_get,
        "rlm.command.list": command_list,
        "rlm.command.get": command_get,
        "rlm.agent.list": agent_list,
        "rlm.agent.get": agent_get,
        "rlm.output.write": output_write,
        "rlm.output.read": output_read,
        "rlm.ping": ping,
    }


def main() -> None:
    run_server("rlm-creative", _tool_handlers())
