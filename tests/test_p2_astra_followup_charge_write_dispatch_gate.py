"""R2 (MEDIUM, cross-vendor gpt-6-astra, final re-review of feat/planning-phase
HEAD 0bccdcb): ``hydra_core.cli._reconcile_attended_terminal_checkpoint`` kept
processing the emitted-envelope loop even when the atomic budget +
``attended_charge_applied`` write had just failed
(``_charge_write_persist_failed = True``) -- it still claimed the envelope in
the ingest ledger and could dispatch it to a squad (``hydra_core.ingest``),
which incurs and charges ADDITIONAL cost into ``state.budget``. The P1-3
guard already omits ``budget`` from that per-envelope write when the charge
write failed, so that downstream cost was never persisted anywhere, and a
retry -- which repairs the charge write -- would see the envelope's id
already claimed in the ledger and skip it as ``skipped_duplicate``, losing
the delegation permanently.

Fixed: when ``_charge_write_persist_failed`` is set, the function no longer
claims, dispatches, or PLAN-re-enters any emitted envelope on THIS call --
it returns the persist error (``ok: False``, ``checkpoint_persist_failed``)
with everything retryable. A retry first repairs the charge (no marker yet,
so ``charge_and_gate`` legitimately runs again -- the first call's charge
never landed anywhere) and then processes the envelope normally, exactly
once.

MUTATION PROOF: revert the ``if _charge_write_persist_failed:`` gate added
around the emitted-envelope block in ``cli.py`` (restore the original
``if _terminal is not None: ... elif emitted or is_planning_task: ...``
chain) and ``test_charge_write_failure_defers_dispatch_then_retry_dispatches_once``
fails: the fake ``dispatch_ingested_envelopes`` is invoked on the FIRST
(failing) call too, so the assertion that it was called exactly zero times
before the retry trips.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from uuid import uuid4

from hydra_core import cli, host_bridge
from hydra_core.ingest import IngestItemResult, IngestOutcome
from hydra_core.state import HydraState, TaskState

HYDRA_ROOT = Path(__file__).resolve().parents[1]

# Mirrors tests/test_hydra69_round6.py's stateful fake supervisor + append/
# merge-dict reducer semantics; duplicated here so this file stays
# self-contained.
_APPEND_KEYS = {"tasks", "envelopes", "artifacts", "episodic_refs",
                "semantic_queries", "verdicts", "hitl_history"}
_MERGE_DICT_KEYS = {"error_counters", "plan_submit_attempts",
                     "attended_checkpoint_reconciled", "attended_charge_applied"}


def _merge_patch(values: dict, patch: dict) -> dict:
    out = dict(values)
    for k, v in patch.items():
        if k in _APPEND_KEYS:
            existing = list(out.get(k) or [])
            incoming = v if isinstance(v, list) else [v]
            incoming = [
                (item.model_dump(mode="json") if hasattr(item, "model_dump") else item)
                for item in incoming
            ]
            out[k] = existing + incoming
        elif k in _MERGE_DICT_KEYS:
            merged = dict(out.get(k) or {})
            merged.update(v or {})
            out[k] = merged
        else:
            out[k] = v
    return out


class _Snap:
    def __init__(self, values: dict, next_: tuple):
        self.values = values
        self.next = next_


class _StatefulFakeSup:
    def __init__(self, initial_values: dict):
        self._values = dict(initial_values)
        self.update_calls: list[tuple[dict, str | None]] = []

    def get_state(self, config):
        return _Snap(dict(self._values), ())

    def update_state(self, config, patch, as_node=None):
        self.update_calls.append((dict(patch), as_node))
        self._values = _merge_patch(self._values, patch)

    def invoke(self, arg, config=None):  # pragma: no cover -- never reached
        raise AssertionError("this flow never re-enters the graph")


class _FlakyKeyedSup(_StatefulFakeSup):
    """Fails the FIRST checkpoint write whose patch contains `trigger_key`,
    then behaves normally on every later call."""

    def __init__(self, initial_values: dict, trigger_key: str):
        super().__init__(initial_values)
        self._trigger_key = trigger_key
        self._armed = True

    def update_state(self, config, patch, as_node=None):
        if self._armed and self._trigger_key in patch:
            self._armed = False
            raise RuntimeError(
                f"simulated checkpoint persistence failure ({self._trigger_key})"
            )
        super().update_state(config, patch, as_node=as_node)


class _FakeDispatcher:
    def __init__(self, tmp_path):
        self.project_root = tmp_path

    def call_mcp(self, *a, **k):  # pragma: no cover -- never reached
        raise AssertionError("this flow never calls an MCP tool directly")

    def set_squad_packs(self, packs):
        pass


def _submit(capsys, wf_id, run_id, call_key, result_path, project):
    rc = cli._cmd_attended_submit(argparse.Namespace(
        project=str(project), workflow_id=wf_id, run_id=run_id,
        call_key=call_key, result=str(result_path), verbose=False))
    captured = capsys.readouterr()
    out = captured.out or captured.err
    return rc, json.loads(out)


def _write_result(tmp_path, name, payload):
    p = tmp_path / name
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


def test_charge_write_failure_defers_dispatch_then_retry_dispatches_once(
    monkeypatch, tmp_path, capsys,
):
    wf = str(uuid4())
    task_id = str(uuid4())
    state = HydraState(root_goal="ship it", workflow_id=wf)
    fake_sup = _FlakyKeyedSup(state.model_dump(mode="json"), "attended_charge_applied")

    monkeypatch.setattr(cli, "_attended_live_dispatcher",
                        lambda *a, **k: _FakeDispatcher(tmp_path))
    monkeypatch.setattr("hydra_core.supervisor.build_supervisor",
                        lambda **k: fake_sup)

    charge_calls: list[float] = []

    def _fake_charge_and_gate(state, cost, toks, **_kw):
        charge_calls.append(cost)
        state.budget.spent_usd = float(state.budget.spent_usd) + float(cost)
        return (False, False)
    monkeypatch.setattr("hydra_core.governance.charge_and_gate", _fake_charge_and_gate)

    # The delegation envelope this call emits to an MCP squad (engineering).
    # `dispatch_ingested_envelopes` is stubbed rather than driven through the
    # real pp stage loop -- this test targets cli.py's persist-failure gate,
    # not engineering codegen mechanics (covered elsewhere, e.g.
    # tests/test_hybrid_dispatch_e2e.py).
    dev_task_id = str(uuid4())
    dev_task = {
        "id": dev_task_id, "type": "DEV_TASK", "origin_squad": "customer-support",
        "workflow_id": wf, "owner": "backend", "repo": "hydra",
        "target_repo_id": "hydra", "branch": "wf",
        "instructions": "do the thing",
    }

    dispatch_calls: list[list[dict]] = []
    downstream_cost = 0.35

    def _fake_dispatch(state, raw_envelopes, *, packs, dispatcher, already_ingested,
                        emit_fn=None):
        raws = list(raw_envelopes)
        dispatch_calls.append(raws)
        state.budget.spent_usd = float(state.budget.spent_usd) + downstream_cost
        outcome = IngestOutcome()
        for raw in raws:
            outcome.items.append(IngestItemResult(
                envelope_id=str(raw["id"]), envelope_type=raw.get("type"),
                target="engineering", status="done",
            ))
        outcome.charged_usd = downstream_cost
        return outcome
    monkeypatch.setattr("hydra_core.ingest.dispatch_ingested_envelopes", _fake_dispatch)

    # Seed the squad cursor exactly like a non-engineering attended task
    # (mirrors tests/test_hydra69_round6.py's TestDefect2 cursor seeding).
    cfile = host_bridge.begin_squad_stage(
        workflow_id=wf, task_id=task_id, squad_slug="customer-support",
        entrypoint="claude-skill", lead_agent="general-purpose",
        pack_cwd=str(tmp_path), request_text="handle the ticket",
        project_root=tmp_path,
    )
    cfile_path = host_bridge.cursor_path(tmp_path, wf, task_id)
    call_key = f"squad-{task_id}-0"

    r0 = _write_result(tmp_path, "r0.json", {
        "text": "handled; delegating engineering work", "cost_usd": 0.10,
        "tokens_in": 5, "tokens_out": 5,
        "emitted_envelopes": [dev_task],
    })

    rc1, body1 = _submit(capsys, wf, task_id, call_key, r0, tmp_path)
    assert rc1 == 1
    assert body1.get("error") == "checkpoint_persist_failed"

    # Load-bearing: nothing was claimed or dispatched on the failing call.
    assert dispatch_calls == [], (
        "the emitted DEV_TASK must not be dispatched while the charge write "
        f"is still unpersisted; dispatch_ingested_envelopes was called with "
        f"{dispatch_calls!r}"
    )
    from hydra_core.ingest import load_ingested_ids
    assert dev_task_id not in load_ingested_ids(tmp_path, wf), (
        "the emitted DEV_TASK's id must not be claimed in the ingest ledger "
        "while the charge write is still unpersisted"
    )
    values1 = fake_sup._values
    assert not (values1.get("attended_charge_applied") or {}), (
        "the failed charge+marker write must not have landed"
    )
    _budget_after_call1 = (values1.get("budget") or {}).get("spent_usd")
    assert not _budget_after_call1, (
        "no cost -- neither the attended charge nor the downstream dispatch "
        f"cost -- may reach the checkpoint on the failing call; got "
        f"spent_usd={_budget_after_call1!r}"
    )
    assert charge_calls == [0.10], "the attended charge itself still ran once this call"

    # Retry with the SAME call_key: the charge write repairs, and the
    # deferred envelope is now claimed + dispatched exactly once.
    rc2, body2 = _submit(capsys, wf, task_id, call_key, r0, tmp_path)
    assert rc2 == 0, body2
    assert len(dispatch_calls) == 1, (
        f"the retry must dispatch the deferred envelope exactly once; got "
        f"{len(dispatch_calls)} dispatch call(s)"
    )
    assert dispatch_calls[0][0]["id"] == dev_task_id
    assert charge_calls == [0.10, 0.10], (
        "the retry legitimately re-runs charge_and_gate (the first call's "
        f"charge never landed anywhere); got {charge_calls}"
    )
    values2 = fake_sup._values
    assert (values2.get("budget") or {}).get("spent_usd") == 0.10 + downstream_cost, (
        "the checkpoint must land exactly one attended charge plus the "
        f"downstream dispatch cost; got "
        f"{(values2.get('budget') or {}).get('spent_usd')!r}"
    )
    assert any((values2.get("attended_charge_applied") or {}).values())
    assert dev_task_id in load_ingested_ids(tmp_path, wf), (
        "the retry must claim the envelope's id in the ingest ledger"
    )

    # A third submit must be a true no-op: no re-dispatch, no re-charge.
    prior_dispatch_count = len(dispatch_calls)
    prior_charge_count = len(charge_calls)
    rc3, body3 = _submit(capsys, wf, task_id, call_key, r0, tmp_path)
    assert rc3 == 0, body3
    assert len(dispatch_calls) == prior_dispatch_count
    assert len(charge_calls) == prior_charge_count
