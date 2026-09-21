"""P1 plan-barrier predicates.

Every predicate here is a NO-OP today: nothing yet sets ``plan_status`` away
from its default "none", so ``plan_barrier_active`` is always False. These
tests force ``plan_status`` (and, where needed, ``depends_on`` /
``plan_revision``) to prove each guard actually holds work back -- and that
reverting any one of them makes the corresponding test fail.
"""
from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from hydra_core.cli import (
    _next_attended_task,
    _next_engineering_task,
    _next_stub_attended_task,
    _run_first_step_dispatch_pass,
)
from hydra_core.ingest import dispatch_ingested_envelopes
from hydra_core.squad_loader import SquadPack, discover_squads
from hydra_core.state import HydraState, TaskState, plan_barrier_active, plan_deps_satisfied

HYDRA_ROOT = Path(__file__).resolve().parents[1]

_BARRIER_STATES = ["authoring", "drafted", "judged", "rejected"]


@pytest.fixture
def packs():
    return discover_squads(HYDRA_ROOT)


def _stub_pack(slug: str) -> SquadPack:
    return SquadPack(slug=slug, name=slug, description=slug, entrypoint="stub")


# --------------------------------------------------------------------------- #
# 1 + 8 — node_dispatch: barrier holds non-planning tasks; "none" is inert
# --------------------------------------------------------------------------- #

class TestNodeDispatchBarrier:
    def _run(self, monkeypatch, plan_status: str):
        from hydra_core.supervisor import build_supervisor, _PurePythonRunner

        # No task in this test ever reaches execute_squad (barrier or missing
        # pack short-circuits every one) -- but guard against a regression
        # silently calling through by making it loud if it ever does.
        def _boom(*_a, **_k):
            raise AssertionError("execute_squad must not run under an active barrier")
        monkeypatch.setattr("hydra_core.supervisor.execute_squad", _boom)

        class _OfflineDispatcher:
            allow_offline_mcp_dispatch = True

        runner = build_supervisor(
            project_root=HYDRA_ROOT, dispatcher=_OfflineDispatcher(),
            force_pure_python=True,
            # P5b hostless-path audit: one-shot invoke, no attended host.
            force_trivial_plan_rigor=True,
        )
        assert isinstance(runner, _PurePythonRunner)

        state = HydraState(
            root_goal="test", selected_squads=["planning", "engineering", "garland"],
            target_repo_id="hydra", plan_status=plan_status,
        )
        state.tasks.append(TaskState(owner_squad="planning", description="author plan"))
        state.tasks.append(TaskState(owner_squad="engineering", description="implement x"))
        state.tasks.append(TaskState(owner_squad="garland", description="make asset"))
        state.tasks.append(TaskState(owner_squad="engineering", description="implement y"))

        final = runner.invoke(state, stop_before="judge_per_squad")
        by_desc = {t.description: t for t in final.tasks}
        return by_desc

    @pytest.mark.parametrize("plan_status", _BARRIER_STATES)
    def test_barrier_holds_non_planning_tasks(self, monkeypatch, plan_status):
        by_desc = self._run(monkeypatch, plan_status)
        for desc in ("implement x", "make asset", "implement y"):
            t = by_desc[desc]
            assert t.status == "pending", f"{desc} was mutated to {t.status!r}"

    def test_none_status_is_inert(self, monkeypatch):
        """Regression guard for the whole run: with plan_status == 'none'
        (the default), engineering still dispatches (no pack skip)."""
        # Rebuild without the execute_squad trap -- "none" must let dispatch
        # actually attempt engineering (it will fail for lack of a live pp
        # harness, but it must NOT be skipped as "pending").
        from hydra_core.supervisor import build_supervisor, _PurePythonRunner

        class _OfflineDispatcher:
            allow_offline_mcp_dispatch = True

        runner = build_supervisor(
            project_root=HYDRA_ROOT, dispatcher=_OfflineDispatcher(),
            force_pure_python=True,
            # P5b hostless-path audit: one-shot invoke, no attended host.
            force_trivial_plan_rigor=True,
        )
        state = HydraState(
            root_goal="test", selected_squads=["engineering"],
            target_repo_id="hydra", plan_status="none",
        )
        state.tasks.append(TaskState(owner_squad="engineering", description="implement x"))
        final = runner.invoke(state, stop_before="judge_per_squad")
        t = [x for x in final.tasks if x.description == "implement x"][0]
        assert t.status != "pending", "plan_status='none' must not hold dispatch back"


