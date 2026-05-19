"""RLM Creative — thin MCP shim over the RLM-CLI-Starter Claude Code pack.

Exposes the RLM skill / command / agent surface as read-only introspection
plus a sandboxed output writer matching the squad.yaml
`output_dir: RLM/output/{phase}/{topic}-{date}.md` convention.

Tools:
  rlm.skill.list / rlm.skill.get(name)
  rlm.command.list / rlm.command.get(name)   — filters to rlm-* commands
  rlm.agent.list / rlm.agent.get(slug)       — from RLM/agents/*.md
  rlm.output.write(phase, topic, content)    — persist under RLM/output/{phase}/
  rlm.output.read(path)
  rlm.ping
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
    root = resolve_root("HYDRA_RLM_ROOT", "C:/AiAppDeployments/RLM-CLI-Starter")

    def skill_list(args: dict[str, Any]) -> dict[str, Any]:
        return {"root": str(root),
                "skills": list_dir(root, ".claude/skills", only_dirs=True)}

    def skill_get(args: dict[str, Any]) -> dict[str, Any]:
        return read_markdown(root, f".claude/skills/{args.get('name','')}/SKILL.md")

    def command_list(args: dict[str, Any]) -> dict[str, Any]:
        all_cmds = list_dir(root, ".claude/commands", suffix=".md")
        rlm_only = [c for c in all_cmds if c["name"].startswith("rlm-")]
        return {"root": str(root), "commands": rlm_only}

    def command_get(args: dict[str, Any]) -> dict[str, Any]:
        return read_markdown(root, f".claude/commands/{args.get('name','')}.md")

    def agent_list(args: dict[str, Any]) -> dict[str, Any]:
        return {"root": str(root),
                "agents": list_dir(root, "RLM/agents", suffix=".md")}

    def agent_get(args: dict[str, Any]) -> dict[str, Any]:
        return read_markdown(root, f"RLM/agents/{args.get('slug','')}.md")

    def output_write(args: dict[str, Any]) -> dict[str, Any]:
        phase = args.get("phase", "general")
        topic = args.get("topic", "untitled")
        content = args.get("content", "")
        return write_output(root, f"RLM/output/{phase}", topic, content)

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
