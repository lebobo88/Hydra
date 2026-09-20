"""`hydra status` (per-workflow and global) and the `hydra-mem` MCP read
surfaces must SURFACE a poisoned checkpoint explicitly (see
`hydra_core.state.make_checkpoint_serde` / `PoisonedStateError`), not hide it
behind the ordinary "checkpoint unavailable" trace fallback or an
empty/normal-looking record.

Recovery for a poisoned workflow is NOT in-place repair — there is none. The
operator's only options are to replay from an earlier clean phase
(`hydra replay --from-phase <phase> <id>`) or quarantine/abandon the
workflow_id. Every surface below states this in its `detail` text.
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
    live_execution = False

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


def _seed_workflow():
    from hydra_core.supervisor import build_supervisor, _PurePythonRunner

    wf = uuid4()
    tasks = [TaskState(owner_squad="engineering", description="ship the change")]
    state = HydraState(
        workflow_id=wf,
        root_goal="status poison regression",
        selected_squads=["engineering"],
        phase="synthesis",
        tasks=tasks,
    )
    sup = build_supervisor(project_root=HYDRA_ROOT, dispatcher=_StubDispatcher())
    assert not isinstance(sup, _PurePythonRunner), "langgraph required for this test"
    config = {"configurable": {"thread_id": str(wf)}}
    sup.update_state(config, state.model_dump(mode="json"), as_node="judge_per_squad")
    return str(wf)


def _poison_field(wf: str, patch: dict) -> None:
    from hydra_core.supervisor import build_supervisor

    sup = build_supervisor(project_root=HYDRA_ROOT, dispatcher=_StubDispatcher())
    config = {"configurable": {"thread_id": wf}}
    sup.update_state(config, patch, as_node="judge_per_squad")


def _status(wf: str | None) -> tuple[int, dict]:
    args = argparse.Namespace(project=str(HYDRA_ROOT), workflow_id=wf)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli._cmd_status(args)
    out = buf.getvalue()
    start = out.index("{")
    return rc, json.loads(out[start:])


# --------------------------------------------------------------------------
# `hydra status <id>`
# --------------------------------------------------------------------------

def test_status_single_workflow_reports_poison_not_checkpoint_unavailable(hermetic):
    wf = _seed_workflow()
    _poison_field(wf, {"verdicts": [
        {"outcome": "pass", "target_envelope_id": "x",
         "score_json": {"quality": float("nan")}},
    ]})

    rc, payload = _status(wf)

    assert rc == 0
    assert payload["workflow_id"] == wf
    assert payload["status"] == "unjudgeable"
    assert "verdicts" in payload["field"], payload
    assert "checkpoint unavailable" not in json.dumps(payload)
    # Recovery options must be stated.
    assert "replay" in payload["detail"].lower()
    assert "quarantine" in payload["detail"].lower()


def test_status_single_workflow_clean_control(hermetic):
    wf = _seed_workflow()
    rc, payload = _status(wf)

    assert rc == 0
    assert payload["workflow_id"] == wf
    assert payload["phase"] == "synthesis"
    assert "status" not in payload
    assert "field" not in payload


# --------------------------------------------------------------------------
# global `hydra status`
# --------------------------------------------------------------------------

def test_status_global_reports_poisoned_row_and_lists_others(hermetic):
    # The global listing path builds a real supervisor (squad discovery),
    # which requires `project_root` to actually contain `squads/<n>/
    # squad.yaml` — so `--project` must be HYDRA_ROOT itself here, unlike the
    # single-workflow tests above. Only `.hydra/<workflow_id>/` marker dirs
    # are synthesized (and removed in teardown); the checkpoint DB is fully
    # isolated via HYDRA_CHECKPOINT_DB from the fixture, so no real workflow
    # state is touched.
    poisoned_wf = _seed_workflow()
    _poison_field(poisoned_wf, {"artifacts": [
        {"ref": "run-1", "kind": "code", "cost_usd": float("-inf")},
    ]})
    clean_wf = _seed_workflow()

    marker_dirs = []
    for wf in (poisoned_wf, clean_wf):
        d = HYDRA_ROOT / ".hydra" / wf
        d.mkdir(parents=True)
        (d / "trace.jsonl").write_text("", encoding="utf-8")
        marker_dirs.append(d)
    try:
        rc, payload = _status_at(HYDRA_ROOT, None)
    finally:
        for d in marker_dirs:
            (d / "trace.jsonl").unlink(missing_ok=True)
            d.rmdir()

    assert rc == 0
    rows = {r["workflow_id"]: r for r in payload["workflows"]}
    assert rows[poisoned_wf]["status"] == "unjudgeable"
    assert "artifacts" in rows[poisoned_wf]["field"]
    assert "replay" in rows[poisoned_wf]["detail"].lower()
    assert "quarantine" in rows[poisoned_wf]["detail"].lower()
    # The clean workflow is unaffected and still listed normally.
    assert rows[clean_wf]["phase"] == "synthesis"
    assert "status" not in rows[clean_wf]


def _status_at(project: Path, wf: str | None) -> tuple[int, dict]:
    args = argparse.Namespace(project=str(project), workflow_id=wf)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli._cmd_status(args)
    out = buf.getvalue()
    start = out.index("{")
    return rc, json.loads(out[start:])


# --------------------------------------------------------------------------
# hydra-mem MCP surfaces
# --------------------------------------------------------------------------

def test_mcp_workflow_status_reports_poison(monkeypatch):
    from mcp_servers.hydra_memory import server as s

    monkeypatch.setattr(s, "_load_state_values", lambda wf: {
        "values": {}, "ts": None, "unjudgeable": True,
        "field": "$.verdicts[0].score_json.quality",
    })
    handler = s._tool_handlers()["hydra-mem.workflow_status"]
    out = handler({"workflow_id": "wf-poisoned"})

    assert out["unjudgeable"] is True
    assert out["field"] == "$.verdicts[0].score_json.quality"
    assert "replay" in out["detail"].lower()
    assert "quarantine" in out["detail"].lower()
    assert out.get("phase") is None


def test_mcp_workflow_status_clean_control(monkeypatch):
    from mcp_servers.hydra_memory import server as s

    monkeypatch.setattr(s, "_load_state_values", lambda wf: {
        "values": {"phase": "synthesis", "root_goal": "ok", "tasks": []},
        "ts": "2026-01-01T00:00:00+00:00",
    })
    handler = s._tool_handlers()["hydra-mem.workflow_status"]
    out = handler({"workflow_id": "wf-clean"})

    assert out["phase"] == "synthesis"
    assert "unjudgeable" not in out


def test_mcp_workflows_list_reports_poison_and_others(monkeypatch):
    from mcp_servers.hydra_memory import server as s

    rows = {
        "wf-poisoned": {"unjudgeable": True, "field": "$.artifacts[0].cost_usd",
                        "values": {}, "ts": None},
        "wf-clean": {"values": {"phase": "approval", "root_goal": "fine",
                                "selected_squads": []},
                     "ts": "2026-01-01T00:00:00+00:00"},
    }

    class _FakeConn:
        def close(self):
            pass

    monkeypatch.setattr(s, "_open_checkpoints_ro", lambda: _FakeConn())
    monkeypatch.setattr(s, "_checkpoint_thread_ids", lambda conn, cap=None: list(rows))
    monkeypatch.setattr(s, "_load_state_values", lambda wf: rows[wf])

    handler = s._tool_handlers()["hydra-mem.workflows_list"]
    out = handler({"limit": 50})
    by_id = {w["workflow_id"]: w for w in out["workflows"]}

    assert by_id["wf-poisoned"]["unjudgeable"] is True
    assert by_id["wf-poisoned"]["field"] == "$.artifacts[0].cost_usd"
    assert "replay" in by_id["wf-poisoned"]["detail"].lower()
    assert "quarantine" in by_id["wf-poisoned"]["detail"].lower()
    # Clean workflow still lists normally, unaffected.
    assert by_id["wf-clean"]["phase"] == "approval"
    assert "unjudgeable" not in by_id["wf-clean"]
