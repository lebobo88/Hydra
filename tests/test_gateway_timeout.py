"""Unit tests for the Hydra gateway's per-tool-class timeout resolver.

Covers the fix for the flat 120s ceiling that truncated cross-vendor
codex/gemini judge & generate calls on large artifacts. No network / no LLM:
exercises `AsyncBackendPool._resolve_tool_timeout` and the two parsing helpers
directly.
"""
from __future__ import annotations

import pytest

from mcp_servers.hydra_gateway import server as gw


@pytest.fixture()
def pool() -> gw.AsyncBackendPool:
    # Empty spec set — the resolver never touches backends.
    return gw.AsyncBackendPool({})


# ─── classification ────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    ("server", "tool"),
    [
        ("pp_codex", "critique"),
        ("pp_codex", "generate"),
        ("pp_gemini", "critique"),
        ("pp_gemini", "generate"),
        ("pp_harness", "best_of"),
        # final-segment classification: a dotted tool still resolves by its tail
        ("some_backend", "foo.generate"),
        ("some_backend", "deep.nested.critique"),
    ],
)
def test_llm_tools_get_long_timeout(pool: gw.AsyncBackendPool, server: str, tool: str) -> None:
    assert pool._resolve_tool_timeout(server, tool, {}) == 1800.0


@pytest.mark.parametrize(
    "tool",
    [
        # hydra_control control-plane tools run a synchronous CLI subprocess whose
        # own cap (plan 180s / step 300s / submit 900s) must fire BEFORE the
        # gateway's, else the gateway returns a hard `failed` + tears the backend
        # down (the observed 4/4 planner timeouts). They resolve by final segment.
        "hydra.workflow.plan",
        "hydra.workflow.step",
        "hydra.workflow.submit_host_result",
        "hydra.workflow.submit_envelopes",
        "hydra.workflow.launch",
        "hydra.workflow.resume",
    ],
)
def test_control_plane_tools_get_long_timeout(pool: gw.AsyncBackendPool, tool: str) -> None:
    # Gateway cap (1800s ceiling) must exceed hydra_control's max subprocess cap
    # (submit=900s) so the subprocess's clean in-band timeout wins, not the
    # gateway's hard teardown.
    assert pool._resolve_tool_timeout("hydra_control", tool, {}) == 1800.0


@pytest.mark.parametrize(
    ("server", "tool"),
    [
        ("eights", "list"),
        ("executive_suite", "roster.get"),
        ("hydra_memory", "memory_search"),
        ("agentsmith", "ping"),
        ("pp_harness", "start_run"),  # not an LLM call → short cap
    ],
)
def test_ordinary_tools_get_short_timeout(pool: gw.AsyncBackendPool, server: str, tool: str) -> None:
    assert pool._resolve_tool_timeout(server, tool, {}) == 120.0


# ─── env overrides (read live, no reimport needed) ─────────────────────────

def test_long_timeout_env_override(monkeypatch: pytest.MonkeyPatch, pool: gw.AsyncBackendPool) -> None:
    monkeypatch.setenv("HYDRA_GATEWAY_LONG_TOOL_TIMEOUT_S", "600")
    assert pool._resolve_tool_timeout("pp_codex", "critique", {}) == 600.0


def test_short_timeout_env_override(monkeypatch: pytest.MonkeyPatch, pool: gw.AsyncBackendPool) -> None:
    monkeypatch.setenv("HYDRA_GATEWAY_TOOL_TIMEOUT_S", "45")
    assert pool._resolve_tool_timeout("eights", "list", {}) == 45.0


def test_invalid_env_override_falls_back_to_default(monkeypatch: pytest.MonkeyPatch, pool: gw.AsyncBackendPool) -> None:
    # A typo / zero must NOT disable the timeout — fall back to the default.
    for bad in ("0", "-5", "not-a-number", "nan"):
        monkeypatch.setenv("HYDRA_GATEWAY_LONG_TOOL_TIMEOUT_S", bad)
        assert pool._resolve_tool_timeout("pp_codex", "critique", {}) == 1800.0


# ─── per-call override parsing (defensive) ─────────────────────────────────

