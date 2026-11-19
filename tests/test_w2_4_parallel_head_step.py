"""Hydra#92 W2.4 step 2: one attended step serves every ready head leg.

The rule (D2): a step opens every unrecorded head that is not yet open unless
the budget gate blocks it; open heads are always re-served (no budget check),
in declared order; a budget block never stops the walk. Uses the
``_install`` / ``_ChannelSup`` harness of ``tests/test_w2_1_cli_head_legs.py``.
"""
from __future__ import annotations

import json
from pathlib import Path

from hydra_core import cli, dispatch_seam, head_registry, host_bridge
from hydra_core.state import head_leg_run_id, head_result_key
from tests.test_w2_1_cli_head_legs import (
    _cursor_files, _install, _state, _step, _submit, _task, _ticket_files,
    _tid, _values, _wf,
)


def _rid(head: str) -> str:
    return head_leg_run_id(_tid, head)


def _cursor_bytes(project: Path, head: str) -> bytes:
    return Path(host_bridge.cursor_path(project, _wf, _rid(head))).read_bytes()


def _ticket_bytes(project: Path, head: str) -> bytes:
    rid = _rid(head)
    tp = dispatch_seam.ticket_path(project, _wf, rid, f"squad-{rid}-0")
    with open(dispatch_seam._long_path(tp), "rb") as f:
        return f.read()


def _cursor_names(project: Path) -> set[str]:
    return {p.stem for p in _cursor_files(project)}


def _ticket_run_ids(project: Path) -> set[str]:
    # ticket file name: <run_id>.squad-<run_id[:...]>.<digest>.json
    return {p.name.split(".")[0] for p in _ticket_files(project)}


def _runs(heads) -> set[str]:
    return {_rid(h) for h in heads}


def _spent(sup) -> float:
    return _values(sup)["budget"]["spent_usd"]


def _seed_h1(st):
    st.attended_head_results = {
        head_result_key(_tid, "h1"): {"text": "seeded", "head": "h1", "leg": "head"}}
    return st


def test_three_heads_one_step_returns_three_actions_in_declared_order(
        tmp_path, monkeypatch, capsys):
    _install(monkeypatch, _state(_task(["h3", "h1", "h2"])))

    resp = _step(tmp_path, capsys)

    assert 'host_actions' in resp
    acts = resp["host_actions"]
    assert [a["head"] for a in acts] == ["h3", "h1", "h2"]
    run_ids = [a["run_id"] for a in acts]
    assert run_ids == [_rid(h) for h in ("h3", "h1", "h2")]
    assert len(set(run_ids)) == 3
    call_keys = [a["host_action"]["call_key"] for a in acts]
    assert len(set(call_keys)) == 3
    assert call_keys == [f"squad-{r}-0" for r in run_ids]
    assert all(a["ok"] is True and a["leg"] == "head" for a in acts)
    assert len(_cursor_files(tmp_path)) == 3
    assert len(_ticket_files(tmp_path)) == 3
    assert resp["host_action"] == acts[0]["host_action"]
    assert resp["run_id"] == acts[0]["run_id"]
    assert "budget_blocked_heads" not in resp


def test_restep_with_all_legs_open_is_idempotent(tmp_path, monkeypatch, capsys):
    sup, _d, _p = _install(monkeypatch, _state(_task(["h3", "h1", "h2"])))
    first = _step(tmp_path, capsys)
    cursors = {h: _cursor_bytes(tmp_path, h) for h in ("h1", "h2", "h3")}
    spent = _spent(sup)
    cur_files, tick_files = _cursor_files(tmp_path), _ticket_files(tmp_path)

    again = _step(tmp_path, capsys)

    assert [a["run_id"] for a in again["host_actions"]] == [
        a["run_id"] for a in first["host_actions"]]
    assert [a["host_action"]["call_key"] for a in again["host_actions"]] == [
        a["host_action"]["call_key"] for a in first["host_actions"]]
    assert {h: _cursor_bytes(tmp_path, h) for h in cursors} == cursors
    assert _cursor_files(tmp_path) == cur_files
    assert _ticket_files(tmp_path) == tick_files
    assert _spent(sup) == spent
    assert all(a["leg"] == "head" for a in again["host_actions"])
    assert again["run_id"] != _tid


