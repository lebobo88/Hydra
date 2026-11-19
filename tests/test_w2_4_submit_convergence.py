"""Hydra#92 W2.4 step 3: submitting the head results in any order converges.

After one step opens h1, h2 and h3 (D2), the host submits the three head results in
any order, or from two real processes at once. The checkpoint after the third head
result is recorded ("post-heads snapshot", D8) must equal the in-order run exactly,
except ``budget.spent_usd`` (float addition is order dependent; compared within 1e-9,
or exactly for binary-exact costs). Everything runs against a real SqliteSaver file
with the production checkpoint serde (``tests/_w2_4_submit_child.py``), every verb
call on a fresh connection, all state paths under ``tmp_path``.
"""
from __future__ import annotations

import itertools
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

from hydra_core import cli, host_bridge
from hydra_core.state import HydraState, head_leg_run_id, head_result_key
from tests._w2_4_submit_child import Harness
from tests.test_w2_1_cli_head_legs import _tid, _wf

HEADS = ("h1", "h2", "h3")
COSTS_ORDER_SENSITIVE = {"h1": 0.1, "h2": 0.2, "h3": 0.3}
COSTS_EXACT = {"h1": 0.25, "h2": 0.5, "h3": 0.125}
H2_DISSENT = "h2 dissent: the scope of the board brief is too broad"
_CHILD = Path(__file__).parent / "_w2_4_submit_child.py"
_VOLATILE = re.compile(r"(_at$|^ts$|timestamp|^time$|elapsed|^created$|^updated$)")
_PERMUTATIONS = list(itertools.permutations(HEADS))


@pytest.fixture
def hydra_env(tmp_path, monkeypatch):
    """Every Hydra state path under tmp_path; returns the env a child process inherits."""
    home = tmp_path / "home"
    home.mkdir()
    values = {
        "HYDRA_HOME": home,
        "HYDRA_EPISODIC_DB": home / "episodic.db",
        "HYDRA_CHECKPOINT_DB": home / "checkpoints.db",
        "HYDRA_BACKENDS": home / "backends.json",
        "HYDRA_EIGHTS_SPOOL": home / "eights-pending",
        "HYDRA_EIGHTS_DEAD_LETTER": home / "eights-pending-dead",
    }
    for k, v in values.items():
        monkeypatch.setenv(k, str(v))
    monkeypatch.setenv("HYDRA_TEST_NO_DAEMONS", "1")
    monkeypatch.delenv("HYDRA_ALLOW_DETACHED", raising=False)
    return dict(os.environ)


def _new(base: Path, name: str, heads=HEADS) -> Harness:
    d = base / name
    d.mkdir()
    (d / "proj").mkdir()
    h = Harness(d / "proj", d / "checkpoint.sqlite", heads=heads)
    h.seed()
    return h


def _rid(head: str) -> str:
    return head_leg_run_id(_tid, head)


def _result_file(h: Harness, head: str, cost: float, call_key: str) -> Path:
    body = {"text": f"{head} output", "cost_usd": cost, "tokens_in": 10,
            "tokens_out": 5, "model": "m"}
    if head == "h2":
        body["dissents"] = [H2_DISSENT]
    p = h.project.parent / f"result-{head}-{call_key}.json"
    p.write_text(json.dumps(body), encoding="utf-8")
    return p


def _served(resp: dict) -> list[dict]:
    return resp["host_actions"] if "host_actions" in resp else [resp]


def _submit(h: Harness, head: str, cost: float, call_key: str, *, want_rc=0) -> dict:
    rf = _result_file(h, head, cost, call_key)
    rc, out, err = h.submit_raw(_rid(head), call_key, rf)
    assert rc == want_rc, (out or err)[:800]
    return json.loads(out or err)


def _calls(step_resp: dict) -> dict[str, str]:
    return {a["head"]: a["host_action"]["call_key"] for a in step_resp["host_actions"]}


def _scrub(obj):
    if isinstance(obj, dict):
        return {k: _scrub(v) for k, v in obj.items() if not _VOLATILE.search(k)}
    if isinstance(obj, list):
        return [_scrub(v) for v in obj]
    return obj


def snapshot(h: Harness) -> tuple[dict, float]:
    """The normalised post-heads checkpoint and its spent_usd (compared separately)."""
    vals = HydraState.model_validate(h.values()).model_dump(mode="json")
    snap = _scrub(json.loads(json.dumps(vals, default=str)))
    text = json.dumps(snap, sort_keys=True)
    base = h.project.parent
    for form in (str(base).replace("\\", "\\\\"), str(base), base.as_posix()):
        text = text.replace(form, "<root>")
    snap = json.loads(text)
    spent = snap["budget"].pop("spent_usd")
    return snap, spent


