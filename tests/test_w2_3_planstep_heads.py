"""Hydra#91 step 1: ``PlanStep.heads`` (schema + carry onto TaskState).

* ``heads`` is an optional list of slugs, declared LAST on ``PlanStep`` and
  hidden from every dump while empty via ``Field(exclude_if=...)`` ALONE (no
  model_serializer / field_serializer: a custom serializer once turned a
  validation-bypassing NaN budget into ``None`` in json-mode dumps).
* A head-less plan dumps and materialises byte-identically to the start commit
  (fixture ``w2_3_headless_plan_S46e456e.json``, produced by the capture script
  next to it at commit 46e456e BEFORE any source edit).
* ``materialise_plan_steps`` carries the declared order onto ``TaskState.heads``.
"""
from __future__ import annotations

import json
import math
import re
import runpy
import sqlite3
import tomllib
from pathlib import Path
from typing import Any

import pytest

from hydra_core.schemas import Plan, PlanStep
from hydra_core.state import HydraState, TaskState
from hydra_core.supervisor import materialise_plan_steps
from tests.test_w2_2_head_dissent_record import _serde

_FIXTURES = Path(__file__).parent / "fixtures"
_CAPTURE = _FIXTURES / "w2_3_headless_plan_S46e456e.capture.txt"
_FIXTURE = _FIXTURES / "w2_3_headless_plan_S46e456e.json"
_ROOT = Path(__file__).resolve().parents[1]
_ORDER = ["h3", "h1", "h2"]


def _step(**kw: Any) -> PlanStep:
    base: dict[str, Any] = dict(
        step_id="s1", target_squad="engineering", envelope_type="DEV_TASK",
        description="d", estimated_budget_usd=1.0, rationale="r")
    base.update(kw)
    return PlanStep(**base)


@pytest.fixture(scope="module")
def _capture() -> dict:
    mod = runpy.run_path(str(_CAPTURE), run_name="w2_3_capture")
    return json.loads(json.dumps(mod["capture"](), sort_keys=True))


def _approved_state(steps: list[dict[str, Any]]) -> HydraState:
    plan = runpy.run_path(str(_CAPTURE), run_name="w2_3_capture")["sample_plan"]()
    ref = plan.model_dump(mode="json")
    ref["steps"] = steps
    return HydraState(
        root_goal="sample goal", workflow_id=plan.workflow_id, plan_ref=ref,
        plan_status="judged", plan_revision=1, phase="approval",
        hitl_history=[{"gate_node": "plan_gate", "resolution": "approve",
                       "plan_revision": 1}],
    )


def _raw_steps() -> list[dict[str, Any]]:
    return [
        {"step_id": "a", "target_squad": "engineering",
         "envelope_type": "DEV_TASK", "description": "with heads",
         "heads": list(_ORDER)},
        {"step_id": "b", "target_squad": "engineering",
         "envelope_type": "DEV_TASK", "description": "head-less"},
    ]


# --------------------------------------------------------------------------- #
# schema                                                                      #
# --------------------------------------------------------------------------- #

def test_planstep_heads_default_empty_and_omitted_from_dump() -> None:
    s = _step()
    assert getattr(s, "heads", None) == []
    assert "heads" not in s.model_dump()
    assert "heads" not in s.model_dump(mode="json")
    assert "heads" not in json.loads(s.model_dump_json())
    assert "heads" not in s.model_dump_json()
    # An explicit include does not resurrect an empty list.
    assert s.model_dump(include={"heads"}) == {}
    assert s.model_dump(mode="json", include={"heads"}) == {}
    assert json.loads(s.model_dump_json(include={"heads"})) == {}


def test_planstep_heads_round_trip_preserves_order() -> None:
    s = _step(heads=list(_ORDER))
    assert getattr(s, "heads", None) == _ORDER
    dumped = s.model_dump(mode="json")
    assert dumped.get("heads") == _ORDER
    assert list(dumped)[-1] == "heads"  # declared last
    assert json.loads(s.model_dump_json()).get("heads") == _ORDER
    again = PlanStep.model_validate_json(s.model_dump_json())
    assert getattr(again, "heads", None) == _ORDER
    plan = runpy.run_path(str(_CAPTURE), run_name="w2_3_capture")["sample_plan"]()
    plan = plan.model_copy(update={"steps": [s]})
    back = Plan.model_validate(plan.model_dump(mode="json"))
    assert getattr(back.steps[0], "heads", None) == _ORDER


# --------------------------------------------------------------------------- #
# head-less identity with the start-commit capture                            #
# --------------------------------------------------------------------------- #

_FORMS = ["a_json", "b_python_repr", "c_exclude_none", "d_by_alias",
          "e_model_dump_json", "f_include", "g_exclude", "h_steps"]


@pytest.mark.parametrize("form", _FORMS)
def test_headless_plan_dumps_identical_to_start_capture(
        form: str, _capture: dict) -> None:
    want = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    assert form in want
    assert _capture[form] == want[form]


def test_headless_materialise_identical_to_start_capture(_capture: dict) -> None:
    want = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    assert _capture["i_materialise_patch"] == want["i_materialise_patch"]
    assert all(t["heads"] == [] for t in want["i_materialise_patch"]["tasks"])


# --------------------------------------------------------------------------- #
# materialise carry                                                           #
# --------------------------------------------------------------------------- #

