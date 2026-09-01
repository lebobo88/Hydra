"""Unit tests for MCPStdioDispatcher live-call timeouts (Fix A).

Covers the deadlock fix: the supervisor's own dispatcher (used by
`hydra run --live`) awaited stdio connect / session.initialize /
session.call_tool with NO timeout, so a hung MCP backend wedged the engine at
0 CPU forever. These tests exercise the per-class resolver and the bounded
`_async_call` path with a fake in-memory MCP SDK (no network / no real server).
"""
from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

import pytest

from hydra_core.dispatcher import MCPStdioDispatcher, _env_float

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def disp() -> MCPStdioDispatcher:
    d = MCPStdioDispatcher(project_root=REPO_ROOT)
    # Pretend one server is registered so spec lookup in _async_call succeeds.
    d._servers = {"pp_harness": {"command": "noop", "args": []},
                  "pp_codex": {"command": "noop", "args": []}}
    return d


# ─── resolver classification ────────────────────────────────────────────────

@pytest.mark.parametrize("server,tool", [
    ("pp_codex", "generate"),
    ("pp_agy", "critique"),
    ("pp_codex", "foo.generate"),       # dotted → classified by tail
    ("pp_harness", "start_stage"),      # pp_harness LLM-wrapping stage tool
    ("pp_harness", "record_attempt"),
])
def test_long_tools_get_long_timeout(disp, server, tool):
    assert disp._resolve_tool_timeout(server, tool) == 1800.0


@pytest.mark.parametrize("server,tool", [
    ("pp_harness", "start_run"),
    ("pp_harness", "finalize_run"),
    ("executive_suite", "es.roster.list"),
    ("eights", "list"),
])
def test_ordinary_tools_get_short_timeout(disp, server, tool):
    assert disp._resolve_tool_timeout(server, tool) == 120.0


def test_env_overrides_and_clamp(monkeypatch, disp):
    monkeypatch.setenv("HYDRA_DISPATCH_TOOL_TIMEOUT_S", "45")
    monkeypatch.setenv("HYDRA_DISPATCH_LONG_TOOL_TIMEOUT_S", "600")
    monkeypatch.setenv("HYDRA_DISPATCH_MAX_TOOL_TIMEOUT_S", "300")
    assert disp._resolve_tool_timeout("eights", "list") == 45.0
    # long (600) clamped down to hard-max (300)
    assert disp._resolve_tool_timeout("pp_codex", "generate") == 300.0


def test_connect_timeout_env_override(monkeypatch, disp):
    assert disp._connect_timeout() == 20.0
    monkeypatch.setenv("HYDRA_DISPATCH_CONNECT_TIMEOUT_S", "5")
    assert disp._connect_timeout() == 5.0


def test_env_float_rejects_bad_values(monkeypatch):
    monkeypatch.setenv("X", "abc"); assert _env_float("X", 120.0) == 120.0
    monkeypatch.setenv("X", "0");   assert _env_float("X", 120.0) == 120.0
    monkeypatch.setenv("X", "-5");  assert _env_float("X", 120.0) == 120.0
    monkeypatch.setenv("X", "300"); assert _env_float("X", 120.0) == 300.0
    monkeypatch.delenv("X", raising=False); assert _env_float("X", 120.0) == 120.0


# ─── fake in-memory MCP SDK for the bounded _async_call path ─────────────────

class _FakeSession:
    def __init__(self, *, init_delay=0.0, call_delay=0.0, aexit_raise=False):
        self.init_delay = init_delay
        self.call_delay = call_delay
        self.aexit_raise = aexit_raise
        self.init_count = 0
        self.call_count = 0
    async def __aenter__(self): return self
    async def __aexit__(self, *exc):
        if self.aexit_raise:
            raise RuntimeError("teardown boom")
        return False
    async def initialize(self):
        self.init_count += 1
        if self.init_delay:
            await asyncio.sleep(self.init_delay)
    async def list_tools(self):
        return types.SimpleNamespace(tools=[])
    async def call_tool(self, tool, args):
        self.call_count += 1
        if self.call_delay:
            await asyncio.sleep(self.call_delay)
        return types.SimpleNamespace(content=None)


class _FakeStdioCtx:
    async def __aenter__(self): return (None, None)
    async def __aexit__(self, *exc): return False


