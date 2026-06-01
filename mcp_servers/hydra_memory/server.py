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
import logging
import os
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any

logger = logging.getLogger("hydra_memory")

# Add project root to sys.path so `hydra_core` resolves when launched as
# a child process from `.mcp.json`.
_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parents[2]))

from hydra_core.memory import (  # noqa: E402
    EPISODIC_DB, append_episodic, get_index, list_episodic, query_by_cell,
    resolve_episodic, search_episodic, tag_episodic,
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
        # Back-compat first: an explicit pre-computed embedding signals the
        # legacy vector path, so honor it even when a query is also present.
        emb = args.get("embedding")
        if emb:
            index = get_index(args.get("index") or "default")
            refs = index.search(emb, k=int(args.get("k", 5)))
            return {"refs": [r.model_dump(mode="json") for r in refs]}
        # Primary path: honest full-text search over episodic memory by query.
        query = (args.get("query") or "").strip()
        if query:
            refs = search_episodic(
                query,
                k=int(args.get("k", 5)),
                workflow_id=args.get("workflow_id"),
                cell=args.get("cell"),
            )
            out: dict[str, Any] = {
                "query": query, "count": len(refs), "source": "local",
                "refs": [r.model_dump(mode="json") for r in refs],
            }
            # Opt-in: enrich with TheEights hybrid search when enabled/reachable.
            # Local refs/count/source/query are always present; federation only
            # adds to the result (eights hits) or flags degradation — it never
            # removes or reshapes the local payload.
            fed = _maybe_federate_search(query, args)
            hits = fed.get("hits")
            if hits is not None:
                out["eights"] = hits
                out["source"] = "local+eights"
            elif fed.get("degraded"):
                out["degraded"] = True
                out["degraded_reason"] = fed.get("reason")
            return out
        return {"refs": [], "count": 0}

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


# Real advertised schemas. An empty `{"type":"object"}` strips param types and
# makes callers (incl. the gateway) mangle nested objects / numeric args; these
# give every memory tool a typed surface.
_CELL_ENUM = sorted(ALL_CELLS)
_TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "hydra-mem.write_episodic": {
        "description": "Append a payload to episodic memory; returns a MemoryRef key.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "workflow_id": {"type": "string"},
                "kind": {"type": "string", "default": "note"},
                "payload": {"type": "object"},
                "key": {"type": "string"},
                "cells": {"type": "array", "items": {"type": "string",
                                                     "enum": _CELL_ENUM}},
                "origin_squad": {"type": "string"},
            },
            "required": ["workflow_id"],
        },
    },
    "hydra-mem.read_episodic": {
        "description": "Resolve an episodic key to its full row.",
        "inputSchema": {
            "type": "object",
            "properties": {"key": {"type": "string"}},
            "required": ["key"],
        },
    },
    "hydra-mem.list_workflow": {
        "description": "List every episodic row for a workflow_id.",
        "inputSchema": {
            "type": "object",
            "properties": {"workflow_id": {"type": "string"}},
            "required": ["workflow_id"],
        },
    },
    "hydra-mem.semantic_search": {
        "description": ("Full-text search over episodic memory by query "
                        "(LIKE across payload/kind/key). Optional workflow_id "
                        "and cell narrow the scan."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "k": {"type": "integer", "minimum": 1, "maximum": 50,
                      "default": 5},
                "workflow_id": {"type": "string"},
                "cell": {"type": "string", "enum": _CELL_ENUM},
                "index": {"type": "string"},
                "embedding": {"type": "array", "items": {"type": "number"}},
            },
        },
    },
    "hydra-mem.query_eights": {
        "description": "Query episodic rows tagged with a TheEights cell.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "cell": {"type": "string", "enum": _CELL_ENUM},
                "limit": {"type": "integer", "minimum": 1, "default": 50},
                "workflow_id": {"type": "string"},
            },
            "required": ["cell"],
        },
    },
    "hydra-mem.tag_memory": {
        "description": "Attach TheEights cells to an existing episodic row.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "key": {"type": "string"},
                "cells": {"type": "array", "items": {"type": "string",
                                                     "enum": _CELL_ENUM}},
                "replace": {"type": "boolean", "default": False},
            },
            "required": ["key"],
        },
    },
}


# ---------- optional TheEights federation (opt-in) ----------
#
# Default behavior is pure-local SQLite search. When HYDRA_MEM_FEDERATE_EIGHTS
# is truthy, semantic_search ALSO queries TheEights eights.memory.search
# (hybrid vector+graph+episodic) and attaches its hits alongside the local
# refs. Federation degrades to local-only on any failure and never blocks the
# local result.
#
# The EightsAttestor wraps a synchronous MCPStdioDispatcher whose `_run` drives
# its own event loop via run_until_complete. That CANNOT run on the MCP SDK's
# already-running async handler thread (nested loops raise) and the dispatcher
# loop is thread-affine. So federation runs on a single dedicated worker thread
# (its own loop, reused consistently) with a bounded result timeout — a hung
# daemon times out into local-only instead of blocking the search.
_EIGHTS_ATTESTOR: Any = None
_EIGHTS_FED_DISABLED = False
_FED_EXECUTOR: Any = None
_FED_LOCK = threading.Lock()

# Circuit breaker. fut.cancel() does NOT stop an already-running federation
# call, so a run of timeouts would otherwise pile work onto the single-worker
# executor (each new search waits the full timeout behind the stuck one). After
# _FED_BREAKER_THRESHOLD consecutive failures we stop attempting federation for
# _FED_BREAKER_COOLDOWN_S, degrading cleanly to local-only until Eights recovers.
_FED_BREAKER_THRESHOLD = 3
_FED_BREAKER_COOLDOWN_S = 60.0
_FED_CONSECUTIVE_FAILURES = 0
_FED_COOLDOWN_UNTIL = 0.0


