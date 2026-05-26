"""Squad-scoped tool injection — prompt-level filtering.

Claude Code does not support per-subagent MCP scoping for plugin-provided
agents (Hydra's agents are plugin-provided). Instead, we filter at the
prompt level: when composing the system prompt for a squad dispatch, we
inject a tool-scope directive that lists only the tools the squad is
authorized to use.

This is a soft enforcement layer complementing the hard RBAC in
dispatcher.py. The model may still attempt to call tools outside the
scope, but the dispatcher will reject them.

See: https://code.claude.com/docs/en/sub-agents (mcpServers ignored for
plugin subagents).
"""
from __future__ import annotations

from typing import Any

from .squad_loader import SquadPack, ToolSpec
from .toolshed import ToolShed


def build_tool_scope_directive(pack: SquadPack,
                                toolshed: ToolShed | None = None) -> str:
    """Build a tool-scope directive for a squad's system prompt.

    Returns a markdown block listing authorized tools with descriptions.
    Agents receiving this directive should prefer these tools and avoid
    calling tools not listed.
    """
    if not pack.tools:
        return ""

    lines = [
        f"## Tool Scope — {pack.name}",
        "",
        "You are authorized to use ONLY the following tools. "
        "Do not attempt to call tools outside this list.",
        "",
    ]

    for tool in pack.tools:
        desc = tool.notes or tool.name
        server = f" (server: {tool.mcp_server})" if tool.mcp_server else ""
        priv = f" [{tool.privilege}]"
        lines.append(f"- **{tool.name}**{server}{priv}: {desc}")

    if toolshed and pack.tools:
        lines.extend([
            "",
            "To discover additional tools on authorized servers, use:",
            "- `toolshed.search(query, server=<server>)` — find tools by keyword",
            "- `toolshed.describe(server, tool_name)` — get full schema",
        ])

    return "\n".join(lines)


def build_node_tool_scope(
    node_name: str,
    selected_squads: list[str],
    packs: dict[str, SquadPack],
) -> str:
    """Build a tool-scope directive scoped to a supervisor node.

    Different nodes need different tool subsets:
    - intake: router, classification tools
    - planner: squad discovery, taxonomy
    - dispatch: squad-specific tools per selected squad
    - synthesis: memory, envelope tools
    - postcheck: governance, budget tools
    """
    tool_categories = _NODE_TOOL_CATEGORIES.get(node_name, set())
    if not tool_categories:
        return ""

    lines = [
        f"## Node Tool Scope — {node_name}",
        "",
        f"This node uses tools in categories: {', '.join(sorted(tool_categories))}",
        "",
    ]

    if node_name == "dispatch":
        for slug in selected_squads:
            pack = packs.get(slug)
            if pack and pack.tools:
                lines.append(f"### {pack.name} ({slug})")
                for tool in pack.tools:
                    lines.append(f"  - {tool.name} [{tool.privilege}]")
                lines.append("")

    return "\n".join(lines)


_NODE_TOOL_CATEGORIES: dict[str, set[str]] = {
    "intake": {"read", "config"},
    "planner": {"read", "config"},
    "approval": {"governance"},
    "dispatch": {"execute", "write", "read"},
    "judge_per_squad": {"judge", "read"},
    "synthesis": {"memory", "read", "write"},
    "judge_synthesis": {"judge", "read"},
    "postcheck": {"governance", "read"},
}


def filter_tools_for_squad(
    all_tools: list[dict[str, Any]],
    pack: SquadPack,
) -> list[dict[str, Any]]:
    """Filter a tool list to only include tools declared in a squad's pack.

    Used when generating per-squad tool manifests for contexts where
    MCP scoping IS supported (non-plugin subagents, future API support).
    """
    allowed_names = {t.name for t in pack.tools}
    allowed_servers = {t.mcp_server for t in pack.tools if t.mcp_server}

    return [
        t for t in all_tools
        if t.get("name") in allowed_names
        or (t.get("mcp_server") in allowed_servers and t.get("name") in allowed_names)
    ]


def squad_tool_manifest(pack: SquadPack) -> dict[str, Any]:
    """Generate a tool manifest for a squad.

    Returns a dict suitable for serialization as a per-squad tool config.
    This is the data that WOULD go into a per-squad .mcp.json if Claude Code
    supported it for plugin subagents.
    """
    return {
        "squad": pack.slug,
        "entrypoint": pack.entrypoint,
        "tools": [
            {
                "name": t.name,
                "mcp_server": t.mcp_server,
                "privilege": t.privilege,
                "notes": t.notes,
            }
            for t in pack.tools
        ],
        "limitation": (
            "Claude Code ignores mcpServers for plugin subagents. "
            "This manifest is enforced via dispatcher RBAC (hard) and "
            "prompt-level tool-scope directives (soft)."
        ),
    }
