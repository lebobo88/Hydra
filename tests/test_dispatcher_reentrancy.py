"""Regression: MCPStdioDispatcher._run must be safe to call whether or not an
event loop is already running on the current thread.

Before the re-entrancy fix, calling `_run` (via `call_mcp`) from inside a running
loop raised `RuntimeError: ... loop is already running`, leaking the built
coroutine un-awaited and surfacing as "pp_harness unreachable -> failed" — the
dispatch streak this test locks down.
"""
import asyncio

from hydra_core.dispatcher import MCPStdioDispatcher


def test_run_safe_without_loop(tmp_path):
    d = MCPStdioDispatcher(tmp_path)

    async def _coro():
        return 7

    # Fast path: no running loop on this thread.
    assert d._run(_coro()) == 7


def test_run_safe_under_running_loop(tmp_path):
    d = MCPStdioDispatcher(tmp_path)

    async def _inner():
        async def _coro():
            return 42
        # _run is invoked from INSIDE a running loop (mirrors call_mcp reached
        # from a compiled-LangGraph node / async caller). Pre-fix: RuntimeError.
        return d._run(_coro())

    assert asyncio.run(_inner()) == 42


def test_run_propagates_exception_under_running_loop(tmp_path):
    d = MCPStdioDispatcher(tmp_path)

    async def _inner():
        async def _boom():
            raise ValueError("kaboom")
        return d._run(_boom())

    import pytest
    with pytest.raises(ValueError, match="kaboom"):
        asyncio.run(_inner())
