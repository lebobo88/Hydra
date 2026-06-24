"""Phase-4 governance: Cerberus venom gate is now INVOKED at execution time
(F11). RBAC governs which tool a squad may call; the venom gate governs what the
ARGS do — rm -rf, force-push to main, prod deploy, card charge."""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from hydra_core import venom
from hydra_core.venom import (
    register_venom, clear_registry, gate_runtime_action,
    classify_runtime_venoms, VenomRefused,
)
from hydra_core.dispatcher import MCPStdioDispatcher


def _sink(_record):  # custom audit sink — no episodic DB writes in tests
    return "audit-test-key"


@pytest.fixture(autouse=True)
def _clean_registry():
    clear_registry()
    yield
    clear_registry()


def _register_shell():
    register_venom(
        "shell.destructive", owner_squad="security-reviewer",
        refusal_patterns=[r"rm\s+-rf\s+/", r"\bdd\s+if="],
        requires_human=True, audit_sink=_sink,
    )


def _register_force_push():
    register_venom(
        "git.force_push", owner_squad="security-reviewer",
        refusal_patterns=[r"push\s+(?:--force|-f)\b.*\b(main|master)\b"],
        requires_human=True, audit_sink=_sink,
    )


# --------------------------------------------------------------------------- #
# classifier + gate
# --------------------------------------------------------------------------- #
def test_unregistered_signature_is_not_gated():
    # No venoms registered → classifier returns nothing (registry is the opt-in).
    assert classify_runtime_venoms("rm -rf / now") == []
    assert gate_runtime_action(cmd=["rm", "-rf", "/"]) == []


def test_hard_refusal_on_destructive_shell():
    _register_shell()
    with pytest.raises(VenomRefused) as ei:
        gate_runtime_action(cmd=["rm", "-rf", "/"])
    assert ei.value.capability == "shell.destructive"


def test_any_force_push_is_blocked_pending_review():
    _register_force_push()
    # The constitution's `unguarded_venom` refusal matches the `push --force` arg
    # token itself → every force-push is BLOCKED pending HITL review (raises
    # VenomRefused), feature branch or not. The human approves or denies.
    with pytest.raises(VenomRefused):
        gate_runtime_action(cmd=["git", "push", "--force", "origin", "feature-x"])
    with pytest.raises(VenomRefused):
        gate_runtime_action(cmd=["git", "push", "--force", "origin", "main"])


# --------------------------------------------------------------------------- #
# dispatcher wiring
# --------------------------------------------------------------------------- #
def _dispatcher() -> MCPStdioDispatcher:
    return MCPStdioDispatcher(Path(tempfile.mkdtemp()))


def test_spawn_subprocess_blocks_destructive_without_running():
    _register_shell()
    d = _dispatcher()
    out = d.spawn_subprocess(["rm", "-rf", "/"])
    assert out["status"] == "rejected"
    assert out.get("venom_refused") is True
    assert out["capability"] == "shell.destructive"


def test_venom_gate_returns_hitl_block_for_force_push():
    _register_force_push()
    d = _dispatcher()
    block = d._venom_gate(cmd=["git", "push", "--force", "origin", "feature"])
    assert block is not None
    assert block.get("hitl_required") is True
    assert block.get("venom_refused") is True
    assert block.get("capability") == "git.force_push"


def test_clean_call_mcp_args_not_gated():
    _register_shell()
    d = _dispatcher()
    # Benign args → no venom signature → gate returns None (call proceeds).
    assert d._venom_gate(server="pp_harness", tool="start_run",
                         args={"request_text": "add a button", "mode": "single"}) is None