# --------------------------------------------------------------------------- #
# 2 — fleet predicate false under the barrier
# --------------------------------------------------------------------------- #

def test_fleet_never_engages_under_active_barrier(monkeypatch):
    """Two distinct-repo engineering tasks would normally satisfy the fleet
    predicate (fleet_parallel + >=2 candidates + >=2 distinct repos). Under an
    active barrier they must stay untouched (still pending), proving
    `_use_fleet` picked up `not plan_barrier_active(state)`."""
    from hydra_core.supervisor import build_supervisor, _PurePythonRunner

    def _boom(*_a, **_k):
        raise AssertionError("execute_squad must not run under an active barrier")
    monkeypatch.setattr("hydra_core.supervisor.execute_squad", _boom)

    class _OfflineDispatcher:
        allow_offline_mcp_dispatch = True

    runner = build_supervisor(
        project_root=HYDRA_ROOT, dispatcher=_OfflineDispatcher(),
        force_pure_python=True,
        # P5b hostless-path audit: one-shot invoke, no attended host.
        force_trivial_plan_rigor=True,
    )
    state = HydraState(
        root_goal="test", selected_squads=["engineering"],
        fleet_parallel=True, plan_status="authoring",
    )
    state.tasks.append(TaskState(owner_squad="engineering", description="a",
                                 target_repo_id="hydra"))
    state.tasks.append(TaskState(owner_squad="engineering", description="b",
                                 target_repo_id="hydra"))
    final = runner.invoke(state, stop_before="judge_per_squad")
    for t in final.tasks:
        assert t.status == "pending"


# --------------------------------------------------------------------------- #
# 3 — _next_stub_attended_task refuses a stub task ahead of the planning task
# --------------------------------------------------------------------------- #

def test_stub_selector_refuses_stub_ahead_of_planning_under_barrier():
    packs = {"stubby": _stub_pack("stubby")}
    state = HydraState(root_goal="x", plan_status="drafted")
    state.tasks.append(TaskState(owner_squad="stubby", description="stub first"))
    state.tasks.append(TaskState(owner_squad="planning", description="author plan"))

    task, pack = _next_stub_attended_task(state, packs)
    assert task is None and pack is None

    # Sanity: with the barrier cleared, the stub task IS selectable again.
    state.plan_status = "none"
    task, pack = _next_stub_attended_task(state, packs)
    assert task is not None and task.description == "stub first"


# --------------------------------------------------------------------------- #
# 4 — dispatch_ingested_envelopes holds a DEV_TASK under the barrier
# --------------------------------------------------------------------------- #

def test_ingest_holds_dev_task_under_barrier(packs, monkeypatch):
    def _boom(*_a, **_k):
        raise AssertionError("execute_squad must not run while the barrier holds")
    monkeypatch.setattr("hydra_core.ingest.execute_squad", _boom)

    state = HydraState(root_goal="x", plan_status="judged")
    dev = {
        "id": str(uuid4()), "type": "DEV_TASK", "origin_squad": "rlm-gaming",
        "workflow_id": str(state.workflow_id), "owner": "backend", "repo": "hydra",
        "target_repo_id": "hydra", "branch": "wf",
        "instructions": "do the thing",
    }
    outcome = dispatch_ingested_envelopes(state, [dev], packs=packs, dispatcher=object())
    assert [it.status for it in outcome.items] == ["deferred_to_host"]
    assert outcome.new_tasks == []


# --------------------------------------------------------------------------- #
# 5 — attended_completed (surfaced/aborted) does not release a dependent
# --------------------------------------------------------------------------- #

