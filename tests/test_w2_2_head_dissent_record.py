"""Hydra#90 step 2: retained head/lead dissents reach the DECISION_RECORD.

``cli._materialize_attended_results`` attaches each head's retained dissents
(attributed ``[head:<slug>]``) and the lead's own (``[lead]``) to the squad's
DECISION_RECORD, de-duplicated, and ``node_synthesis`` carries them verbatim
into the workflow record. Head-less tasks are byte-identical to the starting
commit (fixture ``materialize_headless_S6e5dc1e.json``).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import UUID

import pytest

from hydra_core import cli, host_bridge
from hydra_core.state import HydraState, head_result_key
from hydra_core.supervisor import build_supervisor as _REAL_BUILD_SUPERVISOR
from tests.test_w2_1_cli_head_legs import (
    _state, _step, _submit, _task, _tid, _values,
)
from tests.test_w2_1_lead_leg import _install_keyed, _lead_submit

_FIXTURE = (Path(__file__).parent / "fixtures"
            / "materialize_headless_S6e5dc1e.json")
_HEADS = ["h1", "h2", "h3"]
_COST = {"cost_usd": 0.25, "tokens_in": 10, "tokens_out": 5, "model": "m"}
_R1_EXPECTED = ["[head:h2] rollout risk unpriced"]


# --------------------------------------------------------------------------- #
# helpers                                                                     #
# --------------------------------------------------------------------------- #

def _run_flow(project, capsys, *, heads=None, head_dissents=None,
              lead_dissents=None):
    """Real step + submit CLI path for every head leg, then the lead leg.

    Returns the HydraState rebuilt from the checkpoint values.
    """
    heads = list(heads or _HEADS)
    head_dissents = head_dissents or {}
    prompts: dict[str, str] = {}
    for h in heads:
        s = _step(project, capsys)
        assert s["head"] == h, s
        prompts[h] = s["host_action"]["prompt"]
        res = {"text": f"{h} output", **_COST}
        if h in head_dissents:
            res["dissents"] = head_dissents[h]
        _submit(project, s["run_id"], s["host_action"]["call_key"], res, capsys)
    lead = _step(project, capsys)
    assert lead["leg"] == "lead", lead
    prompts["lead"] = lead["host_action"]["prompt"]
    if lead_dissents is None:
        _lead_submit(project, capsys, lead)
    else:
        _submit(project, lead["run_id"], lead["host_action"]["call_key"],
                {"text": "lead synthesis", "cost_usd": 1.0, "tokens_in": 4,
                 "tokens_out": 2, "model": "m", "dissents": lead_dissents},
                capsys)
    return prompts


def _state_for(heads):
    return _state(_task(list(heads)))


def _state_of(sup) -> HydraState:
    return HydraState.model_validate(_values(sup))


def _exec_env(envs: list[dict]) -> dict:
    return next(e for e in envs if e["origin_squad"] == "executive")


def _materialized(sup):
    return cli._materialize_attended_results(_state_of(sup))


def _assert_r1(envs: list[dict]) -> None:
    assert _exec_env(envs)["dissenting_opinions"] == _R1_EXPECTED


def _synthesis(envs: list[dict], arts: list[dict]) -> dict:
    """node_synthesis via the pure-python runner (supervisor.py untouched)."""
    # _install_keyed monkeypatches supervisor.build_supervisor for the CLI
    # flow; use the reference bound at import time, before any patching.
    with patch("hydra_core.supervisor.emit_trace"):
        runner = _REAL_BUILD_SUPERVISOR(dispatcher=MagicMock(project_root=None),
                                  force_pure_python=True)
    fn = dict(runner.steps)["synthesis"]
    st = HydraState(root_goal="brief the board")
    st.selected_squads = ["executive"]
    st.envelopes = envs
    st.artifacts = arts
    with patch("hydra_core.supervisor.emit_trace"):
        return fn(st) or {}


def _hand_state(heads: dict[str, str], head_results: dict[str, Any],
                lead_dissents=None) -> HydraState:
    rec: dict[str, Any] = {
        "task_id": _tid, "owner_squad": "executive", "status": "complete",
        "final_status": "complete", "run_id": "run-lead", "summary": "ok",
        "artifact_ref": {"key": "mem:lead"}, "heads": heads}
    if lead_dissents is not None:
        rec["dissents"] = lead_dissents
    return HydraState(
        root_goal="g", workflow_id=UUID("11111111-2222-4333-8444-555555555555"),
        attended_results=[rec], attended_head_results=head_results)


def _hd(statement: str, plaza: str = "h1") -> dict:
    return {"plaza": plaza, "mythic": plaza, "statement": statement,
            "cell": "kan", "rationale": ""}


def _dissents_of(state: HydraState) -> list[str]:
    return cli._materialize_attended_results(state)[0][0]["dissenting_opinions"]


# --------------------------------------------------------------------------- #
# R1-R4 through the real step/submit flow                                     #
# --------------------------------------------------------------------------- #

def test_r1_head_dissent_survives_an_omitting_lead(tmp_path, monkeypatch, capsys):
    sup, _d, _p = _install_keyed(monkeypatch, _state_for(_HEADS))
    _run_flow(tmp_path, capsys, head_dissents={"h2": ["rollout risk unpriced"]})

    envs, _arts = _materialized(sup)

    # Precondition: the lead leg really carried no dissent of its own.
    rec = next(r for r in _values(sup)["attended_results"] if r["task_id"] == _tid)
    assert "dissents" not in rec
    _assert_r1(envs)


def test_r2_lead_repeat_is_not_duplicated(tmp_path, monkeypatch, capsys):
    sup, _d, _p = _install_keyed(monkeypatch, _state_for(_HEADS))
    _run_flow(tmp_path, capsys,
              head_dissents={"h2": ["rollout risk unpriced"]},
              lead_dissents=["Rollout  risk UNPRICED"])

    envs, _arts = _materialized(sup)

    got = _exec_env(envs)["dissenting_opinions"]
    assert got == ["[head:h2] rollout risk unpriced"]
    assert sum("unpriced" in g.lower() for g in got) == 1


def test_r3_lead_own_kept_after_heads(tmp_path, monkeypatch, capsys):
    sup, _d, _p = _install_keyed(monkeypatch, _state_for(_HEADS))
    _run_flow(tmp_path, capsys,
              head_dissents={"h2": ["rollout risk unpriced"]},
              lead_dissents=["budget overrun"])

    envs, _arts = _materialized(sup)

    assert _exec_env(envs)["dissenting_opinions"] == [
        "[head:h2] rollout risk unpriced", "[lead] budget overrun"]


def test_r4_same_statement_two_heads_keeps_first_declared(
        tmp_path, monkeypatch, capsys):
    sup, _d, _p = _install_keyed(monkeypatch, _state_for(_HEADS))
    _run_flow(tmp_path, capsys,
              head_dissents={"h1": ["cost unclear"], "h3": ["cost unclear"]})

    envs, _arts = _materialized(sup)

    assert _exec_env(envs)["dissenting_opinions"] == ["[head:h1] cost unclear"]


def test_r4b_declared_order_not_alphabetical(tmp_path, monkeypatch, capsys):
    """Heads declared [h3, h1, h2]: h1 and h3 both dissent identically. The
    declared-first head (h3) wins; alphabetical-by-accident would pick h1."""
    declared = ["h3", "h1", "h2"]
    sup, _d, _p = _install_keyed(monkeypatch, _state_for(declared))
    _run_flow(tmp_path, capsys, heads=declared,
              head_dissents={"h1": ["cost unclear"], "h3": ["cost unclear"]})

    # The order really survives the checkpoint round trip.
    rec = next(r for r in _values(sup)["attended_results"] if r["task_id"] == _tid)
    assert list(rec["heads"]) == declared

    envs, _arts = _materialized(sup)

    assert _exec_env(envs)["dissenting_opinions"] == ["[head:h3] cost unclear"]


def test_r4c_distinct_statements_follow_declared_head_order(
        tmp_path, monkeypatch, capsys):
    declared = ["h3", "h1", "h2"]
    sup, _d, _p = _install_keyed(monkeypatch, _state_for(declared))
    _run_flow(tmp_path, capsys, heads=declared,
              head_dissents={"h1": ["from one"], "h2": ["from two"],
                             "h3": ["from three"]})

    envs, _arts = _materialized(sup)

    assert _exec_env(envs)["dissenting_opinions"] == [
        "[head:h3] from three", "[head:h1] from one", "[head:h2] from two"]


# --------------------------------------------------------------------------- #
# R5 malformed retained entries, and absent head data                         #
# --------------------------------------------------------------------------- #

def test_r5_malformed_retained_entries_skipped():
    junk = [5, {"statement": 7}, {"statement": "  "}, {"plaza": "h1"},
            {"plaza": "h1", "mythic": "h1", "statement": "ok", "cell": "kan",
             "rationale": ""}]
    st = _hand_state({"h1": "complete"},
                     {head_result_key(_tid, "h1"): {"dissents": junk}})

    assert _dissents_of(st) == ["[head:h1] ok"]


def test_head_with_no_record_or_no_dissents_is_skipped_without_error():
    st = _hand_state(
        {"h1": "complete", "h2": "missing", "h3": "complete", "h4": "complete"},
        {head_result_key(_tid, "h1"): {"status": "complete"},     # no dissents
         head_result_key(_tid, "h3"): {"dissents": "not-a-list"},  # wrong type
         head_result_key(_tid, "h4"): {"dissents": None},          # null
         head_result_key(_tid, "h9"): {"dissents": [_hd("stray", "h9")]}},
        lead_dissents=[_hd("lead says", "lead")])

    # h2 has no entry at all; h9 is not named by rec['heads'] so it is ignored.
    assert _dissents_of(st) == ["[lead] lead says"]


def test_record_without_heads_mapping_gains_no_dissents():
    st = HydraState(
        root_goal="g", workflow_id=UUID("11111111-2222-4333-8444-555555555555"),
        attended_results=[{
            "task_id": _tid, "owner_squad": "executive", "status": "complete",
            "run_id": "r", "dissents": [_hd("sneaky", "lead")]}],
        attended_head_results={
            head_result_key(_tid, "h1"): {"dissents": [_hd("sneaky2")]}})

    env = cli._materialize_attended_results(st)[0][0]

    assert env["dissenting_opinions"] == []


# --------------------------------------------------------------------------- #
# statement rendering is verbatim (no re-redaction, no re-truncation)         #
# --------------------------------------------------------------------------- #

def test_statement_attached_as_stored_newline_and_brackets_pinned():
    long = "x" * 2500
    st = _hand_state(
        {"h1": "complete", "h[2]": "complete"},
        {head_result_key(_tid, "h1"): {"dissents": [
            _hd("line one\nline two"),
            _hd("[head:h2] spoofed"),
            _hd(long)]},
         head_result_key(_tid, "h[2]"): {"dissents": [_hd("slug brackets", "h[2]")]}})

    got = _dissents_of(st)

    # A newline stays inside ONE list element, byte for byte; the attribution
    # prefix is always the outermost one; nothing is truncated or re-redacted;
    # brackets in a slug are not escaped.
    assert got == [
        "[head:h1] line one\nline two",
        "[head:h1] [head:h2] spoofed",
        "[head:h1] " + long,
        "[head:h[2]] slug brackets",
    ]


def test_dedupe_collapses_newlines_and_case():
    st = _hand_state(
        {"h1": "complete", "h2": "complete"},
        {head_result_key(_tid, "h1"): {"dissents": [_hd("a\nb  c")]},
         head_result_key(_tid, "h2"): {"dissents": [_hd("A B C", "h2")]}})

    assert _dissents_of(st) == ["[head:h1] a\nb  c"]


# --------------------------------------------------------------------------- #
# R6 synthesis arrival                                                        #
# --------------------------------------------------------------------------- #

def test_r6_synthesis_carries_attached_dissent(tmp_path, monkeypatch, capsys):
    sup, _d, _p = _install_keyed(monkeypatch, _state_for(_HEADS))
    _run_flow(tmp_path, capsys, head_dissents={"h2": ["rollout risk unpriced"]})
    envs, arts = _materialized(sup)
    _assert_r1(envs)

    patch_out = _synthesis(envs, arts)

    assert "[head:h2] rollout risk unpriced" in (
        patch_out["envelopes"][0]["dissenting_opinions"])


# --------------------------------------------------------------------------- #
# R7 end to end from the producer side                                        #
# --------------------------------------------------------------------------- #

def test_r7_producer_contract_to_squad_and_synthesis_record(
        tmp_path, monkeypatch, capsys):
    sup, _d, _p = _install_keyed(monkeypatch, _state_for(_HEADS))
    contract = host_bridge._LEG_DISSENT_CONTRACT
    assert contract

    prompts: dict[str, str] = {}
    for h in _HEADS:
        s = _step(tmp_path, capsys)
        assert s["head"] == h
        prompts[h] = s["host_action"]["prompt"]
        res = {"text": f"{h} output (no dissent prose)", **_COST}
        if h == "h2":
            # Follows the contract the prompt just published.
            assert contract in prompts[h]
            res["dissents"] = [{"statement": "rollout risk unpriced",
                                "rationale": "no cost model"}]
        _submit(tmp_path, s["run_id"], s["host_action"]["call_key"], res, capsys)
    lead = _step(tmp_path, capsys)
    assert lead["leg"] == "lead"
    _lead_submit(tmp_path, capsys, lead)

    assert contract in prompts["h2"]
    envs, arts = _materialized(sup)
    exec_env = _exec_env(envs)
    assert "[head:h2] rollout risk unpriced" in exec_env["dissenting_opinions"]

    synth = _synthesis(envs, arts)["envelopes"][0]
    assert "[head:h2] rollout risk unpriced" in synth["dissenting_opinions"]


# --------------------------------------------------------------------------- #
# R8 head-less byte identity                                                  #
# --------------------------------------------------------------------------- #

def _norm(o: Any) -> Any:
    if isinstance(o, dict):
        return {k: _norm(v) for k, v in sorted(o.items())
                if k not in ("id", "created_at")}
    if isinstance(o, list):
        return [_norm(v) for v in o]
    return o


def test_r8_headless_output_byte_identical_to_starting_commit():
    st = HydraState(
        root_goal="g",
        workflow_id=UUID("11111111-2222-4333-8444-555555555555"),
        attended_results=[
            {"task_id": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
             "owner_squad": "executive", "status": "complete",
             "final_status": "complete", "run_id": "run-x",
             "summary": "brief done", "artifact_ref": {"key": "mem:brief"}},
            {"task_id": "bbbbbbbb-bbbb-4ccc-8ddd-eeeeeeeeeeee",
             "owner_squad": "engineering", "status": "complete",
             "run_id": "run_pp1", "summary": "built"}])

    envs, arts = cli._materialize_attended_results(st)

    got = json.dumps({"envelopes": _norm(envs), "artifacts": _norm(arts)},
                     indent=2, sort_keys=True) + "\n"
    assert got == _FIXTURE.read_text(encoding="utf-8")
    assert all(e["dissenting_opinions"] == [] for e in envs)
    assert len(envs) == 2


# --------------------------------------------------------------------------- #
# R9 negative: lead-only sourcing is detected by R1                           #
# --------------------------------------------------------------------------- #

def test_r9_lead_only_sourcing_fails_r1(tmp_path, monkeypatch, capsys):
    sup, _d, _p = _install_keyed(monkeypatch, _state_for(_HEADS))
    _run_flow(tmp_path, capsys, head_dissents={"h2": ["rollout risk unpriced"]})
    calls: list[str] = []

    def lead_only(state, rec):
        calls.append(str(rec.get("task_id")))
        return [f"[lead] {e['statement']}" for e in (rec.get("dissents") or [])]

    monkeypatch.setattr(cli, "_attended_task_dissents", lead_only, raising=False)

    # Precondition: the head dissent IS retained in state; only the sourcing
    # differs, so an empty result cannot come from missing data.
    assert _values(sup)["attended_head_results"][
        head_result_key(_tid, "h2")]["dissents"][0]["statement"] == (
            "rollout risk unpriced")
    envs, _arts = _materialized(sup)

    assert calls == [_tid]  # the lead-only variant was the one consulted
    assert _exec_env(envs)["dissenting_opinions"] == []
    with pytest.raises(AssertionError):
        _assert_r1(envs)
