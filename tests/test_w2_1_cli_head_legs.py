"""Hydra#89 step 3: per-head squad legs in the attended CLI verbs.

Drives ``cli._cmd_attended_step`` / ``cli._cmd_attended_submit`` in-process
with an argparse Namespace against the hermetic home and a stateful fake
supervisor whose ``update_state`` applies the real channel reducers (append
for ``tasks`` etc., merge-dict for the attended_* markers and
``attended_head_results``, replace otherwise). Checkpoint state is always
read back through ``sup.get_state`` (channel values), never from a returned
patch. ``head_registry.resolve_head`` is monkeypatched so no test depends on
sibling pack checkouts or the operator's real ~/.hydra.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from uuid import UUID

import pytest

from hydra_core import cli, dispatch_seam, head_registry, host_bridge
from hydra_core.state import HydraState, TaskState, head_leg_run_id, head_result_key

_wf = "11111111-2222-4333-8444-555555555555"
_tid = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
_fixture = (Path(__file__).parent / "fixtures"
            / "cli_headless_squad_flow_S216b381.json")

_append_keys = {"tasks", "envelopes", "artifacts", "episodic_refs",
                "semantic_queries", "verdicts", "hitl_history"}
_merge_dict_keys = {"error_counters", "plan_submit_attempts",
                    "attended_checkpoint_reconciled", "attended_charge_applied",
                    "attended_head_results"}


class _Snap:
    def __init__(self, values: dict):
        self.values = values
        self.next = ()


class _ChannelSup:
    """Stateful fake supervisor modelling the checkpoint as channel merges."""

    def __init__(self, state: HydraState):
        self._values = state.model_dump(mode="json")

    def get_state(self, config):
        return _Snap(json.loads(json.dumps(self._values, default=str)))

    def update_state(self, config, patch, as_node=None):
        for k, v in patch.items():
            if k in _append_keys:
                inc = v if isinstance(v, list) else [v]
                inc = [i.model_dump(mode="json") if hasattr(i, "model_dump") else i
                       for i in inc]
                self._values[k] = list(self._values.get(k) or []) + inc
            elif k in _merge_dict_keys:
                merged = dict(self._values.get(k) or {})
                merged.update(v or {})
                self._values[k] = merged
            else:
                self._values[k] = v

    def invoke(self, arg, config=None):  # pragma: no cover
        raise AssertionError("head legs never re-enter the graph")


class _Disp:
    live_execution = True
    project_root = None

    def __init__(self):
        self.calls: list[tuple] = []

    def set_squad_packs(self, packs):
        pass

    def call_mcp(self, server, tool, args, squad_id=None, **_kw):
        self.calls.append((server, tool))
        return {"status": "done", "result": {}}


class _Agent:
    slug = "lead"
    authority = "gatekeeper"


class _Pack:
    slug = "executive"
    name = "Exec"
    agents = [_Agent()]
    gates: list = []
    invoke = None
    tools = ()

    def __init__(self, entrypoint: str = "claude-native"):
        self.entrypoint = entrypoint


class _Written:
    key = "mem:test"

    def model_dump(self, mode="json"):
        return {"key": self.key}


def _install(monkeypatch, state: HydraState, *, entrypoint="claude-native",
             resolver=None):
    sup = _ChannelSup(state)
    disp = _Disp()
    pack = _Pack(entrypoint)
    monkeypatch.setattr(cli, "_attended_live_dispatcher", lambda *a, **k: disp)
    monkeypatch.setattr("hydra_core.supervisor.build_supervisor", lambda **k: sup)
    monkeypatch.setattr("hydra_core.squad_loader.discover_squads",
                        lambda *a, **k: {pack.slug: pack})
    monkeypatch.setattr("hydra_core.squad_node._maybe_write_claude_shim",
                        lambda *a, **k: None)
    monkeypatch.setattr("hydra_core.artifact_store.write_native_artifact",
                        lambda *a, **k: _Written())

    def _resolve(squad, head, **_kw):
        return head_registry.HeadResolution(
            squad=squad, head=head, plugin="plug", agent_name=head,
            qualified_name=f"plug:{head}", agent_path=None, rule="slug")

    monkeypatch.setattr(head_registry, "resolve_head", resolver or _resolve)
    return sup, disp, pack


def _task(heads: list[str], owner: str = "executive") -> TaskState:
    return TaskState(task_id=UUID(_tid), owner_squad=owner,
                     description="brief the board", heads=heads)


def _state(task: TaskState, budget_usd: float = 50.0) -> HydraState:
    st = HydraState(root_goal="brief the board", workflow_id=UUID(_wf), tasks=[task])
    st.budget.budget_usd = budget_usd
    return st


def _step(project: Path, capsys, want_rc: int = 0) -> dict:
    rc = cli._cmd_attended_step(argparse.Namespace(
        project=str(project), workflow_id=_wf, verbose=False))
    out = capsys.readouterr().out
    assert rc == want_rc, out[:800]
    return json.loads(out)


def _submit(project: Path, run_id: str, call_key: str, result: dict, capsys,
            want_rc: int = 0) -> dict:
    rf = project / f"result-{run_id}-{call_key}.json"
    rf.write_text(json.dumps(result), encoding="utf-8")
    rc = cli._cmd_attended_submit(argparse.Namespace(
        project=str(project), workflow_id=_wf, run_id=run_id,
        call_key=call_key, result=str(rf), verbose=False))
    cap = capsys.readouterr()
    assert rc == want_rc, (cap.out or cap.err)[:800]
    return json.loads(cap.out or cap.err)


def _values(sup: _ChannelSup) -> dict:
    return sup.get_state({"configurable": {"thread_id": _wf}}).values


def _ticket_files(project: Path) -> list[Path]:
    d = project / ".hydra" / _wf / "attended" / "tickets"
    return sorted(d.glob("*.json")) if d.exists() else []


def _cursor_files(project: Path) -> list[Path]:
    d = project / ".hydra" / _wf / "attended"
    return sorted(p for p in d.glob("*.json")) if d.exists() else []


def _run_three_heads(project: Path, capsys, costs=(0.25, 0.5, 0.125)):
    steps, submits = [], []
    for i, cost in enumerate(costs, start=1):
        s = _step(project, capsys)
        assert s.get("run_id") == head_leg_run_id(_tid, f"h{i}"), s
        steps.append(s)
        submits.append(_submit(
            project, s["run_id"], s["host_action"]["call_key"],
            {"text": f"head {i} output", "cost_usd": cost, "tokens_in": 10,
             "tokens_out": 5, "model": "m"}, capsys))
    return steps, submits


# =========================================================================== #
# Head legs                                                                   #
# =========================================================================== #

class TestHeadLegs:
    def test_three_rounds_open_three_head_cursors_and_tickets(
            self, tmp_path, monkeypatch, capsys):
        sup, _d, _p = _install(monkeypatch, _state(_task(["h1", "h2", "h3"])))

        steps, _subs = _run_three_heads(tmp_path, capsys)

        run_ids = [head_leg_run_id(_tid, h) for h in ("h1", "h2", "h3")]
        assert [s["run_id"] for s in steps] == run_ids
        assert [s["head"] for s in steps] == ["h1", "h2", "h3"]
        assert all(s["leg"] == "head" for s in steps)
        assert [s["host_action"]["agent_type"] for s in steps] == [
            "plug:h1", "plug:h2", "plug:h3"]
        for rid in run_ids:
            assert Path(host_bridge.cursor_path(tmp_path, _wf, rid)).exists()
        assert not Path(host_bridge.cursor_path(tmp_path, _wf, _tid)).exists()
        assert len(_cursor_files(tmp_path)) == 3
        assert len(_ticket_files(tmp_path)) == 3
        for rid in run_ids:
            call_key = f"squad-{rid}-0"
            tp = dispatch_seam.ticket_path(tmp_path, _wf, rid, call_key)
            with open(dispatch_seam._long_path(tp), encoding="utf-8") as f:
                t = json.load(f)
            assert t["call_key"] == call_key and t["kind"] == "squad"
        assert sorted(_values(sup)["attended_head_results"]) == sorted(
            head_result_key(_tid, h) for h in ("h1", "h2", "h3"))

    def test_task_not_complete_after_heads_and_spend_is_sum(
            self, tmp_path, monkeypatch, capsys):
        sup, _d, _p = _install(monkeypatch, _state(_task(["h1", "h2", "h3"])))
        costs = (0.25, 0.5, 0.125)

        _run_three_heads(tmp_path, capsys, costs)

        v = _values(sup)
        assert v["budget"]["spent_usd"] == pytest.approx(sum(costs))
        want = {f"{head_leg_run_id(_tid, h)}:squad-{head_leg_run_id(_tid, h)}-0"
                for h in ("h1", "h2", "h3")}
        assert set(v["attended_charge_applied"]) == want
        assert _tid not in v["attended_completed_task_ids"]
        assert _tid not in v["attended_done_task_ids"]
        assert not any(r.get("task_id") == _tid
                       for r in (v.get("attended_results") or []))
        rec = v["attended_head_results"][head_result_key(_tid, "h2")]
        assert rec["head"] == "h2" and rec["leg"] == "head"
        assert rec["run_id"] == head_leg_run_id(_tid, "h2")
        assert rec["cost_usd"] == pytest.approx(0.5)

    def test_duplicate_submit_no_recharge_and_stale_call_key_refused(
            self, tmp_path, monkeypatch, capsys):
        sup, _d, _p = _install(monkeypatch, _state(_task(["h1", "h2", "h3"])))
        s1 = _step(tmp_path, capsys)
        _submit(tmp_path, s1["run_id"], s1["host_action"]["call_key"],
                {"text": "one", "cost_usd": 0.25}, capsys)
        s2 = _step(tmp_path, capsys)
        ck = s2["host_action"]["call_key"]
        res = {"text": "two", "cost_usd": 0.5}
        _submit(tmp_path, s2["run_id"], ck, res, capsys)
        before = _values(sup)

        dup = _submit(tmp_path, s2["run_id"], ck, res, capsys)
        stale = _submit(tmp_path, s2["run_id"], "squad-other-9", res, capsys,
                        want_rc=1)

        after = _values(sup)
        assert dup["ok"] is True
        assert stale["error"] == "stale_call_key"
        for key in ("budget", "attended_charge_applied", "attended_head_results"):
            assert after[key] == before[key], key
        assert after["budget"]["spent_usd"] == pytest.approx(0.75)

    def test_fourth_step_opens_lead_cursor_like_headless(
            self, tmp_path, monkeypatch, capsys):
        _install(monkeypatch, _state(_task(["h1", "h2", "h3"])))
        _run_three_heads(tmp_path, capsys)

        lead = _step(tmp_path, capsys)

        assert lead["run_id"] == _tid
        # Hydra#89 step 4: the lead cursor is the task_id cursor and is
        # stamped leg="lead" (a head-less cursor still has no "leg" key).
        assert "head" not in lead and lead["leg"] == "lead"
        assert lead["host_action"]["call_key"] == f"squad-{_tid}-0"
        assert not lead["host_action"]["agent_type"].startswith("plug:")
        assert Path(lead["cursor_path"]) == Path(
            host_bridge.cursor_path(tmp_path, _wf, _tid))
        assert Path(lead["cursor_path"]).exists()

    def test_unresolved_head_no_cursor_no_ticket(
            self, tmp_path, monkeypatch, capsys):
        def _bad(squad, head, **_kw):
            return head_registry.HeadResolution(
                squad=squad, head=head, plugin="plug", agent_name=head,
                qualified_name=f"plug:{head}", agent_path=None,
                rule="unresolved", error="no such agent")

        _install(monkeypatch, _state(_task(["h1"])), resolver=_bad)

        out = _step(tmp_path, capsys, want_rc=1)

        assert out["ok"] is False and out["status"] == "head_unresolved"
        assert out["head"] == "h1" and out["task_id"] == _tid
        assert out["error"] == "no such agent"
        assert _cursor_files(tmp_path) == []
        assert _ticket_files(tmp_path) == []

    def test_heads_on_non_native_entrypoint_unsupported(
            self, tmp_path, monkeypatch, capsys):
        _install(monkeypatch, _state(_task(["h1"])), entrypoint="claude-skill")

        out = _step(tmp_path, capsys, want_rc=1)

        assert out["ok"] is False
        assert out["status"] == "heads_unsupported_entrypoint"
        assert _cursor_files(tmp_path) == []
        assert _ticket_files(tmp_path) == []

    def test_invalid_leg_run_id_returns_clean_response(
            self, tmp_path, monkeypatch, capsys):
        def _boom(task_id, head):
            raise ValueError("bad leg id")

        monkeypatch.setattr("hydra_core.state.head_leg_run_id", _boom)
        _install(monkeypatch, _state(_task(["h1"])))

        out = _step(tmp_path, capsys, want_rc=1)

        assert out["ok"] is False and out["status"] == "head_unresolved"
        assert "bad leg id" in out["error"]
        assert _cursor_files(tmp_path) == []
        assert _ticket_files(tmp_path) == []

    def test_budget_blocked_at_head_step_opens_no_cursor(
            self, tmp_path, monkeypatch, capsys):
        st = _state(_task(["h1", "h2"]), budget_usd=1.0)
        st.budget.spent_usd = 1.0
        _install(monkeypatch, st)

        out = _step(tmp_path, capsys)

        assert out["ok"] is False
        assert out["status"] == "budget_blocked"
        assert out["task_id"] == _tid
        assert _cursor_files(tmp_path) == []
        assert _ticket_files(tmp_path) == []

    def test_open_head_cursor_reserved_not_reopened(
            self, tmp_path, monkeypatch, capsys):
        _install(monkeypatch, _state(_task(["h1", "h2"])))
        first = _step(tmp_path, capsys)

        again = _step(tmp_path, capsys)

        assert again["run_id"] == first["run_id"] == head_leg_run_id(_tid, "h1")
        assert again["host_action"]["call_key"] == first["host_action"]["call_key"]
        assert len(_cursor_files(tmp_path)) == 2

    def test_unreadable_head_cursor_refused(self, tmp_path, monkeypatch, capsys):
        _install(monkeypatch, _state(_task(["h1"])))
        first = _step(tmp_path, capsys)
        Path(first["cursor_path"]).write_text("{not json", encoding="utf-8")

        out = _step(tmp_path, capsys, want_rc=1)

        assert out["status"] == "dispatch_seam_cursor_unreadable"

    def test_head_leg_emitted_envelope_is_rejected_not_ingested(
            self, tmp_path, monkeypatch, capsys):
        sup, _d, _p = _install(monkeypatch, _state(_task(["h1"])))
        s1 = _step(tmp_path, capsys)
        dev_task = {
            "id": "99999999-0000-4000-8000-000000000001", "type": "DEV_TASK",
            "origin_squad": "executive", "target_squad": "engineering",
            "workflow_id": _wf, "title": "t", "description": "delegate me",
        }

        out = _submit(tmp_path, s1["run_id"], s1["host_action"]["call_key"],
                      {"text": "x", "cost_usd": 0.25,
                       "emitted_envelopes": [dev_task]}, capsys)

        v = _values(sup)
        assert [t["task_id"] for t in v["tasks"]] == [_tid]
        assert v["open_pp_runs"] == []
        assert v["envelopes"] == []
        assert out["status"] == "envelopes_rejected"
        assert out["rejected_envelopes"][0]["reason"] == "head_leg_cannot_delegate"
        assert out["rejected_envelopes"][0]["envelope_id"] == dev_task["id"]
        cursor = host_bridge.load_cursor(
            host_bridge.cursor_path(tmp_path, _wf, s1["run_id"]))
        assert cursor["rejected_envelopes"][0]["reason"] == "head_leg_cannot_delegate"
        assert v["attended_head_results"][head_result_key(_tid, "h1")]
        assert _tid not in v["attended_completed_task_ids"]


# =========================================================================== #
# Empty-heads regression: a head-less squad task is what it was at the        #
# starting commit (golden captured from commit 216b381).                      #
# =========================================================================== #

_volatile = re.compile(r"(_at$|^ts$|timestamp|^time$|elapsed|^created$|^updated$)")


def _norm(obj, root: Path):
    if isinstance(obj, dict):
        return {k: _norm(v, root) for k, v in sorted(obj.items())
                if not _volatile.search(k)}
    if isinstance(obj, list):
        return [_norm(v, root) for v in obj]
    if isinstance(obj, str):
        for form in (str(root).replace("\\", "\\\\"), str(root), root.as_posix()):
            obj = obj.replace(form, "<ROOT>")
        return obj.replace("\\", "/")
    return obj


def capture_headless_flow(root: Path, monkeypatch, capsys) -> dict:
    """Run step + submit for a head-less squad task; return normalised output.

    Normalised: tmp project root -> '<ROOT>', path separators -> '/', and any
    key that is a wall-clock timestamp (the _volatile pattern). Ids are fixed
    inputs, so they are compared verbatim.
    """
    sup, _d, _p = _install(monkeypatch, _state(_task([])))
    step = _step(root, capsys)
    cursor_after_step = host_bridge.load_cursor(step["cursor_path"])
    tp = dispatch_seam.ticket_path(root, _wf, step["run_id"],
                                   step["host_action"]["call_key"])
    with open(dispatch_seam._long_path(tp), encoding="utf-8") as f:
        ticket = json.load(f)
    sub = _submit(root, step["run_id"], step["host_action"]["call_key"],
                  {"text": "done", "cost_usd": 0.375, "tokens_in": 7,
                   "tokens_out": 3, "model": "m"}, capsys)
    cursor_after_submit = host_bridge.load_cursor(step["cursor_path"])
    v = _values(sup)
    keys = ("budget", "attended_completed_task_ids", "attended_done_task_ids",
            "attended_results", "attended_charge_applied",
            "attended_checkpoint_reconciled", "attended_head_results",
            "open_pp_runs", "budget_downgrade_active")
    out = {"step": step, "cursor_after_step": cursor_after_step,
           "ticket": ticket, "submit": sub,
           "cursor_after_submit": cursor_after_submit,
           "checkpoint": {k: v.get(k) for k in keys}}
    return _norm(json.loads(json.dumps(out, default=str)), root)


def test_empty_heads_flow_unchanged_from_starting_commit(
        tmp_path, monkeypatch, capsys):
    root = tmp_path / "proj"
    root.mkdir()

    got = capture_headless_flow(root, monkeypatch, capsys)

    want = json.loads(_fixture.read_text(encoding="utf-8"))
    assert got == want
    assert got["checkpoint"]["attended_head_results"] == {}
    assert got["checkpoint"]["attended_completed_task_ids"] == [_tid]
