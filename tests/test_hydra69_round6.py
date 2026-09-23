"""Hydra#69 round 6: four findings from the cross-vendor (codex gpt-6-astra)
review of feat/planning-phase at HEAD b63ad07.

1. HIGH - Reconciliation marker precedes completion of reconciliation.
   `attended_checkpoint_reconciled` (round 5) was folded into the SAME
   atomic write as the budget charge, before the later checkpoint writes a
   terminal `_cmd_attended_submit` still has to make (attempt-counter bump,
   PLAN acceptance/rejection outcome, completion ids). Fixed by splitting
   CHARGE evidence (`attended_charge_applied`, written with the charge) from
   FULLY-RECONCILED evidence (`attended_checkpoint_reconciled`, written only
   after every downstream write for the call has succeeded).

2. HIGH (new in round 5) - A different call_key double-charges a terminal
   cursor. Fixed by persisting the cursor's own trusted terminal call
   identity (`terminal_call_key`, stamped by `host_bridge.submit_host_result`
   the instant a cursor first goes terminal) and validating a submitted
   call_key against it before ever returning a terminal cursor's cached
   result; the reconciliation/charge identity is derived from that trusted
   identity, never from the caller-supplied call_key directly.

3. MED - Terminal checkpoint parking only covered plan_gate. Fixed by
   making every abort/reject terminal resolution -- at ANY gate -- force
   `next` to `()` via the same `as_node="postcheck"` write, and by refusing
   to continue a bare interrupt whose durable `hitl_history` already records
   a terminal resolution for the parked gate.

4. LOW (new) - `step` reported a stamped abort as
   `plan_approved_not_materialised`. Fixed by factoring the SAME
   approval-evidence predicate `materialise_plan_steps` uses into one shared
   helper (`state.plan_gate_approve_evidence`) so `step` and the
   materialiser can never disagree.

Every test here is proven as a property: reverting the corresponding fix
makes the test fail.
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


# =========================================================================== #
# Shared scaffolding (mirrors tests/test_plan_submit_rejection.py's stateful
# fake supervisor + attended step/submit helpers; duplicated here so this
# file stays self-contained).
# =========================================================================== #

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
        next_ = ("plan_gate",) if self._values.get("plan_status") == "drafted" else ()
        return _Snap(dict(self._values), next_)

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


class _FakeAttendedDispatcher:
    project_root = None

    def call_mcp(self, *a, **k):  # pragma: no cover -- must never be reached
        raise AssertionError("no MCP calls expected for a squad-cursor stage")

    def set_squad_packs(self, packs):
        pass


def _plan_dict(workflow_id, *, plan_id=None, revision=1, supersedes=None, steps=None):
    d = {
        "id": str(plan_id or uuid4()),
        "type": "PLAN",
        "origin_squad": "planning",
        "target_squad": "hydra",
        "workflow_id": str(workflow_id),
        "rigor": "standard",
        "goal_restatement": "ship the thing",
        "summary": "ship the thing",
        "plan_revision": revision,
        "steps": steps or [],
    }
    if supersedes is not None:
        d["supersedes"] = str(supersedes)
    return d


def _step(capsys, wf_id):
    rc = cli._cmd_attended_step(
        argparse.Namespace(project=str(HYDRA_ROOT), workflow_id=wf_id, verbose=False))
    out = capsys.readouterr().out
    assert rc == 0, out
    return json.loads(out)


def _submit(capsys, wf_id, run_id, call_key, result_path, *, project=None):
    rc = cli._cmd_attended_submit(argparse.Namespace(
        project=str(project or HYDRA_ROOT), workflow_id=wf_id, run_id=run_id,
        call_key=call_key, result=str(result_path), verbose=False))
    captured = capsys.readouterr()
    out = captured.out or captured.err
    return rc, json.loads(out)


def _write_result(tmp_path, name, payload):
    p = tmp_path / name
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


# =========================================================================== #
# Defect 1 (HIGH) -- the FULL-reconciliation marker is written only after
# every downstream checkpoint write a terminal submit needs to make has
# actually succeeded; a repair retry never re-charges.
# =========================================================================== #

class TestDefect1FullReconciliationOrdering:
    def _plan_task_setup(self, monkeypatch, tmp_path, trigger_key: str):
        task = TaskState(owner_squad="planning", description="author a plan for: ship it")
        wf = uuid4()
        state = HydraState(root_goal="ship it", workflow_id=wf, tasks=[task])
        fake_sup = _FlakyKeyedSup(state.model_dump(mode="json"), trigger_key)

        class _FakeDispatcher:
            project_root = tmp_path

            def call_mcp(self, *a, **k):
                raise AssertionError("PLAN flow must never call an MCP tool")

            def set_squad_packs(self, packs):
                pass

        monkeypatch.setattr(cli, "_attended_live_dispatcher",
                            lambda *a, **k: _FakeDispatcher())
        monkeypatch.setattr("hydra_core.supervisor.build_supervisor",
                            lambda **k: fake_sup)
        charge_calls: list[float] = []
        monkeypatch.setattr(
            "hydra_core.governance.charge_and_gate",
            lambda state, cost, toks, **_kw: (charge_calls.append(cost), (False, False))[1],
        )
        monkeypatch.setenv("HYDRA_PLAN_PHASE", "1")
        return str(wf), str(task.task_id), fake_sup, charge_calls

    def test_attempt_counter_write_failure_repairs_without_double_charge(
        self, monkeypatch, tmp_path, capsys,
    ):
        """The REJECTION path's failure point: the attempt-counter write
        (`plan_submit_attempts`) fails on the first submit, AFTER the charge
        has already landed."""
        wf_id, task_id, fake_sup, charge_calls = self._plan_task_setup(
            monkeypatch, tmp_path, "plan_submit_attempts")

        step1 = _step(capsys, wf_id)
        call_key_0 = step1["host_action"]["call_key"]

        r0 = _write_result(tmp_path, "r0.json", {
            "text": "no plan emitted", "cost_usd": 0.05, "tokens_in": 1, "tokens_out": 1,
        })
        rc1, body1 = _submit(capsys, wf_id, task_id, call_key_0, r0)
        assert rc1 == 1
        assert body1.get("error") == "checkpoint_persist_failed"
        values1 = fake_sup._values
        assert not (values1.get("plan_submit_attempts") or {}).get(task_id), (
            "the failed attempt-counter write must not have landed"
        )
        assert not (values1.get("attended_checkpoint_reconciled") or {}), (
            "the marker must never be set before every downstream write lands"
        )
        assert charge_calls == [0.05], "the charge itself must still have landed"

        # Retry with the SAME call_key: must repair the missing attempt
        # counter WITHOUT re-charging.
        rc2, body2 = _submit(capsys, wf_id, task_id, call_key_0, r0)
        assert rc2 == 0, body2
        values2 = fake_sup._values
        assert (values2.get("plan_submit_attempts") or {}).get(task_id) == 1, (
            f"the retry must repair the missing attempt-counter write: {body2}"
        )
        assert charge_calls == [0.05], "a repair retry must NEVER re-invoke charge_and_gate"
        _recon = values2.get("attended_checkpoint_reconciled") or {}
        assert any(v is True for v in _recon.values()), _recon

        # A third submit must be a true no-op.
        prior_call_count = len(fake_sup.update_calls)
        rc3, body3 = _submit(capsys, wf_id, task_id, call_key_0, r0)
        assert rc3 == 0, body3
        assert charge_calls == [0.05]
        assert len(fake_sup.update_calls) == prior_call_count, (
            "a fully-reconciled retry must not write to the checkpoint again"
        )

    def test_completion_write_failure_repairs_without_double_charge(
        self, monkeypatch, tmp_path, capsys,
    ):
        """The ACCEPTANCE path's failure point: the completion write
        (`attended_completed_task_ids`/`attended_done_task_ids`/
        `attended_results`) fails on the first submit, AFTER the charge and
        the PLAN's own graph re-entry have already landed."""
        wf_id, task_id, fake_sup, charge_calls = self._plan_task_setup(
            monkeypatch, tmp_path, "attended_completed_task_ids")

        step1 = _step(capsys, wf_id)
        call_key_0 = step1["host_action"]["call_key"]

        plan = _plan_dict(wf_id)
        r0 = _write_result(tmp_path, "r0.json", {
            "text": "plan authored", "cost_usd": 0.10, "tokens_in": 5, "tokens_out": 5,
            "emitted_envelopes": [plan],
        })
        rc1, body1 = _submit(capsys, wf_id, task_id, call_key_0, r0)
        assert rc1 == 1
        assert body1.get("error") == "checkpoint_persist_failed"
        values1 = fake_sup._values
        assert task_id not in (values1.get("attended_completed_task_ids") or []), (
            "the failed completion write must not have landed"
        )
        assert not (values1.get("attended_checkpoint_reconciled") or {})
        # The plan itself WAS durably drafted+re-entered on this call --
        # only the completion bookkeeping failed.
        assert values1.get("plan_status") == "drafted"
        assert charge_calls == [0.10]

        rc2, body2 = _submit(capsys, wf_id, task_id, call_key_0, r0)
        assert rc2 == 0, body2
        values2 = fake_sup._values
        assert task_id in (values2.get("attended_completed_task_ids") or []), (
            f"the retry must repair the missing completion write: {body2}"
        )
        assert charge_calls == [0.10], "a repair retry must never re-charge"
        _recon = values2.get("attended_checkpoint_reconciled") or {}
        assert any(v is True for v in _recon.values()), _recon

        prior_call_count = len(fake_sup.update_calls)
        rc3, body3 = _submit(capsys, wf_id, task_id, call_key_0, r0)
        assert rc3 == 0, body3
        assert charge_calls == [0.10]
        assert len(fake_sup.update_calls) == prior_call_count, (
            "a fully-reconciled retry must not write to the checkpoint again"
        )


