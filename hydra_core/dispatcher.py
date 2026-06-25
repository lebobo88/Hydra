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
import os
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


def _env_float(name: str, default: float) -> float:
    """Read a positive float from env; fall back to default on unset/invalid/<=0."""
    raw = os.environ.get(name, "")
    if not raw:
        return default
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return default
    return val if val > 0 else default


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


BACKEND_REGISTRY = Path.home() / ".hydra" / "backends.json"


def _load_backend_registry() -> dict[str, dict[str, Any]]:
    """Read the Hydra-owned backend registry at ``~/.hydra/backends.json``.

    This file contains the same server specs as ``~/.claude.json`` mcpServers
    but lives outside Claude Code's discovery path. Used in gateway mode when
    backends are no longer registered in ``~/.claude.json`` but Hydra's
    internal dispatcher still needs to reach them.

    Returns {} if the file is missing or unreadable.
    """
    if not BACKEND_REGISTRY.exists():
        return {}
    try:
        raw = json.loads(BACKEND_REGISTRY.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if isinstance(raw, dict):
        return {name: _strip_comments(spec) for name, spec in raw.items()
                if isinstance(spec, dict)}
    return {}


def _load_mcp_config(project_root: Path) -> dict[str, dict[str, Any]]:
    """Merge backend sources in precedence order.

    Resolution: ``~/.hydra/backends.json`` (base) → ``~/.claude.json``
    mcpServers (override) → project ``.mcp.json`` (final override).

    In standalone mode (no gateway): backends.json doesn't exist, so
    ``~/.claude.json`` is the only source — identical to pre-gateway behavior.

    In gateway mode: backends removed from ``~/.claude.json`` are still found
    via ``backends.json``, so Hydra's internal dispatcher (supervisor, judge,
    squad_node) continues working.
    """
    merged = _load_backend_registry()
    for name, spec in _load_user_scope_mcp().items():
        merged[name] = spec
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

    # RC3/RC4: marks this as a REAL dispatcher that talks to live MCP servers.
    # `build_supervisor` keys off this to auto-enable autonomous pp codegen
    # (drive_pp_loop) and a real cross-vendor judge (MCPCritiqueClient) on EVERY
    # run against a real dispatcher — the interactive skill / gateway / host-bound
    # paths, not only the cli `--live` flag. Stub/test dispatchers omit it and
    # keep their dry, scaffold-only, skeleton-judge behaviour unchanged.
    live_execution: bool = True

    # --- live MCP call timeouts (env-tunable) --------------------------------
    # Bound every await against a backend MCP server so a hung/wedged server
    # surfaces as a failed result instead of freezing the supervisor at 0 CPU.
    # The gateway hardened this already (mcp_servers/hydra_gateway/server.py);
    # this direct dispatcher — used by `hydra run --live` — had no timeout.
    _DEFAULT_CONNECT_TIMEOUT = 20.0
    _DEFAULT_TOOL_TIMEOUT = 120.0
    _DEFAULT_LONG_TOOL_TIMEOUT = 1800.0
    _DEFAULT_MAX_TOOL_TIMEOUT = 3600.0
    # Calls that are an LLM generate/critique (slow) get the long timeout.
    _LONG_TOOL_SERVERS = frozenset({"pp_codex", "pp_gemini"})
    _LONG_TOOL_NAMES = frozenset({"generate", "critique"})
    _LONG_PP_HARNESS_TOOLS = frozenset({
        "start_stage", "start_best_of_stage", "record_attempt",
        "retry_with_critique",
    })

    def __init__(self, project_root: Path, *, verbose: bool = False):
        self.project_root = project_root
        self.verbose = verbose
        self._servers = _load_mcp_config(project_root)
        self._sessions: dict[str, Any] = {}
        self._stack: Optional[AsyncExitStack] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._squad_packs: dict[str, Any] = {}
        self._active_handoffs: list[dict[str, Any]] = []
        self._tool_tracker: Any = None

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

    def _connect_timeout(self) -> float:
        return _env_float("HYDRA_DISPATCH_CONNECT_TIMEOUT_S",
                          self._DEFAULT_CONNECT_TIMEOUT)

    def _resolve_tool_timeout(self, server: str, tool: str) -> float:
        """Wall-clock cap for a single tool call. Env-tunable; LLM
        generate/critique calls get the long timeout, everything else short.
        """
        short = _env_float("HYDRA_DISPATCH_TOOL_TIMEOUT_S",
                           self._DEFAULT_TOOL_TIMEOUT)
        long = _env_float("HYDRA_DISPATCH_LONG_TOOL_TIMEOUT_S",
                          self._DEFAULT_LONG_TOOL_TIMEOUT)
        hard_max = _env_float("HYDRA_DISPATCH_MAX_TOOL_TIMEOUT_S",
                              self._DEFAULT_MAX_TOOL_TIMEOUT)
        base = tool.rsplit(".", 1)[-1] if tool else tool
        is_long = (
            server in self._LONG_TOOL_SERVERS
            or base in self._LONG_TOOL_NAMES
            or (server == "pp_harness" and base in self._LONG_PP_HARNESS_TOOLS)
        )
        return min(long if is_long else short, hard_max)

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
            self._record_tool_usage(server, tool, squad_id, "rejected")
            return {"status": "rejected", "error": rejection}
        # F11: Cerberus venom gate AFTER RBAC, BEFORE dispatch. RBAC governs
        # *which tool* a squad may call; the venom gate governs *what the args
        # do* (rm -rf, force-push to main, prod deploy, card charge). A hard
        # refusal or a requires-human verdict short-circuits the call.
        venom_block = self._venom_gate(server=server, tool=tool, args=args)
        if venom_block is not None:
            self._record_tool_usage(server, tool, squad_id, "rejected")
            return venom_block
        import time as _time
        _t0 = _time.monotonic()
        result = self._run(self._async_call(server, tool, args))
        _dur = (_time.monotonic() - _t0) * 1000
        status = result.get("status", "unknown") if isinstance(result, dict) else "unknown"
        self._record_tool_usage(server, tool, squad_id, status, _dur)
        return result

    def _record_tool_usage(self, server: str, tool: str,
                           squad_id: str | None, status: str,
                           duration_ms: float = 0.0) -> None:
        if self._tool_tracker is None:
            return
        try:
            self._tool_tracker.record(
                workflow_id="",
                squad_id=squad_id or "unknown",
                node_name="dispatch",
                server=server,
                tool=tool,
                status=status,
                duration_ms=duration_ms,
            )
        except Exception:
            pass

    def _venom_gate(
        self, *, server: str | None = None, tool: str | None = None,
        args: Any = None, cmd: Any = None,
    ) -> dict[str, Any] | None:
        """Run the Cerberus venom gate over a runtime action. Returns a rejection
        envelope when the action is REFUSED or REQUIRES HUMAN approval, else None.
        No registered venom matching the signature → None (fast common path).
        Fail-CLOSED on an explicit VenomRefused / requires_human AND on a
        gate-internal error (a matched venom whose gate errored must not slip
        through). Only a missing venom MODULE (ImportError) fails open."""
        try:
            from .venom import gate_runtime_action, VenomRefused
        except Exception:  # noqa: BLE001 — venom module optional
            return None
        try:
            verdicts = gate_runtime_action(
                server=server, tool=tool, args=args, cmd=cmd, raise_on_refuse=True,
            )
        except VenomRefused as vr:
            # A venom-class action is BLOCKED pending human review (the constitution's
            # `unguarded_venom` refusal means "requires HITL review", not a silent
            # hard-deny). Surface it so the supervisor routes a constitution_breach
            # HITL; the human approves or denies. Autonomous execution stops here.
            logger.warning("Cerberus blocked %s (HITL): %s", vr.capability, "; ".join(vr.reasons))
            return {"status": "rejected", "error": str(vr),
                    "venom_refused": True, "hitl_required": True,
                    "capability": vr.capability, "reasons": list(vr.reasons),
                    "audit_key": vr.audit_key}
        except Exception as exc:  # noqa: BLE001
            # FAIL-CLOSED. This branch is reached ONLY when a venom signature
            # already matched (gate_runtime_action calls require_cerberus_pass for
            # a registered capability) and the gate itself errored — e.g. a
            # degraded/locked episodic audit store throwing inside the gate. A
            # venom-class action whose gate could not complete must NOT proceed
            # unchecked (the old fail-open silently converted a pending refusal
            # into an allow). Block + route to HITL. Non-venom actions never reach
            # here, so this cannot wedge ordinary dispatch.
            logger.warning("venom gate internal error (fail-CLOSED → HITL): %r", exc)
            return {"status": "rejected",
                    "error": f"venom gate internal error: {exc}",
                    "hitl_required": True, "gate_error": True}
        # Defensive: a capability configured to PASS-with-requires_human (no
        # constitution breach) also blocks autonomously and routes to HITL.
        hil = [v for v in verdicts if getattr(v, "requires_human", False)]
        if hil:
            action = ".".join(c for c in (server, tool) if c) or "subprocess"
            logger.warning("Cerberus requires human approval for venom-class action %s", action)
            return {"status": "rejected",
                    "error": "venom-class action requires human approval",
                    "hitl_required": True,
                    "audit_keys": [v.audit_key for v in hil]}
        return None

    def spawn_subprocess(self, cmd: list[str], env: dict[str, str] | None = None) -> dict[str, Any]:
        venom_block = self._venom_gate(cmd=cmd)
        if venom_block is not None:
            return venom_block
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

    # Host-executor seam: the base MCP/stdio dispatcher is HEADLESS — there is no
    # Claude Code host to spawn a subagent — so it returns None, signaling callers
    # to fall back (codex generation for engineering; host_pickup for skills). A
    # host-attached integration subclasses/wraps this with a real implementation
    # (sets supports_host_agent=True and runs the named Claude subagent).
    supports_host_agent: bool = False

    def run_host_agent(
        self, agent_type: str, prompt: str, *,
        cwd: str | None = None, timeout_s: int | None = None,
    ) -> dict[str, Any] | None:
        return None

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
                    f"server {server!r} not registered in backends.json, "
                    f"~/.claude.json, or .mcp.json. "
                    f"Known: {sorted(self._servers)[:10]}"
                ),
            }

        params = StdioServerParameters(
            command=spec["command"],
            args=list(spec.get("args", [])),
            env=spec.get("env"),
            cwd=spec.get("cwd"),
        )

        # WS3c — connect-retry with deterministic jittered backoff.
        #
        # The retry loop covers ONLY the transport connection + session
        # initialisation (stdio_client + ClientSession.initialize). Once a
        # session is established, session.call_tool is invoked EXACTLY ONCE.
        # If call_tool itself raises, we do NOT retry — the tool may have
        # side-effected (start_run, finalize_run, writes, venom-class) and
        # re-invoking it is unsafe.
        #
        # Fix 1a (R3-tail): A `called` flag guards against the case where
        # call_tool succeeds but context-manager __aexit__ teardown then raises.
        # Without the flag, the outer except re-enters the retry loop and
        # double-executes the tool. With the flag:
        #   - `called` is set True immediately before session.call_tool().
        #   - The outer except checks `called`: if True, the call was attempted
        #     and we must NOT retry — return failed immediately.
        #   - After call_tool succeeds, the result payload is captured in a local
        #     variable; __aexit__ teardown exceptions are caught and logged so
        #     the successful result is still returned.
        #
        # Jitter is deterministic: derived via SHA-256 from server+tool+attempt
        # so the same triple always waits the same amount and replays are stable.
        import hashlib as _hashlib

        _MAX_CONNECT_ATTEMPTS = 3
        last_connect_exc: Exception | None = None
        # Fix 1a: tracks whether session.call_tool was invoked this attempt.
        # Reset to False at the top of each connection attempt.
        called = False
        # Fix 1a: holds the successful payload if call_tool returned before
        # __aexit__ teardown raised. We return this rather than discarding it.
        _call_result: dict[str, Any] | None = None

        for _connect_attempt in range(1, _MAX_CONNECT_ATTEMPTS + 1):
            called = False
            _call_result = None
            try:
                async with stdio_client(params) as (read, write):
                    async with ClientSession(read, write) as session:
                        await asyncio.wait_for(
                            session.initialize(), self._connect_timeout()
                        )
                        if self.verbose:
                            tools = await asyncio.wait_for(
                                session.list_tools(), self._connect_timeout()
                            )
                            names = [t.name for t in tools.tools]
                            if tool not in names:
                                return {
                                    "status": "failed",
                                    "error": f"tool {tool!r} not exposed by {server!r}",
                                    "available": names[:30],
                                }
                        # Connection established — invoke the tool ONCE.
                        # Any exception from call_tool is NOT retried (see
                        # outer except guard on `called`).
                        called = True
                        _eff_timeout = self._resolve_tool_timeout(server, tool)
                        try:
                            result = await asyncio.wait_for(
                                session.call_tool(tool, args), _eff_timeout
                            )
                        except (asyncio.TimeoutError, TimeoutError):
                            # Hung backend — do NOT retry (the tool may have
                            # side-effected; `called` is already True). Surface a
                            # failed result so the supervisor moves on / HITLs
                            # instead of freezing the engine at 0 CPU.
                            return {
                                "status": "failed",
                                "timeout": True,
                                "error": (
                                    f"tool {tool!r} on {server!r} timed out "
                                    f"after {_eff_timeout}s"
                                ),
                                "server": server, "tool": tool,
                                "phase": "call_tool", "timeout_s": _eff_timeout,
                            }
                        except Exception as call_exc:
                            return {
                                "status": "failed",
                                "error": (
                                    f"call_tool raised after connect: "
                                    f"{type(call_exc).__name__}: {call_exc!s}"
                                ),
                                "server": server, "tool": tool,
                            }
                        # Capture result BEFORE exiting context managers so a
                        # teardown exception in __aexit__ does not lose the payload.
                        # Gate on the RAW CallToolResult.isError: an MCP tool that
                        # returns a structured error (without raising) sets
                        # isError=True but still carries content — flattening it
                        # into a "done" payload (the old behavior) masked tool-level
                        # failures as success downstream (judge/codex/skill all read
                        # status). We key off the boolean BEFORE _extract_mcp_result
                        # drops it. (We do NOT scan the flattened content for an
                        # "error" key — that would false-fail any legitimate success
                        # payload carrying an `error_rate`/`error` metric field.)
                        _extracted = _extract_mcp_result(result)
                        # `is True` (not just truthy): the MCP SDK's
                        # CallToolResult.isError is a real bool, so this matches a
                        # genuine tool error while ignoring test doubles whose
                        # auto-created mock attributes are truthy-but-not-True.
                        if getattr(result, "isError", False) is True:
                            _call_result = {
                                "status": "failed", "tool": tool, "server": server,
                                "result": _extracted,
                                "error": (
                                    _extracted if isinstance(_extracted, str)
                                    else str(_extracted)
                                ),
                            }
                        else:
                            _call_result = {"status": "done", "tool": tool,
                                            "result": _extracted}
                        # Returning here unwinds the `async with` stack; if
                        # __aexit__ raises it will be caught by the outer except
                        # which checks `_call_result is not None` and returns it.
                        return _call_result
            except Exception as exc:  # noqa: BLE001 — connection/init or teardown failure
                # Fix 1a: if call_tool was attempted, do NOT retry regardless of
                # whether the exception is from teardown or the call itself.
                # If we already captured _call_result, the call succeeded and only
                # __aexit__ teardown raised — return the successful payload and log.
                if _call_result is not None:
                    logger.debug(
                        "MCP context teardown raised after successful call_tool "
                        "for %s.%s (%s); returning captured result.",
                        server, tool, type(exc).__name__,
                    )
                    return _call_result
                if called:
                    # call_tool was invoked but we have no result (it raised).
                    # Already returned in the inner except above; reaching here
                    # means a re-raise path we should not retry.
                    return {
                        "status": "failed",
                        "error": (
                            f"post-call_tool error (no retry): "
                            f"{type(exc).__name__}: {exc!s}"
                        ),
                        "server": server, "tool": tool,
                    }
                last_connect_exc = exc
                if _connect_attempt < _MAX_CONNECT_ATTEMPTS:
                    # Fix 6: deterministic jitter via SHA-256; no wallclock/hash().
                    _seed = (server + tool + str(_connect_attempt)).encode()
                    _n = int.from_bytes(
                        _hashlib.sha256(_seed).digest()[:4], "big"
                    ) % 400
                    _jitter_s = 0.1 + _n / 1000.0
                    await asyncio.sleep(_jitter_s)
                    logger.debug(
                        "MCP connect attempt %d/%d for %s.%s failed (%s); retrying in %.3fs",
                        _connect_attempt, _MAX_CONNECT_ATTEMPTS, server, tool,
                        type(exc).__name__, _jitter_s,
                    )

        return {
            "status": "failed",
            "error": f"{type(last_connect_exc).__name__}: {last_connect_exc!s}",
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
