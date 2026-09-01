"""E2-30: `hydra finalize` resumes synthesis/postcheck over attended results.

The attended loop (`hydra step` / `hydra submit-host-result`) marks tasks
attended-done but never re-entered the graph, so an interactive workflow ended
with no engine DECISION_RECORD, no judge_synthesis verdict, no postcheck and no
RA-8 episodic row (phase stranded at "synthesis").

These tests drive the real CLI entry point (`cli._cmd_finalize`) against a
hermetic LangGraph checkpoint (HYDRA_CHECKPOINT_DB in tmp_path), a stub
dispatcher (no MCP transport, no pp harness, no network) and a redirected
episodic DB.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from hydra_core import cli
from hydra_core.state import HydraState, TaskState

HYDRA_ROOT = Path(__file__).resolve().parents[1]


class _StubDispatcher:
    """Non-live dispatcher: build_supervisor wires no critique client for it,
    so judge_synthesis produces skeleton verdicts without any MCP call."""

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
    """Isolate the checkpoint DB, the eights spool and the episodic DB."""
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


def _seed_workflow(*, completed_all: bool = True):
    """Create a checkpointed three-task attended workflow (executive, garland,
    engineering) whose tasks the host already drove, and return (wf, state)."""
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
    if not completed_all:
        completed = completed[:-1]
        results = results[:-1]

    state = HydraState(
        workflow_id=wf,
        root_goal="attended finalize regression",
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
    return str(wf), tasks


def _finalize(wf: str) -> tuple[int, dict]:
    args = argparse.Namespace(project=str(HYDRA_ROOT), workflow_id=wf, verbose=False)
    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli._cmd_finalize(args)
    out = buf.getvalue()
    start = out.index("{")
    return rc, json.loads(out[start:])


def test_finalize_synthesizes_over_attended_results(hermetic) -> None:
    """Three attended tasks -> one engine DECISION_RECORD with three artifact
    refs, an episodic row, and a terminal phase."""
    from hydra_core.memory import list_episodic

    wf, tasks = _seed_workflow()
    rc, payload = _finalize(wf)

    assert rc == 0, payload
    assert payload["ok"] is True
    assert payload["status"] == "finalized"
    assert payload["decision_record_id"]
    assert len(payload["artifact_refs"]) == 3, payload["artifact_refs"]
    # The tail really ran: postcheck set a terminal phase and judge_synthesis
    # recorded a verdict on the synthesized record.
    assert payload["phase"] == "done", payload

    # RA-8: the DECISION_RECORD reached episodic memory.
    rows = list_episodic(wf, db=hermetic)
    kinds = [r.get("kind") for r in rows]
    assert "decision_record" in kinds, kinds

    # The synthesized record cites every attended squad.
    from hydra_core.supervisor import build_supervisor
    sup = build_supervisor(project_root=HYDRA_ROOT, dispatcher=_StubDispatcher())
    snap = sup.get_state({"configurable": {"thread_id": wf}})
    state = HydraState.model_validate(snap.values)
    assert state.verdicts, "judge_synthesis recorded no verdict"
    origins = {e.get("origin_squad") for e in state.envelopes}
    assert {"executive", "garland", "engineering"} <= origins, origins
    record = next(e for e in reversed(state.envelopes)
                  if e.get("type") == "DECISION_RECORD" and e.get("origin_squad") == "hydra")
    for squad in ("executive", "garland", "engineering"):
        assert squad in record["rationale"], record["rationale"]


def test_finalize_reports_tasks_pending(hermetic) -> None:
    """One task still open -> tasks_pending, and the graph is NOT resumed."""
    wf, tasks = _seed_workflow(completed_all=False)
    rc, payload = _finalize(wf)

    assert rc == 0
    assert payload["ok"] is False
    assert payload["status"] == "tasks_pending"
    assert payload["pending"] == [str(tasks[-1].task_id)]

    from hydra_core.supervisor import build_supervisor
    sup = build_supervisor(project_root=HYDRA_ROOT, dispatcher=_StubDispatcher())
    snap = sup.get_state({"configurable": {"thread_id": wf}})
    state = HydraState.model_validate(snap.values)
    assert not [e for e in state.envelopes if e.get("type") == "DECISION_RECORD"]
    assert state.attended_finalized_record_id is None


def test_finalize_is_idempotent(hermetic) -> None:
    """A second finalize returns already_finalized with the same record id and
    writes no second DECISION_RECORD (which would duplicate the RA-8 rows)."""
    from hydra_core.memory import list_episodic

    wf, _tasks = _seed_workflow()
    rc1, first = _finalize(wf)
    assert rc1 == 0 and first["status"] == "finalized"
    rows_after_first = len(list_episodic(wf, db=hermetic))

    rc2, second = _finalize(wf)
    assert rc2 == 0
    assert second["ok"] is True
    assert second["status"] == "already_finalized"
    assert second["decision_record_id"] == first["decision_record_id"]
    assert len(list_episodic(wf, db=hermetic)) == rows_after_first


def test_materialize_skips_tasks_that_already_have_envelopes() -> None:
    """A task the in-graph dispatch (or ingest) already produced an envelope for
    is not double-counted by the attended materialiser."""
    t = TaskState(owner_squad="engineering", description="x")
    state = HydraState(
        root_goal="g",
        tasks=[t],
        envelopes=[{"type": "DECISION_RECORD", "origin_squad": "engineering",
                    "_task_id": str(t.task_id)}],
        attended_results=[{"task_id": str(t.task_id), "owner_squad": "engineering",
                           "run_id": "r1", "status": "complete",
                           "final_status": "complete"}],
    )
    envelopes, artifacts = cli._materialize_attended_results(state)
    assert envelopes == [] and artifacts == []


def test_step_reports_ready_to_finalize_alias() -> None:
    """`step` keeps `no_pending_task` as a compatibility field alongside the new
    `ready_to_finalize` status."""
    src = (HYDRA_ROOT / "hydra_core" / "cli.py").read_text(encoding="utf-8")
    assert '"status": "ready_to_finalize"' in src
    assert '"no_pending_task": True' in src
