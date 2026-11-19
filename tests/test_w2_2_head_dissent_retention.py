"""Hydra#90 step 1: head/lead dissents reach the retained result records.

The producer contract (_LEG_DISSENT_CONTRACT) is published to legged cursors
only, and a legged submit's explicit ``dissents`` list is retained as a
bounded, redacted, structured field. Head-less squad tasks are byte-identical
to the starting commit (fixture ``w2_2_headless_Sb3fddab.json``).
"""
from __future__ import annotations

import json
import re as _re
import socket
import subprocess
from pathlib import Path
from typing import Any

import pytest

from hydra_core import dispatch_seam, host_bridge
from hydra_core.state import head_leg_run_id, head_result_key
from tests.test_w2_1_cli_head_legs import (
    _install, _state, _step, _submit, _task, _tid, _values, capture_headless_flow,
)
from tests.test_w2_1_lead_leg import _COSTS, _install_keyed, _lead_submit

_FIXTURE = Path(__file__).parent / "fixtures" / "w2_2_headless_Sb3fddab.json"
_BEGIN_FIXTURE = (Path(__file__).parent / "fixtures"
                  / "begin_squad_stage_headless_S0720869.json")
_HEADS = ["h1", "h2", "h3"]


def _bounded(raw, plaza="h1"):
    fn = getattr(host_bridge, "_bounded_leg_dissents", None)
    assert fn is not None, "host_bridge._bounded_leg_dissents is missing"
    return fn(raw, plaza=plaza)


def _heads_with(project, capsys, extra: dict[str, dict]) -> list[dict]:
    """Step + submit the three heads; ``extra[h]`` is merged into h's result."""
    steps = []
    for h in _HEADS:
        s = _step(project, capsys)
        assert s["run_id"] == head_leg_run_id(_tid, h), s
        steps.append(s)
        res = {"text": f"{h} output", "cost_usd": _COSTS[h],
               "tokens_in": 10, "tokens_out": 5, "model": "m"}
        res.update(extra.get(h, {}))
        _submit(project, s["run_id"], s["host_action"]["call_key"], res, capsys)
    return steps


def _all_json_text(project: Path):
    for p in (project / ".hydra").rglob("*.json"):
        with open(dispatch_seam._long_path(p), encoding="utf-8") as f:
            yield p, f.read()


def _head_rec(sup, head):
    return _values(sup)["attended_head_results"][head_result_key(_tid, head)]


# --------------------------------------------------------------------------- #
# T1 producer contract                                                        #
# --------------------------------------------------------------------------- #

def test_t1_contract_published_to_head_and_lead_only(
        tmp_path, monkeypatch, capsys):
    contract = getattr(host_bridge, "_LEG_DISSENT_CONTRACT", None)
    assert contract, "_LEG_DISSENT_CONTRACT is missing"
    _install_keyed(monkeypatch, _state(_task(_HEADS)))
    for _h in _HEADS:
        s = _step(tmp_path, capsys)
        ha = s["host_action"]
        assert contract in ha["prompt"]
        assert "result.dissents" in ha["instructions"]
        _submit(tmp_path, s["run_id"], ha["call_key"],
                {"text": "x", "cost_usd": 0.1}, capsys)
    lead = _step(tmp_path, capsys)
    assert lead["leg"] == "lead"
    assert contract in lead["host_action"]["prompt"]
    assert "result.dissents" in lead["host_action"]["instructions"]

    proj2 = tmp_path / "headless"
    proj2.mkdir()
    _install_keyed(monkeypatch, _state(_task([])))
    s2 = _step(proj2, capsys)
    assert "leg" not in s2
    for text in (s2["host_action"]["prompt"], s2["host_action"]["instructions"]):
        assert "## Dissents" not in text and "result.dissents" not in text


# --------------------------------------------------------------------------- #
# T2 retention, T6 text is not a source                                       #
# --------------------------------------------------------------------------- #

