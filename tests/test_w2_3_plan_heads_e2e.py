"""Hydra#91 step 3: PlanStep.heads end to end, ingest -> approval -> attended step.

Every hop runs the REAL function (loaded from the capture script next to the
head-less fixture, ``fixtures/w2_3_headless_e2e_S2c29c0d.capture.txt``):

1. ``ingest.dispatch_ingested_envelopes`` (PLAN branch, head validation)
2. ``node_plan_judge`` of ``build_supervisor(force_pure_python=True)`` (plan gate)
3. ``cli._cmd_resume_locked`` with ``gate_only`` approve (``materialise_plan_steps``)
4. ``cli._cmd_attended_step`` (the host_action for the first head leg)

State moves between hops through a real ``SqliteSaver`` using the production
checkpoint serde; the attended step reads it through a new connection. Only head
resolution, ``SquadPack`` objects, the dispatcher, pack discovery in the step and
the artifact/shim writes are stubbed. No judge or LLM call is made.
"""
from __future__ import annotations

import json
import runpy
from pathlib import Path

import pytest

from hydra_core.state import head_leg_run_id

_FIXTURES = Path(__file__).parent / "fixtures"
_CAPTURE = _FIXTURES / "w2_3_headless_e2e_S2c29c0d.capture.txt"
_FIXTURE = _FIXTURES / "w2_3_headless_e2e_S2c29c0d.json"
_ORDER = ["h3", "h1", "h2"]


@pytest.fixture(scope="module")
def cap() -> dict:
    return runpy.run_path(str(_CAPTURE), run_name="w2_3_e2e_capture")


def _work(tmp_path: Path, name: str) -> Path:
    d = tmp_path / name
    d.mkdir()
    return d


def test_e2e_plan_heads_ingest_approve_step_returns_first_declared_head_leg(
        cap, tmp_path):
    assert _ORDER != sorted(_ORDER)  # first declared is not alphabetically first
    res = cap["scenario"](_work(tmp_path, "ok"), _ORDER, ("h1", "h2", "h3"))

    # ingest accepted the plan, and the real judge raised the plan gate
    assert [i["status"] for i in res["ingest"]] == ["drafted"], res["ingest"]
    assert res["plan_ref_present"] is True
    assert res["gate_pending"] == "plan_gate"

    # approval materialised exactly the plan's tasks, heads in declared order,
    # read back from the real SqliteSaver through a fresh connection
    assert res["resume"]["rc"] == 0, res["resume"]
    assert res["plan_status"] == "approved"
    assert res["next_after_approve"] == []
    assert len(res["tasks"]) == 1, res["tasks"]
    task = res["tasks"][0]
    assert task["plan_step_id"] == "step-a"
    assert task["owner_squad"] == "executive"
    assert task["heads"] == _ORDER

    # the first attended step is the head leg for the FIRST DECLARED head
    assert res["step"]["rc"] == 0, res["step"]
    step = res["step"]["out"]
    assert step["ok"] is True and step["status"] == "awaiting_host"
    assert step["leg"] == "head"
    assert step["head"] == "h3"
    assert step["task_id"] == task["task_id"]
    assert step["run_id"] == head_leg_run_id(task["task_id"], "h3")
    assert step["host_action"]["agent_type"] == "plug:h3"
    assert step["host_action"]["call_key"] == f"squad-{step['run_id']}-0"
    # no lead leg was opened; the step opened all three head legs (W2.4), the
    # first declared one being the top-level leg
    for other in ("h1", "h2"):
        assert step["run_id"] != head_leg_run_id(task["task_id"], other)
        assert step["host_action"]["agent_type"] != f"plug:{other}"
    assert step["run_id"] != task["task_id"]
    attended = Path(step["cursor_path"]).parent
    assert sorted(p.name for p in attended.glob("*.json")) == sorted(
        f"{head_leg_run_id(task['task_id'], h)}.json" for h in _ORDER)


def test_e2e_unknown_head_plan_never_reaches_attended_head_leg(cap, tmp_path):
    res = cap["scenario"](_work(tmp_path, "bad"), ["h3", "ghost"], ("h3",))

    # rejected at ingest, naming the step and the head
    [item] = res["ingest"]
    assert item["status"] == "failed"
    assert "step-a" in item["detail"] and "ghost" in item["detail"]
    assert "does not resolve" in item["detail"]
    assert item["errors"][0]["field"] == "steps[0].heads"
    assert res["plan_patch_keys"] == []
    assert res["plan_ref_present"] is False
    assert res["gate_pending"] is None  # no plan gate was raised

    # the same later hops yield no task and no head leg
    assert res["tasks"] == []
    assert res["plan_status"] != "approved"
    assert not (res["resume"]["out"] or {}).get("materialised_task_ids")
    step = res["step"]["out"] or {}
    assert "host_action" not in step, step
    assert "head" not in step and step.get("leg") != "head"
    assert step.get("status") != "awaiting_host"
    assert step.get("no_pending_task") is True, step
    assert not list(Path(res["root"]).rglob("attended/*.json"))


def test_e2e_headless_plan_step_identical_to_start_capture(cap, tmp_path):
    got = cap["capture"](_work(tmp_path, "headless"))
    want = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    assert got == want
    assert got["step"]["host_action"]["agent_type"] == "executive-suite:boardroom"
    assert "head" not in got["step"] and "leg" not in got["step"]
