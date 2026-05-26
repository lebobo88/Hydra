"""Hydra Toolshed — MCP server exposing search-describe-execute meta-tools.

Wraps large MCP tool catalogs behind 3 meta-tools so agents can discover
tools without loading all schemas upfront. Implements the Speakeasy
Dynamic Toolsets pattern.

Tools:
  - toolshed.search    — keyword search across registered tool catalogs
  - toolshed.describe  — return full schema for a specific tool
  - toolshed.execute   — proxy a call to the underlying MCP server
  - toolshed.categories — list tool categories with counts
  - toolshed.stats     — catalog statistics
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parents[2]))

from hydra_core.toolshed import build_default_shed  # noqa: E402


def _tool_handlers() -> dict[str, callable]:
    shed = build_default_shed()

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
            "query": args.get("query", ""),
        }

    def describe(args: dict[str, Any]) -> dict[str, Any]:
        server = args.get("server", "")
        tool_name = args.get("tool_name", "")
        entry = shed.describe(server, tool_name)
        if entry is None:
            return {"error": "not_found", "server": server, "tool_name": tool_name}
        return entry

    def execute(args: dict[str, Any]) -> dict[str, Any]:
        server = args.get("server", "")
        tool_name = args.get("tool_name", "")
        tool_args = args.get("args", {})
        squad_id = args.get("squad_id")
        return shed.execute(server, tool_name, tool_args, squad_id=squad_id)

    def categories(args: dict[str, Any]) -> dict[str, Any]:
        server = args.get("server")
        cats = shed.list_categories(server=server)
        return {"categories": cats}

    def stats(args: dict[str, Any]) -> dict[str, Any]:
        return shed.stats()

    return {
        "toolshed.search": search,
        "toolshed.describe": describe,
        "toolshed.execute": execute,
        "toolshed.categories": categories,
        "toolshed.stats": stats,
    }


TOOL_SCHEMAS = {
    "toolshed.search": {
        "description": "Search the tool catalog by keyword. Returns matching tools with relevance scores.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search keywords"},
                "server": {"type": "string", "description": "Filter by server (pp_harness, eights, agentsmith)"},
                "category": {"type": "string", "description": "Filter by category (read, write, execute, judge, governance, evolution, memory, config)"},
                "limit": {"type": "integer", "description": "Max results (default 10)", "default": 10},
            },
            "required": ["query"],
        },
    },
    "toolshed.describe": {
        "description": "Get the full schema and metadata for a specific tool.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "server": {"type": "string", "description": "MCP server name"},
                "tool_name": {"type": "string", "description": "Tool name to describe"},
            },
            "required": ["server", "tool_name"],
        },
    },
    "toolshed.execute": {
        "description": "Execute a tool on its underlying MCP server (proxied call).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "server": {"type": "string", "description": "MCP server name"},
                "tool_name": {"type": "string", "description": "Tool to execute"},
                "args": {"type": "object", "description": "Tool arguments"},
                "squad_id": {"type": "string", "description": "Calling squad for RBAC"},
            },
            "required": ["server", "tool_name"],
        },
    },
    "toolshed.categories": {
        "description": "List tool categories with counts, optionally filtered by server.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "server": {"type": "string", "description": "Filter by server"},
            },
        },
    },
    "toolshed.stats": {
        "description": "Return catalog statistics: total tools, server counts, categories.",
        "inputSchema": {"type": "object", "properties": {}},
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
    server = Server("hydra-toolshed")

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
    sys.stderr.write("hydra-toolshed: serving in bare-stdio fallback mode\n")
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