def test_t2_head_dissents_retained_on_head_record_only(
        tmp_path, monkeypatch, capsys):
    sup, _d, _p = _install_keyed(monkeypatch, _state(_task(_HEADS)))
    d = [{"statement": "rollout risk unpriced", "rationale": "no cost model"}]

    _heads_with(tmp_path, capsys, {"h2": {"dissents": d}})

    assert _head_rec(sup, "h2").get("dissents") == [
        {"plaza": "h2", "mythic": "h2", "statement": "rollout risk unpriced",
         "cell": "kan", "rationale": "no cost model"}]
    assert "dissents" not in _head_rec(sup, "h1")
    assert "dissents" not in _head_rec(sup, "h3")


def test_t6_artifact_text_is_not_a_dissent_source(
        tmp_path, monkeypatch, capsys):
    sup, _d, _p = _install_keyed(monkeypatch, _state(_task(_HEADS)))

    _heads_with(tmp_path, capsys, {
        h: {"text": "body\n## Dissents\n- x"} for h in _HEADS})

    for h in _HEADS:
        assert "dissents" not in _head_rec(sup, h)
    assert any(True for _ in _all_json_text(tmp_path))
    for p, text in _all_json_text(tmp_path):
        assert '"dissents"' not in text, p


# --------------------------------------------------------------------------- #
# T3 / T4 / T5 helper bounds, malformed, redaction                            #
# --------------------------------------------------------------------------- #

def test_t3_bounds_on_stored_strings():
    raw = [{"statement": "s" * 2000, "rationale": "r" * 1000} for _ in range(12)]

    out = _bounded(raw)

    assert len(out) == 10
    assert all(len(e["statement"]) == 500 for e in out)
    assert all(len(e["rationale"]) == 200 for e in out)
    assert all(set(e) == {"plaza", "mythic", "statement", "cell", "rationale"}
               for e in out)


def test_t3_redaction_changes_length_and_bounds_hold_on_stored_string():
    email = "alice.smith@example.com"
    out = _bounded([
        {"statement": email + " " + "x" * 700, "rationale": email + " " + "y" * 700},
        email,
    ])

    assert len(out) == 2
    for e in out:
        assert len(e["statement"]) <= 500 and len(e["rationale"]) <= 200
        assert email not in e["statement"] and email not in e["rationale"]
    assert out[0]["statement"] != (email + " " + "x" * 700)[:500]
    assert out[1]["statement"] != email and out[1]["statement"]


def test_t4_malformed_and_blank_entries_skipped():
    out = _bounded([5, None, ["x"], {"rationale": "r"}, {"statement": 7},
                    "", "   ", {"statement": "  \t "}, "kept one"])

    assert [e["statement"] for e in out] == ["kept one"]


def test_cap_counts_kept_entries_not_raw_entries():
    raw = [5, None, ["x"], {"rationale": "r"}, {"statement": 7},
           "", "   ", {"statement": "  "}, 3.5, [], "eleventh and valid"]
    assert len(raw) == 11

    out = _bounded(raw)
    assert [e["statement"] for e in out] == ["eleventh and valid"]

    many = [f"d{i}" for i in range(15)]
    assert [e["statement"] for e in _bounded(many)] == many[:10]
    mixed = [None] * 4 + many
    assert [e["statement"] for e in _bounded(mixed)] == many[:10]


def test_non_str_rationale_ignored():
    out = _bounded([{"statement": "a risk", "rationale": 5}])
    assert out[0]["rationale"] == ""


def test_t5_email_never_stored_through_the_flow(tmp_path, monkeypatch, capsys):
    sup, _d, _p = _install_keyed(monkeypatch, _state(_task(_HEADS)))
    email = "bob.jones@example.com"

    _heads_with(tmp_path, capsys, {"h1": {"dissents": [
        {"statement": f"ask {email} first", "rationale": f"owner {email}"}]}})

    rec = _head_rec(sup, "h1")
    assert rec.get("dissents"), rec
    assert email not in json.dumps(rec)
    for p, text in _all_json_text(tmp_path):
        assert email not in text, p


# --------------------------------------------------------------------------- #
# T7 hermetic classification                                                  #
# --------------------------------------------------------------------------- #

