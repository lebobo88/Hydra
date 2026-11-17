"""Gateway caps for hydra_control verbs that outlive the 120 s short default.

Hermetic: exercises `AsyncBackendPool._resolve_tool_timeout` and the shared
`hydra_core.control_plane_timeouts` module only.
"""
from __future__ import annotations

import pytest

from hydra_core import control_plane_timeouts as cpt
from mcp_servers.hydra_control import server as hc
from mcp_servers.hydra_gateway import server as gw


@pytest.fixture()
def pool(monkeypatch: pytest.MonkeyPatch) -> gw.AsyncBackendPool:
    for name in (
        "HYDRA_GATEWAY_TOOL_TIMEOUT_S",
        "HYDRA_GATEWAY_LONG_TOOL_TIMEOUT_S",
        "HYDRA_GATEWAY_MAX_TOOL_TIMEOUT_S",
        "HYDRA_FINALIZE_TIMEOUT_S",
    ):
        monkeypatch.delenv(name, raising=False)
    return gw.AsyncBackendPool({})


def test_finalize_gateway_cap_exceeds_hydra_control_cap(pool: gw.AsyncBackendPool) -> None:
    resolved = pool._resolve_tool_timeout("hydra_control", "hydra.workflow.finalize", {})
    assert resolved > hc._FINALIZE_TIMEOUT_S, resolved
    assert resolved >= hc._FINALIZE_TIMEOUT_S + 60, resolved
    assert resolved > 120, resolved


@pytest.mark.parametrize(
    "tool", ["hydra.repo.resolve", "hydra.cockpit.audit", "hydra.envelope.record"]
)
def test_in_process_verbs_exceed_short_default(pool: gw.AsyncBackendPool, tool: str) -> None:
    assert pool._resolve_tool_timeout("hydra_control", tool, {}) > 120.0


def test_finalize_tracks_shared_cap(
    pool: gw.AsyncBackendPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HYDRA_FINALIZE_TIMEOUT_S", "3200")
    resolved = pool._resolve_tool_timeout("hydra_control", "hydra.workflow.finalize", {})
    assert resolved >= 3260, resolved


@pytest.mark.parametrize(
    ("server", "tool"),
    [
        ("eights", "list"),
        ("hydra_control", "hydra.repo.list"),
        ("hydra_control", "hydra.telemetry.tail"),
        ("some_backend", "foo.resolve"),
    ],
)
def test_short_tools_stay_short(pool: gw.AsyncBackendPool, server: str, tool: str) -> None:
    assert pool._resolve_tool_timeout(server, tool, {}) == 120.0


def test_shared_module_matches_hydra_control(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HYDRA_FINALIZE_TIMEOUT_S", raising=False)
    assert hc._FINALIZE_TIMEOUT_S == cpt.FINALIZE_TIMEOUT_DEFAULT_S
    names = set(hc._TOOL_SCHEMAS) | set(hc.HYDRA_CONTROL_TOOL_SPECS)
    missing = cpt.HYDRA_CONTROL_LONG_VERBS - names
    assert not missing, missing


@pytest.mark.parametrize("raw", [None, "", "abc", "0", "-5", "nan"])
def test_finalize_timeout_s_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch, raw: str | None
) -> None:
    if raw is None:
        monkeypatch.delenv("HYDRA_FINALIZE_TIMEOUT_S", raising=False)
    else:
        monkeypatch.setenv("HYDRA_FINALIZE_TIMEOUT_S", raw)
    assert cpt.finalize_timeout_s() == 600


def test_finalize_timeout_s_reads_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HYDRA_FINALIZE_TIMEOUT_S", "900")
    assert cpt.finalize_timeout_s() == 900.0
