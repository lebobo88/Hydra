"""P5a: HYDRA_PLAN_PHASE graph topology + planner seeding acceptance tests.

Scope note: P5a is graph topology + planner seeding ONLY. The ingest PLAN
branch, step materialisation, plan_revision retirement, the CLI
--rigor/--modify-plan/force-dispatch surfaces, and the flag default flip are
all P5b. These tests exercise the flag, the seeding, the requires_human_approval
stand-down (and its deliberate budget_exhausted exception), the plan_gate
topology, and the closed `phase` Literal.

Following tests/test_plan_triage.py's convention: node_planner is exercised
as the BARE step function extracted from a pure-python supervisor runner
(`dict(runner.steps)["planner"]`), and its RETURNED PATCH DICT is asserted on
directly. This is deliberate, not a shortcut: `PurePythonRunner.invoke()` has
no interrupt semantics, so a full `invoke()` call immediately runs the next
step (e.g. `node_approval`) after `node_planner` sets
`requires_human_approval`, clearing `pending_hitl` before a test could ever
observe it. The patch dict IS what a real (langgraph) interrupt_before pause
would show the operator, so it is the correct unit to assert against here.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from hydra_core.squad_node import Dispatcher
from hydra_core.state import HydraState, TaskState
from hydra_core.supervisor import build_supervisor, _PurePythonRunner


def _planner_fn(project_root=None):
    stub_dispatcher = MagicMock(spec=Dispatcher)
    stub_dispatcher._tool_tracker = None
    runner = build_supervisor(
        project_root=project_root,
        dispatcher=stub_dispatcher,
        force_pure_python=True,
    )
    fn = dict(runner.steps).get("planner")
    assert fn is not None, "planner step not found in runner"
    return fn


LONG_GOAL = (
    "Refactor the billing pipeline for full correctness and rollback safety "
    "across every service " * 2
)


def _state(goal=LONG_GOAL, selected_squads=("engineering",), **kw):
    s = HydraState(root_goal=goal, selected_squads=list(selected_squads))
    # WS1-E requires an explicit target repo for any engineering task, or
    # node_planner surfaces before it ever reaches the risk/plan gates this
    # module is testing -- set a placeholder so that check is a no-op.
    s.target_repo_id = "hydra"
    for k, v in kw.items():
        setattr(s, k, v)
    return s


# ---------------------------------------------------------------------------
# Flag OFF (default): byte-for-byte unchanged precedence.
# ---------------------------------------------------------------------------

def test_flag_off_no_planning_task_seeded(monkeypatch):
    # P5b: the flag now ships ON by default (unset == on), so asserting
    # legacy flag-off behaviour requires an explicit opt-out.
    monkeypatch.setenv("HYDRA_PLAN_PHASE", "0")
    patch = _planner_fn()(_state())
    assert "planning" not in {t.owner_squad for t in patch["tasks"]}
    assert "plan_status" not in patch


def test_flag_off_high_risk_still_gates_approval_reason_and_node(monkeypatch):
    # P5b: explicit opt-out (see test_flag_off_no_planning_task_seeded above).
    monkeypatch.setenv("HYDRA_PLAN_PHASE", "0")
    state = _state(tasks=[TaskState(owner_squad="engineering", description="x", priority="P0",
                                     acceptance_criteria=["done"])])
    patch = _planner_fn()(state)
    assert patch["requires_human_approval"] is True
    assert patch["phase"] == "approval"
    assert patch["pending_hitl"]["reason"] == "high_risk"
    assert patch["pending_hitl"]["gate_node"] == "approval"


# ---------------------------------------------------------------------------
# Flag ON: seeding.
# ---------------------------------------------------------------------------

def test_flag_on_major_rigor_seeds_one_planning_task_p2(monkeypatch):
    monkeypatch.setenv("HYDRA_PLAN_PHASE", "1")
    state = _state(tasks=[TaskState(owner_squad="engineering", description="x", priority="P0",
                                     acceptance_criteria=["done"])])
    patch = _planner_fn()(state)
    assert patch["plan_rigor"] == "major"
    planning_tasks = [t for t in patch["tasks"] if t.owner_squad == "planning"]
    assert len(planning_tasks) == 1, planning_tasks
    assert planning_tasks[0].priority == "P2"
    assert patch["plan_status"] == "authoring"


def test_flag_on_trivial_rigor_seeds_no_planning_task(monkeypatch):
    monkeypatch.setenv("HYDRA_PLAN_PHASE", "1")
    state = _state(goal="Fix a typo in the README.", selected_squads=("research-ds",))
    state.budget.budget_usd = 1.0  # under _TRIVIAL_BUDGET_USD (default 50.0 is not trivial)
    patch = _planner_fn()(state)
    assert patch["plan_rigor"] == "trivial"
    assert "planning" not in {t.owner_squad for t in patch["tasks"]}
    assert "plan_status" not in patch


# ---------------------------------------------------------------------------
# Flag ON: high_risk stands down -> plan gate, not sight-unseen approval.
# ---------------------------------------------------------------------------

def test_flag_on_high_risk_task_does_not_gate_approval_directly(monkeypatch):
    monkeypatch.setenv("HYDRA_PLAN_PHASE", "1")
    state = _state(tasks=[TaskState(owner_squad="engineering", description="x", priority="P0",
                                     acceptance_criteria=["done"])])
    patch = _planner_fn()(state)
    # requires_human_approval must NOT fire purely because of high_risk/needs_ac_hitl
    # when the plan gate is active and budget is not exhausted.
    assert patch["requires_human_approval"] is False
    assert patch["phase"] == "dispatch"
    assert patch["plan_status"] == "authoring"
    assert "pending_hitl" not in patch


# ---------------------------------------------------------------------------
# Flag ON: budget_exhausted MUST NOT stand down (C-a regression guard).
# ---------------------------------------------------------------------------

def test_flag_on_funded_over_budget_still_gates_at_approval_not_dispatch(monkeypatch):
    monkeypatch.setenv("HYDRA_PLAN_PHASE", "1")
    state = _state(tasks=[TaskState(owner_squad="engineering", description="x", priority="P0",
                                     acceptance_criteria=["done"])])
    state.budget.budget_usd = 1.0
    state.budget.spent_usd = 2.0
    patch = _planner_fn()(state)
    reason = patch.get("pending_hitl", {}).get("reason")
    gate_node = patch.get("pending_hitl", {}).get("gate_node")
    print(f"observed reason={reason!r} gate_node={gate_node!r}")
    assert patch["requires_human_approval"] is True
    assert reason == "over_budget", reason
    assert gate_node == "approval", gate_node


def test_unfunded_workflow_is_not_a_risk_signal_under_plan_gate(monkeypatch):
    monkeypatch.setenv("HYDRA_PLAN_PHASE", "1")
    state = _state(tasks=[TaskState(owner_squad="engineering", description="x", priority="P0",
                                     acceptance_criteria=["done"])])
    state.budget.budget_usd = 0.0
    patch = _planner_fn()(state)
    assert patch["requires_human_approval"] is False


# ---------------------------------------------------------------------------
# phase Literal is unchanged.
# ---------------------------------------------------------------------------

def test_phase_literal_member_set_unchanged():
    literal = HydraState.model_fields["phase"].annotation
    members = set(literal.__args__)
    assert members == {
        "intake", "planning", "approval", "dispatch",
        "executing", "judge_per_squad", "synthesis", "judge_synthesis",
        "postcheck", "done", "surfaced",
    }


# ---------------------------------------------------------------------------
# Pure-python runner still exposes node_planner (the "planner" step) by name.
# ---------------------------------------------------------------------------

def test_pure_python_runner_exposes_node_planner_by_name(monkeypatch):
    monkeypatch.delenv("HYDRA_PLAN_PHASE", raising=False)
    stub_dispatcher = MagicMock(spec=Dispatcher)
    stub_dispatcher._tool_tracker = None
    runner = build_supervisor(dispatcher=stub_dispatcher, force_pure_python=True)
    assert isinstance(runner, _PurePythonRunner)
    names = [name for name, _fn in runner.steps]
    assert "planner" in names
    assert "plan_judge" in names
    assert "plan_gate" in names


# ---------------------------------------------------------------------------
# plan_gate is in interrupt_before (langgraph path), with the HITL request
# visible BEFORE the interrupt.
# ---------------------------------------------------------------------------

def test_plan_gate_in_interrupt_before(monkeypatch, tmp_path):
    pytest.importorskip("langgraph")
    stub_dispatcher = MagicMock(spec=Dispatcher)
    stub_dispatcher._tool_tracker = None
    sup = build_supervisor(
        dispatcher=stub_dispatcher,
        checkpoint_path=tmp_path / "cp.db",
    )
    # Compiled pregel graphs expose their interrupt_before nodes; walk a few
    # plausible attribute names rather than depending on one langgraph
    # version's internal layout.
    interrupt_before = (
        getattr(sup, "interrupt_before_nodes", None)
        or getattr(sup, "interrupt_before", None)
    )
    assert interrupt_before is not None, "could not introspect interrupt_before on compiled graph"
    assert "plan_gate" in interrupt_before
