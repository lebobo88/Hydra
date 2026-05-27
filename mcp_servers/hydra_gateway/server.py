"""Hydra Gateway — unified MCP server proxying all backend servers.

Consolidates 8 individual MCP server registrations into 1. Claude Code
registers only `hydra_gateway`; it discovers backends from
``~/.hydra/backends.json`` and proxies tool calls to them.

Backend tools are exposed under their original server-qualified names:
``{server}__{tool_name}`` → Claude sees ``mcp__hydra_gateway__{server}__{tool_name}``.

Also exposes 7 gateway meta-tools for search/describe/navigate/health.

Architecture:
- AsyncBackendPool manages long-lived async MCP client sessions
- Reads ~/.hydra/backends.json (NOT ~/.claude.json — avoids circular dep)
- Self-excludes hydra_gateway/hydra_toolshed entries to prevent recursion
- Per-backend failure isolation: one backend down doesn't affect others
"""
from __future__ import annotations

import asyncio
import json
import logging
import signal
import sys
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parents[2]))

from hydra_core.dispatcher import BACKEND_REGISTRY, _load_backend_registry, _strip_comments  # noqa: E402
from hydra_core.toolshed import build_default_shed, ProgressiveDisclosureTree  # noqa: E402
from hydra_core.squad_loader import discover_squads  # noqa: E402
from hydra_core.router import compute_tool_scope  # noqa: E402

_SELF_NAMES = frozenset({"hydra_gateway", "hydra_toolshed"})
_CONNECT_TIMEOUT = 10.0


class AsyncBackendPool:
    """Manages async MCP client sessions to backend servers.

    Each backend gets its own AsyncExitStack so a crash in one backend's
    transport never cascades to others. CancelledError (not a subclass of
    Exception in Python 3.11+) is caught explicitly to prevent it from
    escaping into the MCP SDK's task group and crashing the gateway.
    """

    def __init__(self, specs: dict[str, dict[str, Any]]) -> None:
        self._specs = {k: v for k, v in specs.items() if k not in _SELF_NAMES}
        self._stacks: dict[str, AsyncExitStack] = {}
        self._sessions: dict[str, Any] = {}
        self._tool_cache: dict[str, list[dict[str, Any]]] = {}
        self._failed: set[str] = set()

    @property
    def server_names(self) -> list[str]:
        return sorted(self._specs.keys())

    async def _teardown_backend(self, server: str) -> None:
        """Tear down a single backend's stack, session, and cache."""
        self._sessions.pop(server, None)
        self._tool_cache.pop(server, None)
        stack = self._stacks.pop(server, None)
        if stack:
            try:
                await stack.aclose()
            except (asyncio.CancelledError, Exception):
                pass

    async def _connect(self, server: str) -> Any:
        """Open a stdio session to a backend. Returns the ClientSession."""
        if server in self._sessions:
            return self._sessions[server]
        if server not in self._specs:
            return None

        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
        except ImportError:
            return None

        # Tear down any previous broken stack for this server
        await self._teardown_backend(server)

        spec = self._specs[server]
        params = StdioServerParameters(
            command=spec["command"],
            args=list(spec.get("args", [])),
            env=spec.get("env"),
            cwd=spec.get("cwd"),
        )
        stack = AsyncExitStack()
        try:
            transport = await asyncio.wait_for(
                stack.enter_async_context(stdio_client(params)),
                timeout=_CONNECT_TIMEOUT,
            )
            read, write = transport
            session = await stack.enter_async_context(
                ClientSession(read, write)
            )
            await asyncio.wait_for(session.initialize(), timeout=_CONNECT_TIMEOUT)
            self._stacks[server] = stack
            self._sessions[server] = session
            self._failed.discard(server)
            logger.info("Connected to backend: %s", server)
            return session
        except (asyncio.CancelledError, Exception) as exc:
            logger.warning("Failed to connect to %s: %s", server, exc)
            self._failed.add(server)
            try:
                await stack.aclose()
            except (asyncio.CancelledError, Exception):
                pass
            return None

    async def list_tools(self, server: str) -> list[dict[str, Any]]:
        """Get tool list from a backend (cached after first successful call)."""
        if server in self._tool_cache:
            return self._tool_cache[server]

        session = await self._connect(server)
        if session is None:
            return []

        try:
            result = await asyncio.wait_for(session.list_tools(), timeout=_CONNECT_TIMEOUT)
            tools = [
                {
                    "name": t.name,
                    "description": getattr(t, "description", t.name) or t.name,
                    "inputSchema": getattr(t, "inputSchema", {"type": "object"}),
                }
                for t in result.tools
            ]
            self._tool_cache[server] = tools
            return tools
        except (asyncio.CancelledError, Exception) as exc:
            logger.warning("list_tools failed for %s: %s", server, exc)
            self._failed.add(server)
            return []

    _TOOL_TIMEOUT = 120.0

    async def call_tool(self, server: str, tool: str,
                        args: dict[str, Any]) -> dict[str, Any]:
        """Forward a tool call to a backend. Retries once on stale session."""
        for attempt in range(2):
            session = await self._connect(server)
            if session is None:
                return {
                    "status": "failed",
                    "error": f"backend {server!r} not connected",
                }
            try:
                result = await asyncio.wait_for(
                    session.call_tool(tool, args),
                    timeout=self._TOOL_TIMEOUT,
                )
                return _extract_result(result)
            except asyncio.TimeoutError:
                await self._teardown_backend(server)
                self._failed.add(server)
                return {
                    "status": "failed",
                    "error": f"tool {tool} on {server} timed out after {self._TOOL_TIMEOUT}s",
                    "server": server,
                    "tool": tool,
                }
            except (asyncio.CancelledError, Exception) as exc:
                await self._teardown_backend(server)
                self._failed.add(server)
                if attempt == 0:
                    logger.info("Retrying %s.%s after failure: %s", server, tool, exc)
                    continue
                return {
                    "status": "failed",
                    "error": f"{type(exc).__name__}: {exc}",
                    "server": server,
                    "tool": tool,
                }
        return {"status": "failed", "error": f"exhausted retries for {server}.{tool}"}

    async def discover_all_tools(self) -> list[dict[str, Any]]:
        """Discover tools from all backends. Returns namespaced tool list."""
        all_tools: list[dict[str, Any]] = []
        for server in self.server_names:
            backend_tools = await self.list_tools(server)
            for t in backend_tools:
                all_tools.append({
                    "name": f"{server}__{t['name']}",
                    "description": f"[{server}] {t.get('description', t['name'])}",
                    "inputSchema": t.get("inputSchema", {"type": "object"}),
                    "_backend_server": server,
                    "_backend_tool": t["name"],
                })
        return all_tools

    async def health(self) -> dict[str, Any]:
        return {
            "registered": self.server_names,
            "connected": sorted(self._sessions.keys()),
            "failed": sorted(self._failed),
            "cached_tool_counts": {s: len(t) for s, t in self._tool_cache.items()},
        }

    async def close(self) -> None:
        """Shut down all backend connections independently."""
        for server in list(self._stacks):
            try:
                await self._teardown_backend(server)
            except (asyncio.CancelledError, Exception) as exc:
                logger.warning("Error closing %s: %s", server, exc)
        self._stacks.clear()
        self._sessions.clear()
        self._tool_cache.clear()


