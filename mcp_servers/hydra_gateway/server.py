"""Hydra Gateway — unified MCP server proxying all backend servers.

Integrates RBAC enforcement (Phase A), meta-tool facade (Phase B),
intent-based tool gating (Phase C1), and progressive disclosure (Phase C3)
into a single MCP endpoint.

Replaces the need to register 8 separate MCP servers at user scope.
Agents interact with one server that routes to the correct backend.

Tools:
  - gateway.discover     — list available servers and categories
  - gateway.search       — search tools by keyword across all servers
  - gateway.describe     — get full schema for a specific tool
  - gateway.call         — execute a tool on its backend server (with RBAC)
  - gateway.scope        — get the tool scope for a goal + squad combo
  - gateway.health       — check which backend servers are reachable

Resource URIs:
  - hydra://squad/{slug}/tools  — tools available to a specific squad
  - hydra://server/{name}/tools — all tools on a specific server
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parents[2]))

from hydra_core.toolshed import ToolShed, ProgressiveDisclosureTree, build_default_shed  # noqa: E402
from hydra_core.squad_loader import discover_squads  # noqa: E402
from hydra_core.tool_scope import (  # noqa: E402
    build_tool_scope_directive,
    squad_tool_manifest,
)
from hydra_core.router import compute_tool_scope  # noqa: E402


def _build_gateway() -> tuple[ToolShed, dict, ProgressiveDisclosureTree]:
    """Build the gateway's internal state."""
    project_root = _HERE.parents[2]
    shed = build_default_shed()
    packs = discover_squads(project_root)
    tree = ProgressiveDisclosureTree(shed, packs)
    return shed, packs, tree


def _tool_handlers() -> dict[str, callable]:
    shed, packs, tree = _build_gateway()

    def discover(args: dict[str, Any]) -> dict[str, Any]:
        server_filter = args.get("server")
        squad_filter = args.get("squad")

        if squad_filter:
            pack = packs.get(squad_filter)
            if pack is None:
                return {"error": "unknown_squad", "squad": squad_filter}
            return squad_tool_manifest(pack)

        result: dict[str, Any] = {
            "servers": shed.list_servers(),
            "squads": [
                {
                    "slug": s,
                    "name": p.name,
                    "entrypoint": p.entrypoint,
                    "tool_count": len(p.tools),
                }
                for s, p in sorted(packs.items())
            ],
        }
        if server_filter:
            result["categories"] = shed.list_categories(server=server_filter)
        else:
            result["categories"] = shed.list_categories()
        return result

    def search(args: dict[str, Any]) -> dict[str, Any]:
        results = shed.search(
            args.get("query", ""),
            server=args.get("server"),
            category=args.get("category"),
            limit=int(args.get("limit", 10)),
        )
        return {
            "results": [
                {
                    "server": r.server,
                    "name": r.name,
                    "description": r.description,
                    "category": r.category,
                    "relevance": r.relevance_score,
                }
                for r in results
            ],
            "count": len(results),
        }

    def describe(args: dict[str, Any]) -> dict[str, Any]:
        entry = shed.describe(args.get("server", ""), args.get("tool_name", ""))
        if entry is None:
            return {"error": "not_found"}
        return entry

    def call(args: dict[str, Any]) -> dict[str, Any]:
        server = args.get("server", "")
        tool_name = args.get("tool_name", "")
        tool_args = args.get("args", {})
        squad_id = args.get("squad_id")
        if squad_id:
            pack = packs.get(squad_id)
            if pack:
                allowed = {t.name for t in pack.tools}
                if tool_name not in allowed:
                    mcp_servers = {t.mcp_server for t in pack.tools if t.mcp_server}
                    full_names = {f"{t.mcp_server}.{t.name}" if t.mcp_server else t.name for t in pack.tools}
                    key = f"{server}.{tool_name}"
                    if key not in full_names and tool_name not in allowed:
                        return {
                            "status": "rejected",
                            "error": f"RBAC: squad {squad_id!r} not authorized for {tool_name} on {server}",
                            "allowed_tools": sorted(allowed),
                        }
        return shed.execute(server, tool_name, tool_args, squad_id=squad_id)

    def scope(args: dict[str, Any]) -> dict[str, Any]:
        goal = args.get("goal", "")
        squad_slugs = args.get("squads", [])
        if isinstance(squad_slugs, str):
            squad_slugs = [squad_slugs]
        ts = compute_tool_scope(goal, squad_slugs, packs, toolshed=shed)
        return {
            "relevant_tools": list(ts.relevant_tools),
            "relevant_categories": list(ts.relevant_categories),
            "intent_keywords": list(ts.intent_keywords),
            "tool_count": ts.tool_count,
        }

    def health(args: dict[str, Any]) -> dict[str, Any]:
        return {
            "toolshed": shed.stats(),
            "squads_discovered": len(packs),
            "squad_slugs": sorted(packs.keys()),
            "status": "ok",
        }

    def navigate(args: dict[str, Any]) -> dict[str, Any]:
        path = args.get("path", "")
        result = tree.navigate(path)
        result["token_estimate"] = tree.token_estimate()
        return result

    return {
        "gateway.discover": discover,
        "gateway.search": search,
        "gateway.describe": describe,
        "gateway.call": call,
        "gateway.scope": scope,
        "gateway.health": health,
        "gateway.navigate": navigate,
    }


