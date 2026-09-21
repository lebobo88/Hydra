"""The ONE authoritative choke point: `hydra_core.state.make_checkpoint_serde`
scans every checkpoint READ for a non-finite float, regardless of which node,
conditional edge, `as_node=` jump, or CLI command touches it.

FAIL this round closes: `cli._cmd_finalize` scanned only the NEWLY
materialized envelopes/artifacts it was about to inject, then re-entered the
graph via `as_node="judge_per_squad"` -- whose conditional edge routes
straight to `synthesis` WITHOUT ever running `node_judge_per_squad`. A
LEGACY checkpoint that already held a non-finite value in `state.verdicts`
(or `state.envelopes`/`state.artifacts`/`state.plan_ref`) BEFORE finalize was
ever called passed through untouched: `node_synthesis` consumed the poisoned
collection, produced a fresh FINITE `DecisionRecord`, and `node_judge_
synthesis` judged only that new record -- so the workflow reached
postcheck/done and the poisoned stored state was never surfaced.

These tests seed a checkpoint directly (bypassing every write-side guard, the
same way a checkpoint written before those guards existed would look today),
then drive the real CLI entry point and assert the poison is caught BEFORE
synthesis/postcheck/done -- not via any per-node scan, but via the
deserialization choke point itself.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from hydra_core import cli
from hydra_core.state import HydraState, PoisonedStateError, TaskState

HYDRA_ROOT = Path(__file__).resolve().parents[1]


class _StubDispatcher:
    live_execution = False

    def call_mcp(self, server: str, tool: str, args: Any,
                 *, squad_id: str | None = None) -> dict[str, Any]:
        return {"status": "done", "result": {"ok": True}}

    def spawn_subprocess(self, *_a, **_kw) -> dict[str, Any]:
        return {"status": "done"}

    def emit_claude_prompt(self, prompt: str, agent: str | None = None) -> dict[str, Any]:
        return {"status": "host_pickup_required"}

    def invoke_claude_skill(self, skill: str, args: Any) -> dict[str, Any]:
        return {"status": "host_pickup_required"}

    def set_squad_packs(self, packs: dict) -> None:
        pass


@pytest.fixture()
def hermetic(tmp_path, monkeypatch):
    monkeypatch.setenv("HYDRA_CHECKPOINT_DB", str(tmp_path / "checkpoints.db"))
    monkeypatch.setenv("HYDRA_EIGHTS_SPOOL", str(tmp_path / "spool"))
    monkeypatch.setenv("HYDRA_EIGHTS_DEAD_LETTER", str(tmp_path / "dead"))
    monkeypatch.setattr(cli, "_attended_live_dispatcher",
                        lambda *_a, **_kw: _StubDispatcher())

    from hydra_core import memory as _mem
    episodic_db = tmp_path / "episodic.db"
    _orig = _mem.append_episodic

    def _patched(workflow_id, kind, payload, *, key=None, db=None,
                 cells=None, origin_squad=None):
        return _orig(workflow_id, kind, payload, key=key, db=episodic_db,
                     cells=cells, origin_squad=origin_squad)

    monkeypatch.setattr(_mem, "append_episodic", _patched)
    return episodic_db


def _seed_workflow():
    """A three-task attended workflow, fully complete, parked at
    phase="synthesis" -- exactly the state `hydra finalize` expects."""
    from hydra_core.supervisor import build_supervisor, _PurePythonRunner

    wf = uuid4()
    tasks = [
        TaskState(owner_squad="executive", description="frame the decision"),
        TaskState(owner_squad="garland", description="draft the creative"),
        TaskState(owner_squad="engineering", description="ship the change"),
    ]
    results = []
    for t in tasks:
        rec = {
            "task_id": str(t.task_id),
            "owner_squad": t.owner_squad,
            "run_id": f"run-{t.owner_squad}",
            "status": "complete",
            "final_status": "complete",
            "summary": "pass",
            "cost_usd": 0.5,
        }
        if t.owner_squad != "engineering":
            rec["artifact_ref"] = {"tier": "episodic",
                                   "key": f"native:{t.owner_squad}:attended",
                                   "summary": "attended artifact"}
        results.append(rec)

    completed = [str(t.task_id) for t in tasks]
    state = HydraState(
        workflow_id=wf,
        root_goal="choke point regression",
        selected_squads=["executive", "garland", "engineering"],
        phase="synthesis",
        tasks=tasks,
        attended_completed_task_ids=completed,
        attended_done_task_ids=list(completed),
        attended_results=results,
    )
    sup = build_supervisor(project_root=HYDRA_ROOT, dispatcher=_StubDispatcher())
    assert not isinstance(sup, _PurePythonRunner), "langgraph required for this test"
    config = {"configurable": {"thread_id": str(wf)}}
    sup.update_state(config, state.model_dump(mode="json"), as_node="judge_per_squad")
    return str(wf)


def _poison_field(wf: str, patch: dict) -> None:
    """Write `patch` directly onto the checkpoint -- bypassing every
    write-side guard, exactly like a checkpoint written before those guards
    existed would look today. `update_state`'s WRITE side never scans (only
    the choke point's READ side does), so this succeeds unconditionally."""
    from hydra_core.supervisor import build_supervisor

    sup = build_supervisor(project_root=HYDRA_ROOT, dispatcher=_StubDispatcher())
    config = {"configurable": {"thread_id": wf}}
    sup.update_state(config, patch, as_node="judge_per_squad")


def _finalize(wf: str) -> tuple[int, dict]:
    args = argparse.Namespace(project=str(HYDRA_ROOT), workflow_id=wf, verbose=False)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli._cmd_finalize(args)
    out = buf.getvalue()
    start = out.index("{")
    return rc, json.loads(out[start:])


