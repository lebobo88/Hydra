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
    ("pp_gemini", "critique"),
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