# =========================================================================== #
# Defect 2 (HIGH) -- a terminal cursor refuses a different call_key instead
# of returning (and re-billing) its cached result.
# =========================================================================== #

class TestDefect2StaleCallKeyRefusal:
    def _seed_cursor(self, tmp_path, wf, task_id):
        host_bridge.begin_squad_stage(
            workflow_id=wf, task_id=task_id, squad_slug="customer-support",
            entrypoint="claude-skill", lead_agent="general-purpose",
            pack_cwd=str(tmp_path), request_text="handle the ticket",
            project_root=tmp_path,
        )
        return host_bridge.cursor_path(tmp_path, wf, task_id)

    def test_different_call_key_against_terminal_cursor_is_refused(self, tmp_path):
        wf = str(uuid4())
        task_id = str(uuid4())
        cfile = self._seed_cursor(tmp_path, wf, task_id)
        call_key = f"squad-{task_id}-0"

        res1 = host_bridge.submit_host_result(
            _FakeAttendedDispatcher(), cursor_file=cfile, call_key=call_key,
            result={"text": "done", "cost_usd": 1.5, "tokens_in": 10, "tokens_out": 5},
        )
        assert res1["status"] in ("complete", "complete_unpersisted", "surfaced")
        assert res1["terminal_call_key"] == call_key
        assert res1.get("already_charged") is False

        # A DIFFERENT call_key against the now-terminal cursor must be
        # refused structurally, not treated as an idempotent re-submit.
        other_key = f"squad-{task_id}-1"
        res2 = host_bridge.submit_host_result(
            _FakeAttendedDispatcher(), cursor_file=cfile, call_key=other_key,
            result={"text": "done again", "cost_usd": 1.5, "tokens_in": 10, "tokens_out": 5},
        )
        assert res2.get("error_code") == "stale_call_key"
        assert "ignored" in res2

        # The SAME call_key remains idempotent.
        res3 = host_bridge.submit_host_result(
            _FakeAttendedDispatcher(), cursor_file=cfile, call_key=call_key,
            result={"text": "done", "cost_usd": 1.5, "tokens_in": 10, "tokens_out": 5},
        )
        assert res3.get("error_code") != "stale_call_key"
        assert res3["status"] == res1["status"]

    def test_legacy_cursor_without_terminal_call_key_accepts_any_call_key(self, tmp_path):
        """A cursor persisted before `terminal_call_key` existed (simulated
        by deleting the field after a real terminal transition) must accept
        ANY call_key on retry -- already fully charged/settled by definition
        of being on disk -- never refused, never re-charged."""
        wf = str(uuid4())
        task_id = str(uuid4())
        cfile = self._seed_cursor(tmp_path, wf, task_id)
        call_key = f"squad-{task_id}-0"
        host_bridge.submit_host_result(
            _FakeAttendedDispatcher(), cursor_file=cfile, call_key=call_key,
            result={"text": "done", "cost_usd": 1.5, "tokens_in": 10, "tokens_out": 5},
        )
        cursor = host_bridge.load_cursor(cfile)
        assert cursor.get("terminal_call_key") == call_key
        del cursor["terminal_call_key"]
        host_bridge.save_cursor(cfile, cursor)

        res = host_bridge.submit_host_result(
            _FakeAttendedDispatcher(), cursor_file=cfile,
            call_key="some-completely-different-key",
            result={"text": "done", "cost_usd": 1.5, "tokens_in": 10, "tokens_out": 5},
        )
        assert res.get("error_code") != "stale_call_key"
        assert res.get("terminal_call_key") is None

    def test_cli_submit_refuses_stale_call_key_without_charging_or_marking(
        self, monkeypatch, tmp_path, capsys,
    ):
        wf = str(uuid4())
        task = TaskState(owner_squad="customer-support", description="handle the ticket")
        task_id = str(task.task_id)
        call_key = f"squad-{task_id}-0"

        host_bridge.begin_squad_stage(
            workflow_id=wf, task_id=task_id, squad_slug="customer-support",
            entrypoint="claude-skill", lead_agent="general-purpose",
            pack_cwd=str(tmp_path), request_text="handle the ticket",
            project_root=tmp_path,
        )
        state = HydraState(root_goal="x", workflow_id=wf, tasks=[task])
        fake_sup = _StatefulFakeSup(state.model_dump(mode="json"))

        monkeypatch.setattr(cli, "_attended_live_dispatcher",
                            lambda *a, **k: _FakeAttendedDispatcher())
        monkeypatch.setattr("hydra_core.supervisor.build_supervisor",
                            lambda **k: fake_sup)

        result_path = tmp_path / "result.json"
        result_path.write_text(json.dumps({
            "text": "resolved the ticket", "cost_usd": 1.5,
            "tokens_in": 10, "tokens_out": 5,
        }), encoding="utf-8")

        rc1, body1 = _submit(capsys, wf, task_id, call_key, result_path, project=tmp_path)
        assert rc1 == 0, body1
        assert float((fake_sup._values.get("budget") or {}).get("spent_usd") or 0.0) == \
            pytest.approx(1.5)

        # A DIFFERENT call_key against the SAME (now terminal) cursor must
        # never charge again nor write a reconciliation marker.
        other_key = f"squad-{task_id}-1"
        rc2, body2 = _submit(capsys, wf, task_id, other_key, result_path, project=tmp_path)
        assert rc2 == 1
        assert body2.get("error") == "stale_call_key"
        spent_after_stale = float(
            (fake_sup._values.get("budget") or {}).get("spent_usd") or 0.0
        )
        assert spent_after_stale == pytest.approx(1.5), (
            "a stale call_key must never charge the budget a second time"
        )
        assert not (fake_sup._values.get("attended_checkpoint_reconciled") or {}).get(
            f"{task_id}:{other_key}"
        ), "a stale call_key must never mint its own reconciliation marker"