def test_completed_but_not_done_dependency_does_not_release_dependent():
    state = HydraState(root_goal="x")
    upstream = TaskState(owner_squad="engineering", description="upstream")
    dependent = TaskState(owner_squad="engineering", description="dependent",
                          depends_on=[str(upstream.task_id)])
    state.tasks.extend([upstream, dependent])
    # Upstream is "completed" (attended cursor reached a terminal outcome) but
    # NOT "done" (e.g. it surfaced) -- attended_completed_task_ids carries it,
    # attended_done_task_ids does not.
    state.attended_completed_task_ids = [str(upstream.task_id)]

    assert not plan_deps_satisfied(state, dependent)

    # _next_attended_task must skip the dependent and find nothing else.
    task, kind, pack = _next_attended_task(state, {})
    assert task is None

    # Once genuinely done, the dependent is released.
    state.attended_done_task_ids = [str(upstream.task_id)]
    assert plan_deps_satisfied(state, dependent)


# --------------------------------------------------------------------------- #
# 6 — stale plan_revision is skipped
# --------------------------------------------------------------------------- #

def test_stale_plan_revision_skipped_by_selectors():
    state = HydraState(root_goal="x", plan_revision=2)
    stale = TaskState(owner_squad="engineering", description="stale",
                      plan_revision=1)
    fresh = TaskState(owner_squad="engineering", description="fresh",
                      plan_revision=2)
    state.tasks.extend([stale, fresh])

    task, kind, pack = _next_attended_task(state, {})
    assert task is not None and task.description == "fresh"

    packs = {"stubby": _stub_pack("stubby")}
    stale_stub = TaskState(owner_squad="stubby", description="stale stub",
                           plan_revision=1)
    state2 = HydraState(root_goal="x", plan_revision=2)
    state2.tasks.append(stale_stub)
    task2, pack2 = _next_stub_attended_task(state2, packs)
    assert task2 is None


# --------------------------------------------------------------------------- #
# 7 — _run_first_step_dispatch_pass deadlock fix
# --------------------------------------------------------------------------- #

class _FakeSnap:
    def __init__(self, next_nodes):
        self.next = next_nodes


def test_first_step_dispatch_pass_runs_under_barrier_with_pending_engineering(monkeypatch):
    ran = {"called": False}

    class _FakeSup:
        def invoke(self, *_a, **_k):
            ran["called"] = True

    state = HydraState(root_goal="x", plan_status="authoring")
    state.tasks.append(TaskState(owner_squad="engineering", description="e"))
    snap = _FakeSnap(("dispatch",))

    result = _run_first_step_dispatch_pass(
        _FakeSup(), {}, HYDRA_ROOT, "wf", snap, state)
    assert result is True
    assert ran["called"] is True


def test_first_step_dispatch_pass_reverted_guard_fails(monkeypatch):
    """Proves the guard is load-bearing by actually invoking
    ``_run_first_step_dispatch_pass`` -- not just re-checking the predicates
    it consults.

    The fixed condition is::

        _next_engineering_task(state) is not None and not plan_barrier_active(state)

    Folding ``not plan_barrier_active(state)`` to ``not False`` collapses it
    back to the OLD, unguarded condition::

        _next_engineering_task(state) is not None

    which is exactly the pre-fix deadlock: an engineering task pending under
    an active barrier would suppress the bootstrap pass forever, so the
    planning task's own ``dispatch.deferred_to_host`` marking never happens.
    Patching ``plan_barrier_active`` (as imported into ``hydra_core.cli``) to
    always return False reproduces that old condition byte-for-byte, so this
    test calls the real function twice against the same barrier-active,
    pending-engineering state: once with the fix live (expect it to run),
    once with the old condition simulated (expect it to be suppressed).
    """
    ran = {"called": False}

    class _FakeSup:
        def invoke(self, *_a, **_k):
            ran["called"] = True

    state = HydraState(root_goal="x", plan_status="authoring")
    state.tasks.append(TaskState(owner_squad="engineering", description="e"))
    snap = _FakeSnap(("dispatch",))
    assert _next_engineering_task(state) is not None, (
        "sanity check: an engineering task must be pending for this test to "
        "mean anything"
    )
    assert plan_barrier_active(state) is True, (
        "sanity check: the barrier must be active for this test to mean "
        "anything"
    )

    # With the fix live: the barrier being active means the pending
    # engineering task must NOT suppress the pass.
    result = _run_first_step_dispatch_pass(
        _FakeSup(), {}, HYDRA_ROOT, "wf", snap, state)
    assert result is True
    assert ran["called"] is True

    # Simulate the reverted (pre-fix) guard: force plan_barrier_active to
    # always read False inside hydra_core.cli. The condition then reduces to
    # the old unconditional `_next_engineering_task(state) is not None`,
    # which suppresses the pass -- reproducing the deadlock this fix closes.
    monkeypatch.setattr("hydra_core.cli.plan_barrier_active", lambda _s: False)
    ran["called"] = False
    reverted_result = _run_first_step_dispatch_pass(
        _FakeSup(), {}, HYDRA_ROOT, "wf", snap, state)
    assert reverted_result is False
    assert ran["called"] is False


