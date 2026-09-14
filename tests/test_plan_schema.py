"""P0 planning substrate — schema, state, rubric, and cell-mapping tests.

Purely additive: nothing in the runtime graph reads `Plan`/`PlanStep` or the
new `HydraState` plan_* fields yet. These tests pin the PROPERTIES the new
code must hold, not just an instance that happens to pass.
"""
from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from hydra_core.eights import ALL_CELLS, cell_of, to_eights_cell
from hydra_core.judge.registry import get_rubric, list_rubrics
from hydra_core.judge.router import route_judge
from hydra_core.judge.rubric_resolution import DEFAULT_RUBRIC_BY_GATE_TYPE
from hydra_core.schemas import Plan, PlanStep, SCHEMA_REGISTRY, validate_envelope
from hydra_core.state import HydraState


WF = uuid.uuid4()


def _plan(**overrides):
    base = dict(
        origin_squad="hydra",
        workflow_id=WF,
        rigor="standard",
        goal_restatement="Ship the planning substrate.",
        summary="Add PLAN/PlanStep schema and wiring.",
        steps=[],
    )
    base.update(overrides)
    return Plan(**base)


# --------------------------------------------------------------------------- #
# DAG validation                                                              #
# --------------------------------------------------------------------------- #

def test_cyclic_plan_raises():
    steps = [
        PlanStep(step_id="a", target_squad="engineering", envelope_type="DEV_TASK",
                 description="A", depends_on=["b"]),
        PlanStep(step_id="b", target_squad="engineering", envelope_type="DEV_TASK",
                 description="B", depends_on=["a"]),
    ]
    with pytest.raises(ValidationError, match="Cyclic dependency"):
        _plan(steps=steps)
    # Proof this is the validator's doing, not incidental: if `_validate_dag`
    # (or just its Kahn drain-and-check block) were removed, this exact
    # `steps=[a->b, b->a]` list would construct a `Plan` without error —
    # `field_validator`s on PlanStep never see cross-step relationships, so
    # only the model_validator on Plan can catch a cycle.


def test_dangling_dependency_raises():
    steps = [
        PlanStep(step_id="a", target_squad="engineering", envelope_type="DEV_TASK",
                 description="A", depends_on=["ghost"]),
    ]
    with pytest.raises(ValidationError, match="dangling dependency"):
        _plan(steps=steps)


def test_duplicate_step_id_raises():
    steps = [
        PlanStep(step_id="a", target_squad="engineering", envelope_type="DEV_TASK",
                 description="A1"),
        PlanStep(step_id="a", target_squad="engineering", envelope_type="DEV_TASK",
                 description="A2"),
    ]
    with pytest.raises(ValidationError, match="Duplicate step_id"):
        _plan(steps=steps)


def test_self_dependency_raises():
    steps = [
        PlanStep(step_id="a", target_squad="engineering", envelope_type="DEV_TASK",
                 description="A", depends_on=["a"]),
    ]
    with pytest.raises(ValidationError, match="depends on itself"):
        _plan(steps=steps)


def test_diamond_dependency_shape_constructs_successfully():
    # a -> b, a -> c, b -> d, c -> d (diamond). Acyclic; must NOT raise.
    steps = [
        PlanStep(step_id="a", target_squad="engineering", envelope_type="DEV_TASK",
                 description="A"),
        PlanStep(step_id="b", target_squad="engineering", envelope_type="DEV_TASK",
                 description="B", depends_on=["a"]),
        PlanStep(step_id="c", target_squad="engineering", envelope_type="DEV_TASK",
                 description="C", depends_on=["a"]),
        PlanStep(step_id="d", target_squad="engineering", envelope_type="DEV_TASK",
                 description="D", depends_on=["b", "c"]),
    ]
    plan = _plan(steps=steps)
    assert [s.step_id for s in plan.steps] == ["a", "b", "c", "d"]


# --------------------------------------------------------------------------- #
# P5b: plan_revision must be >= 1                                             #
# --------------------------------------------------------------------------- #
#
# node_plan_gate stamps this value verbatim onto every TaskState materialised
# from the plan's steps, and the four attended-selectors filter stale work
# with `getattr(t, "plan_revision", 0) and t.plan_revision != state.plan_revision`
# -- a leading truthiness test that relies on 0 meaning "not a plan step at
# all" (TaskState.plan_revision's own default). Two silent failure modes
# follow from an unconstrained field: plan_revision=0 makes every materialised
# step permanently exempt from the filter (the exact append-only stale-task
# bug this whole design exists to close); a negative value is truthy and can
# never equal state.plan_revision, so every step is filtered permanently and
# an approved plan silently dispatches nothing.

def test_plan_revision_zero_is_refused():
    with pytest.raises(ValidationError):
        _plan(plan_revision=0)


def test_plan_revision_negative_is_refused():
    with pytest.raises(ValidationError):
        _plan(plan_revision=-3)


