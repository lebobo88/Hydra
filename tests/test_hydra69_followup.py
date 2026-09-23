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
# Hydra#69 follow-up defect 1 (abort atomicity) -- the remaining gap from the
# final cross-vendor review round: an abort/reject at plan_gate must park
# ATOMICALLY. Historically the resolution patch (clearing pending_hitl) and
# `phase="surfaced"` were two SEPARATE `sup.update_state` calls, with
# TheEights/spool work in between -- a crash in that window left a checkpoint
# where the gate was cleared but the workflow was never parked. Fixed by
# folding `phase="surfaced"` (and, scoped to plan_gate, `plan_status=
# "rejected"` on reject) into the SAME `patch` written by the single
# `sup.update_state` call that also clears `pending_hitl`/records the
# resolution.
# =========================================================================== #

class TestFix1AbortAtomicSingleWrite:
    def test_abort_is_exactly_one_update_state_call(self, monkeypatch, tmp_path):
        """The durable checkpoint write and the terminal park happen in ONE
        `sup.update_state` call, not two."""
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

        assert len(sup.updates) == 1, (
            f"abort must write the checkpoint exactly once, got "
            f"{len(sup.updates)}: {sup.updates}"
        )
        (patch, _as_node) = sup.updates[0]
        assert patch.get("pending_hitl") is None
        assert patch.get("phase") == "surfaced"
        assert "hitl_history" in patch and patch["hitl_history"], (
            "the single write must also carry the resolution record"
        )
        assert patch["hitl_history"][0].get("option") == "abort"
        # Never approves/materialises (Fix 1's original guarantee, preserved).
        assert patch.get("plan_status") != "approved"
        assert not patch.get("tasks")

    def test_reject_at_plan_gate_is_exactly_one_update_state_call(self, monkeypatch, tmp_path):
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

        args = _resume_args(tmp_path, wf, "reject")
        from hydra_core.cli import _cmd_resume_locked
        ret = _cmd_resume_locked(args, tmp_path, wf, "reject", None)
        assert ret == 0

        assert len(sup.updates) == 1, (
            f"reject must write the checkpoint exactly once, got "
            f"{len(sup.updates)}: {sup.updates}"
        )
        (patch, _as_node) = sup.updates[0]
        assert patch.get("pending_hitl") is None
        assert patch.get("phase") == "surfaced"
        # Scoped to plan_gate exactly as before -- the `_PLAN_BARRIER_STATES`
        # write only happens when the resolved gate IS plan_gate.
        assert patch.get("plan_status") == "rejected"
        assert "hitl_history" in patch and patch["hitl_history"]

    def test_non_plan_gate_reject_write_count_drops_state_otherwise_unchanged(
        self, monkeypatch, tmp_path,
    ):
        """A non-plan-gate reject (e.g. a budget gate) is not scoped to
        write `plan_status` -- folding `phase="surfaced"` into the single
        write is correct for every gate, but the write COUNT is the only
        thing that changes: final state and JSON output are the same as the
        pre-fix two-write behaviour."""
        wf = str(uuid4())
        pending = {
            "workflow_id": wf, "reason": "over_budget", "gate_node": "budget_gate",
            "options": ["approve", "reject"],
        }
        values = {"pending_hitl": pending, "phase": "approval"}
        sup = _SimpleFakeSup(pending, values)
        _patch_common(monkeypatch, sup)
        monkeypatch.setattr("hydra_core.cli.emit", lambda *a, **k: None)

        args = _resume_args(tmp_path, wf, "reject")
        from hydra_core.cli import _cmd_resume_locked
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            ret = _cmd_resume_locked(args, tmp_path, wf, "reject", None)
        assert ret == 0

        assert len(sup.updates) == 1
        (patch, _as_node) = sup.updates[0]
        assert patch.get("phase") == "surfaced"
        # `plan_status` is NEVER touched for a non-plan-gate reject.
        assert "plan_status" not in patch
        assert sup._values.get("phase") == "surfaced"
        assert "plan_status" not in sup._values

        out = json.loads(captured.getvalue())
        assert out["ok"] is True
        assert out["phase"] == "surfaced"
        assert out["status"] == "surfaced"
        assert out["gate_node"] == "budget_gate"
        assert out["action"] == "reject"
        assert out["pending_hitl"] is None
        assert out["graph_reentered"] is False

    def test_crash_after_write_before_eights_leaves_checkpoint_parked(
        self, monkeypatch, tmp_path,
    ):
        """Simulate the TheEights/spool step raising AFTER the durable
        checkpoint write: the already-persisted checkpoint must still show
        the terminal park (phase=surfaced, plan_status not approved, no
        step tasks) -- the crash must not un-park anything, because nothing
        AFTER the single write is required for the park to hold."""
        wf = str(uuid4())
        pending = _plan_pending(wf, options=["approve", "abort"])
        values = {
            "pending_hitl": pending, "phase": "approval",
            "plan_status": "judged", "plan_ref": _plan_ref(), "plan_revision": 1,
            "tasks": [], "plan_placeholder_task_ids": [],
        }
        sup = _SimpleFakeSup(pending, values)
        monkeypatch.setattr("hydra_core.supervisor.build_supervisor", lambda **_k: sup)
        monkeypatch.setattr("hydra_core.cli._prune_spooled_hitl_requests", lambda *_a: 0)

        class _BoomAfterWrite(RuntimeError):
            pass

        def _boom(*_a, **_k):
            raise _BoomAfterWrite("TheEights connection reset mid-resolve")

        # Both routes the eights resolution can take, since `gate_only` here
        # is False (the default `_resume_args` non-gate-only CLI path).
        monkeypatch.setattr("hydra_core.cli._resolve_eights_hitl_for_workflow", _boom)
        monkeypatch.setattr("hydra_core.cli._resolve_eights_hitl_gate_only_bounded", _boom)
        monkeypatch.setattr("hydra_core.cli.emit", lambda *a, **k: None)

        args = _resume_args(tmp_path, wf, "approve", option="abort")
        from hydra_core.cli import _cmd_resume_locked
        with pytest.raises(_BoomAfterWrite):
            _cmd_resume_locked(args, tmp_path, wf, "approve", "abort")

        # The checkpoint write already happened (exactly once) BEFORE the
        # raise -- the crash never un-persists it.
        assert len(sup.updates) == 1
        assert sup._values.get("phase") == "surfaced"
        assert sup._values.get("plan_status") == "judged"
        assert not sup._values.get("tasks")
        assert sup._values.get("pending_hitl") is None