def _run_order(base: Path, name: str, order, costs, heads=HEADS) -> dict:
    """Fresh state; one step; three submits in `order`; snapshot; one lead step."""
    h = _new(base, name, heads)
    first = h.step()
    assert 'host_actions' in first
    assert len(first["host_actions"]) == 3
    calls = _calls(first)
    for head in order:
        r = _submit(h, head, costs[head], calls[head])
        assert r["ok"] is True and r["status"] != "stale_call_key", r
    snap, spent = snapshot(h)
    lead = h.step()
    return {"h": h, "snap": snap, "spent": spent, "lead": lead,
            "lead_prompt": lead["host_action"]["prompt"]}


def _check_snapshot(snap: dict, costs: dict) -> None:
    """Exact per-field checks that hold for every order (D8)."""
    recs = snap["attended_head_results"]
    assert sorted(recs) == sorted(head_result_key(_tid, x) for x in HEADS)
    for head in HEADS:
        assert recs[head_result_key(_tid, head)]["cost_usd"] == costs[head]
    h2 = recs[head_result_key(_tid, "h2")]
    assert [d["statement"] for d in h2["dissents"]] == [H2_DISSENT]
    assert "dissents" not in recs[head_result_key(_tid, "h1")]
    assert "dissents" not in recs[head_result_key(_tid, "h3")]
    want_marks = {f"{_rid(x)}:squad-{_rid(x)}-0" for x in HEADS}
    assert set(snap["attended_charge_applied"]) == want_marks
    assert snap["open_pp_runs"] == []


def _converges(base: Path, costs: dict, ref_name: str):
    ref = _run_order(base, ref_name, HEADS, costs)
    _check_snapshot(ref["snap"], costs)
    assert ref["lead"]["leg"] == "lead"
    for i, order in enumerate(_PERMUTATIONS):
        got = ref if tuple(order) == HEADS else _run_order(base, f"o{i}", order, costs)
        _check_snapshot(got["snap"], costs)
        assert got["snap"] == ref["snap"], order
        assert got["lead"]["run_id"] == _tid, order
        assert got["lead_prompt"] == ref["lead_prompt"], order
        yield order, got, ref


def test_six_submit_orders_converge(tmp_path, hydra_env):
    seen = []
    for order, got, ref in _converges(tmp_path, COSTS_ORDER_SENSITIVE, "ref"):
        assert abs(got["spent"] - ref["spent"]) <= 1e-9, (order, got["spent"], ref["spent"])
        assert abs(got["spent"] - 0.6) <= 1e-9
        seen.append(order)
    assert sorted(seen) == sorted(_PERMUTATIONS) and len(seen) == 6


def test_six_submit_orders_converge_exact_costs(tmp_path, hydra_env):
    seen = []
    for order, got, ref in _converges(tmp_path, COSTS_EXACT, "ref"):
        assert got["spent"] == 0.875, (order, got["spent"])
        seen.append(order)
    assert len(seen) == 6 and len(set(seen)) == 6


def test_lead_only_after_all_heads_submitted(tmp_path, hydra_env):
    declared = ("h3", "h1", "h2")
    h = _new(tmp_path, "lead_only", declared)
    first = h.step()
    assert [a["head"] for a in first["host_actions"]] == list(declared)
    calls = _calls(first)
    lead_cursor = Path(host_bridge.cursor_path(h.project, _wf, _tid))

    def _spent():
        return snapshot(h)[1]

    # after 0, 1 and 2 submits: no lead, no task_id cursor, spend unchanged by the step
    for done in range(3):
        if done == 0:
            before = _spent()
            again = h.step()
            assert _spent() == before
        else:
            _submit(h, declared[done - 1], 0.25, calls[declared[done - 1]])
            before = _spent()
            again = h.step()
            assert _spent() == before
        assert again.get("leg") != "lead"
        assert all(a.get("leg") != "lead" for a in _served(again))
        assert not lead_cursor.exists()
        assert [a["run_id"] for a in _served(again)] == [
            _rid(x) for x in declared[done:]]
    _submit(h, declared[2], 0.25, calls[declared[2]])
    lead = h.step()
    assert lead["leg"] == "lead" and lead["run_id"] == _tid
    assert "host_actions" not in lead
    assert lead_cursor.exists()