# =========================================================================== #
# Defect 3 (MED) -- terminal checkpoint parking covers every gate, not just
# plan_gate; the bare-interrupt branch refuses to continue past a durably
# recorded terminal resolution for the parked gate.
# =========================================================================== #

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


def _seed_budget_gate_workflow():
    """Seed a REAL LangGraph checkpoint parked at a NON-plan_gate interrupt
    (`approval`, the `over_budget` gate's own node name), mirroring round 5's
    `_seed_plan_gate_workflow` fixture but for the general (any-gate) path
    this round's fix covers."""
    from hydra_core.supervisor import build_supervisor, _PurePythonRunner

    wf = uuid4()
    pending = {
        "workflow_id": str(wf), "reason": "over_budget", "gate_node": "approval",
        "options": ["approve", "abort"],
    }
    state = HydraState(
        workflow_id=wf, root_goal="round 6 defect 3 repro", phase="approval",
        pending_hitl=pending, tasks=[], requires_human_approval=True,
    )
    sup = build_supervisor(project_root=HYDRA_ROOT, dispatcher=_StubDispatcher())
    assert not isinstance(sup, _PurePythonRunner), "langgraph required for this test"
    config = {"configurable": {"thread_id": str(wf)}}
    sup.update_state(config, state.model_dump(mode="json"), as_node="planner")
    snap = sup.get_state(config)
    assert tuple(snap.next) == ("approval",), (
        f"fixture must park at the approval gate; got next={snap.next!r}"
    )
    return str(wf), sup, config


def _resume(project, wf, action, option, *, gate_only=False):
    args = argparse.Namespace(
        project=str(project), workflow_id=wf, action=action, option=option,
        live=False, verbose=False, operator="operator@example.com",
        critique_ref=None, gate_only=gate_only,
    )
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        rc = cli._cmd_resume(args)
    out = buf.getvalue()
    body = None
    if out.strip():
        start = out.index("{")
        body = json.loads(out[start:])
    return rc, body


