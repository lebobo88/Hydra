"""E2-32: attended mode must have a route for stub squads and for workflows
that need no approval, and the approval decision must not flip on budget == 0.

Three regressions, one finding:

1. `_next_engineering_task` accepts only engineering and
   `_next_nonengineering_attended_task` only claude-skill / claude-native /
   agent-impersonation, so a task owned by a `stub`-entrypoint squad was
   invisible to `hydra attended step` -- the workflow reported
   `no_pending_task` forever and never produced the `[STUB]` DecisionRecord
   that `tests/test_stub_surface_ra5.py` pins for the headless path.
2. `hydra plan` parks every workflow at the plan_only `dispatch` interrupt.
   A gated workflow leaves it via `/hydra:approve`; a workflow with
   `requires_human_approval=False` had no caller for that pass at all.
3. `requires_human_approval` was `high_risk or is_over_budget()`, and
   `is_over_budget()` is `spent >= budget` -- trivially True at budget 0.0.
   The identical goal/squad/risk therefore required approval at --budget 0 and
   did not at --budget 0.1.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from uuid import uuid4

import pytest

from hydra_core import cli
from hydra_core.state import HydraState, TaskState

HYDRA_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------

class _StubPack:
    slug = "healthcare"
    name = "Healthcare"
    entrypoint = "stub"
    agents: list = []
    gates: list = []


class _SkillPack:
    class _Agent:
        slug = "boardroom"
        authority = "gatekeeper"

    slug = "executive"
    name = "Executive"
    entrypoint = "agent-impersonation"
    agents = [_Agent()]
    gates: list = []


class _NopDispatcher:
    live_execution = True

    def call_mcp(self, server, tool, args, **_kw):
        return {"status": "done", "result": {}}

    def set_squad_packs(self, packs):
        pass


class _FakeSup:
    """Minimal checkpointer stand-in: one mutable state dict, LangGraph-shaped
    `next` tuple, and an `envelopes` channel with the real append semantics."""

    def __init__(self, state: HydraState, next_nodes: tuple[str, ...] = ()):
        self.values = state.model_dump(mode="json")
        self._next = next_nodes
        self.updates: list[dict] = []
        self.invoked = 0

    # -- checkpoint surface --------------------------------------------------
    def get_state(self, config):
        outer = self

        class _Snap:
            values = outer.values
            next = outer._next

        return _Snap()

    def update_state(self, config, values):
        self.updates.append(dict(values))
        for key, val in values.items():
            if key == "envelopes":            # append reducer
                self.values.setdefault("envelopes", []).extend(val)
            else:                             # replace channel
                self.values[key] = val

    def invoke(self, _inp, config=None):
        self.invoked += 1
        self._next = ()
        self.values["phase"] = "executing"
        return self.values


def _install(monkeypatch, sup, packs):
    monkeypatch.setattr("hydra_core.cli._attended_live_dispatcher",
                        lambda *a, **k: _NopDispatcher())
    monkeypatch.setattr("hydra_core.supervisor.build_supervisor",
                        lambda **k: sup)
    monkeypatch.setattr("hydra_core.squad_loader.discover_squads",
                        lambda *a, **k: packs)


def _step(tmp_path, wf_id, capsys):
    rc = cli._cmd_attended_step(argparse.Namespace(
        project=str(tmp_path), workflow_id=wf_id, verbose=False))
    out = capsys.readouterr().out
    assert rc == 0, f"attended step failed (rc={rc}): {out[:400]}"
    return json.loads(out)


# ---------------------------------------------------------------------------
# 1. stub-only attended workflow
# ---------------------------------------------------------------------------

def test_attended_step_drives_stub_squad_in_process(tmp_path, monkeypatch, capsys):
    """A stub-only workflow yields `stub_surfaced` with a [STUB]
    DecisionRecord, and the task is marked attended-complete."""
    task = TaskState(owner_squad="healthcare", description="assess readiness")
    state = HydraState(root_goal="assess readiness", tasks=[task])
    wf_id = str(state.workflow_id)
    sup = _FakeSup(state)
    _install(monkeypatch, sup, {"healthcare": _StubPack()})

    payload = _step(tmp_path, wf_id, capsys)

    assert payload["status"] == "stub_surfaced", payload
    assert payload["task_id"] == str(task.task_id)
    assert payload["squad_slug"] == "healthcare"
    assert payload["envelope_id"], "stub_surfaced must carry the envelope id"
    assert "[STUB]" in payload["decision_record"]["decision"]
    assert payload["decision_record"]["type"] == "DECISION_RECORD"

    # bookkeeping: attended-complete + the record persisted on the checkpoint
    assert str(task.task_id) in sup.values["attended_completed_task_ids"]
    stub_envs = [e for e in sup.values["envelopes"]
                 if "[STUB]" in str(e.get("decision") or "")]
    assert len(stub_envs) == 1, sup.values["envelopes"]

    # the loop then continues: nothing left to drive. E2-30 renamed this
    # terminal status to `ready_to_finalize` (the attended results still owe
    # synthesis/postcheck) and kept `no_pending_task` as a boolean alias.
    nxt = _step(tmp_path, wf_id, capsys)
    assert nxt["status"] == "ready_to_finalize", nxt
    assert nxt["no_pending_task"] is True, nxt


def test_stub_task_is_not_marked_done_so_governance_still_surfaces(
        tmp_path, monkeypatch, capsys):
    """A stub is `surfaced`, never `done`: it must stay OUT of
    attended_done_task_ids so enforce_governance keeps surfacing the workflow
    for human follow-up."""
    task = TaskState(owner_squad="healthcare", description="assess readiness")
    state = HydraState(root_goal="assess readiness", tasks=[task])
    sup = _FakeSup(state)
    _install(monkeypatch, sup, {"healthcare": _StubPack()})

    _step(tmp_path, str(state.workflow_id), capsys)

    assert sup.values.get("attended_done_task_ids") in (None, [])


def test_stub_drive_reuses_an_in_graph_stub_record(tmp_path, monkeypatch, capsys):
    """Idempotent with the RA-5 in-graph path: when node_dispatch already
    emitted this squad's [STUB] record, no second one is created."""
    task = TaskState(owner_squad="healthcare", description="assess readiness")
    state = HydraState(root_goal="assess readiness", tasks=[task])
    existing_id = str(uuid4())
    state.envelopes = [{
        "id": existing_id,
        "type": "DECISION_RECORD",
        "workflow_id": str(state.workflow_id),
        "origin_squad": "healthcare",
        "target_squad": "hydra",
        "decision": "[STUB] Healthcare not yet implemented",
        "rationale": "already surfaced in-graph",
    }]
    sup = _FakeSup(state)
    _install(monkeypatch, sup, {"healthcare": _StubPack()})

    payload = _step(tmp_path, str(state.workflow_id), capsys)

    assert payload["envelope_id"] == existing_id
    stub_envs = [e for e in sup.values["envelopes"]
                 if "[STUB]" in str(e.get("decision") or "")]
    assert len(stub_envs) == 1, "the in-graph record must be reused, not duplicated"