def _extract_result(result: Any) -> dict[str, Any]:
    content = getattr(result, "content", None)
    if not content:
        return {"status": "done", "result": str(result)}
    out: list[Any] = []
    for c in content:
        text = getattr(c, "text", None)
        if text is None:
            out.append(str(c))
            continue
        try:
            out.append(json.loads(text))
        except (json.JSONDecodeError, TypeError):
            out.append(text)
    payload = out[0] if len(out) == 1 else out
    return {"status": "done", "result": payload}


# ---------- Meta-tool schemas ----------

META_TOOL_SCHEMAS = {
    "gateway.discover": {
        "description": "List available backend servers, squads, and tool categories.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "server": {"type": "string"},
                "squad": {"type": "string"},
            },
        },
    },
    "gateway.search": {
        "description": "Search tools by keyword across all backend servers.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "server": {"type": "string"},
                "limit": {"type": "integer", "default": 10},
            },
            "required": ["query"],
        },
    },
    "gateway.describe": {
        "description": "Get full schema for a specific backend tool.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "server": {"type": "string"},
                "tool_name": {"type": "string"},
            },
            "required": ["server", "tool_name"],
        },
    },
    "gateway.navigate": {
        "description": "Progressive disclosure: navigate the tool tree. Path: '' (squads) → 'squad' (servers) → 'server' (categories) → 'server/category' (tools) → 'server/category/tool' (schema).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
            },
        },
    },
    "gateway.scope": {
        "description": "Compute intent-based tool scope for a goal.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "goal": {"type": "string"},
                "squads": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["goal"],
        },
    },
    "gateway.health": {
        "description": "Check gateway health: backend connections, tool counts, failures.",
        "inputSchema": {"type": "object", "properties": {}},
    },
}


# ---------- MCP Server ----------