class TestDefect3NonPlanGateTerminalPark:
    def test_repeated_abort_at_non_plan_gate_stays_surfaced(self, hermetic):
        wf, sup, config = _seed_budget_gate_workflow()

        rc1, body1 = _resume(HYDRA_ROOT, wf, "approve", "abort")
        assert rc1 == 0, body1
        values1 = sup.get_state(config).values
        assert values1.get("phase") == "surfaced"
        assert tuple(sup.get_state(config).next) == (), (
            "a terminal abort at ANY gate must force next==() so no later "
            "invoke can run the next node headless"
        )

        rc2, body2 = _resume(HYDRA_ROOT, wf, "approve", "abort")
        assert rc2 == 0, body2
        values2 = sup.get_state(config).values
        assert values2.get("phase") == "surfaced"
        assert not values2.get("tasks"), (
            f"a repeated abort at a non-plan gate must never continue the graph: {body2}"
        )

    def test_reject_then_repeated_approve_at_non_plan_gate_stays_surfaced(self, hermetic):
        wf, sup, config = _seed_budget_gate_workflow()

        rc1, body1 = _resume(HYDRA_ROOT, wf, "reject", None)
        assert rc1 == 0, body1
        values1 = sup.get_state(config).values
        assert values1.get("phase") == "surfaced"
        assert tuple(sup.get_state(config).next) == ()

        rc2, body2 = _resume(HYDRA_ROOT, wf, "approve", None)
        assert rc2 == 0, body2
        values2 = sup.get_state(config).values
        assert not values2.get("tasks"), (
            f"an approve after a genuine reject at a non-plan gate must "
            f"never continue the graph: {body2}"
        )

    def test_bare_interrupt_with_durable_terminal_history_refuses_plain_approve(
        self, hermetic,
    ):
        """Defense in depth (layer b): a checkpoint whose `pending_hitl` is
        already cleared but whose `next` is still truthy at a gate durable
        `hitl_history` already recorded a terminal resolution for -- must
        refuse a PLAIN `--action approve` (no option) retry, not just a
        literal `option=="abort"` repeat."""
        from hydra_core.supervisor import build_supervisor, _PurePythonRunner

        wf = uuid4()
        state = HydraState(
            workflow_id=wf, root_goal="round 6 defect 3b repro", phase="approval",
            pending_hitl=None, tasks=[], requires_human_approval=True,
            hitl_history=[{
                "gate_node": "approval", "resolution": "reject", "option": None,
            }],
        )
        sup = build_supervisor(project_root=HYDRA_ROOT, dispatcher=_StubDispatcher())
        assert not isinstance(sup, _PurePythonRunner)
        config = {"configurable": {"thread_id": str(wf)}}
        sup.update_state(config, state.model_dump(mode="json"), as_node="planner")
        assert tuple(sup.get_state(config).next) == ("approval",)

        rc, body = _resume(HYDRA_ROOT, str(wf), "approve", None)
        assert rc == 0, body
        values = sup.get_state(config).values
        assert not values.get("tasks"), (
            f"a plain approve retry must refuse to continue past a durably "
            f"recorded terminal resolution for the parked gate: {body}"
        )
        assert body.get("resumed") is False
        assert body.get("graph_reentered") is False

    def test_genuine_bare_interrupt_with_no_terminal_history_still_continues(
        self, hermetic,
    ):
        """MU7 preservation: a bare interrupt with NO terminal hitl_history
        for the parked gate must still be allowed to continue."""
        from hydra_core.supervisor import build_supervisor, _PurePythonRunner

        wf = uuid4()
        state = HydraState(
            workflow_id=wf, root_goal="round 6 defect 3 MU7 control", phase="approval",
            pending_hitl=None, tasks=[], requires_human_approval=True,
            hitl_history=[{
                "gate_node": "approval", "resolution": "approve", "option": None,
            }],
        )
        sup = build_supervisor(project_root=HYDRA_ROOT, dispatcher=_StubDispatcher())
        assert not isinstance(sup, _PurePythonRunner)
        config = {"configurable": {"thread_id": str(wf)}}
        sup.update_state(config, state.model_dump(mode="json"), as_node="planner")
        assert tuple(sup.get_state(config).next) == ("approval",)

        rc, body = _resume(HYDRA_ROOT, str(wf), "approve", None)
        assert rc == 0, body
        assert body.get("continued_bare_interrupt") is True, (
            f"a genuine bare interrupt with no terminal history must still "
            f"continue the graph: {body}"
        )


# =========================================================================== #
# Defect 4 (LOW) -- `step` reuses the SAME approval-evidence predicate
# `materialise_plan_steps` uses, so a stamped abort is never misreported as
# `plan_approved_not_materialised`.
# =========================================================================== #

class TestDefect4StepSharesApprovalEvidencePredicate:
    def _wedge_values(self, wf: str, *, history) -> dict:
        return {
            "workflow_id": wf, "root_goal": "x", "phase": "dispatch",
            "plan_status": "judged", "plan_revision": 1, "pending_hitl": None,
            "tasks": [], "hitl_history": history,
            "attended_completed_task_ids": [],
            "plan_placeholder_task_ids": [], "plan_superseded_task_ids": [],
        }

    class _AttendedFakeSup:
        def __init__(self, values):
            self.values = dict(values)

        def get_state(self, config):
            outer = self

            class _Snap:
                values = outer.values
                next = ()

            return _Snap()

        def update_state(self, config, patch, as_node=None):
            self.values.update(patch)

        def invoke(self, *a, **k):
            raise AssertionError("must not invoke on the wedge terminal path")

    class _NopDispatcher:
        live_execution = True

        def call_mcp(self, server, tool, args, **_kw):
            return {"status": "done", "result": {}}

        def set_squad_packs(self, packs):
            pass

    def _step(self, tmp_path, wf, values, monkeypatch, capsys):
        sup = self._AttendedFakeSup(values)
        monkeypatch.setattr("hydra_core.cli._attended_live_dispatcher",
                            lambda *a, **k: self._NopDispatcher())
        monkeypatch.setattr("hydra_core.supervisor.build_supervisor",
                            lambda **k: sup)
        monkeypatch.setattr("hydra_core.squad_loader.discover_squads",
                            lambda *a, **k: {})
        rc = cli._cmd_attended_step(argparse.Namespace(
            project=str(tmp_path), workflow_id=wf, verbose=False))
        out = capsys.readouterr().out
        assert rc == 0, f"attended step failed: {out[:400]}"
        return json.loads(out)

    def test_aborted_plan_reports_gate_unresolved_not_approved_not_materialised(
        self, monkeypatch, tmp_path, capsys,
    ):
        """The exact round 6 regression: `resolution == "approve"` WITH
        `option == "abort"` (how the plan_gate's own revision_ceiling_reached
        / unjudgeable_plan branches record an operator abort) must be
        reported as an unresolved/terminal gate, never
        `plan_approved_not_materialised`."""
        wf = str(uuid4())
        history = [{
            "resolution": "approve", "gate_node": "plan_gate",
            "plan_revision": 1, "option": "abort",
        }]
        values = self._wedge_values(wf, history=history)
        out = self._step(tmp_path, wf, values, monkeypatch, capsys)
        assert out["status"] == "plan_gate_unresolved", out
        assert out["status"] != "plan_approved_not_materialised"
        assert out["ok"] is False

    def test_genuine_approve_still_reports_not_materialised(
        self, monkeypatch, tmp_path, capsys,
    ):
        """Control: a genuine (non-abort) approve recorded for this revision
        still reports the distinct materialisation-bug diagnostic."""
        wf = str(uuid4())
        history = [{
            "resolution": "approve", "gate_node": "plan_gate",
            "plan_revision": 1,
        }]
        values = self._wedge_values(wf, history=history)
        out = self._step(tmp_path, wf, values, monkeypatch, capsys)
        assert out["status"] == "plan_approved_not_materialised", out
        assert out["ok"] is False


