"""E2-11: `hydra.venom.cross_check` must gate against a populated registry.

`hydra_core.venom` keeps a process-local `_REGISTRY` that only
`load_cerberus_venoms()` fills, by scanning `squads/*/cerberus.yaml`. The
hydra_control MCP server never called it, so every venom-class capability
raised `VenomUnregistered` and was translated into `ok=True` — the governance
gate approved everything, including `shell.destructive`.

These tests pin the two halves of the fix:
  * an empty registry fails closed (no registry, no verdict);
  * a loaded registry actually refuses a destructive shell command, and still
    passes capabilities that are genuinely not venom-class.

Offline: no MCP transport, no subprocess, no network.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from mcp_servers.hydra_control import server as control_server
from hydra_core.venom import clear_registry, registered_venoms

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def clean_registry():
    clear_registry()
    yield
    clear_registry()


def _cross_check(args: dict) -> dict:
    return control_server._tool_handlers()["hydra.venom.cross_check"](args)


def test_empty_registry_fails_closed(clean_registry, tmp_path, monkeypatch):
    """No cerberus.yaml reachable → registry stays empty → refuse."""
    # A root with no `squads/` directory: load_cerberus_venoms() returns [].
    monkeypatch.setattr(control_server, "_HYDRA_ROOT", tmp_path)
    out = _cross_check({"capability": "shell.destructive",
                        "context": {"command": "rm -rf /"}})
    assert out["ok"] is False, out
    assert out["rationale"] == "venom registry not loaded — failing closed"
    assert registered_venoms() == []


def test_loaded_registry_refuses_destructive_shell(clean_registry, monkeypatch):
    """With the repo's cerberus.yaml loaded, a recursive delete is refused."""
    monkeypatch.setattr(control_server, "_HYDRA_ROOT", REPO_ROOT)
    out = _cross_check({"capability": "shell.destructive",
                        "context": {"command": "rm -rf /"}})
    assert out["ok"] is False, out
    # It must be refused by Cerberus, not waved through as unknown.
    assert "not in venom registry" not in out["rationale"]
    assert "refusal pattern" in out["rationale"], out
    # The lazy load populated the same set `hydra doctor` reports.
    names = {c.name for c in registered_venoms()}
    assert {"shell.destructive", "git.force_push", "deploy.production"} <= names


def test_unregistered_capability_still_passes(clean_registry, monkeypatch):
    """A non-venom capability passes — but only because the registry is real."""
    monkeypatch.setattr(control_server, "_HYDRA_ROOT", REPO_ROOT)
    out = _cross_check({"capability": "totally.not.a.venom.e2e11"})
    assert out["ok"] is True, out
    assert "not in venom registry" in out["rationale"]
    assert registered_venoms(), "registry must be non-empty for the pass to mean anything"


def test_ensure_venom_registry_is_idempotent(clean_registry, monkeypatch):
    monkeypatch.setattr(control_server, "_HYDRA_ROOT", REPO_ROOT)
    first = control_server._ensure_venom_registry()
    second = control_server._ensure_venom_registry()
    assert first > 0
    assert first == second, "re-priming must not duplicate registrations"