def test_materialise_carries_heads_in_declared_order() -> None:
    patch = materialise_plan_steps(_approved_state(_raw_steps()))
    by_step = {t.plan_step_id: t for t in patch["tasks"]}
    assert by_step["a"].heads == _ORDER
    assert by_step["b"].heads == []
    assert _ORDER != sorted(_ORDER)


def test_materialised_heads_order_survives_sqlite_saver(tmp_path: Path) -> None:
    pytest.importorskip("langgraph.checkpoint.sqlite")
    graph_mod = pytest.importorskip("langgraph.graph")
    from langgraph.checkpoint.sqlite import SqliteSaver

    serde = _serde()
    state = _approved_state(_raw_steps())
    patch = materialise_plan_steps(state)
    g = graph_mod.StateGraph(HydraState)
    g.add_node("n", lambda s: {})
    g.set_entry_point("n")
    g.add_edge("n", graph_mod.END)
    conn = sqlite3.connect(str(tmp_path / "cp.db"), check_same_thread=False)
    try:
        sup = g.compile(checkpointer=SqliteSaver(conn, serde=serde))
        cfg = {"configurable": {"thread_id": "wf-heads"}}
        sup.update_state(cfg, {
            "root_goal": "g", "workflow_id": state.workflow_id,
            "tasks": patch["tasks"]}, as_node="n")
        values = sup.get_state(cfg).values
    finally:
        conn.close()
    loaded = HydraState.model_validate(values)
    by_step = {t.plan_step_id: t for t in loaded.tasks}
    assert by_step["a"].heads == ["h3", "h1", "h2"]
    assert by_step["b"].heads == []


# --------------------------------------------------------------------------- #
# non-finite budgets must survive json-mode dumps (exclude_if alone)          #
# --------------------------------------------------------------------------- #

_INC = {"step_id", "estimated_budget_usd", "heads"}
_EXC = {"rationale"}
_KWARGS = {
    "plain": ({}, {}),
    "include": ({"include": _INC},
                {"include": {"steps": {"__all__": _INC}}}),
    "exclude": ({"exclude": _EXC},
                {"exclude": {"steps": {"__all__": _EXC}}}),
}


def _bad_step(how: str, value: float, heads: list[str]) -> PlanStep:
    good = _step(heads=list(heads))
    if how == "copy":
        return good.model_copy(update={"estimated_budget_usd": value})
    fields = good.model_dump()
    fields["estimated_budget_usd"] = value
    fields["heads"] = list(heads)
    return PlanStep.model_construct(**fields)


@pytest.mark.parametrize("target", ["step_copy", "step_construct",
                                    "plan_copy", "plan_construct"])
@pytest.mark.parametrize("kind", ["plain", "include", "exclude"])
@pytest.mark.parametrize("heads", [[], ["h1", "h2"]], ids=["nohead", "heads"])
@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf],
                         ids=["nan", "+inf", "-inf"])
def test_nonfinite_budget_survives_json_dump(
        value: float, heads: list[str], kind: str, target: str) -> None:
    how = target.split("_")[1]
    step = _bad_step(how, value, heads)
    step_kw, plan_kw = _KWARGS[kind]
    if target.startswith("step"):
        dumped = step.model_dump(mode="json", **step_kw)
    else:
        plan = runpy.run_path(str(_CAPTURE), run_name="w2_3_capture")[
            "sample_plan"]().model_copy(update={"steps": [step]})
        dumped = plan.model_dump(mode="json", **plan_kw)["steps"][0]
    got = dumped.get("estimated_budget_usd")
    assert got is not None, dumped
    assert isinstance(got, float), dumped
    if math.isnan(value):
        assert math.isnan(got), dumped
    else:
        assert math.isinf(got) and math.copysign(1.0, got) == math.copysign(
            1.0, value), dumped
    if heads:
        assert dumped.get("heads") == heads, dumped
    else:
        assert "heads" not in dumped, dumped


# --------------------------------------------------------------------------- #
# declaration guards                                                          #
# --------------------------------------------------------------------------- #

def _floor(spec: str) -> tuple[int, int]:
    m = re.search(r">=\s*(\d+)\.(\d+)", spec)
    assert m, f"no >= lower bound in {spec!r}"
    return int(m.group(1)), int(m.group(2))


def test_declared_pydantic_floor_at_least_2_12() -> None:
    proj = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    deps = [d for d in proj["project"]["dependencies"]
            if re.match(r"pydantic\b(?!-)", d)]
    assert len(deps) == 1, deps
    assert _floor(deps[0]) >= (2, 12), deps[0]

    lock = tomllib.loads((_ROOT / "uv.lock").read_text(encoding="utf-8"))
    me = next(p for p in lock["package"] if p["name"] == proj["project"]["name"])
    entries = [r for r in me["metadata"]["requires-dist"]
               if r["name"] == "pydantic"]
    assert len(entries) == 1, entries
    assert _floor(entries[0]["specifier"]) >= (2, 12), entries[0]


def test_planstep_has_no_custom_serializer() -> None:
    dec = PlanStep.__pydantic_decorators__
    assert not dec.model_serializers
    assert not dec.field_serializers
    assert "heads" in PlanStep.model_fields
    assert PlanStep.model_fields["heads"].exclude_if is not None
    assert list(PlanStep.model_fields)[-1] == "heads"
    assert TaskState.model_fields["heads"].default_factory is list