# =========================================================================== #
# Fix 2 revision (HIGH) -- materialise_plan_steps itself refuses to run while
# a plan_gate pending_hitl is still open, absent an explicit approval signal.
# =========================================================================== #

class TestFix2MaterialiseRefusesOpenGate:
    def test_open_plan_gate_without_approval_is_a_no_op(self):
        """The core of the revision: an open plan_gate `pending_hitl`, handed
        to `materialise_plan_steps` with NO `approved_resolution` flag, must
        produce a completely empty patch -- no tasks, no plan_status flip, no
        pending_hitl mutation, no placeholder supersession."""
        from hydra_core.state import HydraState
        from hydra_core.supervisor import materialise_plan_steps

        state = HydraState(
            root_goal="x", plan_status="judged", plan_revision=1,
            plan_ref=_plan_ref(), plan_placeholder_task_ids=["ph-1"],
            pending_hitl=_plan_pending("wf-guard"),
        )
        patch = materialise_plan_steps(state)
        assert patch == {}, f"expected a pure no-op patch, got {patch}"

    def test_open_plan_gate_with_explicit_approval_does_materialise(self):
        """Counterpart: the SAME open-gate state, but with
        `approved_resolution=True` (what `cli.py`'s `--gate-only` approve
        handler passes after confirming action=='approve'), DOES
        materialise -- proving the guard is a refusal of an UNPROVEN
        approval, not of every open-gate call."""
        from hydra_core.state import HydraState
        from hydra_core.supervisor import materialise_plan_steps

        state = HydraState(
            root_goal="x", plan_status="judged", plan_revision=1,
            plan_ref=_plan_ref(), plan_placeholder_task_ids=["ph-1"],
            pending_hitl=_plan_pending("wf-guard"),
        )
        patch = materialise_plan_steps(state, approved_resolution=True)
        assert patch.get("plan_status") == "approved"
        assert patch.get("pending_hitl") is None
        step_tasks = [t for t in (patch.get("tasks") or [])
                      if getattr(t, "plan_step_id", None) == "step-1"]
        assert step_tasks, "an explicit approval must materialise the step task"
        assert patch.get("plan_superseded_task_ids") == ["ph-1"]

    def test_cleared_gate_materialises_without_the_flag(self):
        """The `node_plan_gate` graph route: by the time the graph re-enters
        `plan_gate`, `pending_hitl` is already cleared (the resume handler
        wrote `pending_hitl=None` in the SAME atomic patch that recorded the
        approve resolution, before `sup.invoke` ever ran) -- so the default
        `approved_resolution=False` must still materialise when the gate is
        already closed."""
        from hydra_core.state import HydraState
        from hydra_core.supervisor import materialise_plan_steps

        state = HydraState(
            root_goal="x", plan_status="judged", plan_revision=1,
            plan_ref=_plan_ref(), plan_placeholder_task_ids=["ph-1"],
            pending_hitl=None,
        )
        patch = materialise_plan_steps(state)
        assert patch.get("plan_status") == "approved"
        step_tasks = [t for t in (patch.get("tasks") or [])
                      if getattr(t, "plan_step_id", None) == "step-1"]
        assert step_tasks

    def test_full_resume_approve_path_still_materialises_end_to_end(
        self, monkeypatch, tmp_path,
    ):
        """Re-run (unchanged) of the existing approve-path behaviour through
        the ACTUAL `_cmd_resume_locked` CLI entry point, proving the new
        guard did not regress the legitimate `--gate-only` approve flow this
        suite already pins in `TestFix1AbortDoesNotMaterialise`."""
        monkeypatch.setenv("HYDRA_OPERATOR_ID", "lebobo88")
        monkeypatch.setenv("HYDRA_OPERATOR_KEY", "test-key-material")
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

        args = _resume_args(tmp_path, wf, "approve", gate_only=True)
        from hydra_core.cli import _cmd_resume_locked
        ret = _cmd_resume_locked(args, tmp_path, wf, "approve", None)
        assert ret == 0
        assert sup._values.get("plan_status") == "approved"
        step_tasks = [t for t in sup._values.get("tasks", [])
                      if getattr(t, "plan_step_id", None) == "step-1"]
        assert step_tasks, "gate-only approve must still materialise via the real CLI path"


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
    def test_node_dispatch_build_payload_carries_ac_and_envelope_type(self):
        """Drives the REAL `node_dispatch` closure (the actual supervisor
        code path -- `_build_payload` is a private closure inside it, never
        called directly by any other module) through the pure-Python runner,
        with a capturing fake dispatcher standing in for the pp MCP boundary
        (the lowest external boundary this path crosses). Asserts the
        captured `start_run` payload -- the thing `_via_mcp` actually sends
        downstream -- carries `acceptance_criteria` folded into
        `request_text` and `hydra_envelope_type`, proving `_build_payload`'s
        `CSuiteDecisionPacket` really did carry both fields end to end,
        not just that a hand-built packet with the same field names would."""
        from unittest.mock import MagicMock
        from hydra_core.supervisor import build_supervisor
        from hydra_core.state import HydraState, TaskState

        captured: list[dict] = []

        def _call_mcp(server, tool, args, squad_id=None, **_kw):
            if tool == "start_run":
                captured.append(dict(args))
                return {"status": "done", "result": {"run_id": "run-fix6"}}
            return {"status": "done", "result": {}}

        dispatcher = MagicMock()
        dispatcher._tool_tracker = None
        dispatcher.live_execution = False
        # This is the in-graph (detached) mcp path node_dispatch's own defer
        # guard reserves for a scripted pp dispatcher opting explicitly into
        # offline dispatch (supervisor.py's `_offline_mcp_ok` check) — a
        # non-live dispatcher without it is deferred to the host instead
        # (the attended engineering route this test does NOT exercise).
        dispatcher.allow_offline_mcp_dispatch = True
        dispatcher.call_mcp.side_effect = _call_mcp

        runner = build_supervisor(
            project_root=REPO_ROOT, dispatcher=dispatcher, force_pure_python=True,
        )
        node_dispatch = dict(runner.steps)["dispatch"]

        task = TaskState(
            owner_squad="engineering", description="ship it",
            acceptance_criteria=["passes tests", "docs updated"],
            envelope_type="DEV_TASK", plan_step_id="step-1",
        )
        state = HydraState(root_goal="x", tasks=[task])
        state.target_repo_id = "hydra"

        node_dispatch(state)

        assert captured, "node_dispatch never reached start_run via _build_payload/_via_mcp"
        req = captured[0]["request_text"]
        assert "Acceptance criteria:" in req
        assert "passes tests" in req
        assert "docs updated" in req
        assert captured[0].get("hydra_envelope_type") == "DEV_TASK"

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
    """The ONE stubbed boundary in this e2e test: the pp_harness MCP
    transport (the external pair-programmer process Hydra talks to for
    engineering stages). Everything else the test drives is a REAL Hydra
    function -- `hydra plan`'s compiled graph (`_cmd_plan`), the real
    attended cursor step/submit round-trip (`_cmd_attended_step` /
    `_cmd_attended_submit`), the real gate-only resume handler
    (`_cmd_resume_locked`), and the real finalize path (`_cmd_finalize`).
    `live_execution=False` keeps `_claude_cli_generation_enabled` and the
    fleet path off; `required_cross_vendor=False` lets a single
    same-vendor "claude" judge verdict finalize without a second, distinct
    judge producer (this test is not exercising cross-vendor judge routing,
    which `tests/test_phase4_hardening.py` already covers end to end)."""

    live_execution = False

    def __init__(self, project_root: Path | None = None):
        self.calls: list[tuple[str, str, dict]] = []
        # `dispatch_ingested_envelopes` (the real PLAN-ingest path
        # `_cmd_attended_submit` drives) writes the plan artifact via
        # `dispatcher.project_root` -- a real `MCPStdioDispatcher` always
        # carries this; a bare stub without it produces a genuine
        # "artifact_write_failed" plan rejection.
        self.project_root = project_root

    def call_mcp(self, server, tool, args, *, squad_id=None):
        self.calls.append((server, tool, dict(args)))
        if tool == "start_run":
            return {"status": "done", "result": {"run_id": "run-e2e-1"}}
        if tool == "start_stage":
            return {"status": "done", "result": {"stage_id": "stage-e2e-1"}}
        if tool == "record_attempt":
            return {"status": "done", "result": {"attempt_id": "att-e2e-1"}}
        if tool == "gate_eligible_judges":
            return {"status": "done", "result": {
                "required_cross_vendor": False,
                "rubric_id": "rfc-2119-normative"}}
        if tool == "get_stage_finalize_readiness":
            return {"status": "done", "result": {"can_pass": True}}
        if tool == "finalize_run":
            return {"status": "done", "result": {
                "effective_status": "complete", "downgraded": False}}
        # archive_artifact / record_verdict / record_smoke_status /
        # finalize_stage / ensure_agents_md — no assertion needs their
        # payload shape, just a well-formed "done" envelope.
        return {"status": "done", "result": {"ok": True}}

    def set_squad_packs(self, packs):
        pass


