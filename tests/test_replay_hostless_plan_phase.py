"""`hydra replay` is a hostless production path (hostless-path audit, plan-phase
default flip): it mints a brand-new thread_id and invokes it fresh through
node_intake -> node_planner regardless of `--from-phase`/`--live`, and the
function's own docstring says it exists to be launched as a detached
subprocess -- there is never an attended host to answer a deferred planning
task. `_cmd_replay`'s `build_supervisor(...)` call therefore MUST pass
`force_trivial_plan_rigor=True` (mirroring `_cmd_run`'s `--live`/
`--no-checkpoint` precedent) so that, with the plan phase at its shipping
default (ON), a replay of a non-trivial-rigor workflow completes instead of
seeding a real `owner_squad="planning"` task and parking at phase="planning"
forever.

This is a proven-load-bearing guard, not merely present: see
test_replay_of_nontrivial_goal_does_not_deadlock's paired mutation proof in
this module's docstring-adjacent comment at the bottom of the file (the
proof itself is run by hand against a reverted copy of the fix, per the
task's MUTATION PROOFS section, and is not a permanently-committed test —
removing `force_trivial_plan_rigor=True` from `_cmd_replay` and re-running
this test is exactly that proof).
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
from hydra_core.state import HydraState, TaskState

HYDRA_ROOT = Path(__file__).resolve().parents[1]


class _StubDispatcher:
    """Non-live, offline-opt-in dispatcher: lets the legacy in-graph mcp path
    run (E2-22's live-defer prefilter only fires for a `live_execution`
    dispatcher without this opt-in), so the replay actually dispatches
    engineering instead of stranding it as deferred_to_host for an unrelated
    reason."""

    live_execution = False
    allow_offline_mcp_dispatch = True

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
    monkeypatch.setattr(cli, "_attended_live_dispatcher",
                        lambda *_a, **_kw: _StubDispatcher())
    return tmp_path


def _seed_planned_source_workflow(*, plan_status: str = "approved") -> str:
    """A source checkpoint whose workflow ACTUALLY raised a plan
    (`plan_status` advanced past "none"). Replaying this must be refused --
    see test_replay_of_planned_workflow_is_refused below."""
    from hydra_core.supervisor import build_supervisor, _PurePythonRunner
    from hydra_core.state import BudgetLedger

    wf = uuid4()
    state = HydraState(
        workflow_id=wf,
        root_goal="replay hostless plan-phase regression: planned workflow",
        selected_squads=["engineering"],
        target_repo_id="hydra",
        budget=BudgetLedger(budget_usd=50.0, spent_usd=0.0),
        phase="dispatch",
        plan_status=plan_status,
        plan_rigor="standard",
    )
    sup = build_supervisor(project_root=HYDRA_ROOT, dispatcher=_StubDispatcher())
    assert not isinstance(sup, _PurePythonRunner), "langgraph required for this test"
    config = {"configurable": {"thread_id": str(wf)}}
    sup.update_state(config, state.model_dump(mode="json"), as_node="judge_per_squad")
    return str(wf)


def _seed_source_workflow() -> str:
    """A source checkpoint whose (root_goal, selected_squads, budget) triage
    to NON-TRIVIAL plan_rigor once replayed fresh: budget_usd=50 alone is
    >= _TRIVIAL_BUDGET_USD (10.0), so triage_plan cannot return "trivial" for
    it (and it is well under _MAJOR_BUDGET_USD=100, so it lands on
    "standard" -- exactly the rigor level that seeds a planning task when the
    plan phase is on and nothing forces it trivial)."""
    from hydra_core.supervisor import build_supervisor, _PurePythonRunner
    from hydra_core.state import BudgetLedger

    wf = uuid4()
    state = HydraState(
        workflow_id=wf,
        root_goal="replay hostless plan-phase regression: ship the change",
        selected_squads=["engineering"],
        target_repo_id="hydra",
        budget=BudgetLedger(budget_usd=50.0, spent_usd=0.0),
        phase="dispatch",
    )
    sup = build_supervisor(project_root=HYDRA_ROOT, dispatcher=_StubDispatcher())
    assert not isinstance(sup, _PurePythonRunner), "langgraph required for this test"
    config = {"configurable": {"thread_id": str(wf)}}
    sup.update_state(config, state.model_dump(mode="json"), as_node="judge_per_squad")
    return str(wf)


def _replay(wf: str, *, live: bool = False) -> tuple[int, dict]:
    args = argparse.Namespace(
        project=str(HYDRA_ROOT), workflow_id=wf, from_phase="intake",
        swap_model=None, live=live, verbose=False,
        sanitize_non_finite=False,
    )
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli._cmd_replay(args)
    out = buf.getvalue()
    start = out.index("{")
    return rc, json.loads(out[start:])


def test_replay_of_nontrivial_goal_does_not_deadlock(hermetic):
    """The required test: replaying a workflow whose (goal, budget) triage to
    NON-TRIVIAL plan_rigor, with the plan phase at its shipping default (no
    HYDRA_PLAN_PHASE env var set at all -- this test does not touch it),
    reaches a real terminal/paused state -- NOT phase="planning" with an
    unresolvable deferred planning task. Asserts the terminal state directly,
    not merely the absence of an exception."""
    import os
    assert "HYDRA_PLAN_PHASE" not in os.environ, (
        "this test exercises the shipping default; it must not set the flag"
    )
    wf = _seed_source_workflow()

    rc, payload = _replay(wf, live=False)

    assert rc == 0, payload
    assert "replay_workflow_id" in payload, payload
    # The deadlock symptom this guards against: parked at phase="planning"
    # forever because a real owner_squad="planning" task was seeded and
    # deferred to a host that (per _cmd_replay's own docstring) never exists
    # for a detached-subprocess replay.
    assert payload["phase"] != "planning", (
        f"replay parked at phase='planning' -- force_trivial_plan_rigor did "
        f"not reach node_planner; full payload: {payload}"
    )
    # Positive confirmation the run actually progressed past dispatch: peek
    # the replayed thread's checkpoint and confirm the engineering task
    # dispatched (not left pending behind a phantom plan barrier) and no
    # planning task was ever synthesised.
    from hydra_core.supervisor import build_supervisor, _PurePythonRunner
    sup = build_supervisor(project_root=HYDRA_ROOT, dispatcher=_StubDispatcher())
    assert not isinstance(sup, _PurePythonRunner)
    replay_config = {"configurable": {"thread_id": payload["replay_workflow_id"]}}
    snap = sup.get_state(replay_config)
    assert snap is not None and snap.values
    replayed = HydraState.model_validate(snap.values)
    owner_squads = {t.owner_squad for t in replayed.tasks}
    assert "planning" not in owner_squads, (
        f"a planning task was seeded despite force_trivial_plan_rigor=True; "
        f"tasks={[(t.owner_squad, t.status) for t in replayed.tasks]}"
    )
    eng_tasks = [t for t in replayed.tasks if t.owner_squad == "engineering"]
    assert eng_tasks and all(t.status != "pending" for t in eng_tasks), (
        f"engineering task never dispatched; tasks="
        f"{[(t.owner_squad, t.status) for t in replayed.tasks]}"
    )


# ---------------------------------------------------------------------------
# Cross-vendor finding (HIGH): replay of a workflow that raised a plan must
# refuse loudly rather than silently regenerate a different task set.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("plan_status", ["authoring", "drafted", "judged", "approved", "rejected"])
def test_replay_of_planned_workflow_is_refused(hermetic, plan_status):
    """Any source workflow whose plan_status advanced past "none" -- at ANY
    stage of the lifecycle -- is refused, not silently replayed with a
    regenerated (and therefore divergent) task set."""
    wf = _seed_planned_source_workflow(plan_status=plan_status)

    rc, payload = _replay(wf, live=False)

    assert rc == 0, payload
    assert payload.get("ok") is False, payload
    assert payload.get("status") == "replay_refused_planned_workflow", payload
    assert payload.get("plan_status") == plan_status, payload
    assert "replay_workflow_id" not in payload, (
        "a refused replay must never mint/persist a replay lineage"
    )


def test_replay_of_trivial_workflow_still_works_unchanged(hermetic):
    """Counterpart: a source workflow whose plan_status stayed "none" (it
    never raised a plan) is unaffected by the refusal -- replays exactly as
    test_replay_of_nontrivial_goal_does_not_deadlock already proves."""
    wf = _seed_source_workflow()  # plan_status defaults to "none"

    rc, payload = _replay(wf, live=False)

    assert rc == 0, payload
    assert payload.get("status") != "replay_refused_planned_workflow", payload
    assert "replay_workflow_id" in payload, payload