def test_restep_after_one_submit_returns_remaining_open_legs_no_lead(
        tmp_path, monkeypatch, capsys):
    _install(monkeypatch, _state(_task(["h3", "h1", "h2"])))
    first = _step(tmp_path, capsys)
    by_head = {a["head"]: a for a in first["host_actions"]}
    h1 = by_head["h1"]
    _submit(tmp_path, h1["run_id"], h1["host_action"]["call_key"],
            {"text": "one", "cost_usd": 0.0}, capsys)
    cursors = {h: _cursor_bytes(tmp_path, h) for h in ("h3", "h2")}
    cur_files, tick_files = _cursor_files(tmp_path), _ticket_files(tmp_path)

    nxt = _step(tmp_path, capsys)

    assert [a["head"] for a in nxt["host_actions"]] == ["h3", "h2"]
    for a in nxt["host_actions"]:
        assert a["run_id"] == by_head[a["head"]]["run_id"]
        assert (a["host_action"]["call_key"]
                == by_head[a["head"]]["host_action"]["call_key"])
    assert {h: _cursor_bytes(tmp_path, h) for h in cursors} == cursors
    assert nxt["leg"] == "head" and nxt["run_id"] != _tid
    assert _cursor_files(tmp_path) == cur_files
    assert _ticket_files(tmp_path) == tick_files


def test_budget_block_mixed_order_reserves_open_head(
        tmp_path, monkeypatch, capsys):
    _install(monkeypatch, _seed_h1(_state(_task(["h1", "h2"]))))
    opened = _step(tmp_path, capsys)
    assert opened["head"] == "h2" and "host_actions" not in opened
    assert _cursor_names(tmp_path) == _runs(["h2"])
    h2_cursor = _cursor_bytes(tmp_path, "h2")

    blocked = _state(_task(["h1", "h2"]), budget_usd=1.0)
    blocked.budget.spent_usd = 1.0
    _install(monkeypatch, blocked)
    resp = _step(tmp_path, capsys)

    assert resp["head"] == "h2" and resp["run_id"] == opened["run_id"]
    assert (resp["host_action"]["call_key"]
            == opened["host_action"]["call_key"])
    assert "host_actions" not in resp
    assert resp["budget_blocked_heads"] == ["h1"]
    assert _cursor_bytes(tmp_path, "h2") == h2_cursor
    assert _cursor_names(tmp_path) == _runs(["h2"])
    assert _ticket_run_ids(tmp_path) == _runs(["h2"])

    # variant: h2 and h3 pre-opened, h1 blocked
    p2 = tmp_path / "variant"
    p2.mkdir()
    _install(monkeypatch, _seed_h1(_state(_task(["h1", "h2", "h3"]))))
    pre = _step(p2, capsys)
    assert [a["head"] for a in pre["host_actions"]] == ["h2", "h3"]
    blocked = _state(_task(["h1", "h2", "h3"]), budget_usd=1.0)
    blocked.budget.spent_usd = 1.0
    _install(monkeypatch, blocked)
    resp = _step(p2, capsys)
    assert [a["head"] for a in resp["host_actions"]] == ["h2", "h3"]
    assert resp["head"] == "h2" and resp["run_id"] == _rid("h2")
    assert resp["budget_blocked_heads"] == ["h1"]
    assert _cursor_names(p2) == _runs(["h2", "h3"])
    assert _ticket_run_ids(p2) == _runs(["h2", "h3"])


def test_budget_exhausted_with_nothing_open_returns_budget_blocked(
        tmp_path, monkeypatch, capsys):
    st = _state(_task(["h1", "h2", "h3"]), budget_usd=1.0)
    st.budget.spent_usd = 1.0
    _install(monkeypatch, st)

    out = _step(tmp_path, capsys)

    assert out["ok"] is False
    assert out["status"] == "budget_blocked"
    assert out["task_id"] == _tid
    assert out["squad_slug"] == "executive"
    assert "budget_blocked_heads" not in out and "host_actions" not in out
    assert _cursor_files(tmp_path) == []
    assert _ticket_files(tmp_path) == []


