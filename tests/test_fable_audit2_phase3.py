"""Tests for fable-audit-2 Phase 3 governance-federation findings.

Covers the uncommitted feature slice:

  F35  — optional AgentSmith cross-check inside the Cerberus venom gate
         (``HYDRA_VENOM_CROSS_CHECK=1``); fail-OPEN on transport error.
  F36  — procedural-update risk-class gating in ``procedural.approve``:
         low commits locally, medium fails SOFT (stays pending) without an
         eights verdict, high fails CLOSED (rejected) without one.
  M7   — ``validate_envelope`` passes the opaque internal ``COCKPIT_WRITE``
         type (non-UUID workflow_id) without raising, but still rejects
         genuinely unknown types.
  F32-H — the four governance-federation MCP handlers on hydra_control
         (venom.cross_check + squad.list smoke).

All units are pure-Python — no network, no LLM (AGENTS.md test rule).
"""
from __future__ import annotations

import pytest

from hydra_core.procedural import (
    InMemoryStore,
    ProceduralUpdate,
    approve,
)
from hydra_core.schemas import validate_envelope
from hydra_core.venom import (
    clear_registry,
    register_venom,
    require_cerberus_pass,
)


# ===========================================================================
# F35 — venom AgentSmith cross-check
# ===========================================================================

@pytest.fixture()
def _clean_registry():
    clear_registry()
    yield
    clear_registry()


_BENIGN_ARGS = {"message": "hello world"}


def test_cross_check_refusal_blocks_when_enabled(_clean_registry, monkeypatch):
    """With the toggle on, a cross-check ok=False turns a local allow into a block."""
    monkeypatch.setenv("HYDRA_VENOM_CROSS_CHECK", "1")
    register_venom("echo.benign", owner_squad="security-reviewer", refusal_patterns=[])

    def _xc(cap, args):
        return {"ok": False, "rationale": "smith says no"}

    verdict = require_cerberus_pass(
        "echo.benign", args=_BENIGN_ARGS,
        raise_on_refuse=False, cross_check_fn=_xc,
    )
    assert verdict.allowed is False
    assert any("agentsmith cross-check: smith says no" in r for r in verdict.refusal_reasons)


def test_cross_check_fails_open_on_exception(_clean_registry, monkeypatch):
    """A transport error from AgentSmith must never flip a local allow to a block."""
    monkeypatch.setenv("HYDRA_VENOM_CROSS_CHECK", "1")
    register_venom("echo.benign", owner_squad="security-reviewer", refusal_patterns=[])

    def _xc(cap, args):
        raise RuntimeError("agentsmith transport down")

    verdict = require_cerberus_pass(
        "echo.benign", args=_BENIGN_ARGS,
        raise_on_refuse=False, cross_check_fn=_xc,
    )
    assert verdict.allowed is True


def test_cross_check_ok_true_does_not_block(_clean_registry, monkeypatch):
    monkeypatch.setenv("HYDRA_VENOM_CROSS_CHECK", "1")
    register_venom("echo.benign", owner_squad="security-reviewer", refusal_patterns=[])

    verdict = require_cerberus_pass(
        "echo.benign", args=_BENIGN_ARGS,
        raise_on_refuse=False, cross_check_fn=lambda c, a: {"ok": True, "rationale": "ok"},
    )
    assert verdict.allowed is True


def test_cross_check_ignored_when_env_unset(_clean_registry, monkeypatch):
    """With the toggle off, the cross-check hook is never consulted."""
    monkeypatch.delenv("HYDRA_VENOM_CROSS_CHECK", raising=False)
    register_venom("echo.benign", owner_squad="security-reviewer", refusal_patterns=[])

    called = {"n": 0}

    def _xc(cap, args):
        called["n"] += 1
        return {"ok": False, "rationale": "should be ignored"}

    verdict = require_cerberus_pass(
        "echo.benign", args=_BENIGN_ARGS,
        raise_on_refuse=False, cross_check_fn=_xc,
    )
    assert verdict.allowed is True
    assert called["n"] == 0


# ===========================================================================
# F36 — procedural risk-class gating in approve()
# ===========================================================================

class _FakeAttestor:
    """Minimal eights evolution stub matching approve()'s call shape."""

    def __init__(self, propose_return=None, *, raise_on_propose=False):
        self.propose_return = propose_return
        self.raise_on_propose = raise_on_propose
        self.registered: list[str] = []
        self.proposed: list[str] = []

    def evolution_register(self, *, resource_kind, resource_id, body, summary=""):
        self.registered.append(resource_id)
        return {"ok": True}

    def evolution_propose(self, *, resource_id, summary, body, proposed_by="", workflow_id=None):
        if self.raise_on_propose:
            raise RuntimeError("eights transport down")
        self.proposed.append(resource_id)
        return self.propose_return


