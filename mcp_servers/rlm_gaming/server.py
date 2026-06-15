"""RLM Gaming — thin MCP shim over the RLM-Gaming Arcade-crown Claude Code pack.

Exposes the Arcade-crown skill / command / agent surface as read-only
introspection plus a sandboxed output writer matching the squad.yaml
`output_dir: RLM/output/gaming/{phase}/{topic}-{date}.md` convention.

This is the per-squad shim Hydra's `_via_claude_skill` adapter consults for
`rlmgaming.command.list` (live command catalogue) and `rlmgaming.output.write`
(persist the Arcade crown's design artifacts + DECISION_RECORD). Without this
shim + its `_SKILL_PACK_SHIMS` registry entry, an `rlm-gaming` dispatch would
fall back to the Garland (`rlm_creative`) shim and write into the wrong store.

Tools:
  rlmgaming.skill.list / rlmgaming.skill.get(name)
  rlmgaming.command.list / rlmgaming.command.get(name)   — filters to game-* commands
  rlmgaming.agent.list / rlmgaming.agent.get(slug)        — from .claude/agents/the-*.md
  rlmgaming.output.write(phase, topic, content)           — persist under RLM/output/gaming/{phase}/
  rlmgaming.output.read(path)
  rlmgaming.ping
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
    root = resolve_root("HYDRA_RLM_GAMING_ROOT",
                        str(_HERE.parents[2].parent / "RLM-Gaming"))

    def skill_list(args: dict[str, Any]) -> dict[str, Any]:
        return {"root": str(root),
                "skills": list_dir(root, ".claude/skills", only_dirs=True)}

    def skill_get(args: dict[str, Any]) -> dict[str, Any]:
        return read_markdown(root, f".claude/skills/{args.get('name','')}/SKILL.md")

    def command_list(args: dict[str, Any]) -> dict[str, Any]:
        all_cmds = list_dir(root, ".claude/commands", suffix=".md")
        game_only = [c for c in all_cmds if c["name"].startswith("game-")]
        return {"root": str(root), "commands": game_only}

    def command_get(args: dict[str, Any]) -> dict[str, Any]:
        return read_markdown(root, f".claude/commands/{args.get('name','')}.md")

    def agent_list(args: dict[str, Any]) -> dict[str, Any]:
        return {"root": str(root),
                "agents": list_dir(root, ".claude/agents", suffix=".md")}

    def agent_get(args: dict[str, Any]) -> dict[str, Any]:
        return read_markdown(root, f".claude/agents/{args.get('slug','')}.md")

    def output_write(args: dict[str, Any]) -> dict[str, Any]:
        phase = args.get("phase", "general")
        topic = args.get("topic", "untitled")
        content = args.get("content", "")
        return write_output(root, f"RLM/output/gaming/{phase}", topic, content)

    def output_read(args: dict[str, Any]) -> dict[str, Any]:
        return read_output(root, args.get("path", ""))

    def ping(args: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True, "root": str(root), "exists": root.exists()}

    return {
        "rlmgaming.skill.list": skill_list,
        "rlmgaming.skill.get": skill_get,
        "rlmgaming.command.list": command_list,
        "rlmgaming.command.get": command_get,
        "rlmgaming.agent.list": agent_list,
        "rlmgaming.agent.get": agent_get,
        "rlmgaming.output.write": output_write,
        "rlmgaming.output.read": output_read,
        "rlmgaming.ping": ping,
    }


def main() -> None:
    run_server("rlm-gaming", _tool_handlers())


if __name__ == "__main__":
    main()