def test_t7_classification_attempts_no_io(monkeypatch):
    from hydra_core.dispatcher import MCPStdioDispatcher

    def _boom(*_a, **_k):
        raise RuntimeError("io attempted")

    monkeypatch.setattr(socket.socket, "connect", _boom)
    monkeypatch.setattr(socket, "create_connection", _boom)
    monkeypatch.setattr(subprocess, "Popen", _boom)
    monkeypatch.setattr(MCPStdioDispatcher, "call_mcp", _boom)

    out = _bounded(["rollout risk unpriced",
                    "this shipped successfully last quarter"])

    assert [e["cell"] for e in out] == ["kan", "dui"]


# --------------------------------------------------------------------------- #
# T8 lead leg                                                                 #
# --------------------------------------------------------------------------- #

def test_t8_lead_dissents_retained_on_attended_result(
        tmp_path, monkeypatch, capsys):
    sup, _d, _p = _install_keyed(monkeypatch, _state(_task(_HEADS)))
    _heads_with(tmp_path, capsys, {})
    lead = _step(tmp_path, capsys)
    assert lead["leg"] == "lead"

    body = _submit(tmp_path, lead["run_id"], lead["host_action"]["call_key"],
                   {"text": "lead synthesis", "cost_usd": 1.0, "tokens_in": 4,
                    "tokens_out": 2, "model": "m",
                    "dissents": ["budget overrun"]}, capsys)

    rec = next(r for r in _values(sup)["attended_results"]
               if r["task_id"] == _tid)
    assert (rec.get("dissents") or [{}])[0].get("statement") == "budget overrun"
    assert rec["dissents"][0]["plaza"] == "lead"
    assert body.get("dissents", [{}])[0].get("statement") == "budget overrun"
    assert all("dissents" not in _head_rec(sup, h) for h in _HEADS)


# --------------------------------------------------------------------------- #
# T9 head-less byte identity                                                  #
# --------------------------------------------------------------------------- #

def _norm(obj: Any, root: str) -> Any:
    """Same rule as tests/test_host_bridge_head_legs.py::_norm."""
    def fix(s: str) -> str:
        s = s.replace(root, "<ROOT>").replace("\\", "/")
        return _re.sub(
            r"(### Allowed PlanStep\.envelope_type values\n)[^\n]*", r"\1<TYPES>", s)

    if isinstance(obj, dict):
        return {k: _norm(v, root) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_norm(v, root) for v in obj]
    if isinstance(obj, str):
        return fix(obj)
    return obj


def test_t9_headless_flow_byte_identical(tmp_path, monkeypatch, capsys):
    root = tmp_path / "proj"
    root.mkdir()
    got = capture_headless_flow(root, monkeypatch, capsys)
    want = json.loads(_FIXTURE.read_text(encoding="utf-8"))["headless_flow"]
    assert got == want


@pytest.mark.parametrize("case", ["basic", "rich", "planning"])
def test_t9_headless_begin_squad_stage_byte_identical(tmp_path, case):
    want = json.loads(_FIXTURE.read_text(encoding="utf-8"))["begin_squad_stage"][case]
    kw = dict(json.loads(_BEGIN_FIXTURE.read_text(encoding="utf-8"))[case]["kwargs"])
    res = host_bridge.begin_squad_stage(project_root=str(tmp_path), **kw)
    cursor = json.loads(Path(res["cursor_path"]).read_text(encoding="utf-8"))
    assert _norm(cursor, str(tmp_path)) == want["cursor"]
    assert _norm(res, str(tmp_path)) == want["step_result"]


def test_t9_headless_submit_with_dissents_leaves_no_key(
        tmp_path, monkeypatch, capsys):
    sup, _d, _p = _install(monkeypatch, _state(_task([])))
    s = _step(tmp_path, capsys)

    body = _submit(tmp_path, s["run_id"], s["host_action"]["call_key"],
                   {"text": "done", "cost_usd": 0.5,
                    "dissents": ["should be ignored"]}, capsys)

    cursor = host_bridge.load_cursor(s["cursor_path"])
    assert "dissents" not in cursor
    assert "dissents" not in body
    for r in _values(sup)["attended_results"]:
        assert "dissents" not in r