def test_duplicate_submit_not_recharged(tmp_path, hydra_env):
    h = _new(tmp_path, "dup")
    first = h.step()
    calls = _calls(first)
    for head in HEADS:
        _submit(h, head, COSTS_EXACT[head], calls[head])
    before_vals = h.values()
    before, spent = snapshot(h)
    assert spent == 0.875

    again = _submit(h, "h2", COSTS_EXACT["h2"], calls["h2"])
    assert again["already_charged"] is True
    after, spent_after = snapshot(h)
    assert spent_after == 0.875
    assert after["attended_charge_applied"] == before["attended_charge_applied"]
    assert after["attended_head_results"] == before["attended_head_results"]
    assert after == before
    assert h.values()["attended_head_results"] == before_vals["attended_head_results"]

    stale = _submit(h, "h2", COSTS_EXACT["h2"], "squad-not-the-real-call-key-0", want_rc=1)
    assert stale["ok"] is False and stale["error"] == "stale_call_key"
    final, spent_final = snapshot(h)
    assert spent_final == 0.875 and final == before


def test_submit_refused_while_resume_lock_held(tmp_path, hydra_env):
    h = _new(tmp_path, "locked")
    first = h.step()
    assert len(first["host_actions"]) == 3
    calls = _calls(first)
    cursor_before = {x: Path(host_bridge.cursor_path(h.project, _wf, _rid(x))).read_bytes()
                     for x in HEADS}
    vals_before = h.values()
    lock = h.project / ".hydra" / _wf / "resume.lock"
    lock.write_text(str(os.getpid()), encoding="utf-8")
    try:
        refused = _submit(h, "h1", 0.25, calls["h1"])
    finally:
        lock.unlink()
    assert refused["status"] == "resume_in_progress"
    assert {x: Path(host_bridge.cursor_path(h.project, _wf, _rid(x))).read_bytes()
            for x in HEADS} == cursor_before
    assert h.values() == vals_before

    applied = _submit(h, "h1", 0.25, calls["h1"])
    assert applied["ok"] is True and applied["status"] != "resume_in_progress"
    snap, spent = snapshot(h)
    assert spent == 0.25
    assert set(snap["attended_charge_applied"]) == {f"{_rid('h1')}:squad-{_rid('h1')}-0"}
    assert list(snap["attended_head_results"]) == [head_result_key(_tid, "h1")]


def test_two_process_submits_converge(tmp_path, hydra_env):
    deadline = time.monotonic() + 60.0
    deadline_epoch = time.time() + 60.0
    costs = COSTS_ORDER_SENSITIVE
    ref = _run_order(tmp_path, "ref", HEADS, costs)
    _check_snapshot(ref["snap"], costs)

    # (i) fresh state; one in-process step opens h1, h2 and h3
    h = _new(tmp_path, "twoproc")
    first = h.step()
    assert len(first["host_actions"]) == 3
    calls = _calls(first)
    # (ii) h2 (with its dissent) is submitted in-process and applied
    r2 = _submit(h, "h2", costs["h2"], calls["h2"])
    assert r2["ok"] is True
    # (iii) two real child processes submit h1 and h3, synchronised on a barrier file
    barrier = tmp_path / "barrier"
    barrier.mkdir()
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    procs: dict[str, subprocess.Popen] = {}
    try:
        for head in ("h1", "h3"):
            rf = _result_file(h, head, costs[head], calls[head])
            argv = [sys.executable, str(_CHILD), str(h.project), str(h.db), str(rf),
                    _rid(head), calls[head], str(barrier), head, "2",
                    repr(deadline_epoch), "400"]
            procs[head] = subprocess.Popen(
                argv, shell=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=hydra_env, creationflags=flags)
        outs: dict[str, tuple[str, str]] = {}
        for head, p in procs.items():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError("two-process submit exceeded the 60s deadline")
            try:
                o, e = p.communicate(timeout=remaining)
            except subprocess.TimeoutExpired:
                raise AssertionError(
                    f"child {head} exceeded the 60s deadline") from None
            outs[head] = (o.decode("utf-8", "replace"), e.decode("utf-8", "replace"))
            assert p.returncode == 0, (head, p.returncode, outs[head])
    finally:
        for p in procs.values():
            if p.poll() is None:
                p.kill()
            p.wait(timeout=10)
            for pipe in (p.stdout, p.stderr):
                if pipe is not None:
                    pipe.close()
    for head, (o, _e) in outs.items():
        rec = json.loads(o.strip().splitlines()[-1])
        assert rec["rc"] == 0 and rec["resp"]["ok"] is True, (head, rec)
        assert rec["resp"].get("status") != "resume_in_progress"
        assert 1 <= rec["attempts"] <= 400
    # (iv) before any further step: the post-heads snapshot equals the in-order reference
    snap, spent = snapshot(h)
    _check_snapshot(snap, costs)
    assert snap == ref["snap"]
    assert abs(spent - ref["spent"]) <= 1e-9
    # (v) the lead prompt equals the reference lead prompt
    lead = h.step()
    assert lead["leg"] == "lead" and lead["run_id"] == _tid
    assert lead["host_action"]["prompt"] == ref["lead_prompt"]
