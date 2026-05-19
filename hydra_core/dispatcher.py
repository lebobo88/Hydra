"""Live dispatchers for squad-node execution.

`MCPStdioDispatcher` connects to one or more MCP servers declared in `.mcp.json`
and proxies tool calls into them. This is how the engineering squad reaches the
pair-programmer daemon for a real `pp.harness.start_run`.

Subprocess + claude-skill + impersonation dispatch are stubbed-out delegations
that print a structured envelope intended for the host Claude Code session to
pick up. (In a Claude Code plugin host, those branches would dispatch through
the host's native sub-agent / skill / process APIs; from a headless CLI we just
log the intent.)
"""
from __future__ import annotations

import asyncio
import json
import subprocess
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


# --------- helpers ---------

def _load_mcp_config(project_root: Path) -> dict[str, dict[str, Any]]:
    cfg = project_root / ".mcp.json"
    if not cfg.exists():
        return {}
    raw = json.loads(cfg.read_text(encoding="utf-8"))
    servers = raw.get("mcpServers", {}) or {}
    # Strip "_comment" decorations
    return {
        name: {k: v for k, v in spec.items() if not k.startswith("_")}
        for name, spec in servers.items()
    }


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

    # --- sync facade matching the squad_node.Dispatcher Protocol ---

    def call_mcp(self, server: str, tool: str, args: dict[str, Any]) -> dict[str, Any]:
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
            return {"status": "failed", "error": f"server {server!r} not in .mcp.json"}

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
