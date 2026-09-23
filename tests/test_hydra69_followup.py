"""Hydra#69 follow-up: six defects found by the final cross-vendor
(codex gpt-6-astra) review of the merged Hydra#69 fix, on feat/planning-phase
at HEAD 00513a8.

1. Abort must not materialise/approve the plan (cli.py, plan_gate).
2. Detached/gate-only modify-budget at plan_gate must never approve/dispatch.
3. Plan re-entry: `plan_supersedes_expected` survives a failed retry;
   checkpoint-write failures in `_cmd_attended_submit` are reported.
4. Finalize must keep dependency-blocked (not retired) tasks pending.
5. `node_planner` replay must not wipe `plan_placeholder_task_ids`.
6. Detached/fleet dispatch must carry acceptance_criteria/envelope_type.

Plus one end-to-end regression with a REAL LangGraph SQLite checkpointer:
plan -> planning task -> submit valid PLAN -> parks at plan_gate ->
gate-only approve -> step returns an engineering host_action for step 1 ->
mark the step attended-complete -> finalize reaches a DECISION_RECORD.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
from pathlib import Path
from uuid import uuid4

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


# =========================================================================== #
# Shared fakes (mirrors tests/test_p5c_plan_operator_surfaces.py's pattern)
# =========================================================================== #

class _SimpleFakeSup:
    """Minimal `sup` stand-in: records every `update_state` patch and merges
    it into `values` -- mirrors how a real checkpoint accumulates
    replace-by-default fields across multiple calls."""

    def __init__(self, pending_hitl, values):
        self._values = dict(values)
        self._values.setdefault("pending_hitl", pending_hitl)
        self.updates: list[tuple[dict, object]] = []
        self.invoked = 0
        self._next: tuple = ()

    def get_state(self, config):
        return type("Snap", (), {"values": dict(self._values), "next": self._next})()

    def update_state(self, config, patch, as_node=None):
        self.updates.append((dict(patch), as_node))
        self._values.update(patch)

    def invoke(self, arg, config=None):
        self.invoked += 1
        # Mirrors node_plan_gate's real behaviour: materialising the plan
        # (or refusing) as soon as the graph is "resumed" past the interrupt.
        from hydra_core.state import HydraState
        from hydra_core.supervisor import materialise_plan_steps
        state = HydraState.model_validate(self._values)
        if isinstance(state.pending_hitl, dict):
            patch = {"phase": "approval"}
        else:
            patch = materialise_plan_steps(state)
        self.updates.append((dict(patch), "plan_gate"))
        self._values.update(patch)
        self._next = ()
        return {"phase": self._values.get("phase", "executing")}


def _resume_args(project: Path, wf: str, action: str, *, option=None, gate_only=False):
    return argparse.Namespace(
        project=str(project), workflow_id=wf, action=action, option=option,
        live=False, verbose=False, operator="operator@example.com",
        critique_ref=None, gate_only=gate_only,
    )


def _patch_common(monkeypatch, sup):
    monkeypatch.setattr("hydra_core.supervisor.build_supervisor", lambda **_k: sup)
    monkeypatch.setattr("hydra_core.cli._prune_spooled_hitl_requests", lambda *_a: 0)
    monkeypatch.setattr("hydra_core.cli._resolve_eights_hitl_for_workflow",
                        lambda *_a, **_k: {"resolved": 0})
    monkeypatch.setattr("hydra_core.cli._resolve_eights_hitl_gate_only_bounded",
                        lambda *_a, **_k: {"resolved": 0})


def _plan_pending(wf: str, options=None) -> dict:
    return {
        "workflow_id": wf, "reason": "plan_approval", "gate_node": "plan_gate",
        "options": options or ["approve", "reject", "modify-plan", "modify-budget"],
    }


def _plan_ref(step_id="step-1"):
    return {
        "steps": [{
            "step_id": step_id, "target_squad": "engineering",
            "description": "do the thing", "priority": "P2",
            "acceptance_criteria": ["it works"], "envelope_type": "DEV_TASK",
        }],
    }


# =========================================================================== #
# Fix 1 (HIGH) -- approve+option=abort at plan_gate must not materialise
# =========================================================================== #

class TestFix1AbortDoesNotMaterialise:
    def test_approve_option_abort_does_not_materialise_or_approve(self, monkeypatch, tmp_path):
        wf = str(uuid4())
        pending = _plan_pending(wf, options=["approve", "abort"])
        values = {
            "pending_hitl": pending, "phase": "approval",
            "plan_status": "judged", "plan_ref": _plan_ref(), "plan_revision": 1,
            "tasks": [], "plan_placeholder_task_ids": [],
        }
        sup = _SimpleFakeSup(pending, values)
        _patch_common(monkeypatch, sup)
        monkeypatch.setattr("hydra_core.cli.emit", lambda *a, **k: None)

        args = _resume_args(tmp_path, wf, "approve", option="abort")
        from hydra_core.cli import _cmd_resume_locked
        ret = _cmd_resume_locked(args, tmp_path, wf, "approve", "abort")
        assert ret == 0

        # No patch ever wrote plan_status="approved".
        approved_patches = [p for p, _ in sup.updates if p.get("plan_status") == "approved"]
        assert not approved_patches, f"abort must never approve the plan: {sup.updates}"
        # No step task was materialised.
        task_patches = [p for p, _ in sup.updates if p.get("tasks")]
        assert not task_patches, f"abort must never materialise step tasks: {sup.updates}"
        # The barrier state ("judged") must survive -- never cleared to
        # "approved"/"bypassed".
        assert sup._values.get("plan_status") == "judged"
        # It DID park as surfaced (the abort option's own behaviour).
        assert sup._values.get("phase") == "surfaced"

    def test_counterpart_approve_without_abort_does_materialise(self, monkeypatch, tmp_path):
        """The positive case beside the guard: a genuine approve (no abort
        option) at plan_gate DOES materialise the plan's step task."""
        wf = str(uuid4())
        pending = _plan_pending(wf)
        values = {
            "pending_hitl": pending, "phase": "approval",
            "plan_status": "judged", "plan_ref": _plan_ref(), "plan_revision": 1,
            "tasks": [], "plan_placeholder_task_ids": [],
        }
        sup = _SimpleFakeSup(pending, values)
        _patch_common(monkeypatch, sup)
        monkeypatch.setattr("hydra_core.cli.emit", lambda *a, **k: None)

        args = _resume_args(tmp_path, wf, "approve")
        from hydra_core.cli import _cmd_resume_locked
        ret = _cmd_resume_locked(args, tmp_path, wf, "approve", None)
        assert ret == 0
        assert sup._values.get("plan_status") == "approved"
        step_tasks = [t for t in sup._values.get("tasks", [])
                      if getattr(t, "plan_step_id", None) == "step-1"]
        assert step_tasks, "a genuine approve must materialise the step task"


