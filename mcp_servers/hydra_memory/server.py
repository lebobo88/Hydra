"""Hydra Memory — MCP server exposing the three memory tiers.

Tools:
  - hydra-mem.write_episodic       — append a payload (returns MemoryRef key)
  - hydra-mem.read_episodic        — resolve a key
  - hydra-mem.list_workflow        — list all episodic rows for a workflow_id
  - hydra-mem.semantic_search      — search a named semantic index by query
  - hydra-mem.ping                 — no-arg liveness probe (AgentMesh healthProbe)
  - hydra-mem.workflows_list       — read-only: workflows from checkpoints.db
  - hydra-mem.workflow_status      — read-only: one workflow's live state
  - hydra-mem.squad_list           — read-only: discovered squad packs
  - hydra-mem.hitl_pending         — read-only: pending HITL gates across workflows

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


# ---------- workflow status surface (campaign mesh-console-unification C2) ----
#
# Read-only views over the LangGraph checkpoint store so AgentMesh (and the
# unified console behind it) can show workflows, squads, and pending HITL
# gates WITHOUT touching the supervisor. The checkpoint DB is opened with
# SQLite `mode=ro` — this server never writes workflow state. When langgraph
# is not importable (bare-bootstrap mode) the tools degrade with an explicit
# `degraded` flag rather than fabricating emptiness (Art V).

_SCAN_CAP = 200  # max distinct workflows scanned per call

# Terminal phases — a workflow in any of these has closed out. Everything else
# is non-terminal ("live") and must stay at the top of the list so a bulk
# timestamp bump (e.g. `hydra reap` re-stamping many closed rows) can never page
# genuinely-active runs out of the limit window.
_TERMINAL_PHASES = frozenset({"done", "surfaced"})


def _is_live(phase: Any, has_pending_hitl: bool) -> bool:
    """A workflow is live if it has not reached a terminal phase, or it is
    actively awaiting a human at a gate."""
    return (phase not in _TERMINAL_PHASES) or bool(has_pending_hitl)


def _checkpoints_db_path() -> Path:
    p = os.environ.get("HYDRA_CHECKPOINT_DB")
    return Path(p) if p else (Path.home() / ".hydra" / "checkpoints.db")


def _open_checkpoints_ro():
    import sqlite3
    db = _checkpoints_db_path()
    if not db.exists():
        return None
    return sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True,
                           check_same_thread=False)


def _checkpoint_thread_ids(conn, cap: int = _SCAN_CAP) -> list[str]:
    try:
        rows = conn.execute(
            "SELECT DISTINCT thread_id FROM checkpoints LIMIT ?", (cap,)
        ).fetchall()
        return [r[0] for r in rows]
    except Exception:  # noqa: BLE001 — schema absent / older langgraph layout
        return []


def _load_state_values(workflow_id: str) -> dict[str, Any] | None:
    """Latest checkpoint channel_values for a workflow (read-only), or None."""
    conn = _open_checkpoints_ro()
    if conn is None:
        return None
    try:
        from langgraph.checkpoint.sqlite import SqliteSaver
        saver = SqliteSaver(conn)
        tup = saver.get_tuple({"configurable": {"thread_id": str(workflow_id)}})
        if tup is None:
            return None
        cp = tup.checkpoint or {}
        return {"values": cp.get("channel_values") or {}, "ts": cp.get("ts")}
    finally:
        conn.close()


def _summarize_workflow(workflow_id: str, values: dict[str, Any],
                        ts: Any) -> dict[str, Any]:
    budget = values.get("budget")
    tasks = values.get("tasks") or []
    return {
        "workflow_id": str(workflow_id),
        "phase": values.get("phase"),
        "root_goal": (values.get("root_goal") or "")[:300],
        "selected_squads": list(values.get("selected_squads") or []),
        "budget": budget if isinstance(budget, dict) else (
            budget.model_dump(mode="json") if hasattr(budget, "model_dump") else None),
        "pending_hitl": values.get("pending_hitl"),
        "tasks": [
            {
                "owner_squad": getattr(t, "owner_squad", None) or (t.get("owner_squad") if isinstance(t, dict) else None),
                "status": getattr(t, "status", None) or (t.get("status") if isinstance(t, dict) else None),
                "description": ((getattr(t, "description", None) or (t.get("description") if isinstance(t, dict) else "")) or "")[:200],
            }
            for t in tasks
        ],
        "envelope_count": len(values.get("envelopes") or []),
        "verdict_count": len(values.get("verdicts") or []),
        "updated_at": ts,
    }


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

    def ping(args: dict[str, Any]) -> dict[str, Any]:
        # No-arg liveness probe for AgentMesh's mcp-tool-call healthProbe
        # (campaign mesh-console-unification, C1). list_workflow requires a
        # workflow_id so a generic prober cannot call it; ping is free of
        # required args and touches the episodic DB path read-only to prove
        # the server is actually wired to its storage, not just alive.
        return {
            "ok": True,
            "server": "hydra_memory",
            "episodic_db": str(EPISODIC_DB),
            "episodic_db_exists": Path(EPISODIC_DB).exists(),
            "ts": time.time(),
        }

    def workflows_list(args: dict[str, Any]) -> dict[str, Any]:
        limit = max(1, min(int(args.get("limit", 50)), _SCAN_CAP))
        conn = _open_checkpoints_ro()
        if conn is None:
            return {"workflows": [], "count": 0, "degraded": True,
                    "reason": "checkpoints_db_missing"}
        try:
            thread_ids = _checkpoint_thread_ids(conn)
        finally:
            conn.close()
        out: list[dict[str, Any]] = []
        degraded_reason = None
        for wf in thread_ids:
            try:
                st = _load_state_values(wf)
            except ImportError:
                degraded_reason = "langgraph_unavailable"
                break
            except Exception as e:  # noqa: BLE001 — one bad row must not hide the rest
                out.append({"workflow_id": wf, "phase": None,
                            "error": str(e)[:120]})
                continue
            if st is None:
                continue
            v = st["values"]
            has_gate = bool(v.get("pending_hitl"))
            phase = v.get("phase")
            out.append({
                "workflow_id": wf,
                "phase": phase,
                "root_goal": (v.get("root_goal") or "")[:160],
                "selected_squads": list(v.get("selected_squads") or []),
                "has_pending_hitl": has_gate,
                "live": _is_live(phase, has_gate),
                "updated_at": st["ts"],
            })
        # Live (non-terminal / gated) rows first, then most-recent within each
        # group. Keeps active runs in the limit window regardless of when closed
        # rows were last re-stamped. Error rows (no 'live' key) sort last.
        out.sort(key=lambda r: (bool(r.get("live")), r.get("updated_at") or ""),
                 reverse=True)
        result: dict[str, Any] = {"workflows": out[:limit], "count": len(out)}
        if degraded_reason:
            result["degraded"] = True
            result["reason"] = degraded_reason
        return result

    def workflow_status(args: dict[str, Any]) -> dict[str, Any]:
        wf = str(args["workflow_id"])
        try:
            st = _load_state_values(wf)
        except ImportError:
            return {"workflow_id": wf, "degraded": True,
                    "reason": "langgraph_unavailable"}
        if st is None:
            return {"workflow_id": wf, "error": "not_found"}
        return _summarize_workflow(wf, st["values"], st["ts"])

    def squad_list(args: dict[str, Any]) -> dict[str, Any]:
        from hydra_core.squad_loader import discover_squads
        root = Path(os.environ.get("HYDRA_ROOT") or Path.cwd())
        packs = discover_squads(root)
        squads = []
        for slug, p in packs.items():
            squads.append({
                "slug": slug,
                "name": p.name,
                "version": str(p.version),
                "description": (p.description or "")[:300],
                "entrypoint": p.entrypoint,
                "industries": list(p.industries),
                "accepts": list(p.accepts),
                "emits": list(p.emits),
                "best_of_n": p.best_of_n,
                "deprecated_after": str(p.deprecated_after) if p.deprecated_after else None,
                "agents": [
                    {
                        "slug": a.slug,
                        "role": a.role,
                        "authority": getattr(a, "authority", None),
                        "model_tier": getattr(a, "model_tier", None),
                        "hitl_trigger": getattr(a, "hitl_trigger", None),
                    }
                    for a in p.agents
                ],
                "gates": [
                    {
                        "rubric_id": g.rubric_id,
                        "hitl_required": g.hitl_required,
                        "when": getattr(g, "when", None),
                    }
                    for g in p.gates
                ],
            })
        return {"squads": squads, "count": len(squads)}

    def hitl_pending(args: dict[str, Any]) -> dict[str, Any]:
        conn = _open_checkpoints_ro()
        if conn is None:
            return {"gates": [], "count": 0, "degraded": True,
                    "reason": "checkpoints_db_missing"}
        try:
            thread_ids = _checkpoint_thread_ids(conn)
        finally:
            conn.close()
        gates: list[dict[str, Any]] = []
        for wf in thread_ids:
            try:
                st = _load_state_values(wf)
            except ImportError:
                return {"gates": [], "count": 0, "degraded": True,
                        "reason": "langgraph_unavailable"}
            except Exception:  # noqa: BLE001
                continue
            if st is None:
                continue
            v = st["values"]
            hitl = v.get("pending_hitl")
            if not hitl:
                continue
            gates.append({
                "workflow_id": wf,
                "phase": v.get("phase"),
                "gate_node": (hitl.get("gate_node") if isinstance(hitl, dict) else None) or "unspecified",
                "hitl": hitl,
                "updated_at": st["ts"],
            })
        gates.sort(key=lambda r: r.get("updated_at") or "", reverse=True)
        return {"gates": gates, "count": len(gates)}

    return {
        "hydra-mem.write_episodic": write_episodic,
        "hydra-mem.read_episodic": read_episodic,
        "hydra-mem.list_workflow": list_workflow,
        "hydra-mem.semantic_search": semantic_search,
        "hydra-mem.query_eights": query_eights,
        "hydra-mem.tag_memory": tag_memory,
        "hydra-mem.ping": ping,
        "hydra-mem.workflows_list": workflows_list,
        "hydra-mem.workflow_status": workflow_status,
        "hydra-mem.squad_list": squad_list,
        "hydra-mem.hitl_pending": hitl_pending,
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
    "hydra-mem.ping": {
        "description": ("No-arg liveness probe: returns ok + episodic DB path. "
                        "Used by AgentMesh's mcp-tool-call healthProbe."),
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    "hydra-mem.workflows_list": {
        "description": ("Read-only list of workflows from the LangGraph "
                        "checkpoint store (phase, squads, pending-HITL flag), "
                        "newest first."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "minimum": 1, "maximum": 200,
                          "default": 50},
            },
        },
    },
    "hydra-mem.workflow_status": {
        "description": ("Read-only live state of one workflow: phase, budget, "
                        "tasks, pending_hitl, envelope/verdict counts."),
        "inputSchema": {
            "type": "object",
            "properties": {"workflow_id": {"type": "string"}},
            "required": ["workflow_id"],
        },
    },
    "hydra-mem.squad_list": {
        "description": ("Read-only discovered squad packs: roster, gates, "
                        "accepts/emits envelope types, best_of_n."),
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    "hydra-mem.hitl_pending": {
        "description": ("Read-only pending HITL gates across all workflows "
                        "(workflow_id + gate_node + the HITL payload). The "
                        "live-truth complement to TheEights' hitl_queue."),
        "inputSchema": {
            "type": "object",
            "properties": {},
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
