"""P5c: the operator surfaces for the plan gate.

Ships behind HYDRA_PLAN_PHASE, which STAYS default OFF (tests/conftest.py
pins it off for the whole suite -- these tests never need to flip it, since
none of Task 1/2/3 depend on the flag: they gate on `gate_node == "plan_gate"`
directly, per the brief's central warning).

Task map (see the P5c brief):
  1. `--force-dispatch` records a real `policy_override` trace event (for
     EVERY force-dispatch, not only at plan_gate -- a pre-existing defect
     being closed) and, scoped to plan_gate, stamps `plan_status="bypassed"`
     plus a `hitl_history` entry and a plan-artifact governance note --
     TestForceDispatchPolicyOverride.
  2. `--modify-plan` bumps `plan_revision`, sets `plan_status="authoring"`,
     seeds exactly one planning TaskState carrying the operator's full
     critique (read from `--critique-ref`, never `--option`) and the prior
     plan envelope id as `supersedes_plan_envelope_id`; bounded by
     HYDRA_PLAN_MAX_REVISIONS -- TestModifyPlan. The gate's own options
     (`node_plan_judge`) collapse to approve/abort at the ceiling --
     TestPlanGateOptions.
  3. `--reject` parks at `surfaced` with `plan_status="rejected"` ONLY when
     the rejected gate IS plan_gate -- an unscoped write would raise the
     plan barrier from an ordinary rejection of any other gate, the total-
     dispatch-freeze defect the brief's "READ THIS FIRST" section describes
     -- TestRejectScoping.

Every guard here is proven as a property: the counterpart (the write does
NOT happen off plan_gate, no task is seeded on reject, options stay full
under the ceiling) sits directly beside the positive assertion.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from hydra_core.cli import _cmd_resume_locked
from hydra_core.squad_node import Dispatcher
from hydra_core.state import HydraState, TaskState
from hydra_core.supervisor import build_supervisor, _PurePythonRunner


# =========================================================================== #
# Fake supervisors -- mirror tests/test_fable_audit_2.py's
# TestForceDispatchCapabilityUniform fakes (get_state/update_state/invoke),
# extended with an `updates` log so a test can inspect EVERY patch applied,
# not just the last one.
# =========================================================================== #


class _SimpleFakeSup:
    """A minimal `sup` stand-in: records every `update_state` patch (as
    (patch, as_node) pairs) and merges it into `values`, mirroring how a real
    checkpoint accumulates replace-by-default fields across multiple calls."""

    def __init__(self, pending_hitl, values):
        self._values = dict(values)
        self._values.setdefault("pending_hitl", pending_hitl)
        self.updates: list[tuple[dict, object]] = []
        self.invoked = 0

    def get_state(self, config):
        return type("Snap", (), {"values": dict(self._values), "next": ()})()

    def update_state(self, config, patch, as_node=None):
        self.updates.append((dict(patch), as_node))
        self._values.update(patch)

    def invoke(self, arg, config=None):
        self.invoked += 1
        return {"phase": self._values.get("phase", "executing")}


class _FakeModifyPlanSup(_SimpleFakeSup):
    """Emulates the compiled graph's `as_node="dispatch"` re-entry trick
    (`_reenter_graph_after_dispatch`): a real graph parked at `plan_gate`
    (`next == ("plan_gate",)`) that, once `as_node="dispatch"` fires
    after_dispatch fresh against `plan_status="authoring"`, routes to
    `await_host` -> END (`next` becomes empty) WITHOUT ever re-running
    `node_plan_gate`. `invoke()` must never be called for a correct
    modify-plan re-entry -- asserting that directly is the whole point of
    this fake (see `_reenter_graph_after_dispatch`'s own
    `test_reentry_stops_early_once_parked_at_target`)."""

    def __init__(self, pending_hitl, values):
        super().__init__(pending_hitl, values)
        self._next = ("plan_gate",)

    def get_state(self, config):
        return type("Snap", (), {"values": dict(self._values), "next": self._next})()

    def update_state(self, config, patch, as_node=None):
        super().update_state(config, patch, as_node=as_node)
        if as_node == "dispatch":
            self._next = ()

    def invoke(self, arg, config=None):
        raise AssertionError(
            "modify-plan re-entry must never invoke() the compiled graph -- "
            "that would re-run node_plan_gate against the OLD plan_ref"
        )


def _resume_args(project: Path, wf: str, action: str, *, critique_ref=None, option=None):
    return argparse.Namespace(
        project=str(project), workflow_id=wf, action=action, option=option,
        live=False, verbose=False, operator="operator@example.com",
        critique_ref=critique_ref,
    )


def _patch_common(monkeypatch, sup):
    monkeypatch.setattr("hydra_core.supervisor.build_supervisor", lambda **_k: sup)
    monkeypatch.setattr("hydra_core.cli._prune_spooled_hitl_requests", lambda *_a: 0)
    monkeypatch.setattr("hydra_core.cli._resolve_eights_hitl_for_workflow",
                        lambda *_a, **_k: {"resolved": 0})


# =========================================================================== #
# Task 1 -- force-dispatch policy_override
# =========================================================================== #


class TestForceDispatchPolicyOverride:
    def test_force_dispatch_at_plan_gate_emits_bypassed_and_artifact_note(
        self, monkeypatch, tmp_path,
    ):
        wf = str(uuid4())
        artifact_rel = "docs/plans/test-plan.html"
        (tmp_path / "docs" / "plans").mkdir(parents=True)
        (tmp_path / artifact_rel).write_text(
            "<h1>Plan: ship the thing</h1>\n", encoding="utf-8",
        )
        pending = {"workflow_id": wf, "reason": "plan_approval", "gate_node": "plan_gate"}
        values = {
            "pending_hitl": pending, "phase": "approval",
            "plan_artifact_location": f"repo:artifact:{artifact_rel}",
        }
        sup = _SimpleFakeSup(pending, values)
        _patch_common(monkeypatch, sup)
        emitted: list[tuple] = []
        monkeypatch.setattr("hydra_core.cli.emit", lambda *a, **k: emitted.append(a))

        args = _resume_args(tmp_path, wf, "force-dispatch")
        ret = _cmd_resume_locked(args, tmp_path, wf, "force-dispatch", None)
        assert ret == 0

        policy_events = [a for a in emitted if len(a) >= 3 and a[2] == "policy_override"]
        assert policy_events, f"policy_override must be emitted; saw {[a[2] for a in emitted if len(a) >= 3]}"
        payload = policy_events[0][3]
        assert payload["gate_node"] == "plan_gate"

        bypass_patches = [p for p, _ in sup.updates if p.get("plan_status") == "bypassed"]
        assert bypass_patches, "plan_status='bypassed' must be written at plan_gate"

        bypass_notes = [
            entry for p, _ in sup.updates for entry in p.get("hitl_history", [])
            if isinstance(entry, dict) and entry.get("event") == "plan_gate_bypassed"
        ]
        assert bypass_notes, "a plan_gate_bypassed hitl_history entry must be recorded"

        written = (tmp_path / artifact_rel).read_text(encoding="utf-8")
        assert "Governance Notes" in written
        assert "without plan approval" in written

    def test_force_dispatch_not_at_plan_gate_still_emits_policy_override(
        self, monkeypatch, tmp_path,
    ):
        """The pre-existing defect this phase closes: EVERY force-dispatch
        gets a policy_override event, not only one at plan_gate -- but the
        plan-specific writes stay scoped (the counterpart)."""
        wf = str(uuid4())
        pending = {"workflow_id": wf, "reason": "over_budget", "gate_node": "dispatch"}
        values = {"pending_hitl": pending, "phase": "executing"}
        sup = _SimpleFakeSup(pending, values)
        _patch_common(monkeypatch, sup)
        emitted: list[tuple] = []
        monkeypatch.setattr("hydra_core.cli.emit", lambda *a, **k: emitted.append(a))

        args = _resume_args(tmp_path, wf, "force-dispatch")
        ret = _cmd_resume_locked(args, tmp_path, wf, "force-dispatch", None)
        assert ret == 0

        policy_events = [a for a in emitted if len(a) >= 3 and a[2] == "policy_override"]
        assert policy_events, "policy_override must fire for every force-dispatch"
        assert policy_events[0][3]["gate_node"] == "dispatch"

        bypass_patches = [p for p, _ in sup.updates if p.get("plan_status") == "bypassed"]
        assert not bypass_patches, (
            "plan_status must never be touched by a force-dispatch that is "
            "not at plan_gate"
        )

    def test_force_dispatch_missing_artifact_does_not_block_resume(
        self, monkeypatch, tmp_path,
    ):
        """Fail-soft: no plan artifact on disk (or no plan_artifact_location
        at all) must not prevent the resume/bypass recording itself."""
        wf = str(uuid4())
        pending = {"workflow_id": wf, "reason": "plan_approval", "gate_node": "plan_gate"}
        values = {"pending_hitl": pending, "phase": "approval"}  # no plan_artifact_location
        sup = _SimpleFakeSup(pending, values)
        _patch_common(monkeypatch, sup)
        monkeypatch.setattr("hydra_core.cli.emit", lambda *a, **k: None)

        args = _resume_args(tmp_path, wf, "force-dispatch")
        ret = _cmd_resume_locked(args, tmp_path, wf, "force-dispatch", None)
        assert ret == 0
        assert any(p.get("plan_status") == "bypassed" for p, _ in sup.updates)

    def test_governance_note_failure_is_traced_not_silent(self, monkeypatch, tmp_path):
        """Cross-vendor judge finding (P5c revise round): fail-soft must not
        mean fail-silent. A `plan_artifact_location` that fails containment
        (outside docs/plans, the write_repo_artifact allow-list) must emit
        `plan_governance_note_failed` -- and must still not block the resume
        or the plan_status='bypassed'/policy_override recording, which have
        already happened regardless of whether the artifact note lands."""
        wf = str(uuid4())
        pending = {"workflow_id": wf, "reason": "plan_approval", "gate_node": "plan_gate"}
        values = {
            "pending_hitl": pending, "phase": "approval",
            # Outside docs/plans -- write_repo_artifact's default allow-list
            # refuses this, so the governance-note write must fail loudly
            # (via a trace event), not silently.
            "plan_artifact_location": "repo:artifact:src/main.py",
        }
        sup = _SimpleFakeSup(pending, values)
        _patch_common(monkeypatch, sup)
        emitted: list[tuple] = []
        monkeypatch.setattr("hydra_core.cli.emit", lambda *a, **k: emitted.append(a))

        args = _resume_args(tmp_path, wf, "force-dispatch")
        ret = _cmd_resume_locked(args, tmp_path, wf, "force-dispatch", None)
        assert ret == 0, "an artifact-note failure must never block the resume"

        failure_events = [
            a for a in emitted if len(a) >= 3 and a[2] == "plan_governance_note_failed"
        ]
        assert failure_events, (
            f"a failed governance-note write must be traced; saw "
            f"{[a[2] for a in emitted if len(a) >= 3]}"
        )
        assert failure_events[0][3]["plan_artifact_location"] == "repo:artifact:src/main.py"
        assert failure_events[0][3]["error"]

        # The recording that DID succeed (plan_status/policy_override) must
        # be unaffected by the artifact-side failure.
        assert any(p.get("plan_status") == "bypassed" for p, _ in sup.updates)
        policy_events = [a for a in emitted if len(a) >= 3 and a[2] == "policy_override"]
        assert policy_events

    def test_governance_note_success_emits_no_failure_event(self, monkeypatch, tmp_path):
        """Counterpart: a successful note must NOT also emit
        plan_governance_note_failed."""
        wf = str(uuid4())
        artifact_rel = "docs/plans/ok-plan.html"
        (tmp_path / "docs" / "plans").mkdir(parents=True)
        (tmp_path / artifact_rel).write_text("<h1>Plan</h1>\n", encoding="utf-8")
        pending = {"workflow_id": wf, "reason": "plan_approval", "gate_node": "plan_gate"}
        values = {
            "pending_hitl": pending, "phase": "approval",
            "plan_artifact_location": f"repo:artifact:{artifact_rel}",
        }
        sup = _SimpleFakeSup(pending, values)
        _patch_common(monkeypatch, sup)
        emitted: list[tuple] = []
        monkeypatch.setattr("hydra_core.cli.emit", lambda *a, **k: emitted.append(a))

        args = _resume_args(tmp_path, wf, "force-dispatch")
        ret = _cmd_resume_locked(args, tmp_path, wf, "force-dispatch", None)
        assert ret == 0
        assert not [a for a in emitted if len(a) >= 3 and a[2] == "plan_governance_note_failed"]

    def test_append_plan_governance_note_reuses_artifact_store_containment(self):
        """Consistency guard (P5c revise round finding 2): both the write
        side (write_repo_artifact) and this read-before-write side must call
        the SAME shared containment function -- a hand-rolled second copy
        here is exactly the divergence risk the previous phase already paid
        for once. AST-based so this fails on an actual re-derived path
        check, not merely on a comment mentioning one."""
        import ast
        import inspect
        import textwrap

        from hydra_core.cli import _append_plan_governance_note

        src = inspect.getsource(_append_plan_governance_note)
        tree = ast.parse(textwrap.dedent(src))
        fn_node = tree.body[0]
        assert isinstance(fn_node, ast.FunctionDef)

        calls_shared_fn = any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "resolve_repo_artifact_path"
            for node in ast.walk(fn_node)
        )
        assert calls_shared_fn, (
            "_append_plan_governance_note must validate the read path through "
            "hydra_core.artifact_store.resolve_repo_artifact_path -- the SAME "
            "containment write_repo_artifact uses -- not a second hand-rolled check"
        )

        # Guard against a hand-rolled `Path(project) / relpath` read (the
        # exact shape the finding described) reappearing.
        reinvented_join = any(
            isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div)
            for node in ast.walk(fn_node)
        )
        assert not reinvented_join, (
            "found a raw path-join (Path(...) / ...) inside "
            "_append_plan_governance_note -- the validated path must come "
            "from resolve_repo_artifact_path, not be rebuilt by hand"
        )


# =========================================================================== #
# Task 3 -- reject scoping
# =========================================================================== #


class TestRejectScoping:
    def test_reject_at_plan_gate_sets_rejected(self, monkeypatch, tmp_path):
        wf = str(uuid4())
        pending = {"workflow_id": wf, "reason": "plan_approval", "gate_node": "plan_gate"}
        values = {"pending_hitl": pending, "phase": "approval"}
        sup = _SimpleFakeSup(pending, values)
        _patch_common(monkeypatch, sup)

        args = _resume_args(tmp_path, wf, "reject")
        ret = _cmd_resume_locked(args, tmp_path, wf, "reject", None)
        assert ret == 0

        surfaced_patches = [p for p, _ in sup.updates if p.get("phase") == "surfaced"]
        assert surfaced_patches, "reject must park the workflow surfaced"
        assert any(p.get("plan_status") == "rejected" for p in surfaced_patches), (
            "plan_status='rejected' must be written when the rejected gate IS plan_gate"
        )
        # Absence assertion's counterpart lives in TestModifyPlan (a modify-plan
        # DOES seed a task) -- here, no task is ever seeded by a reject.
        assert not any("tasks" in p for p, _ in sup.updates), (
            "reject must NEVER seed a new planning task (no automatic re-plan)"
        )

    def test_reject_at_other_gate_does_not_touch_plan_status(self, monkeypatch, tmp_path):
        """THE regression this task exists to prevent: an ordinary reject of
        a non-plan gate must never raise the plan barrier. Before the fix,
        this wrote plan_status='rejected' unconditionally here -- with
        HYDRA_PLAN_PHASE off (the module-wide default) that permanently
        freezes every task behind a barrier nothing can ever clear."""
        wf = str(uuid4())
        pending = {"workflow_id": wf, "reason": "over_budget", "gate_node": "dispatch"}
        values = {"pending_hitl": pending, "phase": "executing"}
        sup = _SimpleFakeSup(pending, values)
        _patch_common(monkeypatch, sup)

        args = _resume_args(tmp_path, wf, "reject")
        ret = _cmd_resume_locked(args, tmp_path, wf, "reject", None)
        assert ret == 0

        surfaced_patches = [p for p, _ in sup.updates if p.get("phase") == "surfaced"]
        assert surfaced_patches
        assert all("plan_status" not in p for p in surfaced_patches), (
            "plan_status must not be written by a reject at a non-plan_gate gate"
        )

    def test_reject_at_bare_gate_node_none_does_not_touch_plan_status(
        self, monkeypatch, tmp_path,
    ):
        """gate_node absent entirely (a legacy pending_hitl shape) must also
        not match plan_gate."""
        wf = str(uuid4())
        pending = {"workflow_id": wf, "reason": "policy_breach"}
        values = {"pending_hitl": pending, "phase": "approval"}
        sup = _SimpleFakeSup(pending, values)
        _patch_common(monkeypatch, sup)

        args = _resume_args(tmp_path, wf, "reject")
        ret = _cmd_resume_locked(args, tmp_path, wf, "reject", None)
        assert ret == 0
        surfaced_patches = [p for p, _ in sup.updates if p.get("phase") == "surfaced"]
        assert all("plan_status" not in p for p in surfaced_patches)


# =========================================================================== #
# Task 2 -- --modify-plan
# =========================================================================== #


class TestModifyPlan:
    def test_modify_plan_bumps_revision_seeds_one_task_with_critique_and_supersedes(
        self, monkeypatch, tmp_path,
    ):
        monkeypatch.delenv("HYDRA_PLAN_MAX_REVISIONS", raising=False)
        wf = str(uuid4())
        prior_envelope_id = str(uuid4())
        pending = {
            "workflow_id": wf, "reason": "plan_approval", "gate_node": "plan_gate",
            "options": ["approve", "reject", "modify-plan", "modify-budget"],
        }
        values = {
            "pending_hitl": pending, "phase": "approval",
            "plan_revision": 1, "plan_envelope_id": prior_envelope_id,
            "plan_rigor": "standard", "root_goal": "ship the thing",
        }
        sup = _FakeModifyPlanSup(pending, values)
        _patch_common(monkeypatch, sup)
        monkeypatch.setattr("hydra_core.cli.emit", lambda *a, **k: None)

        # A critique containing ordinary prose punctuation -- the failure mode
        # if someone routed this through _OPTION_RE ([A-Za-z0-9 ,._-]{0,200})
        # instead of a file path: parentheses, a colon, an exclamation mark,
        # and a dollar sign would all be rejected/stripped by that charset.
        critique_text = (
            "This plan misses rollback (needed before prod!), and the budget "
            "line: $500 is too low. Please revise."
        )
        critique_file = tmp_path / "critique.txt"
        critique_file.write_text(critique_text, encoding="utf-8")

        args = _resume_args(tmp_path, wf, "modify-plan", critique_ref=str(critique_file))
        ret = _cmd_resume_locked(args, tmp_path, wf, "modify-plan", None)
        assert ret == 0, "modify-plan must succeed with a valid --critique-ref"

        task_patches = [p for p, _ in sup.updates if "tasks" in p]
        assert len(task_patches) == 1, "modify-plan must seed EXACTLY one planning task"
        tasks = task_patches[0]["tasks"]
        assert len(tasks) == 1
        task = tasks[0]
        assert isinstance(task, TaskState)
        assert task.owner_squad == "planning"
        assert task.plan_revision == 2
        assert task.supersedes_plan_envelope_id == prior_envelope_id
        assert task.plan_critique == critique_text, (
            "the critique must survive with its punctuation intact, untruncated"
        )

        assert any(p.get("plan_status") == "authoring" for p, _ in sup.updates)
        assert any(p.get("plan_revision") == 2 for p, _ in sup.updates)
        assert sup.invoked == 0, (
            "modify-plan re-entry must use as_node='dispatch', never a plain invoke()"
        )

    def test_modify_plan_requires_plan_gate(self, monkeypatch, tmp_path):
        wf = str(uuid4())
        pending = {"workflow_id": wf, "reason": "over_budget", "gate_node": "dispatch"}
        values = {"pending_hitl": pending, "phase": "executing"}
        sup = _SimpleFakeSup(pending, values)
        _patch_common(monkeypatch, sup)

        args = _resume_args(tmp_path, wf, "modify-plan", critique_ref="whatever.txt")
        ret = _cmd_resume_locked(args, tmp_path, wf, "modify-plan", None)
        assert ret == 1
        assert sup.updates == [], "a rejected modify-plan request must mutate nothing"

    def test_modify_plan_requires_critique_ref(self, monkeypatch, tmp_path):
        wf = str(uuid4())
        pending = {"workflow_id": wf, "reason": "plan_approval", "gate_node": "plan_gate"}
        values = {"pending_hitl": pending, "phase": "approval", "plan_revision": 1}
        sup = _SimpleFakeSup(pending, values)
        _patch_common(monkeypatch, sup)

        args = _resume_args(tmp_path, wf, "modify-plan", critique_ref=None)
        ret = _cmd_resume_locked(args, tmp_path, wf, "modify-plan", None)
        assert ret == 1
        assert sup.updates == []

    def test_modify_plan_refuses_at_revision_ceiling(self, monkeypatch, tmp_path):
        monkeypatch.delenv("HYDRA_PLAN_MAX_REVISIONS", raising=False)
        wf = str(uuid4())
        pending = {"workflow_id": wf, "reason": "plan_approval", "gate_node": "plan_gate"}
        # Default ceiling is 2 revisions; plan_revision=3 means 2 already used.
        values = {
            "pending_hitl": pending, "phase": "approval",
            "plan_revision": 3, "plan_envelope_id": str(uuid4()),
        }
        sup = _SimpleFakeSup(pending, values)
        _patch_common(monkeypatch, sup)

        critique_file = tmp_path / "c.txt"
        critique_file.write_text("one more please", encoding="utf-8")
        args = _resume_args(tmp_path, wf, "modify-plan", critique_ref=str(critique_file))
        ret = _cmd_resume_locked(args, tmp_path, wf, "modify-plan", None)
        assert ret == 1
        assert sup.updates == [], "the ceiling must refuse BEFORE mutating any state"

    def test_modify_plan_honors_custom_ceiling_env(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HYDRA_PLAN_MAX_REVISIONS", "1")
        wf = str(uuid4())
        pending = {"workflow_id": wf, "reason": "plan_approval", "gate_node": "plan_gate"}
        values = {
            "pending_hitl": pending, "phase": "approval",
            "plan_revision": 2, "plan_envelope_id": str(uuid4()),
        }
        sup = _SimpleFakeSup(pending, values)
        _patch_common(monkeypatch, sup)
        critique_file = tmp_path / "c.txt"
        critique_file.write_text("nope", encoding="utf-8")
        args = _resume_args(tmp_path, wf, "modify-plan", critique_ref=str(critique_file))
        ret = _cmd_resume_locked(args, tmp_path, wf, "modify-plan", None)
        assert ret == 1, "with HYDRA_PLAN_MAX_REVISIONS=1, revision 2 has already used its one revision"

    def test_modify_plan_critique_ref_from_memoryref_key(self, monkeypatch, tmp_path):
        """--critique-ref may also be a repo:artifact:<path> MemoryRef key,
        resolved relative to the project root -- exactly the shape
        plan_artifact_location itself uses."""
        monkeypatch.delenv("HYDRA_PLAN_MAX_REVISIONS", raising=False)
        wf = str(uuid4())
        rel = "docs/plans/critique-note.txt"
        (tmp_path / "docs" / "plans").mkdir(parents=True)
        critique_text = "Needs a rollback step; the auth flow isn't covered."
        (tmp_path / rel).write_text(critique_text, encoding="utf-8")

        pending = {"workflow_id": wf, "reason": "plan_approval", "gate_node": "plan_gate"}
        values = {
            "pending_hitl": pending, "phase": "approval",
            "plan_revision": 1, "plan_envelope_id": str(uuid4()),
        }
        sup = _FakeModifyPlanSup(pending, values)
        _patch_common(monkeypatch, sup)
        monkeypatch.setattr("hydra_core.cli.emit", lambda *a, **k: None)

        args = _resume_args(tmp_path, wf, "modify-plan", critique_ref=f"repo:artifact:{rel}")
        ret = _cmd_resume_locked(args, tmp_path, wf, "modify-plan", None)
        assert ret == 0
        task_patches = [p for p, _ in sup.updates if "tasks" in p]
        assert task_patches[0]["tasks"][0].plan_critique == critique_text


# =========================================================================== #
# P1-2 (HIGH, cross-vendor gpt-6-astra): modify-plan atomic write.
# =========================================================================== #


class TestP1_2ModifyPlanAtomicWrite:
    """The gate-resolution patch (pending_hitl clear, hitl_history) and the
    FULL revision patch (plan_status="authoring", plan_revision+1, the
    revision task, plan_supersedes_expected, plan_superseded_task_ids) must
    land in ONE atomic `sup.update_state(..., as_node="dispatch")` call --
    not two, with spool pruning / TheEights reconciliation running in
    between. A crash between two separate writes used to leave the OLD
    judged plan cleared of its gate with no revision task ever recorded:
    not resumable via the pending-gate branch (no pending_hitl left) and
    not continuable via the revision task (never written).

    MUTATION PROOF: revert the fold (split the write back into an ordinary
    `sup.update_state(config, patch)` followed by a SEPARATE
    `_reenter_graph_after_dispatch(sup, config, {...revision fields...})`
    call, as the pre-fix code did) and BOTH tests below fail --
    `test_modify_plan_writes_checkpoint_exactly_once` sees `len(sup.updates)
    == 2`, and `test_crash_after_write_still_leaves_revision_durable` sees
    the revision fields (`plan_status`/`plan_revision`/`tasks`) MISSING
    from the checkpoint at the moment of the injected crash, because they
    were still queued for the second, not-yet-run write."""

    def _seeded(self, wf, prior_envelope_id):
        pending = {
            "workflow_id": wf, "reason": "plan_approval", "gate_node": "plan_gate",
            "options": ["approve", "reject", "modify-plan", "modify-budget"],
        }
        values = {
            "pending_hitl": pending, "phase": "approval",
            "plan_revision": 1, "plan_envelope_id": prior_envelope_id,
            "plan_rigor": "standard", "root_goal": "ship the thing",
        }
        return pending, values

    def test_modify_plan_writes_checkpoint_exactly_once(self, monkeypatch, tmp_path):
        monkeypatch.delenv("HYDRA_PLAN_MAX_REVISIONS", raising=False)
        wf = str(uuid4())
        prior_envelope_id = str(uuid4())
        pending, values = self._seeded(wf, prior_envelope_id)
        sup = _FakeModifyPlanSup(pending, values)
        _patch_common(monkeypatch, sup)
        monkeypatch.setattr("hydra_core.cli.emit", lambda *a, **k: None)

        critique_file = tmp_path / "critique.txt"
        critique_file.write_text("please revise", encoding="utf-8")

        args = _resume_args(tmp_path, wf, "modify-plan", critique_ref=str(critique_file))
        ret = _cmd_resume_locked(args, tmp_path, wf, "modify-plan", None)
        assert ret == 0

        assert len(sup.updates) == 1, (
            f"modify-plan must write the checkpoint EXACTLY ONCE -- got "
            f"{len(sup.updates)} call(s): {sup.updates}"
        )
        patch, as_node = sup.updates[0]
        assert as_node == "dispatch"
        assert patch.get("pending_hitl") is None
        assert patch.get("plan_status") == "authoring"
        assert patch.get("plan_revision") == 2
        assert len(patch.get("tasks") or []) == 1
        assert patch.get("plan_supersedes_expected") == prior_envelope_id
        assert sup.invoked == 0

    def test_crash_after_write_still_leaves_revision_durable(self, monkeypatch, tmp_path):
        """Inject a crash in the fail-soft post-write work (spool prune) --
        the single atomic write must already have landed by then, so the
        checkpoint is durable regardless of what happens after."""
        monkeypatch.delenv("HYDRA_PLAN_MAX_REVISIONS", raising=False)
        wf = str(uuid4())
        prior_envelope_id = str(uuid4())
        pending, values = self._seeded(wf, prior_envelope_id)
        sup = _FakeModifyPlanSup(pending, values)
        monkeypatch.setattr("hydra_core.supervisor.build_supervisor", lambda **_k: sup)
        monkeypatch.setattr("hydra_core.cli.emit", lambda *a, **k: None)

        def _explodes(*_a, **_k):
            raise RuntimeError("spool prune crashed (simulated process death)")
        monkeypatch.setattr("hydra_core.cli._prune_spooled_hitl_requests", _explodes)
        monkeypatch.setattr("hydra_core.cli._resolve_eights_hitl_for_workflow",
                            lambda *_a, **_k: {"resolved": 0})

        critique_file = tmp_path / "critique.txt"
        critique_file.write_text("please revise", encoding="utf-8")
        args = _resume_args(tmp_path, wf, "modify-plan", critique_ref=str(critique_file))

        with pytest.raises(RuntimeError):
            _cmd_resume_locked(args, tmp_path, wf, "modify-plan", None)

        # The checkpoint write already happened BEFORE the injected crash --
        # durable regardless of the crash.
        assert len(sup.updates) == 1
        patch, as_node = sup.updates[0]
        assert as_node == "dispatch"
        assert patch.get("plan_status") == "authoring"
        assert patch.get("plan_revision") == 2
        assert len(patch.get("tasks") or []) == 1
        assert patch.get("pending_hitl") is None


# =========================================================================== #
# --critique-ref containment. `critique_ref` is reachable through the
# `hydra.workflow.resume` MCP verb, not just the CLI a trusted human is
# typing at -- an unconstrained read here is an arbitrary-file-read any
# caller of that verb can point anywhere, mirroring the exact class of bug
# `hydra_core.artifact_store.write_repo_artifact` was hardened against on
# the WRITE side. `_read_plan_critique` is tested directly (the actual
# enforcement point) plus once end-to-end through `_cmd_resume_locked` to
# prove the containment actually blocks the real modify-plan path, not just
# the helper in isolation.
# =========================================================================== #


class TestCritiqueRefContainment:
    def test_absolute_path_outside_project_is_refused(self, tmp_path):
        from hydra_core.cli import _PlanCritiqueError, _read_plan_critique

        project = tmp_path / "project"
        project.mkdir()
        outside = tmp_path / "outside.txt"
        outside.write_text("secret content", encoding="utf-8")

        with pytest.raises(_PlanCritiqueError, match="outside the project root"):
            _read_plan_critique(str(outside), project)

    def test_traversal_that_escapes_after_resolution_is_refused(self, tmp_path):
        from hydra_core.cli import _PlanCritiqueError, _read_plan_critique

        project = tmp_path / "project"
        project.mkdir()
        outside = tmp_path / "outside.txt"
        outside.write_text("secret content", encoding="utf-8")

        with pytest.raises(_PlanCritiqueError, match="outside the project root"):
            _read_plan_critique("../outside.txt", project)

    def test_symlink_inside_project_pointing_outside_is_refused(self, tmp_path):
        from hydra_core.cli import _PlanCritiqueError, _read_plan_critique

        project = tmp_path / "project"
        project.mkdir()
        outside = tmp_path / "outside.txt"
        outside.write_text("secret content", encoding="utf-8")
        link = project / "critique_link.txt"
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError):
            pytest.skip("symlink creation not permitted in this environment")

        with pytest.raises(_PlanCritiqueError, match="outside the project root"):
            _read_plan_critique("critique_link.txt", project)

    def test_repo_artifact_memoryref_traversal_is_also_refused(self, tmp_path):
        """The repo:artifact:<path> form goes through the SAME containment
        check -- a crafted key cannot escape via that path either."""
        from hydra_core.cli import _PlanCritiqueError, _read_plan_critique

        project = tmp_path / "project"
        project.mkdir()
        outside = tmp_path / "outside.txt"
        outside.write_text("secret content", encoding="utf-8")

        with pytest.raises(_PlanCritiqueError, match="outside the project root"):
            _read_plan_critique("repo:artifact:../outside.txt", project)

    def test_legitimate_in_project_critique_still_reads_in_full(self, tmp_path):
        """The counterpart: containment must not collateral-damage the
        actual feature -- an in-project file with prose punctuation and
        real length still reads back exactly."""
        from hydra_core.cli import _read_plan_critique

        project = tmp_path / "project"
        project.mkdir()
        (project / "notes").mkdir()
        critique_text = (
            "This misses rollback (needed before prod!), and the budget "
            "line: $500 is too low. Please revise."
        )
        (project / "notes" / "critique.txt").write_text(critique_text, encoding="utf-8")

        assert _read_plan_critique("notes/critique.txt", project) == critique_text
        assert _read_plan_critique(
            str(project / "notes" / "critique.txt"), project,
        ) == critique_text

    def test_modify_plan_end_to_end_refuses_outside_project_critique_ref(
        self, monkeypatch, tmp_path,
    ):
        """End-to-end through the real modify-plan resume path, not just the
        helper in isolation."""
        outside_root = tmp_path / "elsewhere"
        outside_root.mkdir()
        project = tmp_path / "project"
        project.mkdir()
        outside = outside_root / "secret.txt"
        outside.write_text("secret content", encoding="utf-8")

        wf = str(uuid4())
        pending = {"workflow_id": wf, "reason": "plan_approval", "gate_node": "plan_gate"}
        values = {
            "pending_hitl": pending, "phase": "approval",
            "plan_revision": 1, "plan_envelope_id": str(uuid4()),
        }
        sup = _SimpleFakeSup(pending, values)
        _patch_common(monkeypatch, sup)

        args = _resume_args(project, wf, "modify-plan", critique_ref=str(outside))
        ret = _cmd_resume_locked(args, project, wf, "modify-plan", None)
        assert ret == 1
        assert sup.updates == [], "a refused critique-ref must mutate nothing"


# =========================================================================== #
# node_plan_judge's rendered gate options + the revision ceiling
# =========================================================================== #


def _plan_judge_fn():
    stub_dispatcher = MagicMock(spec=Dispatcher)
    stub_dispatcher._tool_tracker = None
    runner = build_supervisor(
        project_root=Path(__file__).resolve().parents[1],
        dispatcher=stub_dispatcher, force_pure_python=True,
    )
    fn = dict(runner.steps).get("plan_judge")
    assert fn is not None
    return fn


def _plan_ref_dict(workflow_id):
    return {
        "id": str(uuid4()), "type": "PLAN", "workflow_id": str(workflow_id),
        "origin_squad": "planning", "target_squad": "hydra", "rigor": "standard",
        "goal_restatement": "ship the thing", "summary": "ship the thing",
        "open_questions": ["who owns rollback?"],
        "steps": [
            {"step_id": "a", "target_squad": "engineering", "envelope_type": "DEV_TASK",
             "description": "wire it up", "depends_on": []},
        ],
    }


class TestPlanGateOptions:
    def test_under_ceiling_offers_modify_plan(self, monkeypatch):
        monkeypatch.delenv("HYDRA_PLAN_MAX_REVISIONS", raising=False)
        node_plan_judge = _plan_judge_fn()
        wf = uuid4()
        state = HydraState(
            root_goal="x", workflow_id=wf, plan_status="drafted", plan_revision=1,
            plan_ref=_plan_ref_dict(wf),
        )
        patch = node_plan_judge(state)
        hitl = patch["pending_hitl"]
        assert hitl["options"] == ["approve", "reject", "modify-plan", "modify-budget"]
        assert hitl["default_option"] == "reject"
        assert hitl["plan_detail"]["revision_ceiling_reached"] is False
        assert hitl["plan_detail"]["open_question_count"] == 1

    def test_at_ceiling_offers_only_approve_and_abort(self, monkeypatch):
        monkeypatch.delenv("HYDRA_PLAN_MAX_REVISIONS", raising=False)
        node_plan_judge = _plan_judge_fn()
        wf = uuid4()
        # Default ceiling is 2; plan_revision=3 means 2 revisions consumed.
        state = HydraState(
            root_goal="x", workflow_id=wf, plan_status="drafted", plan_revision=3,
            plan_ref=_plan_ref_dict(wf),
        )
        patch = node_plan_judge(state)
        hitl = patch["pending_hitl"]
        assert hitl["options"] == ["approve", "abort"]
        assert hitl["default_option"] == "abort"
        assert hitl["plan_detail"]["revision_ceiling_reached"] is True
        assert "modify-plan" not in hitl["options"]
        assert "reject" not in hitl["options"]

    def test_initial_plan_revision_one_is_not_the_ceiling(self, monkeypatch):
        """plan_revision=1 (the very first authored plan, zero revisions
        consumed) must never itself read as ceiling-reached."""
        monkeypatch.delenv("HYDRA_PLAN_MAX_REVISIONS", raising=False)
        node_plan_judge = _plan_judge_fn()
        wf = uuid4()
        state = HydraState(
            root_goal="x", workflow_id=wf, plan_status="drafted", plan_revision=1,
            plan_ref=_plan_ref_dict(wf),
        )
        patch = node_plan_judge(state)
        hitl = patch["pending_hitl"]
        assert hitl["plan_detail"]["revision_ceiling_reached"] is False
        assert hitl["plan_detail"]["revisions_used"] == 0


# =========================================================================== #
# state.py's shared predicate -- proven directly, independent of the graph
# =========================================================================== #


class TestPlanRevisionCeilingPredicate:
    def test_ceiling_predicate_table(self):
        from hydra_core.state import plan_revision_ceiling_reached

        # (plan_revision, max_revisions, expected)
        cases = [
            (1, 2, False),   # initial plan, 0 revisions used
            (2, 2, False),   # 1 revision used
            (3, 2, True),    # 2 revisions used == ceiling
            (4, 2, True),    # past the ceiling
            (1, 1, False),
            (2, 1, True),
        ]
        for plan_revision, max_revisions, expected in cases:
            assert plan_revision_ceiling_reached(plan_revision, max_revisions) is expected, (
                f"plan_revision={plan_revision}, max_revisions={max_revisions}"
            )

    def test_plan_max_revisions_default_and_env_override(self, monkeypatch):
        from hydra_core.state import plan_max_revisions

        monkeypatch.delenv("HYDRA_PLAN_MAX_REVISIONS", raising=False)
        assert plan_max_revisions() == 2
        monkeypatch.setenv("HYDRA_PLAN_MAX_REVISIONS", "5")
        assert plan_max_revisions() == 5
        monkeypatch.setenv("HYDRA_PLAN_MAX_REVISIONS", "not-a-number")
        assert plan_max_revisions() == 2
        monkeypatch.setenv("HYDRA_PLAN_MAX_REVISIONS", "0")
        assert plan_max_revisions() == 2


# =========================================================================== #
# "bypassed" must never join the barrier -- the safety argument Task 1 relies on
# =========================================================================== #


def test_bypassed_is_not_a_plan_barrier_member():
    from hydra_core.state import _PLAN_BARRIER_STATES
    assert "bypassed" not in _PLAN_BARRIER_STATES
    assert "rejected" in _PLAN_BARRIER_STATES


# =========================================================================== #
# hydra.workflow.resume MCP surface -- modify-plan wiring + critique_ref
# validation (Task 2's four synced sites: server _RESUME_ACTIONS/_TOOL_
# SCHEMAS, toolshed's SCHEMA_OVERRIDES parity is covered separately by
# test_hydra_control_schema_parity.py).
# =========================================================================== #


class TestServerResumeSurface:
    def _server(self):
        import importlib
        return importlib.import_module("mcp_servers.hydra_control.server")

    def test_modify_plan_is_a_valid_resume_action(self):
        server = self._server()
        assert "modify-plan" in server._RESUME_ACTIONS

    def test_critique_ref_rejects_leading_dash_and_shell_metacharacters(self):
        server = self._server()
        assert server._CRITIQUE_REF_RE.match("docs/plans/critique.txt")
        assert server._CRITIQUE_REF_RE.match("repo:artifact:docs/plans/x.html")
        assert not server._CRITIQUE_REF_RE.match("-rf")
        assert not server._CRITIQUE_REF_RE.match("a; rm -rf /")
        assert not server._CRITIQUE_REF_RE.match("a`whoami`")

    def test_workflow_resume_rejects_invalid_critique_ref(self, monkeypatch):
        server = self._server()
        handlers = server._tool_handlers()
        resume = handlers["hydra.workflow.resume"]
        result = resume({
            "workflow_id": str(uuid4()),
            "action": "modify-plan",
            "critique_ref": "-rf /",
        })
        assert result == {"ok": False, "error": "invalid_critique_ref"}

    def test_launch_resume_forwards_critique_ref_to_cli(self, monkeypatch):
        server = self._server()
        captured: dict = {}

        def _fake_popen(cmd, *a, **k):
            captured["cmd"] = cmd
            class _P:
                pid = 1234
            return _P()

        monkeypatch.setattr(server, "_detached_allowed", lambda: True)
        monkeypatch.setattr(server.subprocess, "Popen", _fake_popen)
        wf = str(uuid4())
        server._launch_resume(wf, "modify-plan", None, critique_ref="docs/plans/note.txt")
        assert "--critique-ref" in captured["cmd"]
        idx = captured["cmd"].index("--critique-ref")
        assert captured["cmd"][idx + 1] == "docs/plans/note.txt"


# =========================================================================== #
# Hydra#69 defect A (primary): --gate-only approve at plan_gate must
# materialise the plan's TaskStates in the SAME sup.update_state call that
# clears the gate -- cli.py:2688-2718 used to return before sup.invoke, so
# node_plan_gate never ran and the checkpoint wedged at plan_status="judged"
# forever. Also covers the modify-budget re-file (A part 3) and the
# distinct `attended step` wedge terminals (A part 4).
# =========================================================================== #

_OPERATOR_ENV = {"HYDRA_OPERATOR_ID": "lebobo88", "HYDRA_OPERATOR_KEY": "test-key-material"}


def _set_known_operator(monkeypatch) -> None:
    for k, v in _OPERATOR_ENV.items():
        monkeypatch.setenv(k, v)


def _plan_gate_values(wf: str, *, plan_revision: int = 1) -> tuple[dict, str]:
    plan = {
        "id": str(uuid4()), "type": "PLAN", "origin_squad": "planning",
        "target_squad": "hydra", "workflow_id": wf, "rigor": "standard",
        "goal_restatement": "ship it", "summary": "ship it",
        "plan_revision": plan_revision,
        "steps": [
            {"step_id": "a", "target_squad": "engineering",
             "envelope_type": "DEV_TASK", "description": "wire it",
             "depends_on": [], "acceptance_criteria": ["it works"]},
        ],
    }
    placeholder_id = str(uuid4())
    pending = {"workflow_id": wf, "reason": "plan_approval", "gate_node": "plan_gate"}
    values = {
        "workflow_id": wf, "root_goal": "ship it", "phase": "approval",
        "pending_hitl": pending, "plan_status": "judged",
        "plan_revision": plan_revision, "plan_ref": plan,
        "plan_envelope_id": plan["id"],
        "plan_placeholder_task_ids": [placeholder_id],
        "plan_superseded_task_ids": [],
        "tasks": [{
            "task_id": placeholder_id, "owner_squad": "engineering",
            "description": "ship it", "status": "pending", "priority": "P2",
            "retries": 0, "depends_on": [], "plan_revision": 0,
        }],
        "hitl_history": [],
        "budget": {"budget_usd": 10.0, "spent_usd": 0.0},
    }
    return values, placeholder_id


class TestDefectAGateOnlyMaterialisation:
    def test_gate_only_approve_materialises_tasks_and_never_reopens_dispatch(
        self, monkeypatch, tmp_path,
    ):
        _set_known_operator(monkeypatch)
        wf = str(uuid4())
        values, placeholder_id = _plan_gate_values(wf)
        sup = _SimpleFakeSup(values["pending_hitl"], values)
        _patch_common(monkeypatch, sup)

        args = _resume_args(tmp_path, wf, "approve")
        args.gate_only = True
        rc = _cmd_resume_locked(args, tmp_path, wf, "approve", None)
        assert rc == 0

        # The write must be a pure checkpoint mutation with as_node=
        # "postcheck" -- so a later sup.invoke() can never pick up
        # plan_gate's static add_edge("plan_gate", "dispatch") edge and run
        # node_dispatch headlessly.
        assert sup.updates, "expected at least one update_state call"
        _last_patch, _last_as_node = sup.updates[-1]
        assert _last_as_node == "postcheck"
        assert sup.invoked == 0, "gate-only must never call sup.invoke"

        assert sup._values.get("plan_status") == "approved"
        assert sup._values.get("plan_superseded_task_ids") == [placeholder_id]
        materialised = _last_patch.get("tasks") or []
        assert len(materialised) == 1
        step_task = materialised[0]
        assert step_task.plan_step_id == "a"
        assert step_task.plan_revision == 1
        assert step_task.acceptance_criteria == ["it works"]
        assert step_task.envelope_type == "DEV_TASK"

        # Provenance stamped onto the hitl_history resolution record.
        last_resolution = sup._values["hitl_history"][-1]
        assert last_resolution["plan_revision"] == 1
        assert last_resolution.get("plan_envelope_id")

    def test_gate_only_approve_output_reports_plan_status_and_materialised_ids(
        self, monkeypatch, tmp_path, capsys,
    ):
        _set_known_operator(monkeypatch)
        wf = str(uuid4())
        values, _placeholder_id = _plan_gate_values(wf)
        sup = _SimpleFakeSup(values["pending_hitl"], values)
        _patch_common(monkeypatch, sup)

        args = _resume_args(tmp_path, wf, "approve")
        args.gate_only = True
        rc = _cmd_resume_locked(args, tmp_path, wf, "approve", None)
        out = json.loads(capsys.readouterr().out)
        assert rc == 0, out
        assert out["plan_status"] == "approved"
        assert out["materialised_task_ids"], out
        assert out["gate_only"] is True
        assert out["graph_reentered"] is False

    def test_double_approve_is_idempotent(self, monkeypatch, tmp_path):
        """A retried gate-only approve (the SAME gate resolved twice) must
        not double-materialise the step task."""
        _set_known_operator(monkeypatch)
        wf = str(uuid4())
        values, _placeholder_id = _plan_gate_values(wf)
        sup = _SimpleFakeSup(values["pending_hitl"], values)
        _patch_common(monkeypatch, sup)

        args = _resume_args(tmp_path, wf, "approve")
        args.gate_only = True
        rc1 = _cmd_resume_locked(args, tmp_path, wf, "approve", None)
        assert rc1 == 0
        first_tasks = list(sup._values.get("tasks") or [])
        assert len(first_tasks) == 1

        # Simulate the checkpoint carrying the materialised task forward
        # (a real checkpoint's `tasks` channel is append-only) and re-raise
        # the SAME gate, exactly as a retried gate-only approve would see it.
        sup._values["tasks"] = list(values["tasks"]) + [
            {
                "task_id": str(first_tasks[0].task_id),
                "owner_squad": first_tasks[0].owner_squad,
                "description": first_tasks[0].description,
                "status": "pending", "priority": "P2", "retries": 0,
                "depends_on": [], "plan_step_id": "a", "plan_revision": 1,
            }
        ]
        sup._values["pending_hitl"] = values["pending_hitl"]
        sup._values["plan_status"] = "judged"

        rc2 = _cmd_resume_locked(args, tmp_path, wf, "approve", None)
        assert rc2 == 0
        _second_patch, _ = sup.updates[-1]
        assert "tasks" not in _second_patch, (
            "a retried approve against a checkpoint that already carries "
            "this revision's step task must not re-materialise it"
        )


class TestDefectAModifyBudgetRefilesGate:
    def test_modify_budget_at_plan_gate_does_not_clear_gate(self, monkeypatch, tmp_path):
        _set_known_operator(monkeypatch)
        wf = str(uuid4())
        values, _placeholder_id = _plan_gate_values(wf)
        sup = _SimpleFakeSup(values["pending_hitl"], values)
        _patch_common(monkeypatch, sup)

        args = _resume_args(tmp_path, wf, "modify-budget", option="99.0")
        args.gate_only = True
        rc = _cmd_resume_locked(args, tmp_path, wf, "modify-budget", "99.0")
        assert rc == 0
        assert sup._values.get("pending_hitl") is not None, (
            "modify-budget at plan_gate must re-file the gate -- the plan "
            "still needs a genuine approve"
        )
        assert sup._values["pending_hitl"].get("gate_node") == "plan_gate"
        assert sup._values.get("plan_status") == "judged", (
            "modify-budget must not itself approve the plan"
        )
        assert sup._values["budget"]["budget_usd"] == 99.0


class TestDefectAAttendedStepWedgeTerminals:
    """A checkpoint hand-constructed in the exact wedge shape (barrier
    active, no gate pending, nothing selectable) must surface a distinct,
    honest terminal instead of silently falling through to
    ready_to_finalize/tasks_pending."""

    def _wedge_values(self, wf: str, *, approved_history: bool) -> dict:
        history = []
        if approved_history:
            history = [{
                "resolution": "approve", "gate_node": "plan_gate",
                "plan_revision": 1,
            }]
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
        from hydra_core.cli import _cmd_attended_step
        sup = self._AttendedFakeSup(values)
        monkeypatch.setattr("hydra_core.cli._attended_live_dispatcher",
                            lambda *a, **k: self._NopDispatcher())
        monkeypatch.setattr("hydra_core.supervisor.build_supervisor",
                            lambda **k: sup)
        monkeypatch.setattr("hydra_core.squad_loader.discover_squads",
                            lambda *a, **k: {})
        rc = _cmd_attended_step(argparse.Namespace(
            project=str(tmp_path), workflow_id=wf, verbose=False))
        out = capsys.readouterr().out
        assert rc == 0, f"attended step failed: {out[:400]}"
        return json.loads(out)

    def test_unresolved_gate_reports_plan_gate_unresolved(self, monkeypatch, tmp_path, capsys):
        wf = str(uuid4())
        values = self._wedge_values(wf, approved_history=False)
        out = self._step(tmp_path, wf, values, monkeypatch, capsys)
        assert out["status"] == "plan_gate_unresolved"
        assert out["ok"] is False

    def test_approved_but_never_materialised_reports_distinct_status(
        self, monkeypatch, tmp_path, capsys,
    ):
        wf = str(uuid4())
        values = self._wedge_values(wf, approved_history=True)
        out = self._step(tmp_path, wf, values, monkeypatch, capsys)
        assert out["status"] == "plan_approved_not_materialised"
        assert out["ok"] is False