# =========================================================================== #
# Round 6 follow-up (cross-vendor, HIGH) defect 1 -- a LEGACY already-charged
# cursor (its checkpoint has no `attended_charge_applied` marker at all, only
# the cursor sidecar's own `charged=True`) must repair its missing downstream
# writes WITHOUT ever re-invoking `charge_and_gate`.
# =========================================================================== #

class TestFollowupDefect1LegacyChargeNeverReCharges:
    def test_legacy_already_charged_cursor_repairs_without_recharging(
        self, monkeypatch, tmp_path, capsys,
    ):
        """A TRUE legacy cursor -- `terminal_call_key` absent entirely
        (persisted by an older version of this code, or the field was
        stripped/never written), `charged=True` on the sidecar, but the
        checkpoint has NEITHER an `attended_charge_applied` marker NOR the
        completion bookkeeping for it -- must repair the missing checkpoint
        writes WITHOUT EVER re-invoking `charge_and_gate`, and must accept
        the resulting permanent under-charge rather than risk a double
        charge (the follow-up critique's exact repro)."""
        wf = str(uuid4())
        task = TaskState(owner_squad="customer-support", description="handle the ticket")
        task_id = str(task.task_id)
        original_call_key = f"squad-{task_id}-0"

        host_bridge.begin_squad_stage(
            workflow_id=wf, task_id=task_id, squad_slug="customer-support",
            entrypoint="claude-skill", lead_agent="general-purpose",
            pack_cwd=str(tmp_path), request_text="handle the ticket",
            project_root=tmp_path,
        )
        cfile = host_bridge.cursor_path(tmp_path, wf, task_id)

        # Directly (outside the CLI, simulating a PRIOR process/deploy) drive
        # the cursor terminal + charged, then strip `terminal_call_key` to
        # simulate a cursor that predates this reconciliation scheme
        # entirely. The checkpoint (`fake_sup`, built fresh below) never saw
        # ANY of this -- it has no marker and no completion record, exactly
        # the "checkpoint lacks marker and lacks completion" repro.
        host_bridge.submit_host_result(
            _FakeAttendedDispatcher(), cursor_file=cfile, call_key=original_call_key,
            result={"text": "resolved the ticket", "cost_usd": 2.0,
                    "tokens_in": 10, "tokens_out": 5},
        )
        host_bridge.mark_charged(cfile)
        cursor = host_bridge.load_cursor(cfile)
        assert cursor.get("charged") is True
        del cursor["terminal_call_key"]
        host_bridge.save_cursor(cfile, cursor)

        state = HydraState(root_goal="x", workflow_id=wf, tasks=[task])
        fake_sup = _StatefulFakeSup(state.model_dump(mode="json"))
        monkeypatch.setattr(cli, "_attended_live_dispatcher",
                            lambda *a, **k: _FakeAttendedDispatcher())
        monkeypatch.setattr("hydra_core.supervisor.build_supervisor",
                            lambda **k: fake_sup)
        charge_calls: list[float] = []
        monkeypatch.setattr(
            "hydra_core.governance.charge_and_gate",
            lambda state, cost, toks, **_kw: (charge_calls.append(cost), (False, False))[1],
        )

        result_path = tmp_path / "result.json"
        result_path.write_text(json.dumps({
            "text": "resolved the ticket", "cost_usd": 2.0,
            "tokens_in": 10, "tokens_out": 5,
        }), encoding="utf-8")

        # A legacy cursor (no terminal_call_key) accepts ANY call_key --
        # use a DIFFERENT one than the original to also prove this is not
        # merely the same-call_key idempotent path.
        retry_call_key = "some-other-call-key"
        rc1, body1 = _submit(capsys, wf, task_id, retry_call_key, result_path, project=tmp_path)
        assert rc1 == 0, body1
        assert charge_calls == [], (
            "a TRUE legacy already-charged cursor must NEVER invoke "
            "charge_and_gate -- not even once"
        )
        values1 = fake_sup._values
        spent_after_repair = float((values1.get("budget") or {}).get("spent_usd") or 0.0)
        assert spent_after_repair == 0.0, (
            "budget.spent_usd must be UNCHANGED -- an accepted permanent "
            "under-charge is safer than risking a double charge for a cursor "
            "whose original charge cannot be verified"
        )
        assert task_id in (values1.get("attended_completed_task_ids") or []), (
            f"the repair must land the missing completion write: {body1}"
        )
        recon_key = f"{task_id}:legacy"
        assert (values1.get("attended_charge_applied") or {}).get(recon_key) is not None, (
            "the repair must stamp the marker so future retries converge"
        )

        # A second submit (any call_key) must be a true no-op now.
        prior_call_count = len(fake_sup.update_calls)
        rc2, body2 = _submit(capsys, wf, task_id, "yet-another-key", result_path, project=tmp_path)
        assert rc2 == 0, body2
        assert charge_calls == []
        assert len(fake_sup.update_calls) == prior_call_count, (
            "a fully-reconciled retry must not write to the checkpoint again"
        )

    def test_non_legacy_crash_retry_still_recharges_exactly_once(
        self, monkeypatch, tmp_path, capsys,
    ):
        """Control (must NOT regress): a cursor that DOES carry
        `terminal_call_key` (i.e. is NOT legacy), whose first checkpoint
        write genuinely failed to persist (so the atomic
        `attended_charge_applied` + `budget` write never landed at all), is
        safe -- and required -- to re-invoke `charge_and_gate` exactly once
        on retry. This is the crash-recovery case, not the legacy case: the
        recon_key is unique to this one cursor's one terminal call, so an
        absent marker is conclusive proof nothing landed yet."""
        wf = str(uuid4())
        task = TaskState(owner_squad="customer-support", description="handle the ticket")
        task_id = str(task.task_id)
        call_key = f"squad-{task_id}-0"

        host_bridge.begin_squad_stage(
            workflow_id=wf, task_id=task_id, squad_slug="customer-support",
            entrypoint="claude-skill", lead_agent="general-purpose",
            pack_cwd=str(tmp_path), request_text="handle the ticket",
            project_root=tmp_path,
        )

        state = HydraState(root_goal="x", workflow_id=wf, tasks=[task])
        fake_sup = _FlakyKeyedSup(state.model_dump(mode="json"), "attended_charge_applied")
        monkeypatch.setattr(cli, "_attended_live_dispatcher",
                            lambda *a, **k: _FakeAttendedDispatcher())
        monkeypatch.setattr("hydra_core.supervisor.build_supervisor",
                            lambda **k: fake_sup)
        charge_calls: list[float] = []
        monkeypatch.setattr(
            "hydra_core.governance.charge_and_gate",
            lambda state, cost, toks, **_kw: (charge_calls.append(cost), (False, False))[1],
        )

        result_path = tmp_path / "result.json"
        result_path.write_text(json.dumps({
            "text": "resolved the ticket", "cost_usd": 2.0,
            "tokens_in": 10, "tokens_out": 5,
        }), encoding="utf-8")

        rc1, body1 = _submit(capsys, wf, task_id, call_key, result_path, project=tmp_path)
        assert rc1 == 1
        assert body1.get("error") == "checkpoint_persist_failed"
        assert charge_calls == [2.0], "the first call still charges exactly once"
        values1 = fake_sup._values
        assert float((values1.get("budget") or {}).get("spent_usd") or 0.0) == 0.0

        rc2, body2 = _submit(capsys, wf, task_id, call_key, result_path, project=tmp_path)
        assert rc2 == 0, body2
        assert charge_calls == [2.0, 2.0], (
            "a non-legacy crash-retry (unique recon_key, absent marker) "
            "must re-charge exactly once to actually land the charge"
        )
        values2 = fake_sup._values
        assert task_id in (values2.get("attended_completed_task_ids") or [])


