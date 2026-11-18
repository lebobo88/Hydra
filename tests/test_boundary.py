"""Hermetic tests for hydra_core.boundary and the delegating call sites."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from hydra_core import boundary
from hydra_core.dispatcher import MCPStdioDispatcher

HYDRA_ROOT = Path(__file__).resolve().parents[1]


def _pack(*tools: Any) -> SimpleNamespace:
    return SimpleNamespace(entrypoint="mcp", tools=tuple(tools))


# ---- (a)/(b) strict denials ----

def test_strict_denies_unknown_squad() -> None:
    reason = boundary.check_tool_rbac(
        "srv", "tool", "no-such-squad", packs={}, handoffs=[], strict=True,
    )
    assert isinstance(reason, str) and reason.startswith("RBAC:")


def test_strict_denies_squad_id_none() -> None:
    reason = boundary.check_tool_rbac(
        "srv", "tool", None, packs={"x": _pack()}, handoffs=[], strict=True,
    )
    assert isinstance(reason, str) and reason.startswith("RBAC:")


# ---- (c) default-mode parity ----

def test_default_mode_allows_unknown_squad() -> None:
    assert boundary.check_tool_rbac(
        "srv", "tool", "no-such-squad", packs={}, handoffs=[], strict=False,
    ) is None


def test_default_mode_allows_squad_id_none() -> None:
    assert boundary.check_tool_rbac(
        "srv", "tool", None, packs={}, handoffs=[], strict=False,
    ) is None


# ---- (d) strict still authorizes declared / handoff-granted tools ----

def test_strict_authorizes_declared_tool_for_known_pack() -> None:
    packs = {"eng": _pack(SimpleNamespace(name="start_run", mcp_server="srv"))}
    assert boundary.check_tool_rbac(
        "srv", "start_run", "eng", packs=packs, handoffs=[], strict=True,
    ) is None
    denied = boundary.check_tool_rbac(
        "srv", "other", "eng", packs=packs, handoffs=[], strict=True,
    )
    assert isinstance(denied, str) and denied.startswith("RBAC:")


def test_strict_authorizes_tool_granted_by_unexpired_handoff() -> None:
    now = datetime.now(timezone.utc)
    packs = {"eng": _pack()}
    live = [{"squad_id": "eng", "expires_at": now + timedelta(hours=1),
             "granted_tools": ["srv.special"]}]
    expired = [{"squad_id": "eng", "expires_at": now - timedelta(hours=1),
                "granted_tools": ["srv.special"]}]
    assert boundary.check_tool_rbac(
        "srv", "special", "eng", packs=packs, handoffs=live, strict=True,
    ) is None
    denied = boundary.check_tool_rbac(
        "srv", "special", "eng", packs=packs, handoffs=expired, strict=True,
    )
    assert isinstance(denied, str) and denied.startswith("RBAC:")


# ---- (e) delegation from the dispatcher ----

def test_dispatcher_delegates_rbac_to_boundary(monkeypatch) -> None:
    sentinel = object()
    seen: dict[str, Any] = {}

    def fake(server, tool, squad_id, **kw):
        seen.update(server=server, tool=tool, squad_id=squad_id, **kw)
        return sentinel

    monkeypatch.setattr(boundary, "check_tool_rbac", fake)
    disp = MCPStdioDispatcher(project_root=HYDRA_ROOT)
    packs = {"eng": _pack()}
    disp.set_squad_packs(packs)
    assert disp._check_tool_rbac("s", "t", "eng") is sentinel
    assert seen["packs"] is disp._squad_packs
    assert seen["handoffs"] is disp._active_handoffs
    assert (seen["server"], seen["tool"], seen["squad_id"]) == ("s", "t", "eng")
    assert "strict" not in seen


def test_dispatcher_delegates_venom_gate_to_boundary(monkeypatch) -> None:
    sentinel = {"status": "rejected"}
    seen: dict[str, Any] = {}

    def fake(**kw):
        seen.update(kw)
        return sentinel

    monkeypatch.setattr(boundary, "venom_gate", fake)
    disp = MCPStdioDispatcher(project_root=HYDRA_ROOT)
    assert disp._venom_gate(server="s", tool="t", args={"a": 1}, cmd=None) is sentinel
    assert seen["call_mcp"] == disp.call_mcp
    assert (seen["server"], seen["tool"], seen["args"]) == ("s", "t", {"a": 1})


# ---- (f) gate-internal error is fail-closed ----

def test_venom_gate_internal_error_fails_closed(monkeypatch) -> None:
    import hydra_core.venom as venom

    def boom(**_kw):
        raise RuntimeError("audit store locked")

    monkeypatch.setattr(venom, "gate_runtime_action", boom)
    out = boundary.venom_gate(server="s", tool="t")
    assert out is not None
    assert out["status"] == "rejected"
    assert out["hitl_required"] is True
    assert out["gate_error"] is True
    assert "audit store locked" in out["error"]


# ---- redaction helpers ----

def test_redact_envelope_text_fields_only_touches_strings() -> None:
    env = {"objective": "abc", "summary": 5, "other": "keep"}
    out = boundary.redact_envelope_text_fields(env, redact_fn=lambda s: s.upper())
    assert out == {"objective": "ABC", "summary": 5, "other": "keep"}
    assert out is not env and env["objective"] == "abc"


def test_validate_and_redact_failure_emits_and_reraises() -> None:
    events: list[tuple] = []

    def bad(_e):
        raise ValueError("nope")

    with pytest.raises(ValueError):
        boundary.validate_and_redact_envelope(
            {"id": "e1", "type": "T"}, direction="d", squad_id="s",
            trace_root=Path("."), validate_fn=bad, redact_fn=str,
            emit_fn=lambda *a: events.append(a),
        )
    assert events[0][1:3] == ("boundary", "envelope_validation_failed")
    assert events[0][3]["error"] == "nope"


# ---- (g) supervisor synthesis delegates to boundary ----

class _Disp:
    def call_mcp(self, server, tool, args, *, squad_id=None):
        return {"status": "done", "result": {"ok": True}}

    def spawn_subprocess(self, *_a, **_k):
        return {"status": "done"}

    def emit_claude_prompt(self, prompt, agent=None):
        return {"status": "host_pickup_required"}

    def invoke_claude_skill(self, skill, args):
        return {"status": "host_pickup_required"}

    def set_squad_packs(self, packs):
        pass


class _Critique:
    def critique(self, *, vendor, artifact_text, rubric_md):
        return {"outcome": "pass", "critique_md": "good " * 30,
                "score_json": {"correctness": 9}}


def test_supervisor_synthesis_calls_boundary_validate_and_redact(monkeypatch) -> None:
    monkeypatch.setattr("hydra_core.squad_node.harvest_pp_run_artifacts",
                        lambda **_k: None)
    calls: list[dict[str, Any]] = []
    orig = boundary.validate_and_redact_envelope

    def spy(env, **kw):
        calls.append({"env": env, **kw})
        return orig(env, **kw)

    monkeypatch.setattr(boundary, "validate_and_redact_envelope", spy)

    from hydra_core.state import HydraState
    from hydra_core.supervisor import _PurePythonRunner, build_supervisor

    runner = build_supervisor(
        project_root=HYDRA_ROOT, dispatcher=_Disp(), critique_client=_Critique(),
        force_pure_python=True, force_trivial_plan_rigor=True,
    )
    state = HydraState(
        workflow_id=uuid4(), root_goal="boundary delegation",
        selected_squads=["healthcare"],
        envelopes=[{"id": str(uuid4()), "type": "DECISION_RECORD",
                    "origin_squad": "healthcare", "summary": "x"}],
    )
    assert isinstance(runner, _PurePythonRunner)
    runner.invoke(state)
    assert any(c.get("direction") == "synthesis_merge" for c in calls), calls