def _raw_channel_values(wf: str) -> dict:
    """Peek at the persisted checkpoint WITHOUT the scanning serde -- the
    only way to inspect a poisoned checkpoint's contents directly, since
    every scanned read (`sup.get_state`) now refuses it. Used purely to
    prove the poisoned checkpoint was left UNTOUCHED (phase unchanged, no
    DECISION_RECORD appended) rather than silently repaired or advanced."""
    import os
    import sqlite3

    from hydra_core.state import BudgetLedger, TaskState as _TaskState
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
    from langgraph.checkpoint.sqlite import SqliteSaver

    cp_path = os.environ["HYDRA_CHECKPOINT_DB"]
    conn = sqlite3.connect(cp_path, check_same_thread=False)
    try:
        raw_serde = JsonPlusSerializer(
            allowed_msgpack_modules=[HydraState, _TaskState, BudgetLedger],
        )
        saver = SqliteSaver(conn, serde=raw_serde)
        tup = saver.get_tuple({"configurable": {"thread_id": wf}})
        assert tup is not None
        return tup.checkpoint.get("channel_values") or {}
    finally:
        conn.close()


def test_poisoned_verdicts_surface_at_choke_point_before_finalize(hermetic) -> None:
    wf = _seed_workflow()
    _poison_field(wf, {"verdicts": [
        {"outcome": "pass", "target_envelope_id": "x",
         "score_json": {"quality": float("nan")}},
    ]})

    rc, payload = _finalize(wf)

    assert rc == 0
    assert payload["ok"] is False
    assert payload["status"] == "unjudgeable"
    assert "verdicts" in payload["field"], payload

    values = _raw_channel_values(wf)
    assert values.get("phase") == "synthesis", "checkpoint must be untouched"
    assert not [e for e in (values.get("envelopes") or [])
                if e.get("type") == "DECISION_RECORD"], "must never reach synthesis"
    assert values.get("attended_finalized_record_id") in (None, ""), values


def test_poisoned_envelopes_surface_at_choke_point_before_finalize(hermetic) -> None:
    wf = _seed_workflow()
    _poison_field(wf, {"envelopes": [
        {"id": "e1", "type": "DEV_TASK", "origin_squad": "engineering",
         "_bad": float("inf")},
    ]})

    rc, payload = _finalize(wf)

    assert rc == 0
    assert payload["ok"] is False
    assert payload["status"] == "unjudgeable"
    assert "envelopes" in payload["field"], payload

    values = _raw_channel_values(wf)
    assert values.get("phase") == "synthesis"
    assert values.get("attended_finalized_record_id") in (None, ""), values


def test_poisoned_artifacts_surface_at_choke_point_before_finalize(hermetic) -> None:
    wf = _seed_workflow()
    _poison_field(wf, {"artifacts": [
        {"ref": "run-1", "kind": "code", "cost_usd": float("-inf")},
    ]})

    rc, payload = _finalize(wf)

    assert rc == 0
    assert payload["ok"] is False
    assert payload["status"] == "unjudgeable"
    assert "artifacts" in payload["field"], payload

    values = _raw_channel_values(wf)
    assert values.get("phase") == "synthesis"
    assert values.get("attended_finalized_record_id") in (None, ""), values


def test_poisoned_plan_ref_surface_at_choke_point_before_finalize(hermetic) -> None:
    wf = _seed_workflow()
    _poison_field(wf, {"plan_ref": {
        "plan_id": "p1", "steps": [{"estimated_budget_usd": float("nan")}],
    }})

    rc, payload = _finalize(wf)

    assert rc == 0
    assert payload["ok"] is False
    assert payload["status"] == "unjudgeable"
    assert "plan_ref" in payload["field"], payload

    values = _raw_channel_values(wf)
    assert values.get("phase") == "synthesis"
    assert values.get("attended_finalized_record_id") in (None, ""), values


def test_clean_checkpoint_unaffected_control(hermetic) -> None:
    """Control: a checkpoint with no non-finite value anywhere finalizes
    exactly as before -- the choke point must never false-positive on
    ordinary, finite data, and no extra judging beyond the normal
    judge_synthesis pass occurs."""
    wf = _seed_workflow()
    rc, payload = _finalize(wf)

    assert rc == 0
    assert payload["ok"] is True
    assert payload["status"] == "finalized"
    assert payload["phase"] == "done"

    from hydra_core.supervisor import build_supervisor
    sup = build_supervisor(project_root=HYDRA_ROOT, dispatcher=_StubDispatcher())
    snap1 = sup.get_state({"configurable": {"thread_id": wf}})
    state1 = HydraState.model_validate(snap1.values)
    verdict_count = len(state1.verdicts)
    assert verdict_count >= 1, state1.verdicts
    # A second read (get_state again) must not trigger any additional
    # judging -- the choke point only SCANS the deserialized value, it never
    # re-dispatches a judge call as a side effect of reading.
    snap2 = sup.get_state({"configurable": {"thread_id": wf}})
    state2 = HydraState.model_validate(snap2.values)
    assert len(state2.verdicts) == verdict_count


def test_poisoned_state_error_carries_field_path() -> None:
    """Unit-level pin on the exception shape the CLI/MCP catch clauses rely
    on -- `.field` is a plain attribute, not buried in `args`."""
    exc = PoisonedStateError("$.verdicts[0].score_json.quality")
    assert exc.field == "$.verdicts[0].score_json.quality"
    assert "non-finite" in str(exc)