def _hermetic_e2e_project(tmp_path, monkeypatch) -> Path:
    import shutil
    project = tmp_path / "proj"
    project.mkdir(parents=True, exist_ok=True)
    shutil.copy(REPO_ROOT / "CONSTITUTION.md", project / "CONSTITUTION.md")
    # F27 preflight (cli.py `_cmd_attended_step`): the real engineering
    # branch refuses to open a stage without these three agent stubs on
    # disk under the target project. Copied from the real plugin assets so
    # the real preflight check runs (not bypassed).
    agents_src = REPO_ROOT / "plugins" / "hydra" / "agents"
    agents_dst = project / "plugins" / "hydra" / "agents"
    agents_dst.mkdir(parents=True, exist_ok=True)
    for name in ("engineer.md", "judge-cross-vendor.md", "judge-same-vendor.md"):
        shutil.copy(agents_src / name, agents_dst / name)
    from hydra_core.squad_loader import discover_squads as _real_discover_squads
    monkeypatch.setattr("hydra_core.cli.discover_squads",
                        lambda *_a, **_k: _real_discover_squads(REPO_ROOT))
    monkeypatch.setattr("hydra_core.supervisor.discover_squads",
                        lambda *_a, **_k: _real_discover_squads(REPO_ROOT))
    # `_cmd_attended_step`'s engineering branch does a LOCAL
    # `from .squad_loader import discover_squads as _discover` (cli.py),
    # which reads `hydra_core.squad_loader.discover_squads` fresh at call
    # time -- unaffected by the two module-attribute patches above. Patch
    # the source function itself so every call site (including that local
    # import) resolves squads against the real repo root instead of the
    # disposable, squads/-less tmp `project`.
    monkeypatch.setattr("hydra_core.squad_loader.discover_squads",
                        lambda *_a, **_k: _real_discover_squads(REPO_ROOT))
    return project