def test_seam_failure_on_second_leg_then_restep(tmp_path, monkeypatch, capsys):
    sup, _d, _p = _install(monkeypatch, _state(_task(["h1", "h2", "h3"])))
    real = cli._apply_dispatch_seam
    fail = {"on": True, "n": 0}
    err = {"ok": False, "status": "dispatch_seam_persist_failed", "retryable": True}

    def _wrapped(*a, **k):
        if fail["on"] and k.get("kind") == "squad":
            fail["n"] += 1
            if fail["n"] == 2:
                return dict(err)
        return real(*a, **k)

    monkeypatch.setattr(cli, "_apply_dispatch_seam", _wrapped)

    failed = _step(tmp_path, capsys, want_rc=1)

    assert failed == err
    assert _cursor_names(tmp_path) == _runs(["h1", "h2"])
    assert _ticket_run_ids(tmp_path) == _runs(["h1"])
    assert _spent(sup) == 0.0
    h1_cursor, h1_ticket = (_cursor_bytes(tmp_path, "h1"),
                            _ticket_bytes(tmp_path, "h1"))
    h2_cursor_after_fail = Path(host_bridge.cursor_path(
        tmp_path, _wf, _rid("h2"))).read_text(encoding="utf-8")
    h2_pending = json.loads(h2_cursor_after_fail)["pending_action"]["call_key"]

    fail["on"] = False
    resp = _step(tmp_path, capsys)

    assert [a["head"] for a in resp["host_actions"]] == ["h1", "h2", "h3"]
    assert _cursor_names(tmp_path) == _runs(["h1", "h2", "h3"])
    assert _ticket_run_ids(tmp_path) == _runs(["h1", "h2", "h3"])
    assert _cursor_bytes(tmp_path, "h1") == h1_cursor
    assert _ticket_bytes(tmp_path, "h1") == h1_ticket
    h2 = resp["host_actions"][1]
    assert h2["run_id"] == _rid("h2")
    assert h2["host_action"]["call_key"] == h2_pending == f"squad-{_rid('h2')}-0"
    assert _spent(sup) == 0.0


def test_head_unresolved_on_second_leg_then_restep(tmp_path, monkeypatch, capsys):
    seen = {"h2": 0}

    def _resolve(squad, head, **_kw):
        if head == "h2":
            seen["h2"] += 1
            if seen["h2"] == 1:
                return head_registry.HeadResolution(
                    squad=squad, head=head, plugin="plug", agent_name=head,
                    qualified_name=f"plug:{head}", agent_path=None,
                    rule="unresolved", error="no such agent")
        return head_registry.HeadResolution(
            squad=squad, head=head, plugin="plug", agent_name=head,
            qualified_name=f"plug:{head}", agent_path=None, rule="slug")

    _install(monkeypatch, _state(_task(["h1", "h2", "h3"])), resolver=_resolve)

    failed = _step(tmp_path, capsys, want_rc=1)

    assert failed == {"ok": False, "status": "head_unresolved", "task_id": _tid,
                      "head": "h2", "error": "no such agent"}
    assert _cursor_names(tmp_path) == _runs(["h1"])
    assert _ticket_run_ids(tmp_path) == _runs(["h1"])
    h1_cursor, h1_ticket = (_cursor_bytes(tmp_path, "h1"),
                            _ticket_bytes(tmp_path, "h1"))

    resp = _step(tmp_path, capsys)

    assert [a["head"] for a in resp["host_actions"]] == ["h1", "h2", "h3"]
    assert _cursor_names(tmp_path) == _runs(["h1", "h2", "h3"])
    assert _ticket_run_ids(tmp_path) == _runs(["h1", "h2", "h3"])
    assert _cursor_bytes(tmp_path, "h1") == h1_cursor
    assert _ticket_bytes(tmp_path, "h1") == h1_ticket