def _install_fake_mcp(monkeypatch, session: _FakeSession):
    # E2-26: these tests drive `call_mcp` against an in-memory fake MCP SDK —
    # `_FakeStdioCtx` forks nothing — so the `HYDRA_TEST_NO_DAEMONS` spawn
    # guard installed by tests/conftest.py must be lifted here, or every
    # transport assertion below would short-circuit to the disabled payload.
    # They are NOT `live_daemon` tests: no real daemon is ever contacted.
    monkeypatch.delenv("HYDRA_TEST_NO_DAEMONS", raising=False)
    mcp = types.ModuleType("mcp")
    mcp.ClientSession = lambda read, write: session
    mcp.StdioServerParameters = lambda **kw: types.SimpleNamespace(**kw)
    client = types.ModuleType("mcp.client")
    stdio = types.ModuleType("mcp.client.stdio")
    stdio.stdio_client = lambda params: _FakeStdioCtx()
    monkeypatch.setitem(sys.modules, "mcp", mcp)
    monkeypatch.setitem(sys.modules, "mcp.client", client)
    monkeypatch.setitem(sys.modules, "mcp.client.stdio", stdio)


def test_call_tool_timeout_returns_failed_exactly_once(monkeypatch, disp):
    monkeypatch.setenv("HYDRA_DISPATCH_TOOL_TIMEOUT_S", "0.2")
    sess = _FakeSession(call_delay=10.0)
    _install_fake_mcp(monkeypatch, sess)
    res = disp.call_mcp("pp_harness", "start_run", {})
    assert res["status"] == "failed"
    assert res.get("timeout") is True
    assert res.get("phase") == "call_tool"
    # The tool was attempted exactly once — a timed-out, possibly-side-effecting
    # call must NOT be retried.
    assert sess.call_count == 1


def test_connect_timeout_retries_and_never_calls_tool(monkeypatch, disp):
    monkeypatch.setenv("HYDRA_DISPATCH_CONNECT_TIMEOUT_S", "0.05")
    sess = _FakeSession(init_delay=5.0)
    _install_fake_mcp(monkeypatch, sess)
    res = disp.call_mcp("pp_harness", "start_run", {})
    assert res["status"] == "failed"
    # initialize is retried up to the connect-attempt ceiling (3); the tool is
    # never invoked because connect never completed.
    assert sess.init_count == 3
    assert sess.call_count == 0


def test_happy_path_returns_done(monkeypatch, disp):
    sess = _FakeSession()
    _install_fake_mcp(monkeypatch, sess)
    res = disp.call_mcp("pp_harness", "start_run", {})
    assert res["status"] == "done"
    assert sess.call_count == 1


def test_success_then_teardown_error_still_returns_done(monkeypatch, disp):
    sess = _FakeSession(aexit_raise=True)
    _install_fake_mcp(monkeypatch, sess)
    res = disp.call_mcp("pp_harness", "start_run", {})
    # call_tool succeeded; __aexit__ raising must NOT lose the captured result.
    assert res["status"] == "done"
    assert sess.call_count == 1