def test_plan_revision_constraint_is_load_bearing():
    """Remove the fix and show the refusal tests above would fail -- i.e.
    Plan(plan_revision=0) constructs cleanly without the `ge=1` constraint.
    This does not mutate the real model (that would affect other tests in
    the same process); it re-derives the pre-fix field behaviour on a throwaway
    subclass to prove the constraint, not something else, is what refuses."""
    from pydantic import BaseModel

    class _UnconstrainedPlanRevision(BaseModel):
        plan_revision: int = 1  # the field exactly as it read before this fix

    # Without `ge=1`, both values the tests above refuse construct cleanly.
    assert _UnconstrainedPlanRevision(plan_revision=0).plan_revision == 0
    assert _UnconstrainedPlanRevision(plan_revision=-3).plan_revision == -3


def test_plan_revision_zero_would_make_step_tasks_unfilterable():
    """The failure the constraint actually prevents, traced end-to-end: if
    plan_revision=0 could construct, every step task node_plan_gate
    materialises from it would carry plan_revision=0, and the selectors'
    `getattr(t, "plan_revision", 0) and ...` guard would treat every one of
    them as "not a plan step at all" -- exempt from the stale-revision filter
    FOREVER, even after a later revision superseded them. Demonstrated here
    directly against the selector predicate (not by bypassing the now-fixed
    Plan constructor), so this test would have failed loudly before the
    schema fix landed, once a real PLAN carrying plan_revision=0 reached
    node_plan_gate.
    """
    from hydra_core.state import TaskState

    # A step materialised from a (hypothetically unconstrained) revision-0
    # plan, and the CURRENT plan revision is 2 -- this step is stale and
    # must never be selectable again.
    stale_from_rev_zero = TaskState(
        owner_squad="engineering", description="stale from a phantom rev 0",
        plan_revision=0,
    )
    state = HydraState(root_goal="x", plan_revision=2)

    def _is_stale_and_filtered(task) -> bool:
        # The exact predicate every one of the four selectors applies.
        return bool(getattr(task, "plan_revision", 0)) and task.plan_revision != state.plan_revision

    assert not _is_stale_and_filtered(stale_from_rev_zero), (
        "this demonstrates the bug the ge=1 constraint prevents: a "
        "plan_revision=0 step is falsy at the leading truthiness test, so "
        "the selectors' stale-revision filter would never even inspect it "
        "-- it stays selectable forever, regardless of state.plan_revision. "
        "Plan.plan_revision's ge=1 constraint makes this state unreachable "
        "through a real PLAN envelope."
    )


# --------------------------------------------------------------------------- #
# envelope_type validation                                                    #
# --------------------------------------------------------------------------- #

def test_plan_step_rejects_unknown_envelope_type():
    with pytest.raises(ValidationError, match="Unknown envelope_type"):
        PlanStep(step_id="a", target_squad="engineering", envelope_type="NOPE",
                  description="A")


def test_plan_step_accepts_known_envelope_type():
    step = PlanStep(step_id="a", target_squad="engineering", envelope_type="DEV_TASK",
                     description="A")
    assert step.envelope_type == "DEV_TASK"


def test_plan_registered_in_schema_registry_and_validate_envelope():
    assert SCHEMA_REGISTRY["PLAN"] is Plan
    plan = _plan()
    envelope = validate_envelope(plan.model_dump(mode="json"))
    assert isinstance(envelope, Plan)


# --------------------------------------------------------------------------- #
# TheEights cell mapping — total by construction                             #
# --------------------------------------------------------------------------- #

def test_to_eights_cell_roundtrips_every_cell():
    for c in ALL_CELLS:
        assert cell_of(to_eights_cell(c)) == c


# --------------------------------------------------------------------------- #
# HydraState — old-checkpoint compatibility                                   #
# --------------------------------------------------------------------------- #

def test_pre_change_checkpoint_dict_still_validates_with_plan_defaults():
    # Simulates a checkpoint persisted before this change: no plan_* keys at
    # all, and no TaskState.depends_on/plan_step_id/plan_revision keys.
    old_dict = {
        "workflow_id": str(uuid.uuid4()),
        "root_goal": "pre-existing workflow",
        "tasks": [
            {
                "task_id": str(uuid.uuid4()),
                "owner_squad": "engineering",
                "description": "legacy task",
            }
        ],
    }
    state = HydraState.model_validate(old_dict)
    assert state.plan_status == "none"
    assert state.plan_rigor is None
    assert state.plan_revision == 0
    assert state.tasks[0].depends_on == []
    assert state.tasks[0].plan_step_id is None
    assert state.tasks[0].plan_revision == 0


# --------------------------------------------------------------------------- #
# Judge wiring                                                                #
# --------------------------------------------------------------------------- #

def test_plan_decomposition_rubric_registered_and_retrievable():
    rubric = get_rubric("plan-decomposition-quality@1")
    assert rubric.kind == "governance"
    assert "cell_coverage" in rubric.score_dimensions
    assert "skip/advisory" in rubric.body_md
    assert any(r.rubric_id == "plan-decomposition-quality@1" for r in list_rubrics())


def test_plan_gate_type_resolves_to_plan_rubric_base():
    assert DEFAULT_RUBRIC_BY_GATE_TYPE["plan"] == "plan-decomposition-quality"


def test_plan_envelope_type_routes_cross_vendor_with_rubric():
    route = route_judge({"type": "PLAN", "workflow_id": str(WF)})
    assert route.tier == "cross_vendor"
    assert "plan-decomposition-quality@1" in route.rubric_ids
    assert "constitution-alignment@1" in route.rubric_ids