def _queued(kind: str) -> tuple[InMemoryStore, ProceduralUpdate]:
    store = InMemoryStore()
    u = ProceduralUpdate(kind=kind, summary="s", body="b", status="pending")
    store.put(u)
    return store, u


def test_low_risk_commits_locally_without_attestor():
    store, u = _queued("routing_heuristic")
    out = approve(u.id, store=store)
    assert out is not None and out.status == "committed"
    assert store.get(u.id).status == "committed"
    assert out.decided_by == "user"


def test_memory_pruning_is_low_risk_and_commits():
    store, u = _queued("memory_pruning")
    out = approve(u.id, store=store)
    assert out.status == "committed"


def test_medium_risk_no_attestor_stays_pending_failsoft():
    store, u = _queued("prompt_rewrite")
    out = approve(u.id, store=store, attestor=None)
    assert out.status == "pending"
    assert store.get(u.id).status == "pending"
    assert "fail-soft" in out.rationale.lower()


def test_high_risk_no_attestor_fails_closed():
    store, u = _queued("policy_adjustment")
    out = approve(u.id, store=store, attestor=None)
    assert out.status == "rejected"
    assert out.decided_by == "governance.fail_closed"


def test_medium_risk_attestor_approved_commits():
    store, u = _queued("prompt_rewrite")
    att = _FakeAttestor(propose_return={"status": "approved"})
    out = approve(u.id, store=store, attestor=att)
    assert out.status == "committed"
    assert att.registered and att.proposed


def test_high_risk_attestor_committed_commits():
    store, u = _queued("deprecation_proposal")
    att = _FakeAttestor(propose_return={"status": "committed"})
    out = approve(u.id, store=store, attestor=att)
    assert out.status == "committed"


def test_high_risk_attestor_transport_error_fails_closed():
    store, u = _queued("policy_adjustment")
    att = _FakeAttestor(raise_on_propose=True)
    out = approve(u.id, store=store, attestor=att)
    assert out.status == "rejected"
    assert out.decided_by == "governance.fail_closed"


def test_medium_risk_attestor_unavailable_stays_pending():
    """Medium risk + eights returns no verdict → fail-soft (pending), not rejected."""
    store, u = _queued("prompt_rewrite")
    att = _FakeAttestor(propose_return=None)
    out = approve(u.id, store=store, attestor=att)
    assert out.status == "pending"


# ===========================================================================
# M7 — validate_envelope opaque COCKPIT_WRITE passthrough
# ===========================================================================

def test_cockpit_write_opaque_type_passes():
    env = validate_envelope({
        "type": "COCKPIT_WRITE",
        "workflow_id": "cockpit-not-a-uuid",
        "origin_squad": "hydra-cockpit",
    })
    assert env.type == "COCKPIT_WRITE"
    assert env.origin_squad == "hydra-cockpit"


def test_cockpit_write_missing_workflow_id_still_passes():
    env = validate_envelope({"type": "COCKPIT_WRITE"})
    assert env.type == "COCKPIT_WRITE"


def test_genuinely_unknown_type_still_raises():
    with pytest.raises(ValueError):
        validate_envelope({"type": "TOTALLY_UNKNOWN_TYPE"})


# ===========================================================================
# F32-H — hydra_control governance-federation handlers
# ===========================================================================

def _control_handlers():
    from mcp_servers.hydra_control.server import _tool_handlers
    return _tool_handlers()


def test_venom_cross_check_handler_requires_capability():
    h = _control_handlers()
    out = h["hydra.venom.cross_check"]({})
    assert out["ok"] is False
    assert "capability" in out["rationale"].lower()


def test_venom_cross_check_handler_unregistered_is_pass(_clean_registry):
    """An unknown capability is not a venom-class action → pass."""
    h = _control_handlers()
    out = h["hydra.venom.cross_check"]({"capability": "totally.not.registered"})
    assert out["ok"] is True


def test_squad_list_handler_returns_squads():
    h = _control_handlers()
    out = h["hydra.squad.list"]({})
    assert out["ok"] is True
    assert isinstance(out["squads"], list) and out["squads"]
    first = out["squads"][0]
    for key in ("slug", "name", "entrypoint", "active", "version"):
        assert key in first