# ---------------------------------------------------------------------------
# 2. mixed task list — stub first, then the squad cursor
# ---------------------------------------------------------------------------

def test_mixed_stub_then_squad_cursor(tmp_path, monkeypatch, capsys):
    """[stub, executive]: the stub is handled first (task-list order), then the
    executive squad cursor opens."""
    stub_task = TaskState(owner_squad="healthcare", description="assess readiness")
    exec_task = TaskState(owner_squad="executive", description="frame the decision")
    state = HydraState(root_goal="assess and frame",
                       tasks=[stub_task, exec_task])
    wf_id = str(state.workflow_id)
    sup = _FakeSup(state)
    _install(monkeypatch, sup, {"healthcare": _StubPack(), "executive": _SkillPack()})

    first = _step(tmp_path, wf_id, capsys)
    assert first["status"] == "stub_surfaced"
    assert first["task_id"] == str(stub_task.task_id)

    second = _step(tmp_path, wf_id, capsys)
    assert second["state"] == "await_squad_agent", second
    assert second["host_action"]["agent_type"] == "boardroom"
    assert second["run_id"] == str(exec_task.task_id)


def test_squad_cursor_wins_when_it_precedes_the_stub(tmp_path, monkeypatch, capsys):
    """[executive, stub]: the stub must not jump the queue."""
    exec_task = TaskState(owner_squad="executive", description="frame the decision")
    stub_task = TaskState(owner_squad="healthcare", description="assess readiness")
    state = HydraState(root_goal="frame and assess",
                       tasks=[exec_task, stub_task])
    sup = _FakeSup(state)
    _install(monkeypatch, sup, {"healthcare": _StubPack(), "executive": _SkillPack()})

    first = _step(tmp_path, str(state.workflow_id), capsys)
    assert first.get("state") == "await_squad_agent", first
    assert first["run_id"] == str(exec_task.task_id)


# ---------------------------------------------------------------------------
# 3. no-approval workflow reaches a dispatch pass
# ---------------------------------------------------------------------------

