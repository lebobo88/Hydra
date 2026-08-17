"""Live dispatchers for squad-node execution.

`MCPStdioDispatcher` connects to one or more MCP servers declared at USER scope
(`~/.claude.json` mcpServers) — with optional project-scope override from
`.mcp.json` when one exists — and proxies tool calls into them. This is how
the engineering squad reaches the pair-programmer daemon for a real
`pp.harness.start_run`.

Hydra no longer ships a project-scope `.mcp.json`; all squad backends
(`pp_harness`, `pp_codex`, `pp_agy`, `hydra_memory`, `executive_suite`,
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
import threading
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


@dataclass
class _PooledMcpSession:
    stdio_cm: Any
    session_cm: Any
    session: Any
    tools: set[str] | None = None


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
    _LONG_TOOL_SERVERS = frozenset({"pp_codex", "pp_agy"})
    _LONG_TOOL_NAMES = frozenset({"generate", "critique"})
    _LONG_PP_HARNESS_TOOLS = frozenset({
        "start_stage", "start_best_of_stage", "record_attempt",
        "retry_with_critique",
    })
    # W2-1: pp_harness joined the pool alongside eights. Every unpooled call
    # opened a fresh stdio_client -> a brand-new pp daemon process -> `new
    # Database()` + `applyMigrations()` INSIDE the SQLite write path (the same
    # path record_verdict/finalize_stage/finalize_run write through). That
    # cold start on every call was the dominant source of the SQLITE_BUSY
    # contention the client-side busy_timeout/retry (pp commit 500298b) had to
    # absorb. Pooling reuses one live session (and therefore one live daemon +
    # one open DB handle) across an entire attended stage's tool calls, so the
    # daemon initializes once instead of once per call. The pooling machinery
    # (_async_call_pooled / _get_or_connect_pooled_session) is already
    # server-agnostic — no pp_harness-specific behavior was added.
    _POOLED_SERVERS = frozenset({"eights", "pp_harness"})
    # P1.3: overall-call backstop overhead (seconds). The per-op timeouts above
    # bound connect / initialize / call_tool, but NOT the stdio context-manager
    # __aexit__ teardown — a wedged child MCP server (pp_harness/pp_codex) can
    # block that teardown indefinitely (observed: a 45-min stall at dispatch with
    # node+python children alive at ~0 CPU). call_mcp wraps the NON-pooled path in
    # an overall deadline = tool_timeout + this overhead, so teardown/connect can
    # never exceed it. The overhead exceeds the worst-case connect budget
    # (3 attempts × ~2 × connect_timeout ≈ 120s) so the backstop only ever fires
    # on a genuinely wedged transport, never on a legitimately slow-but-valid call.
    _DEFAULT_OVERALL_OVERHEAD = 180.0

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
        self._pooled_sessions: dict[str, _PooledMcpSession] = {}
        self._pooled_session_locks: dict[str, asyncio.Lock] = {}
        # Guards driving self._loop so two _run() calls never run_until_complete
        # on it concurrently (slow-path worker thread + any other caller).
        self._run_lock = threading.Lock()

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

        RA-3: claude-skill squads are auto-authorized for their own shim tool
        pair ({prefix}.command.list / {prefix}.output.write) on their shim
        server. These tools are not listed in squad.yaml because they are
        injected by the skill-dispatch infrastructure (_via_claude_skill), not
        declared by the squad author. Importing _SKILL_PACK_SHIMS lazily
        (inside the function) avoids a circular import — squad_node imports
        from schemas/state/iolaus, dispatcher imports nothing from squad_node
        at module level, so the lazy load is safe.
        """
        if squad_id is None:
            return None
        pack = self._squad_packs.get(squad_id)
        if pack is None:
            return None

        # RA-3: auto-authorize each claude-skill squad's own shim tool pair.
        if getattr(pack, "entrypoint", None) == "claude-skill":
            # Lazy import to avoid a circular-import if the import graph changes.
            try:
                from .squad_node import _SKILL_PACK_SHIMS  # noqa: PLC0415
                shim = _SKILL_PACK_SHIMS.get(squad_id)
                if shim is not None:
                    shim_server = shim["server"]
                    shim_prefix = shim["prefix"]
                    if server == shim_server and tool in (
                        f"{shim_prefix}.command.list",
                        f"{shim_prefix}.output.write",
                    ):
                        return None  # authorized: this squad's own shim pair
            except ImportError:
                pass  # fail-open on import error; fall through to declared-tools check

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
            # RA-3: structured warning with the dispatch.rbac_denied event key
            # so log aggregators can identify shim-authorization failures as a
            # distinct signal from general RBAC violations.
            logger.warning(
                "dispatch.rbac_denied server=%s tool=%s squad=%s: %s",
                server, tool, squad_id, rejection,
                extra={
                    "event": "dispatch.rbac_denied",
                    "server": server,
                    "tool": tool,
                    "squad_id": squad_id,
                },
            )
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
        if server in self._POOLED_SERVERS:
            result = self._run(self._async_call_pooled(server, tool, args))
        else:
            # P1.3: bound the non-pooled path with an overall deadline so a
            # wedged stdio __aexit__ teardown (not covered by the inner per-op
            # timeouts) can never freeze the stage loop.
            _overall = (self._resolve_tool_timeout(server, tool)
                        + self._overall_overhead())
            result = self._run(self._call_with_deadline(
                self._async_call(server, tool, args), _overall, server, tool))
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
        # F35: inject AgentSmith cross-check when HYDRA_VENOM_CROSS_CHECK=1.
        # The callable wraps self.call_mcp so venom.py stays runtime-agnostic
        # (no provider SDK imports in venom.py — only a plain Callable here).
        _xc_fn = None
        if os.environ.get("HYDRA_VENOM_CROSS_CHECK") == "1":
            _self = self  # capture for closure

            def _agentsmith_cross_check(cap: str, xargs: Any) -> Optional[dict]:
                try:
                    result = _self.call_mcp(
                        "agentsmith",
                        "agentsmith.hydra.venom_cross_check",
                        {"capability": cap, "context": xargs if isinstance(xargs, dict) else {}},
                    )
                    if isinstance(result, dict):
                        inner = result.get("result", result)
                        if isinstance(inner, dict):
                            return inner
                except Exception:  # noqa: BLE001 — fail-open on transport error
                    pass
                return None

            _xc_fn = _agentsmith_cross_check
        try:
            verdicts = gate_runtime_action(
                server=server, tool=tool, args=args, cmd=cmd, raise_on_refuse=True,
                cross_check_fn=_xc_fn,
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
        # run_until_complete is ILLEGAL when a loop is already running on THIS
        # thread (call_mcp reached from inside a compiled-LangGraph node or any
        # async caller): asyncio raises RuntimeError *before* the coroutine is
        # awaited, leaking it un-awaited and surfacing as 'pp_harness
        # unreachable -> failed' (the dispatch streak). Detect that case and
        # drive the coroutine on a dedicated worker thread — which has no
        # running loop of its own — blocking for the result. We drive
        # self._loop (not a throwaway) so pooled sessions bound to it stay
        # valid; self._loop runs nowhere else, so a worker may drive it.
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            with self._run_lock:
                return self._loop.run_until_complete(coro)  # no running loop
        box: dict[str, Any] = {}

        def _drive() -> None:
            try:
                with self._run_lock:
                    box["value"] = self._loop.run_until_complete(coro)
            except BaseException as exc:  # noqa: BLE001 — re-raised on caller
                box["error"] = exc

        t = threading.Thread(
            target=_drive, name="hydra-dispatch-run", daemon=True)
        t.start()
        t.join()
        if "error" in box:
            raise box["error"]
        return box["value"]

    def _overall_overhead(self) -> float:
        return _env_float("HYDRA_DISPATCH_OVERALL_OVERHEAD_S",
                          self._DEFAULT_OVERALL_OVERHEAD)

    async def _call_with_deadline(self, coro: Any, deadline: float,
                                  server: str, tool: str) -> dict[str, Any]:
        """Bound a whole non-pooled ``_async_call`` — INCLUDING the stdio
        context-manager ``__aexit__`` teardown — with one overall deadline.

        The inner per-op ``wait_for``s in ``_async_call`` cap connect /
        initialize / call_tool, but a wedged child MCP server can still block the
        ``async with`` teardown that runs on return, freezing the stage loop
        (P1.3). ``wait_for`` cancels the coroutine at ``deadline``, injecting
        ``CancelledError`` into the hung teardown await so it unwinds instead of
        hanging. Because ``deadline`` = tool_timeout + overhead (which exceeds the
        connect + call budget), this backstop only ever fires on a genuinely
        wedged transport — never on a legitimately slow-but-valid call.
        """
        try:
            return await asyncio.wait_for(coro, deadline)
        except (asyncio.TimeoutError, TimeoutError):
            logger.warning(
                "MCP call %s.%s exceeded overall deadline %.0fs — abandoning "
                "wedged transport (teardown/connect never returned)",
                server, tool, deadline,
            )
            return {
                "status": "failed", "timeout": True, "phase": "overall",
                "error": (f"tool {tool!r} on {server!r} exceeded overall "
                          f"deadline {deadline}s (wedged transport teardown)"),
                "server": server, "tool": tool, "timeout_s": deadline,
            }
        except Exception as exc:  # noqa: BLE001 — e.g. anyio cancel-scope RuntimeError
            return {
                "status": "failed",
                "error": (f"overall-deadline unwind for {tool!r} on {server!r}: "
                          f"{type(exc).__name__}: {exc!s}"),
                "server": server, "tool": tool,
            }

    def _pooled_session_lock(self, server: str) -> asyncio.Lock:
        lock = self._pooled_session_locks.get(server)
        if lock is None:
            lock = asyncio.Lock()
            self._pooled_session_locks[server] = lock
        return lock

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

    async def _async_call_pooled(self, server: str, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        try:
            pooled = await self._get_or_connect_pooled_session(server)
        except ImportError as e:
            return {"status": "failed", "error": f"mcp SDK not installed: {e!r}"}
        except KeyError:
            return {
                "status": "failed",
                "error": (
                    f"server {server!r} not registered in backends.json, "
                    f"~/.claude.json, or .mcp.json. "
                    f"Known: {sorted(self._servers)[:10]}"
                ),
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "status": "failed",
                "error": f"{type(exc).__name__}: {exc!s}",
                "server": server,
                "tool": tool,
            }

        if self.verbose and pooled.tools is None:
            try:
                tools = await asyncio.wait_for(
                    pooled.session.list_tools(), self._connect_timeout()
                )
                pooled.tools = {t.name for t in tools.tools}
            except Exception:  # noqa: BLE001 — fail-soft; the call itself still proceeds
                pooled.tools = None
        if self.verbose and pooled.tools is not None and tool not in pooled.tools:
            return {
                "status": "failed",
                "error": f"tool {tool!r} not exposed by {server!r}",
                "available": sorted(pooled.tools)[:30],
            }

        _eff_timeout = self._resolve_tool_timeout(server, tool)
        try:
            result = await asyncio.wait_for(
                pooled.session.call_tool(tool, args), _eff_timeout
            )
        except (asyncio.TimeoutError, TimeoutError):
            await self._drop_pooled_session(server)
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
            await self._drop_pooled_session(server)
            return {
                "status": "failed",
                "error": (
                    f"call_tool raised after connect: "
                    f"{type(call_exc).__name__}: {call_exc!s}"
                ),
                "server": server, "tool": tool,
            }

        _extracted = _extract_mcp_result(result)
        if getattr(result, "isError", False) is True:
            return {
                "status": "failed", "tool": tool, "server": server,
                "result": _extracted,
                "error": (
                    _extracted if isinstance(_extracted, str)
                    else str(_extracted)
                ),
            }
        return {"status": "done", "tool": tool, "result": _extracted}

    async def _get_or_connect_pooled_session(self, server: str) -> _PooledMcpSession:
        cached = self._pooled_sessions.get(server)
        if cached is not None:
            return cached
        async with self._pooled_session_lock(server):
            cached = self._pooled_sessions.get(server)
            if cached is not None:
                return cached

            try:
                from mcp import ClientSession, StdioServerParameters  # type: ignore
                from mcp.client.stdio import stdio_client  # type: ignore
            except ImportError:
                raise

            spec = self._servers.get(server)
            if spec is None:
                raise KeyError(server)

            params = StdioServerParameters(
                command=spec["command"],
                args=list(spec.get("args", [])),
                env=spec.get("env"),
                cwd=spec.get("cwd"),
            )

            import hashlib as _hashlib

            _MAX_CONNECT_ATTEMPTS = 3
            last_connect_exc: Exception | None = None
            for _connect_attempt in range(1, _MAX_CONNECT_ATTEMPTS + 1):
                stdio_cm = None
                session_cm = None
                try:
                    stdio_cm = stdio_client(params)
                    read, write = await asyncio.wait_for(
                        stdio_cm.__aenter__(), self._connect_timeout()
                    )
                    session_cm = ClientSession(read, write)
                    session = await session_cm.__aenter__()
                    await asyncio.wait_for(
                        session.initialize(), self._connect_timeout()
                    )
                    pooled = _PooledMcpSession(
                        stdio_cm=stdio_cm,
                        session_cm=session_cm,
                        session=session,
                    )
                    self._pooled_sessions[server] = pooled
                    return pooled
                except Exception as exc:  # noqa: BLE001
                    last_connect_exc = exc
                    await self._close_partial_pool(stdio_cm, session_cm)
                    if _connect_attempt < _MAX_CONNECT_ATTEMPTS:
                        _seed = (server + str(_connect_attempt)).encode()
                        _n = int.from_bytes(
                            _hashlib.sha256(_seed).digest()[:4], "big"
                        ) % 400
                        _jitter_s = 0.1 + _n / 1000.0
                        await asyncio.sleep(_jitter_s)
                        logger.debug(
                            "MCP pooled connect attempt %d/%d for %s failed (%s); retrying in %.3fs",
                            _connect_attempt, _MAX_CONNECT_ATTEMPTS, server,
                            type(exc).__name__, _jitter_s,
                        )
            raise RuntimeError(f"{type(last_connect_exc).__name__}: {last_connect_exc!s}")

    async def _drop_pooled_session(self, server: str) -> None:
        pooled = self._pooled_sessions.pop(server, None)
        if pooled is None:
            return
        await self._close_partial_pool(pooled.stdio_cm, pooled.session_cm)

    async def _close_partial_pool(self, stdio_cm: Any, session_cm: Any) -> None:
        # W2-1: bound each teardown with the same overall-deadline reasoning
        # P1.3 applied to the non-pooled path. Pooling pp_harness means its
        # session no longer tears down after every call (that's the point —
        # it eliminates the per-call daemon cold start), but a DROPPED pooled
        # session (on a call_tool error/timeout, see _async_call_pooled) still
        # tears down via this method, and an MCP child wedged on teardown must
        # not freeze the stage loop here either. `asyncio.wait_for` cancels the
        # wedged `__aexit__` at the deadline so this always returns.
        _deadline = self._connect_timeout()
        if session_cm is not None:
            try:
                await asyncio.wait_for(session_cm.__aexit__(None, None, None), _deadline)
            except Exception:  # noqa: BLE001 — includes asyncio.TimeoutError
                pass
        if stdio_cm is not None:
            try:
                await asyncio.wait_for(stdio_cm.__aexit__(None, None, None), _deadline)
            except Exception:  # noqa: BLE001 — includes asyncio.TimeoutError
                pass


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
