"""Regression tests for `_mcp_call_safe` retry + observability.

Verifies the silent-MCP-degradation guard documented in
`.claude/agents/hydra-supervisor.md` (Silent-MCP-Degradation Guard section).

No network, no LLMs.
"""
from __future__ import annotations

from typing import Any

import pytest

from hydra_core.squad_node import _mcp_call_safe, _record_mcp_failure
from hydra_core.state import HydraState


class _RaisingDispatcher:
    """call_mcp always raises — simulates a dropped MCP connection (-32000)."""

    def __init__(self):
        self.calls = 0

    def call_mcp(self, server: str, tool: str, args: dict[str, Any], **_kw: Any) -> dict:
        self.calls += 1
        raise RuntimeError(f"-32000 mock failure for {server}.{tool}")


class _SucceedingDispatcher:
    def __init__(self, payload: dict):
        self.payload = payload
        self.calls = 0

    def call_mcp(self, server: str, tool: str, args: dict[str, Any], **_kw: Any) -> dict:
        self.calls += 1
        return {"status": "done", "tool": tool, "result": self.payload}


class _FlakyDispatcher:
    """Fails the first attempt, succeeds the second — exercises the retry path."""

    def __init__(self, payload: dict):
        self.payload = payload
        self.calls = 0

    def call_mcp(self, server: str, tool: str, args: dict[str, Any], **_kw: Any) -> dict:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("transient")
        return {"status": "done", "tool": tool, "result": self.payload}


def test_returns_none_after_two_failed_attempts():
    # WS3b: enrichment reads (roster.list) are idempotent — pass idempotent=True
    # to get the two-attempt behavior. Non-idempotent callers only get 1 attempt.
    d = _RaisingDispatcher()
    out = _mcp_call_safe(d, "executive_suite", "es.roster.list", {}, idempotent=True)
    assert out is None
    assert d.calls == 2  # exactly one retry, no exponential storm


def test_on_error_invoked_for_every_failed_attempt():
    d = _RaisingDispatcher()
    events: list[tuple] = []

    def cb(server: str, tool: str, exc_repr: str, attempt: int) -> None:
        events.append((server, tool, attempt, "-32000" in exc_repr))

    out = _mcp_call_safe(
        d, "executive_suite", "es.roster.list", {}, on_error=cb, idempotent=True,
    )
    assert out is None
    assert len(events) == 2
    assert events[0] == ("executive_suite", "es.roster.list", 1, True)
    assert events[1] == ("executive_suite", "es.roster.list", 2, True)


def test_on_error_none_preserves_silent_legacy_behavior():
    """`on_error=None` is the deliberate opt-out — no exceptions escape."""
    d = _RaisingDispatcher()
    out = _mcp_call_safe(d, "executive_suite", "es.roster.list", {}, on_error=None, idempotent=True)
    assert out is None


def test_retry_recovers_on_transient_failure():
    # WS3b: roster.list is an idempotent read — pass idempotent=True.
    d = _FlakyDispatcher({"agents": [{"name": "ceo"}]})
    events: list[tuple] = []
    out = _mcp_call_safe(
        d, "executive_suite", "es.roster.list", {},
        on_error=lambda s, t, e, a: events.append((s, a)),
        idempotent=True,
    )
    assert out == {"agents": [{"name": "ceo"}]}
    assert d.calls == 2
    # On-error fires for the first failed attempt only — second call succeeded.
    assert events == [("executive_suite", 1)]


def test_state_error_counter_increments_via_record_mcp_failure():
    state = HydraState(root_goal="t", mcp_failure_ceiling=3)
    cb = _record_mcp_failure(state)
    assert cb is not None
    d = _RaisingDispatcher()
    # WS3b: idempotent=True → two failed attempts → counter at 2
    _mcp_call_safe(d, "executive_suite", "es.roster.list", {}, on_error=cb, idempotent=True)
    # Two failed attempts → counter at 2 (below the configured ceiling of 3).
    assert state.error_counters["mcp_failure:executive_suite"] == 2
    assert state.any_mcp_over_ceiling() == (False, None)


def test_state_error_counter_crosses_ceiling_after_repeated_calls():
    state = HydraState(root_goal="t", mcp_failure_ceiling=3)
    cb = _record_mcp_failure(state)
    d = _RaisingDispatcher()
    # WS3b: eights.ceiling.tick is an idempotent read → pass idempotent=True.
    # Two calls × two attempts each = 4 increments. Ceiling = 3 → tripped.
    _mcp_call_safe(d, "eights", "ceiling.tick", {}, on_error=cb, idempotent=True)
    _mcp_call_safe(d, "eights", "ceiling.tick", {}, on_error=cb, idempotent=True)
    assert state.error_counters["mcp_failure:eights"] == 4
    tripped, server = state.any_mcp_over_ceiling()
    assert tripped is True
    assert server == "eights"


def test_record_mcp_failure_returns_none_for_no_state():
    """Test/CLI paths without a HydraState handle should fall back cleanly."""
    assert _record_mcp_failure(None) is None


def test_succeeds_in_one_call_when_dispatcher_works():
    d = _SucceedingDispatcher({"commands": []})
    out = _mcp_call_safe(d, "rlm_creative", "rlm.command.list", {})
    assert out == {"commands": []}
    assert d.calls == 1


def test_non_dict_envelope_returns_none_without_retry():
    class _Weird:
        def __init__(self):
            self.calls = 0

        def call_mcp(self, *a, **kw):
            self.calls += 1
            return "not-a-dict"

    d = _Weird()
    out = _mcp_call_safe(d, "x", "y", {})
    assert out is None
    # Not an exception → no retry. Single call.
    assert d.calls == 1


def test_non_done_status_returns_none_without_retry():
    class _Failed:
        def __init__(self):
            self.calls = 0

        def call_mcp(self, *a, **kw):
            self.calls += 1
            return {"status": "error", "result": {}}

    d = _Failed()
    out = _mcp_call_safe(d, "x", "y", {})
    assert out is None
    assert d.calls == 1