# =========================================================================== #
# Round 6 follow-up (cross-vendor, HIGH) defect 2 -- the bare-interrupt
# terminal-history guard must bind to the EXACT gate INSTANCE the checkpoint
# is parked at (the latest hitl_history entry, plus a plan_revision match for
# plan_gate), never just the gate_node's name.
# =========================================================================== #

def _plan_ref(step_id="step-1"):
    return {
        "steps": [{
            "step_id": step_id, "target_squad": "engineering",
            "description": "do the thing", "priority": "P2",
            "acceptance_criteria": ["it works"], "envelope_type": "DEV_TASK",
        }],
    }


class TestFollowupDefect2TerminalHistoryGateInstanceIdentity:
    def test_unit_latest_entry_overall_wins_over_earlier_reject(self):
        """An earlier reject of gate X followed by a later, genuinely
        non-terminal resolution cycle (same gate_node) must never be found
        by skipping past the later entry back to the older reject."""
        history = [
            {"gate_node": "approval", "resolution": "reject", "option": None},
            {"gate_node": "approval", "resolution": "approve", "option": None},
        ]
        entry = cli._bare_interrupt_terminal_resolution(history, ("approval",))
        assert entry is None, (
            "the LATEST entry (a genuine approve) must win over the earlier reject"
        )

    def test_unit_plan_gate_revision_mismatch_does_not_block_new_occurrence(self):
        """A reject recorded against plan_gate REVISION 1, with the
        checkpoint's CURRENT plan_revision now at 2 (a fresh, never-resolved
        occurrence of the same-named gate), must not be treated as a
        terminal resolution for the NEW occurrence."""
        history = [{
            "gate_node": "plan_gate", "resolution": "reject",
            "option": None, "plan_revision": 1,
        }]
        entry = cli._bare_interrupt_terminal_resolution(
            history, ("plan_gate",), current_plan_revision=2)
        assert entry is None, (
            "a stale reject from an OLDER plan_revision must not block a "
            "new, unresolved occurrence of plan_gate at a NEWER revision"
        )

    def test_unit_plan_gate_revision_match_still_blocks(self):
        """Control: the SAME revision's reject still refuses correctly."""
        history = [{
            "gate_node": "plan_gate", "resolution": "reject",
            "option": None, "plan_revision": 1,
        }]
        entry = cli._bare_interrupt_terminal_resolution(
            history, ("plan_gate",), current_plan_revision=1)
        assert entry is not None
        assert entry["resolution"] == "reject"

    def test_cli_bare_interrupt_continues_past_stale_reject_at_bumped_revision(
        self, hermetic,
    ):
        """Full CLI-level integration: a real LangGraph checkpoint parked at
        `plan_gate`, durable `hitl_history` carrying only a revision-1
        reject, but the checkpoint's OWN `plan_revision` has since moved to
        2 (a legitimate new plan cycle, never yet resolved). A plain
        `--action approve` bare-interrupt retry must be allowed to continue
        -- reverting the follow-up fix makes this wrongly refuse."""
        from hydra_core.supervisor import build_supervisor, _PurePythonRunner

        wf = uuid4()
        state = HydraState(
            workflow_id=wf, root_goal="round 6 follow-up defect 2 repro",
            phase="approval", plan_status="judged", plan_ref=_plan_ref(),
            plan_revision=2, pending_hitl=None, tasks=[],
            hitl_history=[{
                "gate_node": "plan_gate", "resolution": "reject",
                "option": None, "plan_revision": 1,
            }],
        )
        sup = build_supervisor(project_root=HYDRA_ROOT, dispatcher=_StubDispatcher())
        assert not isinstance(sup, _PurePythonRunner), "langgraph required for this test"
        config = {"configurable": {"thread_id": str(wf)}}
        sup.update_state(config, state.model_dump(mode="json"), as_node="plan_judge")
        snap = sup.get_state(config)
        assert tuple(snap.next) == ("plan_gate",), (
            f"fixture must park at plan_gate; got next={snap.next!r}"
        )

        rc, body = _resume(HYDRA_ROOT, str(wf), "approve", None)
        assert rc == 0, body
        assert body.get("continued_bare_interrupt") is True, (
            f"a fresh, unresolved plan_gate occurrence at a bumped revision "
            f"must not be blocked by an older revision's stale reject: {body}"
        )


