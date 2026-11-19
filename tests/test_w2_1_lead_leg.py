"""Hydra#89 step 4: lead leg + completion rule for per-head squad tasks.

Drives the attended CLI verbs in-process against the stateful channel-merge
fake supervisor from ``test_w2_1_cli_head_legs``. State is always read back
through ``sup.get_state`` (channel values), never from a returned patch.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from hydra_core import cli, host_bridge
from hydra_core.cli import _attended_pending_task_ids
from hydra_core.state import HydraState, head_leg_run_id, head_result_key
from tests.test_w2_1_cli_head_legs import (
    _ChannelSup, _cursor_files, _install, _state, _step, _submit, _task,
    _ticket_files, _tid, _values, _wf,
)

_HEADS = ["h1", "h2", "h3"]
_COSTS = {"h1": 0.25, "h2": 0.5, "h3": 0.125}
_LEAD_COST = 1.0
_SENTINEL = "SENTINEL-BODY-7f3a9c"


class _KeyedWritten:
    def __init__(self, key):
        self.key = key

    def model_dump(self, mode="json"):
        return {"key": self.key}


def _install_keyed(monkeypatch, state, **kw):
    out = _install(monkeypatch, state, **kw)
    monkeypatch.setattr(
        "hydra_core.artifact_store.write_native_artifact",
        lambda slug, rel, text: _KeyedWritten(f"mem:{Path(rel).stem}"))
    return out


def _run_heads(project, capsys, heads=_HEADS, abort=()):
    for h in heads:
        s = _step(project, capsys)
        assert s["run_id"] == head_leg_run_id(_tid, h), s
        if h in abort:
            host_bridge.abort_stage(None, cursor_file=s["cursor_path"])
        _submit(project, s["run_id"], s["host_action"]["call_key"],
                {"text": f"{_SENTINEL} {h}", "cost_usd": _COSTS[h],
                 "tokens_in": 10, "tokens_out": 5, "model": "m"}, capsys)


def _lead_submit(project, capsys, lead, cost=_LEAD_COST):
    return _submit(project, lead["run_id"], lead["host_action"]["call_key"],
                   {"text": "lead synthesis", "cost_usd": cost, "tokens_in": 4,
                    "tokens_out": 2, "model": "m"}, capsys)


def _pending(sup):
    st = HydraState.model_validate(_values(sup))
    return _attended_pending_task_ids(st)


def _assert_four_cursors(project, sup):
    """The shared four-cursor / four-ticket / four-charge assertion."""
    v = _values(sup)
    assert len(_cursor_files(project)) == 4
    assert len(_ticket_files(project)) == 4
    assert len(v["attended_charge_applied"]) == 4
    assert v["budget"]["spent_usd"] == pytest.approx(
        sum(_COSTS.values()) + _LEAD_COST)


def _full_flow(project, capsys):
    _run_heads(project, capsys)
    lead = _step(project, capsys)
    _lead_submit(project, capsys, lead)
    return lead


class TestLeadLeg:
    def test_four_cursors_tickets_charges_and_completion_timing(
            self, tmp_path, monkeypatch, capsys):
        sup, _d, _p = _install_keyed(monkeypatch, _state(_task(_HEADS)))

        _run_heads(tmp_path, capsys)

        v = _values(sup)
        assert _tid not in v["attended_completed_task_ids"]
        assert _tid in _pending(sup)
        lead = _step(tmp_path, capsys)
        assert lead["run_id"] == _tid and lead["leg"] == "lead"
        assert "head" not in lead
        assert _tid in _pending(sup)
        assert _tid not in _values(sup)["attended_completed_task_ids"]

        _lead_submit(tmp_path, capsys, lead)

        _assert_four_cursors(tmp_path, sup)
        v = _values(sup)
        assert _tid in v["attended_completed_task_ids"]
        assert _tid not in _pending(sup)
        keys = set(v["attended_charge_applied"])
        assert f"{_tid}:squad-{_tid}-0" in keys
        assert len(keys) == 4

    def test_duplicate_lead_submit_does_not_recharge(
            self, tmp_path, monkeypatch, capsys):
        sup, _d, _p = _install_keyed(monkeypatch, _state(_task(_HEADS)))
        lead = _full_flow(tmp_path, capsys)
        before = _values(sup)

        dup = _lead_submit(tmp_path, capsys, lead)

        after = _values(sup)
        assert dup["ok"] is True
        for k in ("budget", "attended_charge_applied",
                  "attended_completed_task_ids", "attended_done_task_ids",
                  "attended_results"):
            assert after[k] == before[k], k

    def test_lead_cursor_and_prompt_carry_handles_only(
            self, tmp_path, monkeypatch, capsys):
        _install_keyed(monkeypatch, _state(_task(_HEADS)))
        _run_heads(tmp_path, capsys)

        lead = _step(tmp_path, capsys)

        prompt = lead["host_action"]["prompt"]
        line = next(ln for ln in prompt.splitlines()
                    if ln.startswith("upstream_refs:"))
        expect = [f"head:{h} mem:{head_leg_run_id(_tid, h)} (complete)"
                  for h in _HEADS]
        assert line == "upstream_refs: " + ", ".join(expect)
        assert line.index("head:h1") < line.index("head:h2") < line.index("head:h3")
        cursor_text = Path(lead["cursor_path"]).read_text(encoding="utf-8")
        assert _SENTINEL not in prompt
        assert _SENTINEL not in cursor_text
        assert _SENTINEL not in json.dumps(lead)

    def test_handle_without_artifact(self, tmp_path, monkeypatch, capsys):
        def _boom(slug, rel, text):
            raise ValueError("no store")

        _install_keyed(monkeypatch, _state(_task(["h1"])))
        monkeypatch.setattr("hydra_core.artifact_store.write_native_artifact", _boom)
        s = _step(tmp_path, capsys)
        _submit(tmp_path, s["run_id"], s["host_action"]["call_key"],
                {"text": "x", "cost_usd": 0.25}, capsys)

        lead = _step(tmp_path, capsys)

        assert ("upstream_refs: head:h1 (no artifact, complete_unpersisted)"
                in lead["host_action"]["prompt"])

    def test_all_complete_marks_done_and_records_heads(
            self, tmp_path, monkeypatch, capsys):
        sup, _d, _p = _install_keyed(monkeypatch, _state(_task(_HEADS)))
        _full_flow(tmp_path, capsys)

        v = _values(sup)
        assert _tid in v["attended_done_task_ids"]
        rec = next(r for r in v["attended_results"] if r["task_id"] == _tid)
        assert rec["heads"] == {"h1": "complete", "h2": "complete", "h3": "complete"}
        assert rec["status"] == "complete"

    def test_failed_head_still_opens_lead_but_task_not_done(
            self, tmp_path, monkeypatch, capsys):
        sup, _d, _p = _install_keyed(monkeypatch, _state(_task(_HEADS)))

        _run_heads(tmp_path, capsys, abort={"h2"})

        v = _values(sup)
        assert v["attended_head_results"][head_result_key(_tid, "h2")]["status"] == "aborted"
        lead = _step(tmp_path, capsys)
        assert lead["leg"] == "lead" and lead["run_id"] == _tid
        assert f"head:h2 (no artifact, aborted)" in lead["host_action"]["prompt"]

        _lead_submit(tmp_path, capsys, lead)

        v = _values(sup)
        assert _tid in v["attended_completed_task_ids"]
        assert _tid not in v["attended_done_task_ids"]
        rec = next(r for r in v["attended_results"] if r["task_id"] == _tid)
        assert rec["heads"] == {"h1": "complete", "h2": "aborted", "h3": "complete"}

    def test_seeded_surfaced_head_blocks_done(self, tmp_path, monkeypatch, capsys):
        sup, _d, _p = _install_keyed(monkeypatch, _state(_task(["h1", "h2"])))
        _run_heads(tmp_path, capsys, heads=["h1", "h2"])
        key = head_result_key(_tid, "h2")
        rec = dict(_values(sup)["attended_head_results"][key])
        rec["status"] = "surfaced"
        sup.update_state(None, {"attended_head_results": {key: rec}})

        lead = _step(tmp_path, capsys)
        assert "head:h2 (no artifact, surfaced)" in lead["host_action"]["prompt"] \
            or "head:h2 mem:" in lead["host_action"]["prompt"]
        _lead_submit(tmp_path, capsys, lead)

        v = _values(sup)
        assert _tid in v["attended_completed_task_ids"]
        assert _tid not in v["attended_done_task_ids"]
        rec = next(r for r in v["attended_results"] if r["task_id"] == _tid)
        assert rec["heads"]["h2"] == "surfaced"

    def test_negative_single_cursor_selection_fails_four_cursor_assertion(
            self, tmp_path, monkeypatch, capsys):
        # Positive control: the shared assertion holds on the real flow.
        sup, _d, _p = _install_keyed(monkeypatch, _state(_task(_HEADS)))
        _full_flow(tmp_path, capsys)
        _assert_four_cursors(tmp_path, sup)

        # Pre-W2.1: head selection always opens the single task_id cursor.
        monkeypatch.setattr(
            "hydra_core.state.head_leg_run_id", lambda task_id, head: task_id)
        proj2 = tmp_path / "pre"
        proj2.mkdir()
        sup2, _d2, _p2 = _install_keyed(monkeypatch, _state(_task(_HEADS)))
        # Same single-cursor behaviour: heads dispatch onto the task cursor.
        for _ in range(4):
            out = capsys.readouterr()  # drain
            rc = cli._cmd_attended_step(argparse.Namespace(
                project=str(proj2), workflow_id=_wf, verbose=False))
            body = json.loads(capsys.readouterr().out or out.out)
            if rc != 0 or "host_action" not in body:
                break
            _submit(proj2, body["run_id"], body["host_action"]["call_key"],
                    {"text": "x", "cost_usd": 0.1}, capsys)
        with pytest.raises(AssertionError):
            _assert_four_cursors(proj2, sup2)


class TestHeadlessUnchanged:
    def test_headless_cursor_has_no_leg_and_record_has_no_heads(
            self, tmp_path, monkeypatch, capsys):
        sup, _d, _p = _install_keyed(monkeypatch, _state(_task([])))

        s = _step(tmp_path, capsys)
        assert "leg" not in s and "head" not in s
        assert "leg" not in host_bridge.load_cursor(s["cursor_path"])
        _submit(tmp_path, s["run_id"], s["host_action"]["call_key"],
                {"text": "x", "cost_usd": 0.5}, capsys)

        v = _values(sup)
        rec = next(r for r in v["attended_results"] if r["task_id"] == _tid)
        assert "heads" not in rec
        assert _tid in v["attended_done_task_ids"]


class _FlakyHeadSup(_ChannelSup):
    """Fails the FIRST write carrying attended_head_results, then behaves."""

    def __init__(self, state):
        super().__init__(state)
        self.armed = True

    def update_state(self, config, patch, as_node=None):
        if self.armed and "attended_head_results" in patch:
            self.armed = False
            raise RuntimeError("simulated checkpoint persistence failure")
        super().update_state(config, patch, as_node=as_node)


class TestHeadLegPersistFailure:
    def test_failed_head_write_then_retry_charges_once(
            self, tmp_path, monkeypatch, capsys):
        st = _state(_task(["h1", "h2"]))
        _install_keyed(monkeypatch, st)
        sup = _FlakyHeadSup(st)
        monkeypatch.setattr("hydra_core.supervisor.build_supervisor",
                            lambda **k: sup)
        s = _step(tmp_path, capsys)
        ck = s["host_action"]["call_key"]
        res = {"text": "x", "cost_usd": 0.25, "tokens_in": 1, "tokens_out": 1}

        body1 = _submit(tmp_path, s["run_id"], ck, res, capsys, want_rc=1)

        assert body1.get("error") == "checkpoint_persist_failed"
        v = _values(sup)
        assert v["attended_charge_applied"] == {}
        assert v["attended_head_results"] == {}
        assert v["budget"]["spent_usd"] == 0

        body2 = _submit(tmp_path, s["run_id"], ck, res, capsys)

        assert body2["ok"] is True
        v = _values(sup)
        assert v["budget"]["spent_usd"] == pytest.approx(0.25)
        assert len(v["attended_charge_applied"]) == 1
        assert list(v["attended_head_results"]) == [head_result_key(_tid, "h1")]

        _submit(tmp_path, s["run_id"], ck, res, capsys)
        assert _values(sup)["budget"]["spent_usd"] == pytest.approx(0.25)
        assert len(_values(sup)["attended_charge_applied"]) == 1