def _build_static_tool_list(
    shed: Any,
    specs: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build the tool list from the static toolshed catalog.

    No live connections needed — the toolshed catalogs ~200 tools across
    8 backends. Tools are namespaced as ``{server}__{tool_name}`` so
    Claude sees ``mcp__hydra_gateway__{server}__{tool_name}``.

    Only tools whose server has a matching entry in ``backends.json``
    (the ``specs`` dict) are advertised. If no backends are configured,
    nothing is advertised (prevents false-advertising dead tools).

    Live backends are connected on-demand only when ``call_tool`` is
    invoked. This avoids startup latency and async context conflicts.
    """
    filtered = {k for k in specs if k not in _SELF_NAMES}
    if not filtered:
        return []
    tools: list[dict[str, Any]] = []
    for server_name in sorted(shed._catalog.keys()):
        if server_name not in filtered:
            continue
        for entry in shed._catalog.get(server_name, []):
            tools.append({
                "name": f"{server_name}__{entry.name}",
                "description": f"[{server_name}] {entry.description}",
                "inputSchema": entry.input_schema or {"type": "object"},
                "_backend_server": server_name,
                "_backend_tool": entry.name,
            })
    return tools


def main() -> None:
    try:
        from mcp.server import Server
        from mcp.server.stdio import stdio_server
        import mcp.types as t
    except ImportError:
        sys.stderr.write("hydra-gateway: mcp SDK required. pip install mcp\n")
        sys.exit(1)

    specs = _load_backend_registry()

    project_root = _HERE.parents[2]
    shed = build_default_shed()
    packs = discover_squads(project_root)
    tree = ProgressiveDisclosureTree(shed, packs)

    # Build tool list from static catalog — no live connections at startup.
    # Backends are connected on-demand when call_tool is invoked.
    cached_tools = _build_static_tool_list(shed, specs)

    # Explicit routing lookup: tool display name → (backend_server, backend_tool).
    # Avoids fragile string splitting on "__".
    _tool_routing: dict[str, tuple[str, str]] = {
        pt["name"]: (pt["_backend_server"], pt["_backend_tool"])
        for pt in cached_tools
    }

    # The pool handles on-demand connections for call_tool proxying.
    pool = AsyncBackendPool(specs)

    server = Server("hydra-gateway")

    @server.list_tools()
    async def _list_tools():
        tools = [
            t.Tool(
                name=pt["name"],
                description=pt["description"],
                inputSchema=pt.get("inputSchema", {"type": "object"}),
            )
            for pt in cached_tools
        ]
        for name, schema in META_TOOL_SCHEMAS.items():
            tools.append(t.Tool(
                name=name,
                description=schema["description"],
                inputSchema=schema["inputSchema"],
            ))
        return tools

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict):
        if name in META_TOOL_SCHEMAS:
            try:
                result = await _handle_meta_tool(
                    name, arguments, pool, shed, packs, tree)
            except (asyncio.CancelledError, Exception) as exc:
                logger.error("meta-tool %s failed: %s", name, exc, exc_info=True)
                result = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
            return [t.TextContent(type="text", text=json.dumps(result, default=str))]

        route = _tool_routing.get(name)
        if route:
            backend_server, backend_tool = route
            result = await pool.call_tool(backend_server, backend_tool, arguments)
            return [t.TextContent(type="text", text=json.dumps(result, default=str))]

        # Fallback for dynamically-discovered tools not in the static catalog
        if "__" in name:
            parts = name.split("__", 1)
            if len(parts) == 2:
                result = await pool.call_tool(parts[0], parts[1], arguments)
                return [t.TextContent(type="text", text=json.dumps(result, default=str))]

        return [t.TextContent(type="text", text=json.dumps({
            "status": "failed",
            "error": f"unknown tool: {name}. Use gateway.search to find tools.",
        }))]

    async def run() -> None:
        async with stdio_server() as (r, w):
            try:
                await server.run(r, w, server.create_initialization_options())
            except (asyncio.CancelledError, Exception) as exc:
                logger.critical("Gateway server exiting: %s", exc, exc_info=True)
            finally:
                await pool.close()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


async def _handle_meta_tool(
    name: str,
    args: dict[str, Any],
    pool: AsyncBackendPool,
    shed: Any,
    packs: dict,
    tree: Any,
) -> dict[str, Any]:
    if name == "gateway.health":
        return await pool.health()

    if name == "gateway.discover":
        squad_filter = args.get("squad")
        if squad_filter and squad_filter in packs:
            from hydra_core.tool_scope import squad_tool_manifest
            return squad_tool_manifest(packs[squad_filter])
        server_info = []
        for s in pool.server_names:
            try:
                tools = await pool.list_tools(s)
                server_info.append({"server": s, "tool_count": len(tools)})
            except (asyncio.CancelledError, Exception) as exc:
                logger.warning("discover failed for %s: %s", s, exc)
                server_info.append({"server": s, "tool_count": 0, "error": str(exc)})
        return {
            "servers": server_info,
            "squads": [
                {"slug": s, "name": p.name, "entrypoint": p.entrypoint}
                for s, p in sorted(packs.items())
            ],
        }

    if name == "gateway.search":
        results = shed.search(
            args.get("query", ""),
            server=args.get("server"),
            limit=int(args.get("limit", 10)),
        )
        return {
            "results": [
                {"server": r.server, "name": r.name,
                 "description": r.description, "relevance": r.relevance_score}
                for r in results
            ],
        }

    if name == "gateway.describe":
        entry = shed.describe(args.get("server", ""), args.get("tool_name", ""))
        return entry or {"error": "not_found"}

    if name == "gateway.navigate":
        return tree.navigate(args.get("path", ""))

    if name == "gateway.scope":
        ts = compute_tool_scope(
            args.get("goal", ""),
            args.get("squads", []),
            packs,
            toolshed=shed,
        )
        return {
            "relevant_tools": list(ts.relevant_tools),
            "relevant_categories": list(ts.relevant_categories),
            "intent_keywords": list(ts.intent_keywords),
        }

    return {"error": f"unknown meta-tool: {name}"}


if __name__ == "__main__":
    main()