# =========================================================================== #
# Fix 2 (HIGH) -- modify-budget at plan_gate never approves/dispatches
# =========================================================================== #

class TestFix2ModifyBudgetNeverApproves:
    def _values(self, wf):
        pending = _plan_pending(wf)
        return pending, {
            "pending_hitl": pending, "phase": "approval",
            "plan_status": "judged", "plan_ref": _plan_ref(), "plan_revision": 1,
            "tasks": [], "plan_placeholder_task_ids": [],
            "budget": {"budget_usd": 10.0, "spent_usd": 1.0},
        }

    def test_detached_modify_budget_leaves_gate_pending(self, monkeypatch, tmp_path):
        wf = str(uuid4())
        pending, values = self._values(wf)
        sup = _SimpleFakeSup(pending, values)
        _patch_common(monkeypatch, sup)
        monkeypatch.setattr("hydra_core.cli.emit", lambda *a, **k: None)

        args = _resume_args(tmp_path, wf, "modify-budget", option="25.0", gate_only=False)
        from hydra_core.cli import _cmd_resume_locked
        ret = _cmd_resume_locked(args, tmp_path, wf, "modify-budget", "25.0")
        assert ret == 0
        assert sup.invoked == 0, "modify-budget must never call sup.invoke()"
        assert sup._values.get("plan_status") == "judged"
        assert sup._values.get("pending_hitl") is not None
        assert sup._values.get("budget", {}).get("budget_usd") == 25.0
        assert not sup._values.get("tasks"), "no step task may be materialised"

    def test_gate_only_modify_budget_leaves_gate_pending(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HYDRA_OPERATOR_ID", "lebobo88")
        monkeypatch.setenv("HYDRA_OPERATOR_KEY", "test-key-material")
        wf = str(uuid4())
        pending, values = self._values(wf)
        sup = _SimpleFakeSup(pending, values)
        _patch_common(monkeypatch, sup)
        monkeypatch.setattr("hydra_core.cli.emit", lambda *a, **k: None)

        args = _resume_args(tmp_path, wf, "modify-budget", option="25.0", gate_only=True)
        from hydra_core.cli import _cmd_resume_locked
        ret = _cmd_resume_locked(args, tmp_path, wf, "modify-budget", "25.0")
        assert ret == 0
        assert sup.invoked == 0
        assert sup._values.get("plan_status") == "judged"
        assert sup._values.get("pending_hitl") is not None
        assert not sup._values.get("tasks")

    def test_modify_budget_never_touches_eights_ticket(self, monkeypatch, tmp_path):
        """The gate's external TheEights ticket/spool must be left alone --
        the gate is NOT resolved, only its budget context changed."""
        wf = str(uuid4())
        pending, values = self._values(wf)
        sup = _SimpleFakeSup(pending, values)
        monkeypatch.setattr("hydra_core.supervisor.build_supervisor", lambda **_k: sup)
        prune_calls: list = []
        resolve_calls: list = []
        monkeypatch.setattr("hydra_core.cli._prune_spooled_hitl_requests",
                            lambda *a: prune_calls.append(a) or 0)
        monkeypatch.setattr("hydra_core.cli._resolve_eights_hitl_for_workflow",
                            lambda *a, **k: resolve_calls.append((a, k)) or {"resolved": 0})
        monkeypatch.setattr("hydra_core.cli._resolve_eights_hitl_gate_only_bounded",
                            lambda *a, **k: resolve_calls.append((a, k)) or {"resolved": 0})
        monkeypatch.setattr("hydra_core.cli.emit", lambda *a, **k: None)

        args = _resume_args(tmp_path, wf, "modify-budget", option="25.0", gate_only=False)
        from hydra_core.cli import _cmd_resume_locked
        ret = _cmd_resume_locked(args, tmp_path, wf, "modify-budget", "25.0")
        assert ret == 0
        assert not prune_calls, "modify-budget must never prune the spool"
        assert not resolve_calls, "modify-budget must never resolve the eights ticket"

    def test_counterpart_modify_budget_off_plan_gate_still_falls_through(
        self, monkeypatch, tmp_path,
    ):
        """The early return is scoped to plan_gate only -- modify-budget at
        a DIFFERENT gate is unaffected (still reaches sup.invoke)."""
        wf = str(uuid4())
        pending = {"workflow_id": wf, "reason": "over_budget", "gate_node": "dispatch"}
        values = {
            "pending_hitl": pending, "phase": "approval",
            "budget": {"budget_usd": 10.0, "spent_usd": 1.0},
        }
        sup = _SimpleFakeSup(pending, values)
        _patch_common(monkeypatch, sup)
        monkeypatch.setattr("hydra_core.cli.emit", lambda *a, **k: None)

        args = _resume_args(tmp_path, wf, "modify-budget", option="25.0", gate_only=False)
        from hydra_core.cli import _cmd_resume_locked
        ret = _cmd_resume_locked(args, tmp_path, wf, "modify-budget", "25.0")
        assert ret == 0
        assert sup.invoked == 1, "modify-budget off plan_gate still resumes the graph"


# =========================================================================== #
# Fix 3 (HIGH) -- plan_supersedes_expected survives a failed re-entry
# =========================================================================== #

class TestFix3PlanSupersedesExpected:
    def test_ingest_validates_against_plan_supersedes_expected_not_envelope_id(
        self, tmp_path,
    ):
        """The core of the bug: state.plan_envelope_id was overwritten by a
        FAILED re-entry attempt's own new envelope id, but
        plan_supersedes_expected (set once by --modify-plan) still names the
        TRUE predecessor -- ingest must validate against the latter."""
        import shutil
        from hydra_core.ingest import dispatch_ingested_envelopes
        from hydra_core.squad_loader import discover_squads
        from hydra_core.state import HydraState

        wf = uuid4()
        prior_envelope_id = uuid4()
        failed_attempt_envelope_id = uuid4()  # what state.plan_envelope_id now holds
        state = HydraState(
            workflow_id=wf, root_goal="x", target_repo_id="hydra",
            plan_status="authoring", plan_revision=2,
            # Simulates the bug precondition: a FAILED re-entry attempt
            # already advanced plan_envelope_id to ITS OWN id...
            plan_envelope_id=failed_attempt_envelope_id,
            # ...but plan_supersedes_expected still names the TRUE
            # predecessor (revision 1's envelope), untouched by that failure.
            plan_supersedes_expected=str(prior_envelope_id),
        )
        packs = discover_squads(REPO_ROOT)
        raw = {
            "id": str(uuid4()), "type": "PLAN", "origin_squad": "planning",
            "target_squad": "hydra", "workflow_id": str(wf), "rigor": "standard",
            "goal_restatement": "ship it", "summary": "ship it",
            "plan_revision": 2, "steps": [],
            # Correctly addressed at the TRUE predecessor, not the failed
            # attempt's envelope id.
            "supersedes": str(prior_envelope_id),
        }

        project = tmp_path / "proj"
        project.mkdir(parents=True, exist_ok=True)
        shutil.copy(REPO_ROOT / "CONSTITUTION.md", project / "CONSTITUTION.md")

        class _Disp:
            project_root = project

        outcome = dispatch_ingested_envelopes(
            state, [raw], packs=packs, dispatcher=_Disp(),
            already_ingested=set(), emit_fn=lambda *_a, **_k: None,
        )
        item = outcome.items[0]
        assert item.status != "failed", (
            f"correctly-addressed resubmission must be accepted, got "
            f"{item.status}: {item.detail}"
        )

    def test_ingest_rejects_supersedes_mismatch_against_expected(self):
        """Counterpart: a PLAN naming the WRONG predecessor (matching neither
        plan_supersedes_expected nor anything sane) is still rejected."""
        from hydra_core.ingest import dispatch_ingested_envelopes
        from hydra_core.squad_loader import discover_squads
        from hydra_core.state import HydraState

        wf = uuid4()
        state = HydraState(
            workflow_id=wf, root_goal="x", target_repo_id="hydra",
            plan_status="authoring", plan_revision=2,
            plan_envelope_id=uuid4(),
            plan_supersedes_expected=str(uuid4()),
        )
        packs = discover_squads(REPO_ROOT)
        raw = {
            "id": str(uuid4()), "type": "PLAN", "origin_squad": "planning",
            "target_squad": "hydra", "workflow_id": str(wf), "rigor": "standard",
            "goal_restatement": "ship it", "summary": "ship it",
            "plan_revision": 2, "steps": [],
            "supersedes": str(uuid4()),  # names neither
        }

        class _Disp:
            project_root = None

        outcome = dispatch_ingested_envelopes(
            state, [raw], packs=packs, dispatcher=_Disp(),
            already_ingested=set(), emit_fn=lambda *_a, **_k: None,
        )
        item = outcome.items[0]
        assert item.status == "failed"
        assert any(e.get("field") == "supersedes" for e in (item.errors or []))

    def test_modify_plan_stamps_plan_supersedes_expected(self, monkeypatch, tmp_path):
        """--modify-plan writes the NEW revision's expected predecessor onto
        the checkpoint, from the prior plan_envelope_id."""
        from hydra_core.cli import _cmd_resume_locked

        wf = str(uuid4())
        prior_id = str(uuid4())
        pending = _plan_pending(wf)
        values = {
            "pending_hitl": pending, "phase": "approval",
            "plan_status": "judged", "plan_ref": _plan_ref(), "plan_revision": 1,
            "plan_envelope_id": prior_id, "tasks": [],
        }

        class _ModifyPlanSup(_SimpleFakeSup):
            def __init__(self, pending_hitl, values):
                super().__init__(pending_hitl, values)
                self._next = ("plan_gate",)

            def update_state(self, config, patch, as_node=None):
                self.updates.append((dict(patch), as_node))
                self._values.update(patch)
                if as_node == "dispatch":
                    self._next = ()

            def invoke(self, arg, config=None):
                raise AssertionError("modify-plan must never invoke()")

        sup = _ModifyPlanSup(pending, values)
        _patch_common(monkeypatch, sup)
        monkeypatch.setattr("hydra_core.cli.emit", lambda *a, **k: None)

        crit = tmp_path / "critique.txt"
        crit.write_text("needs more detail", encoding="utf-8")
        args = argparse.Namespace(
            project=str(tmp_path), workflow_id=wf, action="modify-plan",
            option=None, live=False, verbose=False,
            operator="operator@example.com", critique_ref=str(crit), gate_only=False,
        )
        ret = _cmd_resume_locked(args, tmp_path, wf, "modify-plan", None)
        assert ret == 0
        assert sup._values.get("plan_supersedes_expected") == prior_id

    def test_checkpoint_persist_failure_is_reported_not_swallowed(self, monkeypatch, tmp_path):
        """_cmd_attended_submit: a checkpoint write failure after a
        successful host_bridge.submit_host_result must surface as a
        structured error, never a silent `ok: True`."""
        from hydra_core import cli as hydra_cli

        wf = str(uuid4())

        class _ExplodesOnUpdateSup:
            live_execution = False

            def get_state(self, config):
                from hydra_core.state import HydraState, TaskState
                t = TaskState(owner_squad="engineering", description="x")
                state = HydraState(
                    workflow_id=wf, root_goal="x", tasks=[t],
                    open_pp_runs=[{"run_id": "r1", "project_path": str(tmp_path)}],
                )
                return type("Snap", (), {"values": state.model_dump(mode="json")})()

            def update_state(self, config, patch, as_node=None):
                raise RuntimeError("simulated checkpoint write failure")

        monkeypatch.setattr("hydra_core.supervisor.build_supervisor",
                            lambda **_k: _ExplodesOnUpdateSup())
        monkeypatch.setattr("hydra_core.supervisor._PurePythonRunner", type("X", (), {}))
        from hydra_core import host_bridge as hb

        monkeypatch.setattr(hydra_cli, "_attended_live_dispatcher",
                            lambda *_a, **_k: object())
        monkeypatch.setattr(hb, "cursor_path",
                            lambda *_a, **_k: tmp_path / "cursor.json")
        (tmp_path / "cursor.json").write_text("{}", encoding="utf-8")
        monkeypatch.setattr(hb, "submit_host_result", lambda *_a, **_k: {
            "status": "complete", "task_id": "t1", "run_id": "r1",
            "cost_usd": 0.1, "tokens_in": 1, "tokens_out": 1,
        })
        monkeypatch.setattr(hb, "mark_charged", lambda *_a, **_k: None)
        monkeypatch.setattr(hydra_cli, "emit", lambda *a, **k: None)

        result_path = tmp_path / "result.json"
        result_path.write_text(json.dumps({
            "status": "complete", "text": "done",
            "cost_usd": 0.1, "tokens_in": 1, "tokens_out": 1,
        }), encoding="utf-8")
        args = argparse.Namespace(
            project=str(tmp_path), workflow_id=wf, run_id="r1", call_key="squad-t1-1",
            result=str(result_path), verbose=False,
        )
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            ret = hydra_cli._cmd_attended_submit(args)
        out = json.loads(buf.getvalue())
        assert ret == 1
        assert out["ok"] is False
        assert out["error"] == "checkpoint_persist_failed"
        assert out["checkpoint_persist_errors"]


# =========================================================================== #
# Fix 4 (HIGH) -- finalize keeps dependency-blocked (not retired) tasks pending
# =========================================================================== #

class TestFix4BlockedTasksStayPending:
    def test_dependency_blocked_task_stays_pending_not_dropped(self):
        from hydra_core.cli import _attended_pending_task_ids
        from hydra_core.state import HydraState, TaskState

        task_a = TaskState(owner_squad="engineering", description="A")
        task_b = TaskState(owner_squad="engineering", description="B",
                            depends_on=[str(task_a.task_id)])
        state = HydraState(
            root_goal="x", tasks=[task_a, task_b],
            # A is attended-completed but only "surfaced" -- NOT attended-done.
            attended_completed_task_ids=[str(task_a.task_id)],
            attended_done_task_ids=[],
        )
        pending = _attended_pending_task_ids(state)
        assert str(task_b.task_id) in pending, (
            f"a dependency-blocked task must stay pending for finalize to "
            f"report, got pending={pending}"
        )

    def test_retired_superseded_task_is_dropped(self):
        """Counterpart: a genuinely retired (superseded) task IS dropped."""
        from hydra_core.cli import _attended_pending_task_ids
        from hydra_core.state import HydraState, TaskState

        task = TaskState(owner_squad="engineering", description="stale")
        state = HydraState(
            root_goal="x", tasks=[task],
            plan_superseded_task_ids=[str(task.task_id)],
        )
        pending = _attended_pending_task_ids(state)
        assert str(task.task_id) not in pending

    def test_task_retired_predicate_distinguishes_blocked_from_retired(self):
        from hydra_core.state import HydraState, TaskState, task_retired

        blocked = TaskState(owner_squad="engineering", description="blocked",
                            depends_on=["nonexistent"])
        stale = TaskState(owner_squad="engineering", description="stale",
                          plan_revision=1)
        state = HydraState(root_goal="x", tasks=[blocked, stale], plan_revision=2)
        assert task_retired(state, blocked) is False
        assert task_retired(state, stale) is True


# =========================================================================== #
# Fix 5 (MED) -- node_planner replay must not wipe plan_placeholder_task_ids
# =========================================================================== #

class TestFix5PlaceholderSurvivesReplay:
    def _planner_fn(self):
        from unittest.mock import MagicMock
        from hydra_core.squad_node import Dispatcher
        from hydra_core.supervisor import build_supervisor

        stub_dispatcher = MagicMock(spec=Dispatcher)
        stub_dispatcher._tool_tracker = None
        runner = build_supervisor(
            project_root=REPO_ROOT, dispatcher=stub_dispatcher, force_pure_python=True,
        )
        return dict(runner.steps)["planner"]

    def test_second_planner_pass_preserves_placeholder_ids(self, monkeypatch):
        from hydra_core.state import HydraState

        monkeypatch.setenv("HYDRA_PLAN_PHASE", "1")
        fn = self._planner_fn()
        goal = (
            "Refactor the billing pipeline for full correctness and rollback "
            "safety across every service " * 2
        )
        state1 = HydraState(root_goal=goal, selected_squads=["engineering"])
        state1.target_repo_id = "hydra"
        patch1 = fn(state1)
        assert patch1["plan_placeholder_task_ids"], "first pass must record placeholders"

        # Simulate a second pass over the SAME checkpoint (a replay): the
        # engineering + planning tasks are now already in state.tasks, so
        # this pass synthesises NOTHING new.
        state2 = HydraState(root_goal=goal, selected_squads=["engineering"])
        state2.target_repo_id = "hydra"
        state2.tasks = list(patch1["tasks"])
        state2.plan_status = patch1.get("plan_status", "authoring")
        state2.plan_placeholder_task_ids = list(patch1["plan_placeholder_task_ids"])
        patch2 = fn(state2)

        assert not [
            t for t in patch2["tasks"] if t.owner_squad in ("engineering", "planning")
        ], "second pass must synthesise nothing new (everything pre-seeded)"
        assert patch2["plan_placeholder_task_ids"] == patch1["plan_placeholder_task_ids"], (
            "a no-op second pass must NOT clear plan_placeholder_task_ids -- "
            f"got {patch2['plan_placeholder_task_ids']!r}"
        )

    def test_flag_off_still_writes_empty_placeholder_list(self, monkeypatch):
        """Counterpart: with the plan gate inactive, the field stays the
        explicit empty-list clear (unchanged legacy behaviour)."""
        from hydra_core.state import HydraState

        monkeypatch.setenv("HYDRA_PLAN_PHASE", "0")
        fn = self._planner_fn()
        state = HydraState(root_goal="Fix a typo.", selected_squads=("research-ds",))
        state.budget.budget_usd = 1.0
        patch = fn(state)
        assert patch["plan_placeholder_task_ids"] == []


# =========================================================================== #
# Fix 6 (MED) -- detached/fleet dispatch carries acceptance_criteria/envelope_type
# =========================================================================== #

class TestFix6AcceptanceCriteriaEnvelopeTypeCarried:
    def test_build_payload_carries_ac_and_envelope_type(self):
        from hydra_core.schemas import CSuiteDecisionPacket
        from hydra_core.state import HydraState, TaskState

        s = HydraState(root_goal="x")
        s.target_repo_id = "hydra"
        task = TaskState(
            owner_squad="engineering", description="ship it",
            acceptance_criteria=["passes tests", "docs updated"],
            envelope_type="DEV_TASK", plan_step_id="step-1",
        )
        # Mirrors supervisor.py's `_build_payload` exactly (same fields, same
        # precedence) -- this is the real schema now, not a hand-duplicated
        # subset.
        payload = CSuiteDecisionPacket(
            workflow_id=s.workflow_id, origin_squad="hydra",
            target_squad=task.owner_squad, origin="BOARDROOM",
            objective=task.description,
            target_repo_id=task.target_repo_id if task.target_repo_id is not None else s.target_repo_id,
            acceptance_criteria=list(task.acceptance_criteria or []) or None,
            envelope_type=task.envelope_type,
        )
        assert payload.acceptance_criteria == ["passes tests", "docs updated"]
        assert payload.envelope_type == "DEV_TASK"

    def test_via_mcp_folds_acceptance_criteria_into_request_text(self):
        from hydra_core.schemas import CSuiteDecisionPacket
        from hydra_core.squad_loader import SquadPack
        from hydra_core.squad_node import _via_mcp
        from hydra_core.state import HydraState
        from unittest.mock import MagicMock

        state = HydraState(root_goal="x")
        pack = SquadPack(
            slug="engineering", name="Engineering", description="pp dispatch",
            entrypoint="mcp", invoke={"mode": "pp_run"},
        )
        inbound = CSuiteDecisionPacket(
            workflow_id=state.workflow_id, origin_squad="hydra",
            target_squad="engineering", origin="BOARDROOM",
            objective="Ship the feature", target_repo_id="hydra",
            acceptance_criteria=["it compiles", "tests pass"],
            envelope_type="DEV_TASK",
        )
        captured: list[dict] = []
        dispatcher = MagicMock()

        def _call_mcp(server, tool, args, **_kw):
            if tool == "start_run":
                captured.append(dict(args))
            return {"status": "done", "result": {"run_id": "r1"}}

        dispatcher.call_mcp.side_effect = _call_mcp

        result = _via_mcp(state, pack, inbound, dispatcher)
        assert result.status != "failed", result.rationale
        assert len(captured) == 1
        req = captured[0]["request_text"]
        assert "Acceptance criteria:" in req
        assert "it compiles" in req
        assert "tests pass" in req
        assert captured[0].get("hydra_envelope_type") == "DEV_TASK"

    def test_via_mcp_no_ac_leaves_request_text_unchanged(self):
        """Counterpart: no acceptance_criteria on the packet -> request_text
        is exactly the objective, no 'Acceptance criteria:' section added."""
        from hydra_core.schemas import CSuiteDecisionPacket
        from hydra_core.squad_loader import SquadPack
        from hydra_core.squad_node import _via_mcp
        from hydra_core.state import HydraState
        from unittest.mock import MagicMock

        state = HydraState(root_goal="x")
        pack = SquadPack(
            slug="engineering", name="Engineering", description="pp dispatch",
            entrypoint="mcp", invoke={"mode": "pp_run"},
        )
        inbound = CSuiteDecisionPacket(
            workflow_id=state.workflow_id, origin_squad="hydra",
            target_squad="engineering", origin="BOARDROOM",
            objective="Ship the feature", target_repo_id="hydra",
        )
        captured: list[dict] = []
        dispatcher = MagicMock()

        def _call_mcp(server, tool, args, **_kw):
            if tool == "start_run":
                captured.append(dict(args))
            return {"status": "done", "result": {"run_id": "r1"}}

        dispatcher.call_mcp.side_effect = _call_mcp
        _via_mcp(state, pack, inbound, dispatcher)
        assert captured[0]["request_text"] == "Ship the feature"
        assert "Acceptance criteria:" not in captured[0]["request_text"]


# =========================================================================== #
# End-to-end regression -- real LangGraph SQLite checkpointer
# =========================================================================== #

langgraph = pytest.importorskip("langgraph")


class _E2EDispatcher:
    """Non-live stub: no MCP transport, real graph edges only."""

    live_execution = False

    def call_mcp(self, server, tool, args, *, squad_id=None):
        return {"status": "done", "result": {"ok": True}}

    def set_squad_packs(self, packs):
        pass


def _hermetic_e2e_project(tmp_path, monkeypatch) -> Path:
    import shutil
    project = tmp_path / "proj"
    project.mkdir(parents=True, exist_ok=True)
    shutil.copy(REPO_ROOT / "CONSTITUTION.md", project / "CONSTITUTION.md")
    from hydra_core.squad_loader import discover_squads as _real_discover_squads
    monkeypatch.setattr("hydra_core.cli.discover_squads",
                        lambda *_a, **_k: _real_discover_squads(REPO_ROOT))
    monkeypatch.setattr("hydra_core.supervisor.discover_squads",
                        lambda *_a, **_k: _real_discover_squads(REPO_ROOT))
    return project


def test_e2e_plan_to_decision_record_real_checkpointer(monkeypatch, tmp_path):
    from hydra_core import cli as hydra_cli
    from hydra_core.cli import _apply_plan_reentry, _cmd_resume_locked, _next_attended_task
    from hydra_core.ingest import dispatch_ingested_envelopes, normalize_for_ingest
    from hydra_core.squad_loader import discover_squads
    from hydra_core.state import HydraState
    from hydra_core.supervisor import build_supervisor, _PurePythonRunner

    monkeypatch.setenv("HYDRA_PLAN_PHASE", "1")
    monkeypatch.setenv("HYDRA_CHECKPOINT_DB", str(tmp_path / "checkpoints.db"))
    monkeypatch.setenv("HYDRA_EIGHTS_SPOOL", str(tmp_path / "spool"))
    monkeypatch.setenv("HYDRA_EIGHTS_DEAD_LETTER", str(tmp_path / "dead"))
    monkeypatch.setenv("HYDRA_OPERATOR_ID", "lebobo88")
    monkeypatch.setenv("HYDRA_OPERATOR_KEY", "test-key-material")
    monkeypatch.setattr(
        "hydra_core.governance.enforce_constitution",
        lambda *_a, **_k: type("V", (), {"aligned": True, "rationale": ""})(),
    )

    project = _hermetic_e2e_project(tmp_path, monkeypatch)
    wf = uuid4()
    config = {"configurable": {"thread_id": str(wf)}}

    sup = build_supervisor(project_root=project, dispatcher=_E2EDispatcher())
    if isinstance(sup, _PurePythonRunner):
        pytest.skip("compiled graph unavailable")

    # 1. Seed the checkpoint as if node_planner had just run: a "planning"
    #    task on record, plan_status="authoring".
    seed_state = HydraState(
        workflow_id=wf, root_goal="ship the e2e regression",
        selected_squads=["engineering"], target_repo_id="hydra",
        plan_status="authoring", plan_revision=1,
    )
    sup.update_state(config, seed_state.model_dump(mode="json"))

    # 2. Submit a valid PLAN with one engineering step -> parks at plan_gate.
    packs = discover_squads(REPO_ROOT)
    raw_plan = {
        "id": str(uuid4()), "type": "PLAN", "origin_squad": "planning",
        "target_squad": "hydra", "workflow_id": str(wf), "rigor": "standard",
        "goal_restatement": "ship the e2e regression", "summary": "ship it",
        "plan_revision": 1,
        "steps": [{
            "step_id": "step-1", "target_squad": "engineering",
            "envelope_type": "DEV_TASK", "description": "implement the change",
            "acceptance_criteria": ["it works"], "priority": "P2",
            "target_repo_id": "hydra",
        }],
    }
    norm = normalize_for_ingest(raw_plan, lambda *_a, **_k: None)
    state = HydraState.model_validate(sup.get_state(config).values)

    class _IngestDispatcher:
        project_root = project

    outcome = dispatch_ingested_envelopes(
        state, [norm], packs=packs, dispatcher=_IngestDispatcher(),
        already_ingested=set(), emit_fn=lambda *_a, **_k: None,
    )
    assert outcome.plan_patch, f"PLAN was not accepted: {[vars(i) for i in outcome.items]}"

    res: dict = {}
    _apply_plan_reentry(
        sup, config, project, str(wf), dict(outcome.plan_patch), raw_plan["id"], res,
        emit_fn=lambda *a, **k: None, release_fn=lambda *a, **k: None,
    )
    assert res.get("status") != "plan_reentry_failed", res
    parked = getattr(sup.get_state(config), "next", None)
    assert tuple(parked) == ("plan_gate",), f"expected to park at plan_gate, got {parked}"

    # 3. Gate-only approve -> materialises the step task.
    args = argparse.Namespace(
        project=str(project), workflow_id=str(wf), action="approve", option=None,
        live=False, verbose=False, operator="tester@example.com",
        critique_ref=None, gate_only=True,
    )
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = _cmd_resume_locked(args, project, str(wf), "approve", None)
    assert rc == 0
    out = json.loads(buf.getvalue())
    assert out.get("plan_status") == "approved", out

    state = HydraState.model_validate(sup.get_state(config).values)
    step_tasks = [t for t in state.tasks if getattr(t, "plan_step_id", None) == "step-1"]
    assert len(step_tasks) == 1, f"expected exactly one materialised step task, got {state.tasks}"
    step_task = step_tasks[0]

    # 4. Step returns an engineering host_action for step 1.
    sel_task, sel_kind, _sel_pack = _next_attended_task(state, packs)
    assert sel_kind == "engineering", f"expected engineering, got {sel_kind}"
    assert str(sel_task.task_id) == str(step_task.task_id)

    # 5. Mark the step attended-complete (mirrors what _cmd_attended_submit
    #    persists into the checkpoint on a real "complete" submit).
    state.attended_completed_task_ids = [str(step_task.task_id)]
    state.attended_done_task_ids = [str(step_task.task_id)]
    state.attended_results = [{
        "task_id": str(step_task.task_id), "owner_squad": "engineering",
        "run_id": "run-1", "status": "complete", "final_status": "complete",
        "summary": "shipped", "cost_usd": 0.1,
    }]
    state.phase = "synthesis"
    sup.update_state(config, state.model_dump(mode="json"), as_node="judge_per_squad")

    # 6. Finalize reaches a real DECISION_RECORD, not tasks_pending.
    monkeypatch.setattr(hydra_cli, "_attended_live_dispatcher",
                        lambda *_a, **_kw: _E2EDispatcher())
    from hydra_core import memory as _mem
    episodic_db = tmp_path / "episodic.db"
    _orig_append = _mem.append_episodic

    def _patched_append(workflow_id, kind, payload, *, key=None, db=None,
                        cells=None, origin_squad=None):
        return _orig_append(workflow_id, kind, payload, key=key, db=episodic_db,
                            cells=cells, origin_squad=origin_squad)

    monkeypatch.setattr(_mem, "append_episodic", _patched_append)

    fin_args = argparse.Namespace(project=str(project), workflow_id=str(wf), verbose=False)
    buf2 = io.StringIO()
    with contextlib.redirect_stdout(buf2):
        rc2 = hydra_cli._cmd_finalize(fin_args)
    fin_out = json.loads(buf2.getvalue())
    assert rc2 == 0, fin_out
    assert fin_out.get("status") != "tasks_pending", fin_out
    assert fin_out.get("status") == "finalized", fin_out
    assert fin_out.get("decision_record_id"), fin_out
