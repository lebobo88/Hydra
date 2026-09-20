"""`hydra replay` on a poisoned checkpoint.

Ground truth (verified end-to-end, not asserted from a docstring): `_cmd_replay`
loads the SOURCE checkpoint via `sup.get_state(source_config)` (see
`hydra_core.cli._cmd_replay`), which routes through the identical scanning
serde (`hydra_core.state.make_checkpoint_serde`) every other checkpoint read
uses. A poisoned checkpoint therefore makes PLAIN `hydra replay` refuse
exactly like `hydra status`/`hydra finalize` — it is NOT a working recovery
path by itself, contrary to what an earlier (now-corrected) round of this
same remediation told operators.

The only way to replay a poisoned checkpoint is the explicit opt-in
`--sanitize-non-finite` flag, which bypasses the scanning serde ONLY for the
source read (`_raw_checkpoint_channel_values`), sanitizes with
`strict_json.sanitize_non_finite` (substituting non-finite values with null
and recording every substituted field path), and proceeds — writing the
result only under a freshly minted replay workflow_id, never back over the
source checkpoint.
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
        root_goal="replay poison regression",
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


def _raw_channel_values(wf: str) -> dict:
    """Peek at the persisted checkpoint WITHOUT the scanning serde, to prove
    the source checkpoint is byte-for-byte unaffected by a sanitized replay."""
    import sqlite3

    from hydra_core.state import BudgetLedger, TaskState as _TaskState
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
    from langgraph.checkpoint.sqlite import SqliteSaver

    import os
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


def _replay(wf: str, *, sanitize: bool = False) -> tuple[int, dict]:
    args = argparse.Namespace(
        project=str(HYDRA_ROOT), workflow_id=wf, from_phase="synthesis",
        swap_model=None, live=False, verbose=False,
        sanitize_non_finite=sanitize,
    )
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli._cmd_replay(args)
    out = buf.getvalue()
    start = out.index("{")
    return rc, json.loads(out[start:])


def test_plain_replay_refuses_poisoned_checkpoint(hermetic):
    wf = _seed_workflow()
    _poison_field(wf, {"verdicts": [
        {"outcome": "pass", "target_envelope_id": "x",
         "score_json": {"quality": float("nan")}},
    ]})

    rc, payload = _replay(wf, sanitize=False)

    assert rc == 0
    assert payload["ok"] is False
    assert payload["status"] == "unjudgeable"
    assert "verdicts" in payload["field"], payload
    # Must recommend the actual working recovery, not falsely imply plain
    # replay itself succeeds.
    assert "sanitize-non-finite" in payload["detail"]
    assert "replay_workflow_id" not in payload


def test_sanitize_flag_replays_and_reports_every_substitution(hermetic):
    wf = _seed_workflow()
    _poison_field(wf, {"artifacts": [
        {"ref": "run-1", "kind": "code", "cost_usd": float("-inf")},
    ]})
    before = _raw_channel_values(wf)

    rc, payload = _replay(wf, sanitize=True)

    assert rc == 0
    assert "replay_workflow_id" in payload
    assert payload["sanitized_non_finite_fields"], payload
    assert any("artifacts" in f for f in payload["sanitized_non_finite_fields"])

    # The source checkpoint is byte-for-byte unaffected.
    after = _raw_channel_values(wf)
    assert after == before


def test_sanitize_flag_off_by_default_control(hermetic):
    """Control: an ordinary --sanitize-non-finite=False (the default) call on
    a CLEAN checkpoint behaves exactly like a normal replay always has."""
    wf = _seed_workflow()
    rc, payload = _replay(wf, sanitize=False)

    assert rc == 0
    assert "replay_workflow_id" in payload
    assert payload["sanitized_non_finite_fields"] is None
