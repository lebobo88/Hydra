"""Xenia — thin MCP shim over the Xenia customer-support Claude Code pack.

Exposes the Xenia skill / command / agent surface as read-only introspection
plus a sandboxed output writer matching the squad.yaml
`output_dir: hearth/output/{phase}/{topic}-{date}.md` convention.

The server writes ONLY under hearth/output/ — hearth/progress/events.jsonl
belongs exclusively to the post-output-sla-stamp hook (single-writer rule;
TheEights' xenia-watcher tails that file).

Tools:
  xenia.skill.list / xenia.skill.get(name)
  xenia.command.list / xenia.command.get(name)
  xenia.agent.list / xenia.agent.get(slug)     — from .claude/agents/*.md
  xenia.output.write(phase, topic, content)    — persist under hearth/output/{phase}/
  xenia.output.read(path)
  xenia.ping
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
    root = resolve_root("HYDRA_XENIA_ROOT", str(_HERE.parents[2].parent / "Xenia"))

    def skill_list(args: dict[str, Any]) -> dict[str, Any]:
        return {"root": str(root),
                "skills": list_dir(root, ".claude/skills", only_dirs=True)}

    def skill_get(args: dict[str, Any]) -> dict[str, Any]:
        return read_markdown(root, f".claude/skills/{args.get('name','')}/SKILL.md")

    def command_list(args: dict[str, Any]) -> dict[str, Any]:
        return {"root": str(root),
                "commands": list_dir(root, ".claude/commands", suffix=".md")}

    def command_get(args: dict[str, Any]) -> dict[str, Any]:
        return read_markdown(root, f".claude/commands/{args.get('name','')}.md")

    def agent_list(args: dict[str, Any]) -> dict[str, Any]:
        top = [a for a in list_dir(root, ".claude/agents", suffix=".md")
               if not a.get("is_dir")]
        sub = [a for a in list_dir(root, ".claude/agents/soteria-crew", suffix=".md")
               if not a.get("is_dir")]
        for entry in sub:
            entry["name"] = f"soteria-crew/{entry['name']}"
        return {"root": str(root), "agents": top + sub}

    def agent_get(args: dict[str, Any]) -> dict[str, Any]:
        slug = args.get("slug", "")
        return read_markdown(root, f".claude/agents/{slug}.md")

    def output_write(args: dict[str, Any]) -> dict[str, Any]:
        phase = args.get("phase", "tickets")
        topic = args.get("topic", "untitled")
        content = args.get("content", "")
        return write_output(root, f"hearth/output/{phase}", topic, content)

    def output_read(args: dict[str, Any]) -> dict[str, Any]:
        return read_output(root, args.get("path", ""))

    def ping(args: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True, "root": str(root), "exists": root.exists()}

    return {
        "xenia.skill.list": skill_list,
        "xenia.skill.get": skill_get,
        "xenia.command.list": command_list,
        "xenia.command.get": command_get,
        "xenia.agent.list": agent_list,
        "xenia.agent.get": agent_get,
        "xenia.output.write": output_write,
        "xenia.output.read": output_read,
        "xenia.ping": ping,
    }


def main() -> None:
    run_server("xenia", _tool_handlers())