def test_first_step_runs_dispatch_pass_when_no_approval_gate(
        tmp_path, monkeypatch, capsys):
    """requires_human_approval=False + a bare interrupt → the first attended
    step runs the same headless pass `hydra approve` runs."""
    task = TaskState(owner_squad="healthcare", description="assess readiness")
    state = HydraState(root_goal="assess readiness", tasks=[task],
                       requires_human_approval=False)
    sup = _FakeSup(state, next_nodes=("dispatch",))
    _install(monkeypatch, sup, {"healthcare": _StubPack()})

    _step(tmp_path, str(state.workflow_id), capsys)

    assert sup.invoked == 1, "the no-approval dispatch pass did not run"


def test_dispatch_pass_is_not_rerun_on_later_steps(tmp_path, monkeypatch, capsys):
    """It is a first-step bootstrap: once anything is attended-complete the
    pass must not fire again."""
    task = TaskState(owner_squad="healthcare", description="assess readiness")
    other = TaskState(owner_squad="executive", description="frame it")
    state = HydraState(root_goal="assess", tasks=[task, other],
                       requires_human_approval=False,
                       attended_completed_task_ids=[str(task.task_id)])
    sup = _FakeSup(state, next_nodes=("dispatch",))
    _install(monkeypatch, sup, {"healthcare": _StubPack(), "executive": _SkillPack()})

    _step(tmp_path, str(state.workflow_id), capsys)

    assert sup.invoked == 0


def test_dispatch_pass_skipped_when_a_gate_is_open(tmp_path, monkeypatch, capsys):
    """An open HITL gate resumes only via /hydra:approve or /hydra:resume —
    the attended step must never resume it."""
    task = TaskState(owner_squad="healthcare", description="assess readiness")
    state = HydraState(root_goal="assess readiness", tasks=[task],
                       requires_human_approval=True)
    state.pending_hitl = {"reason": "high_risk", "gate_node": "approval"}
    sup = _FakeSup(state, next_nodes=("approval",))
    _install(monkeypatch, sup, {"healthcare": _StubPack()})

    _step(tmp_path, str(state.workflow_id), capsys)

    assert sup.invoked == 0, "attended step must not resume a gated workflow"


def test_dispatch_pass_skipped_while_engineering_is_pending(
        tmp_path, monkeypatch, capsys):
    """Engineering deferral is out of scope (fix-E2-22): a pending engineering
    task suppresses the pass so the attended engineering cursor stays
    authoritative."""
    eng = TaskState(owner_squad="engineering", description="implement it",
                    target_repo_id="hydra")
    state = HydraState(root_goal="implement it", tasks=[eng],
                       requires_human_approval=False)
    sup = _FakeSup(state, next_nodes=("dispatch",))
    _install(monkeypatch, sup, {"engineering": _StubPack()})
    # No agent stubs on disk → the engineering branch exits with the
    # missing_agent_dependency error; all this test pins is that the dispatch
    # pass did not fire before it.
    cli._cmd_attended_step(argparse.Namespace(
        project=str(tmp_path), workflow_id=str(state.workflow_id), verbose=False))
    capsys.readouterr()

    assert sup.invoked == 0


# ---------------------------------------------------------------------------
# 4. approval determinism across budgets
# ---------------------------------------------------------------------------

def _plan_requires_approval(budget: float, *, spent: float = 0.0) -> bool:
    """Run intake+planner for a fixed goal/squad/risk at the given budget."""
    from hydra_core.supervisor import build_supervisor

    runner = build_supervisor(
        project_root=HYDRA_ROOT,
        dispatcher=cli._NullDispatcher(),
        force_pure_python=True,
    )
    state = HydraState(
        root_goal="Assess the ops console for clinical deployment readiness",
        selected_squads=["healthcare"],
    )
    state.budget.budget_usd = budget
    state.budget.spent_usd = spent
    out = runner.invoke(state)
    final = HydraState.model_validate(out) if isinstance(out, dict) else out
    return bool(final.requires_human_approval)


def test_zero_budget_does_not_flip_the_approval_gate() -> None:
    """E2-32 evidence 3: the same goal / squad / risk must yield the same
    requires_human_approval at budget 0 and 0.1. Zero budget is a
    dispatch-time gate (budget.pre_dispatch_block), not a risk signal."""
    at_zero = _plan_requires_approval(0.0)
    at_tenth = _plan_requires_approval(0.1)
    at_five = _plan_requires_approval(5.0)
    assert at_zero == at_tenth == at_five, (
        f"requires_human_approval flipped on budget alone: "
        f"0={at_zero} 0.1={at_tenth} 5.0={at_five}"
    )


def test_funded_workflow_over_budget_still_requires_approval() -> None:
    """The guard is `budget_usd > 0 and is_over_budget()` — a funded workflow
    that is genuinely exhausted before dispatch still gates."""
    assert _plan_requires_approval(1.0, spent=1.0) is True
