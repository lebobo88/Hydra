"""RA-8 regression test: node_synthesis must persist the synthesized
DECISION_RECORD and per-artifact rows to episodic memory.

The test runs the supervisor through to synthesis and asserts that
memory.append_episodic is called with the expected arguments.  A tmp db path
is used (via monkeypatching) so the real ~/.hydra/episodic.db is never touched.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

HYDRA_ROOT = Path(__file__).resolve().parents[1]


class _StubCritique:
    def critique(self, *, vendor, artifact_text, rubric_md):
        return {"outcome": "pass", "critique_md": "good " * 30,
                "score_json": {"correctness": 9}}


class _ScriptedDispatcher:
    """Non-live scripted dispatcher — runs the full in-graph path."""

    def __init__(self):
        self.calls: list[tuple[str, str]] = []
        self.skill_calls: list[str] = []

    def call_mcp(self, server: str, tool: str, args: Any,
                 *, squad_id: str | None = None) -> dict[str, Any]:
        self.calls.append((server, tool))
        return {"status": "done", "result": {"ok": True}}

    def spawn_subprocess(self, *_a, **_kw) -> dict[str, Any]:
        return {"status": "done"}

    def emit_claude_prompt(self, prompt: str, agent: str | None = None) -> dict[str, Any]:
        return {"status": "host_pickup_required"}

    def invoke_claude_skill(self, skill: str, args: Any) -> dict[str, Any]:
        self.skill_calls.append(skill)
        return {"status": "host_pickup_required"}

    def set_squad_packs(self, packs: dict) -> None:
        pass


@pytest.fixture(autouse=True)
def _no_git_harvest(monkeypatch):
    monkeypatch.setattr("hydra_core.squad_node.harvest_pp_run_artifacts",
                        lambda **_k: None)


def _invoke_supervisor(runner, state):
    from hydra_core.supervisor import _PurePythonRunner
    if isinstance(runner, _PurePythonRunner):
        return runner.invoke(state)
    out = runner.invoke(
        state, config={"configurable": {"thread_id": str(state.workflow_id)}}
    )
    from hydra_core.state import HydraState
    return HydraState.model_validate(out) if isinstance(out, dict) else out


def test_synthesis_persists_decision_record_and_artifacts(
    monkeypatch, tmp_path
) -> None:
    """RA-8: running to synthesis should call memory.append_episodic at least
    once with kind='decision_record' and once per artifact MemoryRef.
    The test monkeypatches append_episodic to a real SQLite in tmp_path so
    the real episodic.db is never touched, and the actual code path is exercised
    (not a mock spy — we verify the DB contents with list_episodic).
    """
    from hydra_core import memory as _mem
    from hydra_core.memory import list_episodic

    tmp_db = tmp_path / "episodic_test.db"

    # Redirect append_episodic to the tmp db for this test.
    _orig = _mem.append_episodic

    def _patched_append(workflow_id, kind, payload, *, key=None, db=None,
                        cells=None, origin_squad=None):
        return _orig(workflow_id, kind, payload, key=key, db=tmp_db,
                     cells=cells, origin_squad=origin_squad)

    monkeypatch.setattr(_mem, "append_episodic", _patched_append)

    from hydra_core.supervisor import build_supervisor
    from hydra_core.state import HydraState

    disp = _ScriptedDispatcher()
    runner = build_supervisor(
        project_root=HYDRA_ROOT,
        dispatcher=disp,
        critique_client=_StubCritique(),
        force_pure_python=True,
    )

    wf_id = uuid4()
    state = HydraState(
        workflow_id=wf_id,
        root_goal="ra8 episodic memory test",
        # Use a stub squad so dispatch completes cleanly without pp calls.
        selected_squads=["healthcare"],
    )
    final = _invoke_supervisor(runner, state)

    # The supervisor must have reached synthesis (phase=done or judge_synthesis
    # or postcheck — just not crashed before synthesis).
    # Allow surfaced too (stub returns surfaced status).
    assert final.phase in (
        "done", "judge_synthesis", "postcheck", "surfaced", "halted"
    ), f"Unexpected final phase: {final.phase}"

    # The episodic db for this workflow_id should now have a decision_record row.
    rows = list_episodic(wf_id, db=tmp_db)
    decision_rows = [r for r in rows if r["kind"] == "decision_record"]
    assert decision_rows, (
        "RA-8: expected at least one episodic row with kind='decision_record' "
        f"after synthesis, but found: {[r['kind'] for r in rows]}"
    )

    dr = decision_rows[0]
    payload = dr["payload"]
    assert "rationale" in payload, f"decision_record payload missing 'rationale': {payload}"
    assert "squads" in payload, f"decision_record payload missing 'squads': {payload}"
    assert "artifact_refs" in payload, f"decision_record payload missing 'artifact_refs': {payload}"


def test_synthesis_persists_one_row_per_artifact_memoryref(
    monkeypatch, tmp_path
) -> None:
    """RA-8 per-artifact loop coverage: when state.artifacts is non-empty,
    node_synthesis must write one episodic row per artifact MemoryRef key
    (in addition to the decision_record row).

    We pre-seed state.artifacts with 2 distinct artifact dicts (carrying `ref`
    keys so their MemoryRef keys are predictable), run the supervisor through
    synthesis with a stub squad (no pp calls needed), then assert:
      - 1 decision_record row
      - at least 2 rows whose payload.ref_key matches the pre-seeded ref keys
    This exercises the per-artifact loop at supervisor.py lines 2857-2865.
    """
    from hydra_core import memory as _mem
    from hydra_core.memory import list_episodic

    tmp_db = tmp_path / "episodic_artifact_test.db"
    _orig = _mem.append_episodic

    def _patched_append(workflow_id, kind, payload, *, key=None, db=None,
                        cells=None, origin_squad=None):
        return _orig(workflow_id, kind, payload, key=key, db=tmp_db,
                     cells=cells, origin_squad=origin_squad)

    monkeypatch.setattr(_mem, "append_episodic", _patched_append)

    from hydra_core.supervisor import build_supervisor
    from hydra_core.state import HydraState

    disp = _ScriptedDispatcher()
    runner = build_supervisor(
        project_root=HYDRA_ROOT,
        dispatcher=disp,
        critique_client=_StubCritique(),
        force_pure_python=True,
    )

    wf_id = uuid4()
    # Pre-seed 2 artifacts with distinct `ref` keys and kinds so node_synthesis
    # builds MemoryRef entries with predictable keys — exercises the per-artifact
    # loop that was untested when artifacts=[].
    art_a = {"ref": f"run-{wf_id}-A", "kind": "pp_run"}
    art_b = {"ref": f"run-{wf_id}-B", "kind": "creative_output"}
    state = HydraState(
        workflow_id=wf_id,
        root_goal="ra8 per-artifact loop test",
        selected_squads=["healthcare"],  # stub — no pp calls; runs cleanly
        artifacts=[art_a, art_b],        # pre-seeded; preserved through append channel
    )
    final = _invoke_supervisor(runner, state)

    # Synthesis must have been reached (allow surfaced — stub produces surfaced).
    assert final.phase in (
        "done", "judge_synthesis", "postcheck", "surfaced", "halted"
    ), f"Unexpected final phase: {final.phase}"

    rows = list_episodic(wf_id, db=tmp_db)
    assert rows, "No episodic rows written — synthesis did not call append_episodic"

    # (1) decision_record row must exist.
    decision_rows = [r for r in rows if r["kind"] == "decision_record"]
    assert decision_rows, (
        f"RA-8: missing decision_record row. Rows written: {[r['kind'] for r in rows]}"
    )

    # (2) one episodic row per pre-seeded artifact ref key.
    # The per-artifact loop uses key=f"ep:{workflow_id}:{_art_ref.key}".
    # _art_ref.key for art_a is art_a["ref"] (picked up via _art.get("ref")).
    persisted_ref_keys = {
        r["payload"].get("ref_key") for r in rows if r["kind"] != "decision_record"
    }
    assert art_a["ref"] in persisted_ref_keys, (
        f"RA-8: artifact ref '{art_a['ref']}' not persisted. "
        f"Persisted ref_keys: {persisted_ref_keys}"
    )
    assert art_b["ref"] in persisted_ref_keys, (
        f"RA-8: artifact ref '{art_b['ref']}' not persisted. "
        f"Persisted ref_keys: {persisted_ref_keys}"
    )

    # (3) total rows = decision_record + at least 2 artifact rows.
    assert len(rows) >= 3, (
        f"RA-8: expected at least 3 rows (1 decision_record + 2 artifacts), "
        f"got {len(rows)}: {[r['kind'] for r in rows]}"
    )


def test_synthesis_episodic_fail_soft(monkeypatch) -> None:
    """RA-8 fail-soft: if append_episodic raises, node_synthesis must still
    complete and return the DecisionRecord normally (no exception propagated)."""
    from hydra_core import memory as _mem

    def _always_raise(*a, **kw):
        raise RuntimeError("simulated DB failure for RA-8 fail-soft test")

    monkeypatch.setattr(_mem, "append_episodic", _always_raise)

    from hydra_core.supervisor import build_supervisor
    from hydra_core.state import HydraState

    disp = _ScriptedDispatcher()
    runner = build_supervisor(
        project_root=HYDRA_ROOT,
        dispatcher=disp,
        critique_client=_StubCritique(),
        force_pure_python=True,
    )

    state = HydraState(
        root_goal="ra8 fail-soft test",
        selected_squads=["healthcare"],
    )
    # Must not raise — the DB failure is swallowed.
    final = _invoke_supervisor(runner, state)

    # Synthesis completed: a DECISION_RECORD envelope exists.
    decision_records = [
        e for e in final.envelopes
        if isinstance(e, dict) and e.get("type") == "DECISION_RECORD"
    ]
    assert decision_records, (
        "RA-8 fail-soft: synthesis must produce a DECISION_RECORD even when "
        "append_episodic raises. Envelopes: "
        f"{[e.get('type', '?') for e in final.envelopes if isinstance(e, dict)]}"
    )
