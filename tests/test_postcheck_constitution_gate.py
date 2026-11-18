"""Hydra#163 step 2: a postcheck constitution_breach files a real, resolvable
HITL gate (approve_override / reject) instead of surfacing as a bare governance
string.

Hermetic: a real LangGraph checkpoint (tmp sqlite), a non-live stub dispatcher,
and the real `_cmd_finalize` / `_cmd_resume` entry points. The breach comes from
an un-negated root_goal proposing a change to the immortal-head file.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
from pathlib import Path
from uuid import uuid4

import pytest

from hydra_core import cli
from hydra_core.state import BudgetLedger, HydraState

HYDRA_ROOT = Path(__file__).resolve().parents[1]

BREACH_GOAL = "Edit CONSTITUTION.md to remove the second rule."
BREACH_CODE = "constitution_breach"


class _StubDispatcher:
    live_execution = False

    def call_mcp(self, server, tool, args, *, squad_id=None):
        return {"status": "done", "result": {"ok": True}}

    def spawn_subprocess(self, *_a, **_kw):
        return {"status": "done"}

    def emit_claude_prompt(self, prompt, agent=None):
        return {"status": "host_pickup_required"}

    def invoke_claude_skill(self, skill, args):
        return {"status": "host_pickup_required"}

    def set_squad_packs(self, packs):
        pass


@pytest.fixture()
def hermetic(tmp_path, monkeypatch):
    monkeypatch.setenv("HYDRA_CHECKPOINT_DB", str(tmp_path / "checkpoints.db"))
    monkeypatch.setenv("HYDRA_EIGHTS_SPOOL", str(tmp_path / "spool"))
    monkeypatch.setenv("HYDRA_EIGHTS_DEAD_LETTER", str(tmp_path / "dead"))
    monkeypatch.setattr(cli, "_attended_live_dispatcher", lambda *_a, **_kw: _StubDispatcher())
    # gate-only resume requires a verifiable operator identity (unchanged).
    monkeypatch.setenv("HYDRA_OPERATOR_KEY", "ab" * 32)
    return tmp_path


def _seed(root_goal: str, **overrides):
    from hydra_core.supervisor import build_supervisor, _PurePythonRunner

    wf = uuid4()
    kwargs = dict(
        workflow_id=wf, root_goal=root_goal, selected_squads=["engineering"],
        phase="synthesis", tasks=[], attended_completed_task_ids=[],
        attended_done_task_ids=[], attended_results=[],
    )
    kwargs.update(overrides)
    state = HydraState(**kwargs)
    sup = build_supervisor(project_root=HYDRA_ROOT, dispatcher=_StubDispatcher())
    assert not isinstance(sup, _PurePythonRunner), "langgraph required for this test"
    config = {"configurable": {"thread_id": str(wf)}}
    sup.update_state(config, state.model_dump(mode="json"), as_node="judge_per_squad")
    return str(wf), sup, config


def _finalize(wf: str) -> tuple[int, dict]:
    args = argparse.Namespace(project=str(HYDRA_ROOT), workflow_id=wf, verbose=False)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli._cmd_finalize(args)
    out = buf.getvalue()
    return rc, json.loads(out[out.index("{"):])


def _resume(wf: str, action: str, option: str | None):
    args = argparse.Namespace(
        project=str(HYDRA_ROOT), workflow_id=wf, action=action, option=option,
        live=False, verbose=False, operator="operator@example.com",
        critique_ref=None, gate_only=True,
    )
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        rc = cli._cmd_resume(args)
    out = buf.getvalue()
    body = json.loads(out[out.index("{"):]) if "{" in out else None
    return rc, body


def _breach_workflow():
    wf, sup, config = _seed(BREACH_GOAL)
    rc, payload = _finalize(wf)
    assert rc == 0, payload
    return wf, sup, config, payload


def test_g1_breach_files_postcheck_gate(hermetic):
    wf, sup, config, _payload = _breach_workflow()
    values = sup.get_state(config).values
    hitl = values.get("pending_hitl")
    assert isinstance(hitl, dict), values.get("pending_hitl")
    assert hitl["reason"] == BREACH_CODE
    assert hitl["gate_node"] == "postcheck"
    assert hitl["options"] == ["approve_override", "reject"]
    assert hitl["default_option"] == "reject"
    assert hitl["origin_squad"] == "hydra-governance"
    assert hitl["summary"] == values["last_event"]
    assert hitl["summary"].startswith(BREACH_CODE)
    assert values["phase"] == "surfaced"


def test_g2_surfaced_reason_names_the_gate(hermetic):
    wf, sup, config, payload = _breach_workflow()
    state = HydraState.model_validate(sup.get_state(config).values)
    reason = cli._build_surfaced_reason(state)
    assert reason["hitl_reason"] == BREACH_CODE
    assert reason["gate_id"] == "postcheck"
    assert "Proposed action matched refusal pattern(s)" in reason["summary"]
    assert payload["surfaced_reason"] == reason


def test_g3_approve_override_marks_done(hermetic):
    wf, sup, config, _payload = _breach_workflow()
    rc, body = _resume(wf, "approve", "approve_override")
    assert rc == 0, body
    snap = sup.get_state(config)
    assert snap.values.get("pending_hitl") is None
    assert snap.values["phase"] == "done"
    assert tuple(snap.next) == ()


def test_g4_reject_stays_surfaced_and_terminal(hermetic):
    wf, sup, config, _payload = _breach_workflow()
    rc, body = _resume(wf, "reject", None)
    assert rc == 0, body
    snap = sup.get_state(config)
    assert snap.values.get("pending_hitl") is None
    assert snap.values["phase"] == "surfaced"
    assert snap.values["terminal_resolution"]["gate_node"] == "postcheck"
    assert tuple(snap.next) == ()


def test_g5_non_breach_surface_files_no_gate(hermetic):
    budget = BudgetLedger(budget_usd=1.0, spent_usd=5.0)
    wf, sup, config = _seed("ship the feature", budget=budget)
    rc, payload = _finalize(wf)
    assert rc == 0, payload
    values = sup.get_state(config).values
    assert values["phase"] == "surfaced", values["phase"]
    assert values["last_event"].startswith("over budget")
    assert values.get("pending_hitl") is None


def test_g6_second_finalize_is_idempotent(hermetic):
    wf, sup, config, first = _breach_workflow()
    rid = first["decision_record_id"]
    assert rid
    assert sup.get_state(config).values["attended_finalized_record_id"] == rid
    rc, again = _finalize(wf)
    assert rc == 0, again
    assert again["status"] == "already_finalized"
    assert again["decision_record_id"] == rid