TOOL_SCHEMAS = {
    "gateway.discover": {
        "description": "List available servers, squads, and tool categories. Optionally filter by server or squad.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "server": {"type": "string", "description": "Filter by MCP server"},
                "squad": {"type": "string", "description": "Show tools for a specific squad"},
            },
        },
    },
    "gateway.search": {
        "description": "Search tools by keyword across all registered servers.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search keywords"},
                "server": {"type": "string", "description": "Filter by server"},
                "category": {"type": "string", "description": "Filter by category"},
                "limit": {"type": "integer", "default": 10},
            },
            "required": ["query"],
        },
    },
    "gateway.describe": {
        "description": "Get full schema and metadata for a specific tool.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "server": {"type": "string"},
                "tool_name": {"type": "string"},
            },
            "required": ["server", "tool_name"],
        },
    },
    "gateway.call": {
        "description": "Execute a tool on its backend server. RBAC enforced when squad_id is provided.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "server": {"type": "string"},
                "tool_name": {"type": "string"},
                "args": {"type": "object"},
                "squad_id": {"type": "string", "description": "Calling squad for RBAC enforcement"},
            },
            "required": ["server", "tool_name"],
        },
    },
    "gateway.scope": {
        "description": "Compute intent-based tool scope for a goal + squad combination.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "goal": {"type": "string", "description": "The goal text to analyze"},
                "squads": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Squad slugs to scope tools for",
                },
            },
            "required": ["goal"],
        },
    },
    "gateway.health": {
        "description": "Check gateway health: toolshed stats, discovered squads, backend status.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    "gateway.navigate": {
        "description": "Progressive disclosure: navigate the tool tree level by level. "
                       "Path format: '' (squads) → 'squad_slug' (servers) → 'server' (categories) "
                       "→ 'server/category' (tools) → 'server/category/tool_name' (full schema).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Slash-delimited path: '' | 'engineering' | 'pp_harness' | 'pp_harness/read' | 'pp_harness/read/get_rubric'",
                },
            },
        },
    },
}


def _serve_with_mcp_sdk() -> bool:
    try:
        from mcp.server import Server  # type: ignore
        from mcp.server.stdio import stdio_server  # type: ignore
        import mcp.types as t  # type: ignore
    except ImportError:
        return False

    handlers = _tool_handlers()
    server = Server("hydra-gateway")

    @server.list_tools()
    async def _list_tools():
        return [
            t.Tool(
                name=name,
                description=schema["description"],
                inputSchema=schema["inputSchema"],
            )
            for name, schema in TOOL_SCHEMAS.items()
        ]

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict):
        if name not in handlers:
            raise ValueError(f"unknown tool: {name}")
        result = handlers[name](arguments)
        return [t.TextContent(type="text", text=json.dumps(result, default=str))]

    import asyncio

    async def run() -> None:
        async with stdio_server() as (r, w):
            await server.run(r, w, server.create_initialization_options())

    asyncio.run(run())
    return True


def _serve_bare() -> None:
    handlers = _tool_handlers()
    sys.stderr.write("hydra-gateway: serving in bare-stdio fallback mode\n")
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError as e:
            sys.stdout.write(json.dumps({"error": "parse_error", "detail": str(e)}) + "\n")
            sys.stdout.flush()
            continue
        try:
            method = msg.get("method") or msg.get("tool")
            args = msg.get("params") or msg.get("arguments") or {}
            if method == "list_tools":
                out = {"id": msg.get("id"), "result": list(TOOL_SCHEMAS.keys())}
            elif method in handlers:
                out = {"id": msg.get("id"), "result": handlers[method](args)}
            else:
                out = {"id": msg.get("id"), "error": f"unknown_method: {method!r}"}
        except Exception as e:
            out = {"id": msg.get("id"), "error": str(e)}
        sys.stdout.write(json.dumps(out, default=str) + "\n")
        sys.stdout.flush()


def main() -> None:
    if not _serve_with_mcp_sdk():
        _serve_bare()


if __name__ == "__main__":
    main()