# --------------------------------------------------------------------------- #
# 9 — pre-upgrade-checkpoint compatibility: no ``plan_status`` attribute at all
# --------------------------------------------------------------------------- #

def test_plan_barrier_active_false_when_plan_status_attribute_absent():
    """A checkpoint written before P1 shipped carries no ``plan_status``
    field at all (not even the default "none") once deserialized into
    whatever plain object a legacy caller hands in. ``plan_barrier_active``
    must treat "attribute absent" the same as "none" -- i.e. inactive --
    rather than raising or defaulting to active."""

    class _NoPlanStatus:
        """Deliberately carries no ``plan_status`` attribute."""

    assert plan_barrier_active(_NoPlanStatus()) is False


# --------------------------------------------------------------------------- #
# 10 — depends_on is enforced even with plan_status == "none" (no active plan)
# --------------------------------------------------------------------------- #

def test_depends_on_is_enforced_even_without_a_plan():
    """P1's dependency gating (``plan_deps_satisfied``, and its unconditional
    call site in ``_next_attended_task``) is deliberately NOT gated behind
    ``plan_barrier_active``. This pins that as intended behavior: a task
    carrying ``depends_on`` is held back even when ``plan_status == "none"``
    (the default -- no plan barrier active at all), because approval moves a
    plan to "approved", which is not a barrier state, and dependency
    ordering must still hold post-approval."""
    state = HydraState(root_goal="x")  # plan_status defaults to "none"
    assert plan_barrier_active(state) is False

    upstream = TaskState(owner_squad="engineering", description="upstream")
    dependent = TaskState(owner_squad="engineering", description="dependent",
                          depends_on=[str(upstream.task_id)])
    state.tasks.extend([upstream, dependent])

    # The dependent is not yet satisfied -- selection must skip it and pick
    # the upstream task instead, even though no plan barrier is active.
    assert not plan_deps_satisfied(state, dependent)
    task, kind, pack = _next_attended_task(state, {})
    assert task is not None and task.description == "upstream"

    # Once the upstream task is genuinely done, the dependent is released.
    state.attended_done_task_ids = [str(upstream.task_id)]
    assert plan_deps_satisfied(state, dependent)


# --------------------------------------------------------------------------- #
# 11 — stale plan_revision is skipped even with plan_status == "none"
# --------------------------------------------------------------------------- #

def test_stale_plan_revision_skipped_without_a_plan():
    """Companion to test_stale_plan_revision_skipped_by_selectors: pins,
    explicitly and by name, that the stale-plan_revision skip in
    ``_next_attended_task`` fires with the default ``plan_status == "none"``
    -- no plan barrier active -- so a reader cannot mistake it for a
    barrier-only guard."""
    state = HydraState(root_goal="x", plan_revision=2)
    assert plan_barrier_active(state) is False

    stale = TaskState(owner_squad="engineering", description="stale",
                      plan_revision=1)
    state.tasks.append(stale)

    task, kind, pack = _next_attended_task(state, {})
    assert task is None