def test_e2e_plan_to_decision_record_real_checkpointer(monkeypatch, tmp_path):
    """Drives the REAL sequence end to end: `hydra plan` (real compiled
    graph, real node_planner/node_intake) -> real attended step opens the
    planning task's cursor -> real attended submit ingests a schema-valid
    PLAN through the SAME `dispatch_ingested_envelopes`/`_apply_plan_
    reentry` call site production code uses -> parks at plan_gate -> real
    gate-only approve (`_cmd_resume_locked`) -> real attended step opens
    the materialised step-1 engineering cursor -> real attended submit for
    BOTH the engineer generate result and the judge verdict, through
    `host_bridge`'s real stage state machine -> real `_cmd_finalize`
    reaches a DECISION_RECORD.

    Two boundaries are stubbed, both at the lowest external edge Hydra
    crosses, never inside Hydra's own step/submit/resume/finalize logic:
    (1) `_E2EDispatcher` stands in for the pp_harness MCP transport (see
    its own docstring); (2) `resolve_repo_project_path` is redirected to a
    disposable, non-git tmp directory instead of the real "hydra" checkout
    so `host_bridge.begin_stage`'s worktree isolation cleanly no-ops
    (mirrors `tests/test_host_bridge.py`'s own bare-tmp_path pattern)
    rather than cutting a real git worktree against this repo."""
    from hydra_core import cli as hydra_cli
    from hydra_core import host_bridge
    from hydra_core.cli import (
        _cmd_attended_step, _cmd_attended_submit, _cmd_finalize,
        _cmd_plan, _cmd_resume_locked,
    )
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
    # Smoke always passes -- the disposable target_repo below has no real
    # build/test command; mirrors test_host_bridge.py / test_phase4_
    # hardening.py's own `_smoke_passes` fixture pattern.
    monkeypatch.setattr(host_bridge, "_run_smoke",
                        lambda *a, **k: ("pass", "fixture smoke pass"))

    project = _hermetic_e2e_project(tmp_path, monkeypatch)

    target_repo = tmp_path / "target_repo"
    target_repo.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("hydra_core.repo_registry.resolve_repo_project_path",
                        lambda *_a, **_k: target_repo)

    fake_dispatcher = _E2EDispatcher(project_root=project)
    monkeypatch.setattr(hydra_cli, "_attended_live_dispatcher",
                        lambda *_a, **_kw: fake_dispatcher)

    # 1. Real `hydra plan`: intake -> node_planner, halts before dispatch.
    #    --rigor=standard forces a plan-gated run (plan_rigor_override wins
    #    over triage) so this test's plan-gate assertions do not depend on
    #    the goal text's computed complexity.
    plan_args = argparse.Namespace(
        project=str(project), goal="ship the e2e regression",
        squad="engineering", budget=None, repo="hydra", repos=None,
        subdir=None, workflow_id_override=None, risk=None, rigor="standard",
    )
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = _cmd_plan(plan_args)
    plan_out = json.loads(buf.getvalue())
    assert rc == 0, plan_out
    wf = plan_out["workflow_id"]
    config = {"configurable": {"thread_id": wf}}

    sup = build_supervisor(project_root=project, dispatcher=fake_dispatcher)
    if isinstance(sup, _PurePythonRunner):
        pytest.skip("compiled graph unavailable")

    state = HydraState.model_validate(sup.get_state(config).values)
    planning_tasks = [t for t in state.tasks if t.owner_squad == "planning"]
    assert planning_tasks, f"hydra plan did not seed a planning task: {plan_out}"
    planning_task = planning_tasks[0]

    # 2. Real attended step: opens the planning task's squad cursor
    #    (claude-native entrypoint -- always host-attended).
    step_args = argparse.Namespace(project=str(project), workflow_id=wf, verbose=False)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = _cmd_attended_step(step_args)
    step_out = json.loads(buf.getvalue())
    assert rc == 0, step_out
    cursor_file = Path(step_out["cursor_path"])
    cursor = json.loads(cursor_file.read_text(encoding="utf-8"))
    call_key = cursor["pending_action"]["call_key"]
    assert call_key == f"squad-{planning_task.task_id}-0", cursor

    # 3. Real attended submit: emit a schema-valid PLAN through the SAME
    #    ingest/plan-reentry call site `_cmd_attended_submit` runs for every
    #    native-pack result in production (dispatch_ingested_envelopes ->
    #    _apply_plan_reentry) -- not a hand-invoked shortcut around it.
    raw_plan = {
        "id": str(uuid4()), "type": "PLAN", "origin_squad": "planning",
        "target_squad": "hydra", "workflow_id": wf, "rigor": "standard",
        "goal_restatement": "ship the e2e regression", "summary": "ship it",
        "plan_revision": 1,
        "steps": [{
            "step_id": "step-1", "target_squad": "engineering",
            "envelope_type": "DEV_TASK", "description": "implement the change",
            "acceptance_criteria": ["it works"], "priority": "P2",
            "target_repo_id": "hydra",
        }],
    }
    plan_result_path = tmp_path / "plan_result.json"
    plan_result_path.write_text(json.dumps({
        "status": "complete", "emitted_envelopes": [raw_plan],
    }), encoding="utf-8")
    plan_submit_args = argparse.Namespace(
        project=str(project), workflow_id=wf, run_id=str(planning_task.task_id),
        call_key=call_key, result=str(plan_result_path), verbose=False,
    )
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = _cmd_attended_submit(plan_submit_args)
    plan_submit_out = json.loads(buf.getvalue())
    assert rc == 0, plan_submit_out
    assert plan_submit_out.get("ok") is True, plan_submit_out
    assert plan_submit_out.get("status") not in ("plan_reentry_failed",), plan_submit_out

    parked = getattr(sup.get_state(config), "next", None)
    assert tuple(parked) == ("plan_gate",), f"expected to park at plan_gate, got {parked}"

    # 4. Real gate-only approve.
    resume_args = argparse.Namespace(
        project=str(project), workflow_id=wf, action="approve", option=None,
        live=False, verbose=False, operator="tester@example.com",
        critique_ref=None, gate_only=True,
    )
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = _cmd_resume_locked(resume_args, project, wf, "approve", None)
    resume_out = json.loads(buf.getvalue())
    assert rc == 0, resume_out
    assert resume_out.get("plan_status") == "approved", resume_out

    state = HydraState.model_validate(sup.get_state(config).values)
    step_tasks = [t for t in state.tasks if getattr(t, "plan_step_id", None) == "step-1"]
    assert len(step_tasks) == 1, f"expected exactly one materialised step task, got {state.tasks}"
    step_task = step_tasks[0]

    # 5. Real attended step: engineer host_action for step 1.
    step_args2 = argparse.Namespace(project=str(project), workflow_id=wf, verbose=False)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = _cmd_attended_step(step_args2)
    step2_out = json.loads(buf.getvalue())
    assert rc == 0, step2_out
    assert step2_out["host_action"]["agent_type"] == "engineer", step2_out
    assert step2_out["task_id"] == str(step_task.task_id), step2_out
    gen_run_id = step2_out["run_id"]

    # 6. Real attended submit for the engineer's generate result, through
    #    host_bridge's real stage state machine (await_generate ->
    #    await_judge), with a fake RESULT PAYLOAD standing in for what a
    #    real host `engineer` subagent would report (text/cost/tokens).
    gen_result_path = tmp_path / "gen_result.json"
    gen_result_path.write_text(json.dumps({
        "status": "complete", "text": "implemented the change",
        "cost_usd": 0.1, "tokens_in": 100, "tokens_out": 50,
        "model": "claude-sonnet-5",
    }), encoding="utf-8")
    gen_submit_args = argparse.Namespace(
        project=str(project), workflow_id=wf, run_id=str(gen_run_id),
        call_key="generate-0", result=str(gen_result_path), verbose=False,
    )
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = _cmd_attended_submit(gen_submit_args)
    gen_out = json.loads(buf.getvalue())
    assert rc == 0, gen_out
    assert gen_out.get("state") == "await_judge", gen_out
    judge_call_key = gen_out["host_action"]["call_key"]

    # 7. Real attended submit for the judge verdict -> stage finalizes.
    judge_result_path = tmp_path / "judge_result.json"
    judge_result_path.write_text(json.dumps({
        "status": "complete", "outcome": "pass", "critique_md": "looks good",
        "judge_producer": "claude", "cost_usd": 0.05,
    }), encoding="utf-8")
    judge_submit_args = argparse.Namespace(
        project=str(project), workflow_id=wf, run_id=str(gen_run_id),
        call_key=judge_call_key, result=str(judge_result_path), verbose=False,
    )
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = _cmd_attended_submit(judge_submit_args)
    judge_out = json.loads(buf.getvalue())
    assert rc == 0, judge_out
    assert judge_out.get("status") == "complete", judge_out

    # 8. Real finalize -> a real DECISION_RECORD, not tasks_pending.
    from hydra_core import memory as _mem
    episodic_db = tmp_path / "episodic.db"
    _orig_append = _mem.append_episodic

    def _patched_append(workflow_id, kind, payload, *, key=None, db=None,
                        cells=None, origin_squad=None):
        return _orig_append(workflow_id, kind, payload, key=key, db=episodic_db,
                            cells=cells, origin_squad=origin_squad)

    monkeypatch.setattr(_mem, "append_episodic", _patched_append)

    fin_args = argparse.Namespace(project=str(project), workflow_id=wf, verbose=False)
    buf2 = io.StringIO()
    with contextlib.redirect_stdout(buf2):
        rc2 = _cmd_finalize(fin_args)
    fin_out = json.loads(buf2.getvalue())
    assert rc2 == 0, fin_out
    assert fin_out.get("status") != "tasks_pending", fin_out
    assert fin_out.get("status") == "finalized", fin_out
    assert fin_out.get("decision_record_id"), fin_out
