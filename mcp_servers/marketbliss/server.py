"""MarketBliss — thin MCP shim over the MarketBliss Claude Code pack.

Exposes the marketing roster/skills/commands as read-only introspection plus a
sandboxed output writer matching the squad.yaml `output_dir:
output/{domain}/{topic}-{date}.md` convention. The pack itself is markdown —
this server does not run any LLM, write any campaign copy, or bypass any gate;
marketing execution happens in the Claude Code session under the MarketBliss
constitution.

Tools:
  mb.roster.list              — list .claude/agents/*.md (the 15 marketing agents)
  mb.agent.get(slug)          — return one agent's markdown + path
  mb.skill.list               — list .claude/skills/*
  mb.skill.get(name)          — return SKILL.md content for the named skill
  mb.command.list             — list .claude/commands/*.md
  mb.command.get(name)        — return one command's markdown
  mb.output.write(domain, topic, content) — persist under output/{domain}/
  mb.output.read(path)        — read a file under output/
  mb.ping                     — health probe
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parents[2]))

from mcp_servers._pack_shim import (  # noqa: E402
    list_dir, read_markdown, read_output, resolve_root, run_server, write_output,
)


def _tool_handlers():
    root = resolve_root("HYDRA_MB_ROOT", str(_HERE.parents[2].parent / "MarketBliss"))

    def roster_list(args: dict[str, Any]) -> dict[str, Any]:
        return {"root": str(root),
                "agents": list_dir(root, ".claude/agents", suffix=".md")}

    def agent_get(args: dict[str, Any]) -> dict[str, Any]:
        slug = args.get("slug", "")
        return read_markdown(root, f".claude/agents/{slug}.md")

    def skill_list(args: dict[str, Any]) -> dict[str, Any]:
        return {"root": str(root),
                "skills": list_dir(root, ".claude/skills", only_dirs=True)}

    def skill_get(args: dict[str, Any]) -> dict[str, Any]:
        name = args.get("name", "")
        return read_markdown(root, f".claude/skills/{name}/SKILL.md")

    def command_list(args: dict[str, Any]) -> dict[str, Any]:
        return {"root": str(root),
                "commands": list_dir(root, ".claude/commands", suffix=".md")}

    def command_get(args: dict[str, Any]) -> dict[str, Any]:
        name = args.get("name", "")
        return read_markdown(root, f".claude/commands/{name}.md")

    def output_write(args: dict[str, Any]) -> dict[str, Any]:
        domain = args.get("domain", "marketing")
        topic = args.get("topic", "untitled")
        content = args.get("content", "")
        return write_output(root, f"output/{domain}", topic, content)

    def output_read(args: dict[str, Any]) -> dict[str, Any]:
        return read_output(root, args.get("path", ""))

    def ping(args: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True, "root": str(root), "exists": root.exists()}

    return {
        "mb.roster.list": roster_list,
        "mb.agent.get": agent_get,
        "mb.skill.list": skill_list,
        "mb.skill.get": skill_get,
        "mb.command.list": command_list,
        "mb.command.get": command_get,
        "mb.output.write": output_write,
        "mb.output.read": output_read,
        "mb.ping": ping,
    }


def main() -> None:
    run_server("marketbliss", _tool_handlers())