def test_wedged_teardown_hits_overall_deadline_backstop(monkeypatch, disp):
    # P1.3: the inner per-op timeouts cap connect / init / call_tool but NOT the
    # stdio __aexit__ teardown. A child that wedges on teardown (the observed
    # 45-min stall at dispatch) must be abandoned at the overall deadline
    # (tool_timeout + overhead), not freeze the stage loop.
    #
    # W2-1: this backstop is specific to the NON-pooled `_async_call` path
    # (every call tears down its stdio session). pp_harness moved to the
    # pooled path (_POOLED_SERVERS), whose session persists across calls and
    # so never exercises a per-call teardown on the happy path at all — see
    # test_pp_harness_calls_reuse_one_pooled_session below for that path's own
    # coverage, and test_pooled_session_drop_hits_teardown_deadline for its
    # equivalent wedged-teardown protection (on DROP, not every call). Use
    # pp_codex here — still non-pooled — so this test keeps validating the
    # P1.3 backstop it was written for. pp_codex is itself a _LONG_TOOL_SERVER
    # (every tool call gets the long timeout class), so override the LONG
    # timeout rather than the short one.
    monkeypatch.setenv("HYDRA_DISPATCH_LONG_TOOL_TIMEOUT_S", "0.1")
    monkeypatch.setenv("HYDRA_DISPATCH_OVERALL_OVERHEAD_S", "0.3")

    sess = _FakeSession()  # fast call_tool; the HANG is in stdio teardown below

    class _HangingTeardownStdioCtx:
        async def __aenter__(self):
            return (None, None)

        async def __aexit__(self, *exc):
            await asyncio.sleep(30)  # wedged child — teardown never returns
            return False

    mcp = types.ModuleType("mcp")
    mcp.ClientSession = lambda read, write: sess
    mcp.StdioServerParameters = lambda **kw: types.SimpleNamespace(**kw)
    client = types.ModuleType("mcp.client")
    stdio = types.ModuleType("mcp.client.stdio")
    stdio.stdio_client = lambda params: _HangingTeardownStdioCtx()
    # E2-26: in-memory fake MCP SDK — nothing is forked. Lift the
    # conftest spawn guard so `call_mcp` reaches the transport path.
    monkeypatch.delenv("HYDRA_TEST_NO_DAEMONS", raising=False)
    monkeypatch.setitem(sys.modules, "mcp", mcp)
    monkeypatch.setitem(sys.modules, "mcp.client", client)
    monkeypatch.setitem(sys.modules, "mcp.client.stdio", stdio)

    import time as _t
    t0 = _t.monotonic()
    res = disp.call_mcp("pp_codex", "generate", {})
    elapsed = _t.monotonic() - t0

    assert res["status"] == "failed"
    assert res.get("phase") == "overall"   # the backstop fired, not the inner call
    assert res.get("timeout") is True
    assert elapsed < 5.0                    # abandoned ~0.4s, not the 30s hang
    assert sess.call_count == 1            # the tool DID run before teardown wedged


def test_pp_harness_calls_reuse_one_pooled_session(monkeypatch, disp):
    # W2-1: pp_harness joined _POOLED_SERVERS so its daemon (new Database() +
    # applyMigrations() on every cold stdio connect) initializes once per
    # dispatcher lifetime instead of once per tool call. Mirrors
    # test_eights_calls_reuse_one_pooled_session.
    sess = _FakeSession()
    enters = {"stdio": 0}

    class _CountingStdioCtx:
        async def __aenter__(self):
            enters["stdio"] += 1
            return (None, None)

        async def __aexit__(self, *exc):
            return False

    mcp = types.ModuleType("mcp")
    mcp.ClientSession = lambda read, write: sess
    mcp.StdioServerParameters = lambda **kw: types.SimpleNamespace(**kw)
    client = types.ModuleType("mcp.client")
    stdio = types.ModuleType("mcp.client.stdio")
    stdio.stdio_client = lambda params: _CountingStdioCtx()
    # E2-26: in-memory fake MCP SDK — nothing is forked. Lift the
    # conftest spawn guard so `call_mcp` reaches the transport path.
    monkeypatch.delenv("HYDRA_TEST_NO_DAEMONS", raising=False)
    monkeypatch.setitem(sys.modules, "mcp", mcp)
    monkeypatch.setitem(sys.modules, "mcp.client", client)
    monkeypatch.setitem(sys.modules, "mcp.client.stdio", stdio)

    first = disp.call_mcp("pp_harness", "start_run", {})
    second = disp.call_mcp("pp_harness", "record_verdict", {})

    assert first["status"] == "done"
    assert second["status"] == "done"
    assert enters["stdio"] == 1            # ONE stdio connect, not two cold starts
    assert sess.init_count == 1
    assert sess.call_count == 2