# =========================================================================== #
# Item 3 REDESIGN (round 6, this drop): every abort/reject at ANY gate
# durably records `HydraState.terminal_resolution` in the SAME
# `as_node="postcheck"` write, and the bare-interrupt branch consults that
# field directly -- instead of scanning `hitl_history` -- so a LATER
# non-resolution note appended to `hitl_history` (e.g. `plan_gate_bypassed`)
# can never mask an earlier terminal reject/abort. `_bare_interrupt_terminal_
# resolution` (the old hitl_history scan) is now a fallback used ONLY when
# `terminal_resolution` reads back `None` (a legacy checkpoint, or a
# checkpoint that has never had a terminal resolution recorded on it).
# =========================================================================== #

def _seed_bare_interrupt_workflow():
    """Seed a REAL LangGraph checkpoint parked at the `approval` interrupt
    with NO `pending_hitl` at all -- a bare interrupt, mirroring the MU7
    shape but for the terminal-abort/reject-at-a-bare-interrupt paths this
    test class covers."""
    from hydra_core.supervisor import build_supervisor, _PurePythonRunner

    wf = uuid4()
    state = HydraState(
        workflow_id=wf, root_goal="round 6 item 3 bare-interrupt repro",
        phase="approval", pending_hitl=None, tasks=[], requires_human_approval=True,
    )
    sup = build_supervisor(project_root=HYDRA_ROOT, dispatcher=_StubDispatcher())
    assert not isinstance(sup, _PurePythonRunner), "langgraph required for this test"
    config = {"configurable": {"thread_id": str(wf)}}
    sup.update_state(config, state.model_dump(mode="json"), as_node="planner")
    snap = sup.get_state(config)
    assert tuple(snap.next) == ("approval",), (
        f"fixture must park at a bare interrupt before 'approval'; got next={snap.next!r}"
    )
    return str(wf), sup, config


