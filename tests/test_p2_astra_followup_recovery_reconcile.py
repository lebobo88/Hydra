"""P2-1 (cross-vendor gpt-6-astra, HIGH): `_cmd_recover_stalled_stage` used to
write its OWN copy of the terminal charge/bookkeeping block --
`attended_completed_task_ids`/`attended_done_task_ids`/`attended_results`/
`open_pp_runs`/`budget` -- but never `attended_charge_applied` /
`attended_checkpoint_reconciled`. Since the step-poll refactor made
`host_bridge.poll_smoke_job` stamp `terminal_call_key` on every terminal
cursor (including ones recovery itself finalizes), a LATER same-call_key
`submit`/`step` reaches `_reconcile_attended_terminal_checkpoint`, finds no
marker for `run_id:terminal_call_key`, and charges the budget a SECOND time.

Fixed: `_cmd_recover_stalled_stage` now routes its terminal result through
the SAME shared `_reconcile_attended_terminal_checkpoint` every other
terminal-driving caller uses, so the markers land and a later reconciliation
attempt against the same identity is cached, not re-charged.

These tests mock `host_bridge.recover_stalled_stage` itself (mirrors
`tests/test_hydra69_round6_gap.py`'s
`TestRemainingGapRecoverStalledStageRefusesTerminalWorkflow`) -- the
CLI-level bookkeeping under test does not depend on the real pp-ledger
recovery mechanics, only on the shape of the dict `recover_stalled_stage`
returns. `host_bridge.mark_charged`/`load_cursor` themselves are NOT
mocked, so the real cursor-file "charged" flag genuinely flows between
calls exactly as it would in production.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
from pathlib import Path
from uuid import uuid4

import pytest

from hydra_core import cli, host_bridge
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
    monkeypatch.setattr(cli, "_attended_live_dispatcher", lambda *_a, **_kw: _StubDispatcher())
    return tmp_path


def _seed_non_terminal_workflow(run_id, task):
    """A normal, non-terminal workflow with one open engineering task and a
    matching open_pp_runs entry -- mirrors what a real attended dispatch
    would have left on the checkpoint just before the stage stalled."""
    from hydra_core.supervisor import build_supervisor, _PurePythonRunner

    wf = uuid4()
    state = HydraState(
        workflow_id=wf, root_goal="P2-1 recovery reconcile repro",
        phase="dispatch", pending_hitl=None, tasks=[task],
        open_pp_runs=[{"run_id": run_id, "task_id": str(task.task_id),
                       "squad_slug": "engineering"}],
    )
    sup = build_supervisor(project_root=HYDRA_ROOT, dispatcher=_StubDispatcher())
    assert not isinstance(sup, _PurePythonRunner), "langgraph required for this test"
    config = {"configurable": {"thread_id": str(wf)}}
    sup.update_state(config, state.model_dump(mode="json"), as_node="dispatch")
    return str(wf), sup, config


def _seed_cursor(wf, run_id):
    cfile = host_bridge.cursor_path(HYDRA_ROOT, wf, run_id)
    cfile.parent.mkdir(parents=True, exist_ok=True)
    host_bridge.save_cursor(cfile, {
        "schema": host_bridge.CURSOR_SCHEMA, "kind": "engineering",
        "workflow_id": wf, "run_id": run_id, "state": "stalled_infra",
        "charged": False,
    })
    return cfile


def _run_recover(monkeypatch, wf, run_id, *, fake_recover):
    monkeypatch.setattr(host_bridge, "recover_stalled_stage", fake_recover)
    args = argparse.Namespace(project=str(HYDRA_ROOT), workflow_id=wf, verbose=False)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        rc = cli._cmd_recover_stalled_stage(args, HYDRA_ROOT, wf, run_id)
    out = buf.getvalue()
    start = out.index("{")
    return rc, json.loads(out[start:])


def _cursor_aware_recover(cfile, run_id, task_id, *, terminal_call_key):
    """A fake `host_bridge.recover_stalled_stage` shaped like the real
    function's contract: reports `already_charged` from the REAL cursor
    file's `charged` flag (exactly like `_step_result` does), so the second
    call in a test correctly observes the first call's `mark_charged`.

    Also flips the on-disk cursor's `state` to the terminal `"complete"` --
    exactly like the real function does before returning -- so
    `host_bridge.mark_charged` (which only sets `charged=True` for a cursor
    whose `state` is already terminal) actually persists across the two
    calls, mirroring production behaviour instead of silently no-op'ing."""
    def _fake(dispatcher, *, cursor_file, workflow_terminal=False):
        cursor = host_bridge.load_cursor(cursor_file)
        already_charged = bool(cursor.get("charged", False))
        if cursor.get("state") != "complete":
            cursor["state"] = "complete"
            cursor["final_status"] = "complete"
            host_bridge.save_cursor(cursor_file, cursor)
        return {
            "ok": True, "status": "complete", "state": "complete",
            "run_id": run_id,
            "stage_id": "stage-1", "task_id": task_id,
            "squad_slug": "engineering", "cost_usd": 0.35,
            "tokens_in": 10, "tokens_out": 5, "cost_source": "measured",
            "terminal_call_key": terminal_call_key,
            "already_charged": already_charged,
        }
    return _fake


