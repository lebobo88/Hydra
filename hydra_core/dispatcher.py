"""Live dispatchers for squad-node execution.

`MCPStdioDispatcher` connects to one or more MCP servers declared at USER scope
(`~/.claude.json` mcpServers) — with optional project-scope override from
`.mcp.json` when one exists — and proxies tool calls into them. This is how
the engineering squad reaches the pair-programmer daemon for a real
`pp.harness.start_run`.

Hydra no longer ships a project-scope `.mcp.json`; all squad backends
(`pp_harness`, `pp_codex`, `pp_gemini`, `hydra_memory`, `executive_suite`,
`rlm_creative`, `eights`, `agentsmith`) are registered once at user scope so
every project — Hydra's own source tree, blank scratch dirs, and downstream
consumers — sees the same set.

Subprocess + claude-skill + impersonation dispatch are stubbed-out delegations
that print a structured envelope intended for the host Claude Code session to
pick up. (In a Claude Code plugin host, those branches would dispatch through
the host's native sub-agent / skill / process APIs; from a headless CLI we just
log the intent.)
"""
from __future__ import annotations

import asyncio
import json
import logging
import subprocess
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


# --------- helpers ---------

def _strip_comments(spec: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in spec.items() if not k.startswith("_")}


def _load_user_scope_mcp() -> dict[str, dict[str, Any]]:
    """Read the top-level `mcpServers` block from `~/.claude.json`.

    Skips per-project overrides nested under `projects.*.mcpServers` — those
    are session-scoped and not relevant to Hydra dispatch. Silently returns
    {} if the file is missing or unreadable; the caller treats absence as
    "no servers" and surfaces `server not configured` per-call.
    """
    cfg = Path.home() / ".claude.json"
    if not cfg.exists():
        return {}
    try:
        raw = json.loads(cfg.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    servers = raw.get("mcpServers", {}) or {}
    return {name: _strip_comments(spec) for name, spec in servers.items()}


def _load_mcp_config(project_root: Path) -> dict[str, dict[str, Any]]:
    """Merge user-scope and (optional) project-scope MCP server registrations.

    User scope is the canonical source; project-scope `.mcp.json` — if present
    — overrides individual entries. Hydra ships no `.mcp.json` of its own as
    of 2026-05-21, but the override path is preserved for downstream consumers
    that need to pin a specific daemon revision per project.
    """
    merged = _load_user_scope_mcp()
    cfg = project_root / ".mcp.json"
    if cfg.exists():
        try:
            raw = json.loads(cfg.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return merged
        for name, spec in (raw.get("mcpServers", {}) or {}).items():
            merged[name] = _strip_comments(spec)
    return merged


# --------- live MCP dispatcher ---------

class MCPStdioDispatcher:
    """Live dispatcher. Opens one stdio session per MCP server declared in
    `.mcp.json`, caches sessions, and proxies `call_mcp` to them.

    For subprocess / skill / impersonation branches we degrade to host-pickup
    envelopes (printed to stderr) — those execute in Claude Code, not here.
    """

    def __init__(self, project_root: Path, *, verbose: bool = False):
        self.project_root = project_root
        self.verbose = verbose
        self._servers = _load_mcp_config(project_root)
        self._sessions: dict[str, Any] = {}
        self._stack: Optional[AsyncExitStack] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._squad_packs: dict[str, Any] = {}
        self._active_handoffs: list[dict[str, Any]] = []

    def set_squad_packs(self, packs: dict[str, Any]) -> None:
        """Inject discovered squad packs for RBAC enforcement."""
        self._squad_packs = packs

    def grant_handoff(self, squad_id: str, granted_tools: list[str],
                      expires_at: datetime | None = None) -> None:
        """Register a Handoff privilege escalation."""
        self._active_handoffs.append({
            "squad_id": squad_id,
            "granted_tools": granted_tools,
            "expires_at": expires_at,
        })

    def _check_tool_rbac(self, server: str, tool: str,
                         squad_id: str | None) -> str | None:
        """Validate that the squad is authorized to call this tool.

        Returns None if authorized, or a rejection reason string.
        Skips enforcement when squad_id is None (CLI/test paths).
        """
        if squad_id is None:
            return None
        pack = self._squad_packs.get(squad_id)
        if pack is None:
            return None
        declared_tools = getattr(pack, "tools", ())
        tool_key = f"{server}.{tool}" if server else tool
        for t in declared_tools:
            t_name = getattr(t, "name", t) if not isinstance(t, str) else t
            t_server = getattr(t, "mcp_server", None)
            if t_name == tool_key:
                return None
            if t_name == tool and (t_server is None or t_server == server):
                return None
        now = datetime.now(timezone.utc)
        for h in self._active_handoffs:
            if h["squad_id"] != squad_id:
                continue
            if h["expires_at"] and h["expires_at"] < now:
                continue
            if tool_key in h["granted_tools"] or tool in h["granted_tools"]:
                return None
        return (
            f"RBAC: squad {squad_id!r} is not authorized for tool "
            f"{tool!r} on server {server!r}. Declared tools: "
            f"{[getattr(t, 'name', t) for t in declared_tools]}"
        )

    # --- sync facade matching the squad_node.Dispatcher Protocol ---

    def call_mcp(self, server: str, tool: str, args: dict[str, Any],
                 *, squad_id: str | None = None) -> dict[str, Any]:
        rejection = self._check_tool_rbac(server, tool, squad_id)
        if rejection:
            logger.warning("MCP RBAC violation: %s", rejection)
            from . import telemetry
            try:
                telemetry.emit(self.project_root, "rbac", "rbac_violation", {
                    "squad_id": squad_id, "server": server, "tool": tool,
                    "reason": rejection,
                })
            except Exception:
                pass
            return {"status": "rejected", "error": rejection}
        return self._run(self._async_call(server, tool, args))

    def spawn_subprocess(self, cmd: list[str], env: dict[str, str] | None = None) -> dict[str, Any]:
        try:
            res = subprocess.run(
                cmd, env=env, capture_output=True, text=True, timeout=300,
            )
            return {
                "returncode": res.returncode,
                "stdout": res.stdout[-4000:],
                "stderr": res.stderr[-2000:],
                "status": "done" if res.returncode == 0 else "failed",
            }
        except Exception as e:
            return {"returncode": -1, "status": "failed", "stderr": str(e)}

    def emit_claude_prompt(self, prompt: str, agent: str | None = None) -> dict[str, Any]:
        # In a Claude Code plugin host this would invoke a sub-agent.
        # Headless: log the intent so the operator (or wrapping host) can act.
        return {
            "status": "host_pickup_required",
            "summary": f"impersonation-prompt for agent={agent!r}, {len(prompt)}b",
            "agent": agent,
            "prompt_preview": prompt[:280],
        }

    def invoke_claude_skill(self, skill: str, args: dict[str, Any]) -> dict[str, Any]:
        return {
            "status": "host_pickup_required",
            "summary": f"skill /{skill} requested",
            "skill": skill,
            "args_preview": {k: str(v)[:120] for k, v in args.items()},
        }

    # --- async core ---

    def _run(self, coro):
        # Run an async coroutine to completion from sync code. Reuse a loop so
        # session bookkeeping inside _stack survives across multiple calls.
        if self._loop is None or self._loop.is_closed():
            self._loop = asyncio.new_event_loop()
        return self._loop.run_until_complete(coro)

    async def _async_call(self, server: str, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        try:
            from mcp import ClientSession, StdioServerParameters  # type: ignore
            from mcp.client.stdio import stdio_client  # type: ignore
        except ImportError as e:
            return {"status": "failed", "error": f"mcp SDK not installed: {e!r}"}

        spec = self._servers.get(server)
        if spec is None:
            return {
                "status": "failed",
                "error": (
                    f"server {server!r} not registered at user scope "
                    f"(~/.claude.json mcpServers) or project scope (.mcp.json). "
                    f"Known: {sorted(self._servers)[:10]}"
                ),
            }

        # Open a fresh session per call. (For production: pool/cache by server.)
        params = StdioServerParameters(
            command=spec["command"],
            args=list(spec.get("args", [])),
            env=spec.get("env"),
        )
        try:
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    if self.verbose:
                        tools = await session.list_tools()
                        names = [t.name for t in tools.tools]
                        if tool not in names:
                            return {
                                "status": "failed",
                                "error": f"tool {tool!r} not exposed by {server!r}",
                                "available": names[:30],
                            }
                    result = await session.call_tool(tool, args)
                    payload = _extract_mcp_result(result)
                    return {"status": "done", "tool": tool, "result": payload}
        except Exception as e:
            return {
                "status": "failed",
                "error": f"{type(e).__name__}: {e!s}",
                "server": server, "tool": tool,
            }


def _extract_mcp_result(result: Any) -> Any:
    """MCP CallToolResult has .content as list[TextContent|...]. Flatten the
    text content into a python dict where possible."""
    content = getattr(result, "content", None)
    if not content:
        return {"raw": str(result)}
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
    return out[0] if len(out) == 1 else out
