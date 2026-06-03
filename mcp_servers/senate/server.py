"""Senate — thin MCP shim over the Senate Claude Code pack (the Curia Crown).

Exposes the jurist roster/skills/commands as read-only introspection plus a
sandboxed output writer matching the squad.yaml `output_dir:
output/{domain}/{topic}-{date}.md` convention. The pack itself is markdown —
this server does not run any LLM, render any legal opinion, or bypass any
gate; counsel happens in the Claude Code session under the Twelve Tables.

Tools:
  senate.roster.list          — list .claude/agents/*.md (the 12 jurists)
  senate.agent.get(slug)      — return one jurist's markdown + path
  senate.skill.list           — list .claude/skills/*
  senate.skill.get(name)      — return SKILL.md content for the named skill
  senate.command.list         — list .claude/commands/*.md
  senate.command.get(name)    — return one command's markdown
  senate.output.write(domain, topic, content) — persist under output/{domain}/
  senate.output.read(path)    — read a file under output/
  senate.ping                 — health probe
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
    root = resolve_root("HYDRA_SENATE_ROOT", str(_HERE.parents[2].parent / "Senate"))

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
        domain = args.get("domain", "legal")
        topic = args.get("topic", "untitled")
        content = args.get("content", "")
        return write_output(root, f"output/{domain}", topic, content)

    def output_read(args: dict[str, Any]) -> dict[str, Any]:
        return read_output(root, args.get("path", ""))

    def ping(args: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True, "root": str(root), "exists": root.exists()}

    return {
        "senate.roster.list": roster_list,
        "senate.agent.get": agent_get,
        "senate.skill.list": skill_list,
        "senate.skill.get": skill_get,
        "senate.command.list": command_list,
        "senate.command.get": command_get,
        "senate.output.write": output_write,
        "senate.output.read": output_read,
        "senate.ping": ping,
    }


def main() -> None:
    run_server("senate", _tool_handlers())
