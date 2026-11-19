"""Hydra#89 W2.1: TaskState.heads, HydraState.attended_head_results, helpers."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from hydra_core.state import (
    HydraState,
    TaskState,
    head_leg_run_id,
    head_result_key,
)


def _task(**kw):
    return TaskState(owner_squad="x", description="d", **kw)


def test_defaults():
    assert _task().heads == []
    assert HydraState().attended_head_results == {}


def test_legacy_checkpoint_loads_with_defaults():
    legacy = HydraState().model_dump(mode="python")
    legacy.pop("attended_head_results")
    legacy["tasks"] = [{"owner_squad": "x", "description": "d"}]
    legacy["attended_results"] = [{"task_id": "a"}]
    st = HydraState.model_validate(legacy)
    assert st.attended_head_results == {}
    assert st.tasks[0].heads == []
    assert st.tasks[0].owner_squad == "x"
    assert st.attended_results == [{"task_id": "a"}]


def test_heads_accepts_valid_slugs():
    assert _task(heads=["curia-papinian", "gaius"]).heads == ["curia-papinian", "gaius"]
    assert _task(heads=[]).heads == []


@pytest.mark.parametrize("bad", [["a", "a"], ["Bad Slug"], ["-a"], [""], ["a--b"], ["../x"]])
def test_heads_rejects_invalid(bad):
    with pytest.raises(ValidationError):
        _task(heads=bad)


def test_head_leg_run_id_and_result_key():
    assert head_leg_run_id("t1", "gaius") == "t1--gaius"
    assert head_result_key("t1", "gaius") == "t1:gaius"
    with pytest.raises(ValueError):
        head_leg_run_id("t1", "../x")


@pytest.mark.parametrize("task_id,head", [
    ("t1", "a--b"), ("t1", "Bad"), ("", "a"), ("t--a", "b"), ("t1", ""),
])
def test_head_leg_run_id_rejections(task_id, head):
    with pytest.raises(ValueError):
        head_leg_run_id(task_id, head)


def test_run_id_is_unambiguous():
    for task_id, head in [("t", "a"), ("task-1", "gaius"), ("t_1", "a-b")]:
        rid = head_leg_run_id(task_id, head)
        assert rid.split("--", 1) == [task_id, head]
    # The previously colliding pair: both forms are now rejected.
    with pytest.raises(ValueError):
        head_leg_run_id("t--a", "b")
    with pytest.raises(ValueError):
        head_leg_run_id("t", "a--b")


def test_channel_merge_replay_keeps_both_head_keys():
    from langgraph.checkpoint.memory import MemorySaver
    from langgraph.graph import END, START, StateGraph

    g = StateGraph(HydraState)
    g.add_node("noop", lambda s: {})
    g.add_edge(START, "noop")
    g.add_edge("noop", END)
    graph = g.compile(checkpointer=MemorySaver())
    cfg = {"configurable": {"thread_id": "w2-1"}}

    graph.update_state(cfg, {"attended_completed_task_ids": ["t1", "t2"]}, as_node="noop")
    k1, k2 = head_result_key("t1", "a"), head_result_key("t1", "b")
    graph.update_state(cfg, {"attended_head_results": {k1: {"status": "done"}}}, as_node="noop")
    graph.update_state(cfg, {"attended_head_results": {k2: {"status": "done"}}}, as_node="noop")

    vals = graph.get_state(cfg).values
    assert set(vals["attended_head_results"]) == {k1, k2}
    # Plain LastValue list: writes to other keys retain it...
    assert vals["attended_completed_task_ids"] == ["t1", "t2"]
    # ...and it is only cleared by writing [] explicitly.
    graph.update_state(cfg, {"attended_completed_task_ids": []}, as_node="noop")
    vals = graph.get_state(cfg).values
    assert vals["attended_completed_task_ids"] == []
    assert set(vals["attended_head_results"]) == {k1, k2}