def _federation_enabled() -> bool:
    return os.environ.get("HYDRA_MEM_FEDERATE_EIGHTS", "").strip().lower() in (
        "1", "true", "yes", "on")


def _fed_timeout() -> float:
    try:
        return float(os.environ.get("HYDRA_MEM_FEDERATE_TIMEOUT", "8"))
    except (TypeError, ValueError):
        return 8.0


def _fed_executor() -> Any:
    """Single-worker executor so every federation call shares one thread (and
    thus one dispatcher event loop). Built lazily under a lock so concurrent
    first calls cannot race two 'single-worker' pools into existence (which
    would break the thread-affinity the shared dispatcher loop relies on)."""
    global _FED_EXECUTOR
    if _FED_EXECUTOR is None:
        with _FED_LOCK:
            if _FED_EXECUTOR is None:
                import concurrent.futures
                _FED_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
                    max_workers=1, thread_name_prefix="mem-fed")
    return _FED_EXECUTOR


def _get_attestor() -> Any:
    """Lazily build a cached EightsAttestor with a real MCP dispatcher. Returns
    None (and latches off) if construction fails, so the handler stays local."""
    global _EIGHTS_ATTESTOR, _EIGHTS_FED_DISABLED
    if _EIGHTS_FED_DISABLED:
        return None
    if _EIGHTS_ATTESTOR is not None:
        return _EIGHTS_ATTESTOR
    try:
        from hydra_core.dispatcher import MCPStdioDispatcher
        from hydra_core.eights.attestation import EightsAttestor
        _EIGHTS_ATTESTOR = EightsAttestor(dispatcher=MCPStdioDispatcher(Path.cwd()))
        return _EIGHTS_ATTESTOR
    except Exception:  # noqa: BLE001 — federation is best-effort
        _EIGHTS_FED_DISABLED = True
        return None


def _maybe_federate_search(query: str, args: dict[str, Any]) -> dict[str, Any]:
    """Opt-in federation to TheEights eights.memory.search.

    Returns a status dict ``{"hits": <payload|None>, "degraded": bool,
    "reason": str|None}``:
      - federation disabled         → hits=None, degraded=False (not attempted)
      - breaker open (cooldown)     → hits=None, degraded=True,  reason="cooldown"
      - attestor unavailable        → hits=None, degraded=True,  reason="unavailable"
      - timeout / error             → hits=None, degraded=True,  reason="timeout"|"error"
      - success                     → hits=<payload>, degraded=False

    Degradation is logged and surfaced to the caller (never silently swallowed),
    and a circuit breaker stops hammering a down daemon."""
    import concurrent.futures as _cf

    global _FED_CONSECUTIVE_FAILURES, _FED_COOLDOWN_UNTIL

    if not _federation_enabled():
        return {"hits": None, "degraded": False, "reason": None}

    now = time.monotonic()
    if now < _FED_COOLDOWN_UNTIL:
        return {"hits": None, "degraded": True, "reason": "cooldown"}

    att = _get_attestor()
    if att is None:
        return {"hits": None, "degraded": True, "reason": "unavailable"}

    try:
        top_k = int(args.get("k", 5))
    except (TypeError, ValueError):
        top_k = 5
    wf = args.get("workflow_id")
    wf = str(wf) if wf else None

    def _trip(reason: str) -> dict[str, Any]:
        """Record a failure, arm the breaker if we've crossed the threshold."""
        global _FED_CONSECUTIVE_FAILURES, _FED_COOLDOWN_UNTIL
        _FED_CONSECUTIVE_FAILURES += 1
        if _FED_CONSECUTIVE_FAILURES >= _FED_BREAKER_THRESHOLD:
            _FED_COOLDOWN_UNTIL = time.monotonic() + _FED_BREAKER_COOLDOWN_S
            logger.warning(
                "eights federation breaker OPEN after %d consecutive %s; "
                "degrading to local-only for %.0fs",
                _FED_CONSECUTIVE_FAILURES, reason, _FED_BREAKER_COOLDOWN_S,
            )
        else:
            logger.warning(
                "eights federation %s (%d/%d) — degrading this search to local-only",
                reason, _FED_CONSECUTIVE_FAILURES, _FED_BREAKER_THRESHOLD,
            )
        return {"hits": None, "degraded": True, "reason": reason}

    # workflow_id is passed per-call (not stamped on the shared attestor) so
    # concurrent searches can't cross-contaminate the audit envelope.
    fut = _fed_executor().submit(att.memory_search, query, top_k=top_k,
                                 workflow_id=wf)
    try:
        hits = fut.result(timeout=_fed_timeout())
        _FED_CONSECUTIVE_FAILURES = 0  # success resets the breaker
        return {"hits": hits, "degraded": False, "reason": None}
    except _cf.TimeoutError:
        fut.cancel()  # drop if still queued; a running call is left to finish
        return _trip("timeout")
    except Exception:  # noqa: BLE001 — any backend error → local-only
        fut.cancel()
        return _trip("error")


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
            t.Tool(
                name=name,
                description=_TOOL_SCHEMAS.get(name, {}).get("description", name),
                inputSchema=_TOOL_SCHEMAS.get(name, {}).get(
                    "inputSchema", {"type": "object"}),
            )
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
