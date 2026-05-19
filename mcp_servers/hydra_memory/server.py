"""Hydra Memory — MCP server exposing the three memory tiers.

Tools:
  - hydra-mem.write_episodic       — append a payload (returns MemoryRef key)
  - hydra-mem.read_episodic        — resolve a key
  - hydra-mem.list_workflow        — list all episodic rows for a workflow_id
  - hydra-mem.semantic_search      — search a named semantic index by query

Resources:
  - hydra://episodic/<workflow_id> — list of rows as JSON

If the `mcp` python package is not installed, this module degrades to a
plain JSON-RPC-over-stdio loop that supports the same tool names. That
makes Hydra usable without external dependencies during early bootstrap.
"""
from __future__ import annotations

import json
import os
import sys
import traceback
from pathlib import Path
from typing import Any

# Add project root to sys.path so `hydra_core` resolves when launched as
# a child process from `.mcp.json`.
_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parents[2]))

from hydra_core.memory import (  # noqa: E402
    EPISODIC_DB, append_episodic, get_index, list_episodic, query_by_cell,
    resolve_episodic, tag_episodic,
)
from hydra_core.eights import ALL_CELLS  # noqa: E402


def _tool_handlers() -> dict[str, callable]:
    def write_episodic(args: dict[str, Any]) -> dict[str, Any]:
        ref = append_episodic(
            workflow_id=args["workflow_id"],
            kind=args.get("kind", "note"),
            payload=args.get("payload", {}),
            key=args.get("key"),
            cells=args.get("cells"),
            origin_squad=args.get("origin_squad"),
        )
        return {"ref": ref.model_dump(mode="json")}

    def read_episodic(args: dict[str, Any]) -> dict[str, Any]:
        row = resolve_episodic(args["key"])
        if row is None:
            return {"error": "not_found", "key": args["key"]}
        return {"row": row}

    def list_workflow(args: dict[str, Any]) -> dict[str, Any]:
        rows = list_episodic(args["workflow_id"])
        return {"rows": rows, "count": len(rows)}

    def semantic_search(args: dict[str, Any]) -> dict[str, Any]:
        index = get_index(args["index"])
        emb = args.get("embedding") or [0.0]
        refs = index.search(emb, k=int(args.get("k", 5)))
        return {"refs": [r.model_dump(mode="json") for r in refs]}

    def query_eights(args: dict[str, Any]) -> dict[str, Any]:
        cell = (args.get("cell") or "").strip().lower()
        if cell not in ALL_CELLS:
            return {"error": "invalid_cell", "cell": cell, "valid": list(ALL_CELLS)}
        rows = query_by_cell(
            cell,
            limit=int(args.get("limit", 50)),
            workflow_id=args.get("workflow_id"),
        )
        return {"cell": cell, "rows": rows, "count": len(rows)}

    def tag_memory(args: dict[str, Any]) -> dict[str, Any]:
        merged = tag_episodic(
            key=args["key"],
            cells=args.get("cells", []),
            replace=bool(args.get("replace", False)),
        )
        return {"key": args["key"], "cells": merged}

    return {
        "hydra-mem.write_episodic": write_episodic,
        "hydra-mem.read_episodic": read_episodic,
        "hydra-mem.list_workflow": list_workflow,
        "hydra-mem.semantic_search": semantic_search,
        "hydra-mem.query_eights": query_eights,
        "hydra-mem.tag_memory": tag_memory,
    }


# ---------- Try the real MCP SDK first ----------

def _serve_with_mcp_sdk() -> bool:
    try:
        from mcp.server import Server  # type: ignore
        from mcp.server.stdio import stdio_server  # type: ignore
        import mcp.types as t  # type: ignore
    except ImportError:
        return False

    handlers = _tool_handlers()
    server = Server("hydra-memory")

    @server.list_tools()
    async def _list_tools():
        return [
            t.Tool(name=name, description=name, inputSchema={"type": "object"})
            for name in handlers
        ]

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict):
        if name not in handlers:
            raise ValueError(f"unknown tool: {name}")
        result = handlers[name](arguments)
        return [t.TextContent(type="text", text=json.dumps(result))]

    import asyncio

    async def run() -> None:
        async with stdio_server() as (r, w):
            await server.run(r, w, server.create_initialization_options())

    asyncio.run(run())
    return True


# ---------- Fallback: bare JSON-RPC over stdio ----------

def _serve_bare() -> None:
    handlers = _tool_handlers()
    sys.stderr.write("hydra-memory: serving in bare-stdio fallback mode (no mcp SDK)\n")
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
                out = {"id": msg.get("id"), "result": list(handlers)}
            elif method in handlers:
                out = {"id": msg.get("id"), "result": handlers[method](args)}
            else:
                out = {"id": msg.get("id"), "error": f"unknown_method: {method!r}"}
        except Exception as e:
            out = {"id": msg.get("id"), "error": str(e),
                   "traceback": traceback.format_exc()}
        sys.stdout.write(json.dumps(out) + "\n")
        sys.stdout.flush()


def main() -> None:
    if not _serve_with_mcp_sdk():
        _serve_bare()