class TestP2_1RecoveryReconcileMarkers:
    def test_recovery_writes_charge_and_reconciled_markers_charged_once(
        self, hermetic, monkeypatch,
    ):
        task = TaskState(owner_squad="engineering", description="ship the feature")
        task_id = str(task.task_id)
        run_id = "run-p2-1-a"
        wf, sup, config = _seed_non_terminal_workflow(run_id, task)
        cfile = _seed_cursor(wf, run_id)

        fake = _cursor_aware_recover(cfile, run_id, task_id, terminal_call_key="tck-p2-1")
        rc, body = _run_recover(monkeypatch, wf, run_id, fake_recover=fake)
        assert rc == 0, body
        assert body["ok"] is True

        values = sup.get_state(config).values
        spent = float((values.get("budget") or {}).get("spent_usd") or 0.0)
        assert spent == pytest.approx(0.35), (
            f"recovery's already-incurred cost must be charged; spent_usd={spent!r}"
        )
        recon_key = f"{run_id}:tck-p2-1"
        charge_marker = (values.get("attended_charge_applied") or {})
        reconciled_marker = (values.get("attended_checkpoint_reconciled") or {})
        assert recon_key in charge_marker, (
            "P2-1: recovery must write attended_charge_applied for its own "
            f"recon_key; got {charge_marker!r}"
        )
        assert reconciled_marker.get(recon_key) is True, (
            "P2-1: recovery must write attended_checkpoint_reconciled for "
            f"its own recon_key; got {reconciled_marker!r}"
        )
        assert task_id in [str(t) for t in values.get("attended_completed_task_ids") or []]
        assert not any(
            (e or {}).get("run_id") == run_id for e in values.get("open_pp_runs") or []
        ), "the recovered run must be removed from open_pp_runs (ready to finalize)"

        # ---- A same-call_key resubmit (the exact scenario the bug allowed
        # to double-charge) must be CACHED, never a second charge. ----
        fake2 = _cursor_aware_recover(cfile, run_id, task_id, terminal_call_key="tck-p2-1")
        rc2, body2 = _run_recover(monkeypatch, wf, run_id, fake_recover=fake2)
        assert rc2 == 0, body2
        values2 = sup.get_state(config).values
        spent2 = float((values2.get("budget") or {}).get("spent_usd") or 0.0)
        assert spent2 == pytest.approx(0.35), (
            "P2-1: a retry against the same terminal_call_key must never "
            f"double-charge; spent_usd went {spent!r} -> {spent2!r}"
        )

    def test_legacy_recovery_without_terminal_call_key_still_charges_once(
        self, hermetic, monkeypatch,
    ):
        """A recovery-terminal shape that never stamps terminal_call_key
        (e.g. the `surfaced` direct-merge/finalize path, which never routes
        through `poll_smoke_job`) collapses to the shared "legacy" charge
        identity -- `already_charged=True` on a later call is conclusive on
        its own and must never re-trigger `charge_and_gate`."""
        task = TaskState(owner_squad="engineering", description="ship the feature")
        task_id = str(task.task_id)
        run_id = "run-p2-1-legacy"
        wf, sup, config = _seed_non_terminal_workflow(run_id, task)
        cfile = _seed_cursor(wf, run_id)

        fake = _cursor_aware_recover(cfile, run_id, task_id, terminal_call_key=None)
        rc, body = _run_recover(monkeypatch, wf, run_id, fake_recover=fake)
        assert rc == 0, body

        values = sup.get_state(config).values
        spent = float((values.get("budget") or {}).get("spent_usd") or 0.0)
        assert spent == pytest.approx(0.35)
        recon_key = f"{run_id}:legacy"
        assert (values.get("attended_checkpoint_reconciled") or {}).get(recon_key) is True, (
            "P2-1: a legacy recovery result must still write the shared "
            f"attended_checkpoint_reconciled marker; got "
            f"{values.get('attended_checkpoint_reconciled')!r}"
        )

        fake2 = _cursor_aware_recover(cfile, run_id, task_id, terminal_call_key=None)
        rc2, body2 = _run_recover(monkeypatch, wf, run_id, fake_recover=fake2)
        assert rc2 == 0, body2
        values2 = sup.get_state(config).values
        spent2 = float((values2.get("budget") or {}).get("spent_usd") or 0.0)
        assert spent2 == pytest.approx(0.35), (
            "P2-1: a legacy (no terminal_call_key) recovery retry must "
            f"never double-charge; spent_usd went {spent!r} -> {spent2!r}"
        )