@pytest.mark.parametrize(
    ("args", "expected"),
    [
        ({"timeout_ms": 2_400_000}, 2400.0),     # int ms
        ({"timeout_ms": "2400000"}, 2400.0),     # numeric-string ms (coercion left it a string)
        ({"timeout_ms": 2_400_000.0}, 2400.0),   # float ms
        ({"timeout_s": 2400}, 2400.0),           # int seconds
        ({"timeout_s": "2400"}, 2400.0),         # numeric-string seconds
    ],
)
def test_per_call_override_raises_base(pool: gw.AsyncBackendPool, args: dict, expected: float) -> None:
    assert pool._resolve_tool_timeout("pp_codex", "critique", args) == expected


def test_override_raises_short_tool_above_its_class(pool: gw.AsyncBackendPool) -> None:
    # An ordinary tool (120s class) can still be extended by an explicit caller value.
    assert pool._resolve_tool_timeout("eights", "list", {"timeout_s": 900}) == 900.0


def test_override_only_raises_never_lowers(pool: gw.AsyncBackendPool) -> None:
    # A small caller value must not shrink the long class default below it.
    assert pool._resolve_tool_timeout("pp_codex", "critique", {"timeout_ms": 1000}) == 1800.0


@pytest.mark.parametrize(
    "args",
    [
        {"timeout_ms": "abc"},          # junk string
        {"timeout_ms": True},           # bool is not a number here
        {"timeout_ms": None},           # explicit null
        {"timeout_ms": 0},              # non-positive
        {"timeout_ms": -10},            # negative
        {"timeout_ms": ["nope"]},       # wrong type
    ],
)
def test_junk_override_is_ignored(pool: gw.AsyncBackendPool, args: dict) -> None:
    assert pool._resolve_tool_timeout("pp_codex", "critique", args) == 1800.0


def test_timeout_ms_preferred_but_falls_back_to_timeout_s(pool: gw.AsyncBackendPool) -> None:
    # junk timeout_ms → fall back to a valid timeout_s
    assert pool._resolve_tool_timeout(
        "pp_codex", "critique", {"timeout_ms": "junk", "timeout_s": 2400}
    ) == 2400.0


# ─── hard-max clamp ────────────────────────────────────────────────────────

def test_override_clamped_to_max_cap(pool: gw.AsyncBackendPool) -> None:
    assert pool._resolve_tool_timeout(
        "pp_codex", "critique", {"timeout_ms": 99_999_999_999}
    ) == 3600.0


def test_max_cap_env_override(monkeypatch: pytest.MonkeyPatch, pool: gw.AsyncBackendPool) -> None:
    monkeypatch.setenv("HYDRA_GATEWAY_MAX_TOOL_TIMEOUT_S", "7200")
    assert pool._resolve_tool_timeout(
        "pp_codex", "critique", {"timeout_s": 100_000}
    ) == 7200.0


def test_undersized_max_cap_never_drops_below_short_floor(
    monkeypatch: pytest.MonkeyPatch, pool: gw.AsyncBackendPool
) -> None:
    # A misconfigured max below the short default must NOT regress ordinary
    # tools below their 120s class cap (hard_max is normalized up to short).
    monkeypatch.setenv("HYDRA_GATEWAY_MAX_TOOL_TIMEOUT_S", "30")
    assert pool._resolve_tool_timeout("eights", "list", {}) == 120.0
    # An LLM tool with an undersized max is also floored at short, not 30.
    assert pool._resolve_tool_timeout("pp_codex", "critique", {}) == 120.0


# ─── helper-level coverage ─────────────────────────────────────────────────

@pytest.mark.parametrize(
    ("value", "ms", "expected"),
    [
        (5000, True, 5.0),
        (5, False, 5.0),
        ("5000", True, 5.0),
        (" 5000 ", True, 5.0),
        (True, True, None),
        (None, False, None),
        ("junk", False, None),
        (0, True, None),
        (-1, False, None),
    ],
)
def test_coerce_timeout_seconds(value: object, ms: bool, expected: float | None) -> None:
    assert gw._coerce_timeout_seconds(value, milliseconds=ms) == expected


def test_env_float_rejects_bad_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("X_TIMEOUT", "abc")
    assert gw._env_float("X_TIMEOUT", 120.0) == 120.0
    monkeypatch.setenv("X_TIMEOUT", "0")
    assert gw._env_float("X_TIMEOUT", 120.0) == 120.0
    monkeypatch.setenv("X_TIMEOUT", "300")
    assert gw._env_float("X_TIMEOUT", 120.0) == 300.0
    monkeypatch.delenv("X_TIMEOUT", raising=False)
    assert gw._env_float("X_TIMEOUT", 120.0) == 120.0