class TestItem3TerminalResolutionField:
    def test_bare_interrupt_reject_writes_single_terminal_resolution(self, hermetic):
        """Required test (3): a bare-interrupt reject must fold its park into
        ONE `as_node="postcheck"` write that leaves `next == ()` and records
        `terminal_resolution` -- not the old generic, node-context-less
        `{"phase": "surfaced"}` write."""
        wf, sup, config = _seed_bare_interrupt_workflow()

        rc, body = _resume(HYDRA_ROOT, wf, "reject", None)
        assert rc == 0, body
        snap = sup.get_state(config)
        assert tuple(snap.next) == (), (
            "a bare-interrupt reject must force next==() via as_node=postcheck"
        )
        term = snap.values.get("terminal_resolution")
        assert term is not None, (
            "a bare-interrupt reject must durably record terminal_resolution, "
            "not just report 'surfaced' for this one response"
        )
        assert term.get("action") == "reject"
        assert term.get("gate_node") is None
        assert term.get("hitl_request_id") is None
        assert term.get("resolved_at")

        # A later, DIFFERENT-action retry must still never continue the graph.
        rc2, body2 = _resume(HYDRA_ROOT, wf, "approve", None)
        assert rc2 == 0, body2
        assert body2.get("resumed") is False
        assert not body2.get("continued_bare_interrupt"), (
            f"a retry after a durably recorded bare-interrupt reject must "
            f"never continue the graph: {body2}"
        )

    def test_bare_interrupt_abort_writes_single_terminal_resolution(self, hermetic):
        """First-time bare-interrupt abort (no prior durable terminal record
        for this parked gate) must ALSO write terminal_resolution, not just
        report 'already terminal' without ever persisting it -- reverting
        this fix leaves a later plain '--action approve' retry free to
        continue the graph."""
        wf, sup, config = _seed_bare_interrupt_workflow()

        rc, body = _resume(HYDRA_ROOT, wf, "approve", "abort")
        assert rc == 0, body
        snap = sup.get_state(config)
        assert tuple(snap.next) == ()
        term = snap.values.get("terminal_resolution")
        assert term is not None
        assert term.get("action") == "approve"
        assert term.get("option") == "abort"
        assert term.get("resolved_at")

        rc2, body2 = _resume(HYDRA_ROOT, wf, "approve", None)
        assert rc2 == 0, body2
        assert not body2.get("continued_bare_interrupt"), (
            f"a plain approve retry after a first-time bare-interrupt abort "
            f"must never continue the graph: {body2}"
        )

    def test_appended_non_resolution_note_cannot_mask_terminal_resolution(self, hermetic):
        """Required test (2): a terminal reject followed by an APPENDED
        non-resolution note in `hitl_history` (mirrors `plan_gate_bypassed`/
        governance notes -- no "resolution" key at all) must not un-terminate
        the workflow. The durable `terminal_resolution` field, not the
        latest `hitl_history` entry, is authoritative: reverting to a pure
        hitl_history scan (this test's proven property) would let the note
        mask the true reject and wrongly continue the graph."""
        from hydra_core.supervisor import build_supervisor, _PurePythonRunner

        wf = uuid4()
        state = HydraState(
            workflow_id=wf, root_goal="round 6 item 3 masking-note repro",
            phase="approval", pending_hitl=None, tasks=[], requires_human_approval=True,
            terminal_resolution={
                "gate_node": "approval", "hitl_request_id": None,
                "action": "reject", "option": None, "plan_revision": None,
                "resolved_at": "2026-09-23T00:00:00+00:00",
            },
            hitl_history=[
                {"gate_node": "approval", "resolution": "reject", "option": None},
                # A LATER, non-resolution note -- no "resolution" key, and
                # (as the real `plan_gate_bypassed` marker does) no
                # "gate_node" match either. A pure hitl_history scan that
                # only ever inspects the LATEST entry would see THIS note,
                # not the reject above it, and wrongly conclude there is no
                # terminal resolution for the parked gate.
                {"event": "some_later_note", "note": "unrelated bookkeeping"},
            ],
        )
        sup = build_supervisor(project_root=HYDRA_ROOT, dispatcher=_StubDispatcher())
        assert not isinstance(sup, _PurePythonRunner)
        config = {"configurable": {"thread_id": str(wf)}}
        sup.update_state(config, state.model_dump(mode="json"), as_node="planner")
        assert tuple(sup.get_state(config).next) == ("approval",)

        rc, body = _resume(HYDRA_ROOT, str(wf), "approve", None)
        assert rc == 0, body
        values = sup.get_state(config).values
        assert not values.get("tasks"), (
            f"a durably recorded terminal_resolution must refuse a plain "
            f"approve retry even when a later, non-resolution hitl_history "
            f"note would mask it under a pure history scan: {body}"
        )
        assert body.get("resumed") is False
        assert body.get("graph_reentered") is False

    def test_two_gates_terminal_resolution_identifies_later_gate(self, hermetic):
        """Required test (6): an earlier gate resolved normally (a genuine
        approve, already in `hitl_history`), and a LATER, DIFFERENT gate
        currently pending is then aborted -- `terminal_resolution` must
        identify the LATER gate's own `hitl_request_id`/`gate_node`, never
        the earlier approved gate's."""
        from hydra_core.supervisor import build_supervisor, _PurePythonRunner

        wf = uuid4()
        pending_b = {
            "id": "gate-b-id", "workflow_id": str(wf), "reason": "reflexion_override",
            "gate_node": "judge_per_squad", "options": ["approve", "abort"],
        }
        state = HydraState(
            workflow_id=wf, root_goal="round 6 item 3 two-gate repro",
            phase="approval", pending_hitl=pending_b, tasks=[],
            hitl_history=[{
                "id": "gate-a-id", "gate_node": "approval",
                "resolution": "approve", "option": None,
            }],
        )
        sup = build_supervisor(project_root=HYDRA_ROOT, dispatcher=_StubDispatcher())
        assert not isinstance(sup, _PurePythonRunner)
        config = {"configurable": {"thread_id": str(wf)}}
        sup.update_state(config, state.model_dump(mode="json"), as_node="planner")

        rc, body = _resume(HYDRA_ROOT, str(wf), "approve", "abort")
        assert rc == 0, body
        values = sup.get_state(config).values
        term = values.get("terminal_resolution")
        assert term is not None
        assert term.get("gate_node") == "judge_per_squad", (
            f"terminal_resolution must identify the LATER (currently "
            f"pending) gate, not the earlier approved one: {term}"
        )
        assert term.get("hitl_request_id") == "gate-b-id", (
            f"terminal_resolution's hitl_request_id must be the LATER gate's "
            f"own id, never the earlier approved gate's 'gate-a-id': {term}"
        )
        assert tuple(sup.get_state(config).next) == ()

    def test_legacy_fallback_skips_trailing_non_resolution_note_to_find_reject(
        self, hermetic,
    ):
        """Legacy-path required test: `terminal_resolution` is ABSENT (a
        checkpoint that predates this field / never had it written), and
        `hitl_history` carries a genuine terminal reject for the parked
        `plan_gate` occurrence followed by a LATER non-resolution note
        (mirrors the real `plan_gate_bypassed` force-dispatch marker -- no
        "resolution" key at all). `_bare_interrupt_terminal_resolution`'s
        legacy scan must skip that note and find the true reject underneath
        it, not be masked by it -- reverting the skip-non-resolution-entries
        fix makes this wrongly continue the graph."""
        from hydra_core.supervisor import build_supervisor, _PurePythonRunner

        wf = uuid4()
        state = HydraState(
            workflow_id=wf, root_goal="round 6 item 3 legacy masking-note repro",
            phase="approval", plan_status="judged", plan_ref=_plan_ref(),
            plan_revision=1, pending_hitl=None, tasks=[],
            hitl_history=[
                {"gate_node": "plan_gate", "resolution": "reject",
                 "option": None, "plan_revision": 1},
                # A LATER, non-resolution note -- no "resolution" key at
                # all, exactly the shape `_bypass_note` (cli.py's
                # force-dispatch handler) writes.
                {"event": "plan_gate_bypassed", "workflow_id": str(wf),
                 "note": "dispatch proceeded without plan approval (force-dispatch)",
                 "resolved_at": "2026-09-23T00:00:00+00:00"},
            ],
        )
        sup = build_supervisor(project_root=HYDRA_ROOT, dispatcher=_StubDispatcher())
        assert not isinstance(sup, _PurePythonRunner), "langgraph required for this test"
        config = {"configurable": {"thread_id": str(wf)}}
        sup.update_state(config, state.model_dump(mode="json"), as_node="plan_judge")
        assert tuple(sup.get_state(config).next) == ("plan_gate",)
        assert sup.get_state(config).values.get("terminal_resolution") is None, (
            "this test must exercise the LEGACY fallback, not the new field"
        )

        rc, body = _resume(HYDRA_ROOT, str(wf), "approve", None)
        assert rc == 0, body
        values = sup.get_state(config).values
        assert not values.get("tasks"), (
            f"the legacy hitl_history fallback must skip a trailing non-"
            f"resolution note and still find the true reject underneath "
            f"it, refusing to continue: {body}"
        )
        assert body.get("resumed") is False
        assert body.get("graph_reentered") is False