def test_pooled_session_drop_hits_teardown_deadline(monkeypatch, disp):
    # W2-1: a pooled session is torn down on drop (call_tool timeout/error —
    # see _async_call_pooled/_drop_pooled_session). _close_partial_pool wraps
    # that teardown in the same overall-deadline reasoning P1.3 gave the
    # non-pooled path, so a wedged child does not hang the DROP path either.
    monkeypatch.setenv("HYDRA_DISPATCH_CONNECT_TIMEOUT_S", "0.2")
    monkeypatch.setenv("HYDRA_DISPATCH_TOOL_TIMEOUT_S", "0.1")

    sess = _FakeSession(call_delay=10.0)  # call_tool times out -> triggers drop

    class _HangingTeardownStdioCtx:
        async def __aenter__(self):
            return (None, None)

        async def __aexit__(self, *exc):
            await asyncio.sleep(30)  # wedged on drop's teardown
            return False

    mcp = types.ModuleType("mcp")
    mcp.ClientSession = lambda read, write: sess
    mcp.StdioServerParameters = lambda **kw: types.SimpleNamespace(**kw)
    client = types.ModuleType("mcp.client")
    stdio = types.ModuleType("mcp.client.stdio")
    stdio.stdio_client = lambda params: _HangingTeardownStdioCtx()
    # E2-26: in-memory fake MCP SDK — nothing is forked. Lift the
    # conftest spawn guard so `call_mcp` reaches the transport path.
    monkeypatch.delenv("HYDRA_TEST_NO_DAEMONS", raising=False)
    monkeypatch.setitem(sys.modules, "mcp", mcp)
    monkeypatch.setitem(sys.modules, "mcp.client", client)
    monkeypatch.setitem(sys.modules, "mcp.client.stdio", stdio)

    import time as _t
    t0 = _t.monotonic()
    res = disp.call_mcp("pp_harness", "start_run", {})
    elapsed = _t.monotonic() - t0

    assert res["status"] == "failed"
    assert res.get("timeout") is True
    assert elapsed < 5.0   # the drop's teardown wedge was abandoned, not hung 30s
    # The session was dropped (not left cached in a half-torn-down state).
    assert "pp_harness" not in disp._pooled_sessions


def test_eights_calls_reuse_one_pooled_session(monkeypatch, disp):
    sess = _FakeSession()
    enters = {"stdio": 0}
    disp._servers["eights"] = {"command": "noop", "args": []}

    class _CountingStdioCtx:
        async def __aenter__(self):
            enters["stdio"] += 1
            return (None, None)

        async def __aexit__(self, *exc):
            return False

    mcp = types.ModuleType("mcp")
    mcp.ClientSession = lambda read, write: sess
    mcp.StdioServerParameters = lambda **kw: types.SimpleNamespace(**kw)
    client = types.ModuleType("mcp.client")
    stdio = types.ModuleType("mcp.client.stdio")
    stdio.stdio_client = lambda params: _CountingStdioCtx()
    # E2-26: in-memory fake MCP SDK — nothing is forked. Lift the
    # conftest spawn guard so `call_mcp` reaches the transport path.
    monkeypatch.delenv("HYDRA_TEST_NO_DAEMONS", raising=False)
    monkeypatch.setitem(sys.modules, "mcp", mcp)
    monkeypatch.setitem(sys.modules, "mcp.client", client)
    monkeypatch.setitem(sys.modules, "mcp.client.stdio", stdio)

    first = disp.call_mcp("eights", "eights.constitution.attest", {})
    second = disp.call_mcp("eights", "eights.hydra.envelope.record", {})

    assert first["status"] == "done"
    assert second["status"] == "done"
    assert enters["stdio"] == 1
    assert sess.init_count == 1
    assert sess.call_count == 2


def test_eights_concurrent_connect_is_single_flight(monkeypatch, disp):
    sess = _FakeSession()
    enters = {"stdio": 0}
    disp._servers["eights"] = {"command": "noop", "args": []}

    class _CountingStdioCtx:
        async def __aenter__(self):
            enters["stdio"] += 1
            await asyncio.sleep(0)
            return (None, None)

        async def __aexit__(self, *exc):
            return False

    mcp = types.ModuleType("mcp")
    mcp.ClientSession = lambda read, write: sess
    mcp.StdioServerParameters = lambda **kw: types.SimpleNamespace(**kw)
    client = types.ModuleType("mcp.client")
    stdio = types.ModuleType("mcp.client.stdio")
    stdio.stdio_client = lambda params: _CountingStdioCtx()
    # E2-26: in-memory fake MCP SDK — nothing is forked. Lift the
    # conftest spawn guard so `call_mcp` reaches the transport path.
    monkeypatch.delenv("HYDRA_TEST_NO_DAEMONS", raising=False)
    monkeypatch.setitem(sys.modules, "mcp", mcp)
    monkeypatch.setitem(sys.modules, "mcp.client", client)
    monkeypatch.setitem(sys.modules, "mcp.client.stdio", stdio)

    async def _concurrent_connect():
        return await asyncio.gather(
            disp._get_or_connect_pooled_session("eights"),
            disp._get_or_connect_pooled_session("eights"),
        )

    first, second = disp._run(_concurrent_connect())

    assert first is second
    assert enters["stdio"] == 1
    assert sess.init_count == 1
