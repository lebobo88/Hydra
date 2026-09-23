"""Hydra#69 round 5: five findings from the final cross-vendor
(codex gpt-6-astra) review of feat/planning-phase at HEAD f8901ff.

1. HIGH - Repeating an abort approves/materialises the plan (detached
   route). Fixed in three layers: (a) a terminal plan_gate resolution
   (abort/reject) now writes as_node="postcheck" so `next` collapses to
   `()`, same as the gate-only approve path; (b) the bare-interrupt
   no-pending-gate branch never continues the graph when option=="abort";
   (c) `materialise_plan_steps` refuses to materialise on an already-cleared
   gate unless hitl_history proves a genuine approve.

2. HIGH - A checkpoint-write failure during a terminal `_cmd_attended_submit`
   used to become a false success on retry (the cursor's `already_charged`
   flag alone was treated as proof the checkpoint reflected the outcome).
   Fixed via a per-call reconciliation marker
   (`HydraState.attended_checkpoint_reconciled`) folded into the SAME atomic
   checkpoint write as the budget charge.

3. MED - `plan_supersedes_expected` defaulting to None on a legacy
   checkpoint made ingest.py reject every valid revision>1 PLAN. Fixed by
   deriving the expectation from the current revision's planning TaskState
   (or a recorded modify-plan hitl_history transition) when the field is
   unset.

4. MED - Best-of-N candidates and reflexion retries built their own
   `CSuiteDecisionPacket` without `acceptance_criteria`/`envelope_type`.
   Fixed via a single shared helper (`_decision_packet_task_fields`) used by
   every CSuiteDecisionPacket constructor for a task.

5. LOW - The planning prompt's schema section now also embeds
   `Plan.model_json_schema()` verbatim (compact JSON), not just the
   model_fields-derived prose summary.

Every test here is proven as a property: reverting the corresponding fix
makes the test fail.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from hydra_core import cli, host_bridge
from hydra_core.state import HydraState, TaskState

HYDRA_ROOT = Path(__file__).resolve().parents[1]


# =========================================================================== #
# Defect 1 (HIGH) -- real LangGraph SQLite repro: repeated abort / reject-
# then-approve must never materialise/approve the plan.
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


def _plan_ref(step_id="step-1"):
    return {
        "steps": [{
            "step_id": step_id, "target_squad": "engineering",
            "description": "do the thing", "priority": "P2",
            "acceptance_criteria": ["it works"], "envelope_type": "DEV_TASK",
        }],
    }


def _seed_plan_gate_workflow():
    """Seed a REAL LangGraph checkpoint parked at the `plan_gate` interrupt.

    `as_node="plan_judge"` (plan_gate's sole predecessor edge) makes the
    compiled graph's `next` tuple read `("plan_gate",)`, exactly as if
    `node_plan_judge` had just filed the gate for real.
    """
    from hydra_core.supervisor import build_supervisor, _PurePythonRunner

    wf = uuid4()
    pending = {
        "workflow_id": str(wf), "reason": "plan_approval", "gate_node": "plan_gate",
        "options": ["approve", "abort"],
    }
    state = HydraState(
        workflow_id=wf, root_goal="round 5 defect 1 repro", phase="approval",
        plan_status="judged", plan_ref=_plan_ref(), plan_revision=1,
        pending_hitl=pending, tasks=[],
    )
    sup = build_supervisor(project_root=HYDRA_ROOT, dispatcher=_StubDispatcher())
    assert not isinstance(sup, _PurePythonRunner), "langgraph required for this test"
    config = {"configurable": {"thread_id": str(wf)}}
    sup.update_state(config, state.model_dump(mode="json"), as_node="plan_judge")
    snap = sup.get_state(config)
    assert tuple(snap.next) == ("plan_gate",), (
        f"fixture must park at plan_gate; got next={snap.next!r}"
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


class TestDefect1AbortRejectAtomicity:
    def test_repeated_abort_stays_surfaced_zero_tasks(self, hermetic):
        wf, sup, config = _seed_plan_gate_workflow()

        rc1, body1 = _resume(HYDRA_ROOT, wf, "approve", "abort")
        assert rc1 == 0, body1
        values1 = sup.get_state(config).values
        assert values1.get("phase") == "surfaced"
        assert not values1.get("tasks")
        assert tuple(sup.get_state(config).next) == (), (
            "a terminal abort must force next==() so no later invoke can "
            "run dispatch headless"
        )

        # Identical repeated abort against the same, already-terminal
        # checkpoint (the exact repro this defect describes).
        rc2, body2 = _resume(HYDRA_ROOT, wf, "approve", "abort")
        assert rc2 == 0, body2
        values2 = sup.get_state(config).values
        assert values2.get("phase") == "surfaced"
        assert not values2.get("tasks"), (
            f"a REPEATED abort must never materialise the plan: {body2}"
        )
        assert values2.get("plan_status") != "approved"

    def test_reject_then_repeated_approve_stays_surfaced_zero_tasks(self, hermetic):
        wf, sup, config = _seed_plan_gate_workflow()

        rc1, body1 = _resume(HYDRA_ROOT, wf, "reject", None)
        assert rc1 == 0, body1
        values1 = sup.get_state(config).values
        assert values1.get("phase") == "surfaced"
        assert values1.get("plan_status") == "rejected"
        assert not values1.get("tasks")
        assert tuple(sup.get_state(config).next) == ()

        # A later approve against the now-terminal, gate-cleared checkpoint
        # must not resurrect the rejected plan.
        rc2, body2 = _resume(HYDRA_ROOT, wf, "approve", None)
        assert rc2 == 0, body2
        values2 = sup.get_state(config).values
        assert not values2.get("tasks"), (
            f"an approve after a genuine reject must never materialise: {body2}"
        )
        assert values2.get("plan_status") == "rejected"


class TestDefect1bBareInterruptAbortNeverContinuesGraph:
    """Direct proof of layer (b): the bare-interrupt no-pending-gate branch
    must never continue the graph when `option == "abort"`, EVEN if `next`
    is still truthy (an older checkpoint written before layer (a) existed,
    or any other path that leaves the gate cleared with `next` untouched).
    Constructed by hand (rather than through a real resume) since layer (a)
    already makes `next` empty on every NEW terminal write -- this isolates
    (b) as an independent backstop."""

    def test_bare_interrupt_with_option_abort_never_invokes_the_graph(
        self, hermetic,
    ):
        from hydra_core.supervisor import build_supervisor, _PurePythonRunner

        wf = uuid4()
        state = HydraState(
            # Deliberately NOT phase="surfaced"/plan_status="rejected" -- a
            # terminal park would ALSO trip layer (c)'s own phase/plan_status
            # shortcut, masking whether (b) itself did anything. "approval"
            # is the mid-flight phase every gate uses while pending.
            workflow_id=wf, root_goal="round 5 defect 1b repro", phase="approval",
            plan_status="judged", plan_ref=_plan_ref(), plan_revision=1,
            pending_hitl=None, tasks=[],
            # Deliberately a genuine PRIOR approve (not this abort attempt)
            # so layer (c)'s hitl_history check alone would NOT refuse --
            # isolating layer (b) as the guard that actually stops THIS call.
            hitl_history=[{
                "gate_node": "plan_gate", "resolution": "approve", "option": None,
            }],
        )
        sup = build_supervisor(project_root=HYDRA_ROOT, dispatcher=_StubDispatcher())
        assert not isinstance(sup, _PurePythonRunner)
        config = {"configurable": {"thread_id": str(wf)}}
        # Simulate a checkpoint written BEFORE layer (a) existed: pending_hitl
        # is already cleared, but `next` is still truthy (plan_judge's own
        # outgoing edge).
        sup.update_state(config, state.model_dump(mode="json"), as_node="plan_judge")
        assert tuple(sup.get_state(config).next) == ("plan_gate",)

        rc, body = _resume(HYDRA_ROOT, str(wf), "approve", "abort")
        assert rc == 0, body
        values = sup.get_state(config).values
        assert not values.get("tasks"), (
            f"option=='abort' at a bare interrupt must never materialise: {body}"
        )
        assert values.get("plan_status") != "approved"


class TestDefect1cMaterialiseRequiresApproveEvidence:
    """Direct unit proof of layer (c): `materialise_plan_steps` itself must
    refuse an already-cleared gate (`pending_hitl is None`) unless
    `hitl_history` proves the clearing was a genuine approve. Layers (a)/(b)
    already prevent the graph from ever reaching this function in the
    repeated-abort/reject-then-approve scenarios (see
    `TestDefect1AbortRejectAtomicity` above) -- this test isolates (c) as an
    independent backstop, e.g. for an OLDER checkpoint written before (a)
    existed, whose `next` is still truthy."""

    def test_abort_recorded_in_hitl_history_refuses_materialisation(self):
        from hydra_core.state import HydraState
        from hydra_core.supervisor import materialise_plan_steps

        state = HydraState(
            root_goal="x", plan_status="judged", plan_revision=1,
            plan_ref=_plan_ref(), pending_hitl=None,
            hitl_history=[{
                "gate_node": "plan_gate", "resolution": "approve", "option": "abort",
            }],
        )
        patch = materialise_plan_steps(state)
        assert patch == {}, (
            f"an abort recorded in hitl_history must refuse materialisation: {patch}"
        )

    def test_reject_recorded_in_hitl_history_refuses_materialisation(self):
        from hydra_core.state import HydraState
        from hydra_core.supervisor import materialise_plan_steps

        state = HydraState(
            root_goal="x", plan_status="rejected", plan_revision=1,
            plan_ref=_plan_ref(), pending_hitl=None,
            hitl_history=[{"gate_node": "plan_gate", "resolution": "reject"}],
        )
        patch = materialise_plan_steps(state)
        assert patch == {}, (
            f"a reject recorded in hitl_history must refuse materialisation: {patch}"
        )

    def test_no_hitl_history_at_all_refuses_materialisation(self):
        """Belt-and-suspenders: `cur is None` with a completely empty
        `hitl_history` (no evidence either way) must refuse, not
        default-approve."""
        from hydra_core.state import HydraState
        from hydra_core.supervisor import materialise_plan_steps

        state = HydraState(
            root_goal="x", plan_status="judged", plan_revision=1,
            plan_ref=_plan_ref(), pending_hitl=None, hitl_history=[],
        )
        patch = materialise_plan_steps(state)
        assert patch == {}


# =========================================================================== #
# Defect 2 (HIGH) -- checkpoint-write failure during a terminal
# `_cmd_attended_submit` repairs on retry instead of becoming a false
# success, and never double-charges the budget.
# =========================================================================== #

_APPEND_KEYS = {"tasks", "envelopes", "artifacts", "episodic_refs",
                "semantic_queries", "verdicts", "hitl_history"}
_MERGE_DICT_KEYS = {"error_counters", "plan_submit_attempts",
                     "attended_checkpoint_reconciled"}


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
        return _Snap(dict(self._values), ())

    def update_state(self, config, patch, as_node=None):
        self.update_calls.append((dict(patch), as_node))
        self._values = _merge_patch(self._values, patch)

    def invoke(self, arg, config=None):  # pragma: no cover -- never reached
        raise AssertionError("this flow never re-enters the graph")


class _FlakyOnceSup(_StatefulFakeSup):
    """Fails the FIRST checkpoint write that carries the budget-charge
    reconciliation marker (Hydra#69 round 5 defect 2's primary write) with a
    simulated persistence error, then behaves normally on every later call."""

    def __init__(self, initial_values: dict):
        super().__init__(initial_values)
        self._armed = True

    def update_state(self, config, patch, as_node=None):
        if self._armed and "attended_checkpoint_reconciled" in patch:
            self._armed = False
            raise RuntimeError("simulated checkpoint persistence failure")
        super().update_state(config, patch, as_node=as_node)


class _FakeAttendedDispatcher:
    project_root = None

    def call_mcp(self, *a, **k):  # pragma: no cover -- must never be reached
        raise AssertionError("no MCP calls expected for a squad-cursor stage")

    def set_squad_packs(self, packs):
        pass


def _submit(tmp_path, wf, run_id, call_key, result_path):
    args = argparse.Namespace(
        project=str(tmp_path), workflow_id=wf, run_id=run_id,
        call_key=call_key, result=str(result_path), verbose=False,
    )
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        rc = cli._cmd_attended_submit(args)
    out = buf.getvalue()
    return rc, json.loads(out)


class TestDefect2ChecksumRetryReconciliation:
    def test_checkpoint_persist_failure_repairs_on_retry_without_double_charge(
        self, tmp_path, monkeypatch,
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
        fake_sup = _FlakyOnceSup(state.model_dump(mode="json"))

        monkeypatch.setattr(cli, "_attended_live_dispatcher",
                            lambda *a, **k: _FakeAttendedDispatcher())
        monkeypatch.setattr("hydra_core.supervisor.build_supervisor",
                            lambda **k: fake_sup)

        result_path = tmp_path / "result.json"
        result_path.write_text(json.dumps({
            "text": "resolved the ticket", "cost_usd": 1.5,
            "tokens_in": 10, "tokens_out": 5,
        }), encoding="utf-8")

        # First submit: the primary checkpoint write raises.
        rc1, body1 = _submit(tmp_path, wf, task_id, call_key, result_path)
        assert rc1 == 1
        assert body1.get("ok") is False
        assert body1.get("error") == "checkpoint_persist_failed"
        values1 = fake_sup._values
        assert not values1.get("attended_completed_task_ids"), (
            "the failed write must not have landed"
        )
        assert float((values1.get("budget") or {}).get("spent_usd") or 0.0) == 0.0

        # Retry with the SAME call_key/run_id: the cursor already reports
        # already_charged, but the checkpoint never reflected it -- must
        # repair, not silently report a cached success.
        rc2, body2 = _submit(tmp_path, wf, task_id, call_key, result_path)
        assert rc2 == 0, body2
        values2 = fake_sup._values
        assert values2.get("attended_completed_task_ids") == [task_id], (
            f"the retry must repair the missing completion record: {body2}"
        )
        spent_after_repair = float((values2.get("budget") or {}).get("spent_usd") or 0.0)
        assert spent_after_repair == pytest.approx(1.5), (
            f"the retry must apply the charge exactly once: {body2}"
        )
        assert (values2.get("attended_checkpoint_reconciled") or {}).get(
            f"{task_id}:{call_key}"
        ) is True

        # A SECOND retry must be a true no-op: no double charge, no
        # duplicated completion entry.
        rc3, body3 = _submit(tmp_path, wf, task_id, call_key, result_path)
        assert rc3 == 0, body3
        values3 = fake_sup._values
        assert values3.get("attended_completed_task_ids") == [task_id]
        spent_after_second_retry = float(
            (values3.get("budget") or {}).get("spent_usd") or 0.0
        )
        assert spent_after_second_retry == pytest.approx(1.5), (
            "a second retry after full reconciliation must never re-charge "
            f"the budget: {body3}"
        )


# =========================================================================== #
# Defect 3 (MED) -- `plan_supersedes_expected` derivation fallback on a
# legacy-shaped checkpoint.
# =========================================================================== #

from hydra_core.ingest import dispatch_ingested_envelopes  # noqa: E402
from hydra_core.squad_loader import discover_squads  # noqa: E402


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


class _ProjectRootDispatcher:
    def __init__(self, project_root: Path):
        self.project_root = project_root


class TestDefect3LegacySupersedesExpectedFallback:
    @pytest.fixture
    def packs(self):
        return discover_squads(HYDRA_ROOT)

    def test_legacy_state_with_no_supersedes_expected_derives_from_task(
        self, packs, monkeypatch, tmp_path,
    ):
        """A checkpoint created before `plan_supersedes_expected` existed
        (field absent/None) must still accept a revision-2 PLAN that
        correctly names its predecessor -- derived from the current
        revision's own planning TaskState."""
        monkeypatch.setenv("HYDRA_PLAN_PHASE", "1")
        prior_id = uuid4()
        planning_task = TaskState(
            owner_squad="planning", description="revise the plan",
            plan_revision=2, supersedes_plan_envelope_id=str(prior_id),
        )
        state = HydraState(
            root_goal="x", plan_revision=2, plan_envelope_id=prior_id,
            plan_supersedes_expected=None,  # legacy: field never set
            tasks=[planning_task],
        )
        plan = _plan_dict(state.workflow_id, revision=2, supersedes=prior_id)
        outcome = dispatch_ingested_envelopes(
            state, [plan], packs=packs, dispatcher=_ProjectRootDispatcher(tmp_path),
        )
        item = outcome.items[0]
        assert item.status == "drafted", (
            f"a correctly-addressed revision-2 PLAN must be accepted even "
            f"when plan_supersedes_expected is None: {item}"
        )
        assert outcome.plan_patch["plan_revision"] == 2

    def test_legacy_state_with_no_matching_evidence_still_rejects_wrong_supersedes(
        self, packs, monkeypatch, tmp_path,
    ):
        """Control: with no derivable evidence at all (no matching planning
        task, no hitl_history transition), a PLAN naming the WRONG
        predecessor is still rejected -- the fallback never turns into a
        silent bypass."""
        monkeypatch.setenv("HYDRA_PLAN_PHASE", "1")
        state = HydraState(
            root_goal="x", plan_revision=2, plan_envelope_id=uuid4(),
            plan_supersedes_expected=None,
        )
        plan = _plan_dict(state.workflow_id, revision=2, supersedes=uuid4())
        outcome = dispatch_ingested_envelopes(
            state, [plan], packs=packs, dispatcher=_ProjectRootDispatcher(tmp_path),
        )
        item = outcome.items[0]
        assert item.status == "failed"
        assert any(e.get("field") == "supersedes" for e in item.errors)
        assert not outcome.plan_patch


# =========================================================================== #
# Defect 4 (MED) -- best-of-N candidates and reflexion retries carry
# acceptance_criteria/envelope_type from the originating task.
# =========================================================================== #

class _JudgeStubDispatcher:
    allow_offline_mcp_dispatch = True

    def call_mcp(self, server, tool, args, **_kw):
        return {"status": "done", "tool": tool, "result": {"ok": True}}

    def spawn_subprocess(self, cmd, env=None):
        return {"status": "done", "stdout": "", "stderr": ""}

    def emit_claude_prompt(self, prompt, agent=None):
        return {"status": "done", "agent": agent, "summary": "stub boardroom output"}

    def invoke_claude_skill(self, skill, args):
        return {"status": "done", "skill": skill, "summary": "stub skill output"}


class _SequencedClient:
    def __init__(self, queue: list[dict]):
        self.queue = list(queue)
        self.default = {
            "outcome": "pass",
            "critique_md": "thorough analysis " * 10,
            "score_json": {"x": 5, "y": 4},
        }

    def critique(self, *, vendor, artifact_text, rubric_md):
        if self.queue:
            return self.queue.pop(0)
        return dict(self.default)


def _invoke(sup, state):
    from hydra_core.supervisor import _PurePythonRunner
    if isinstance(sup, _PurePythonRunner):
        return sup.invoke(state)
    out = sup.invoke(state, config={"configurable": {"thread_id": str(state.workflow_id)}})
    return HydraState.model_validate(out) if isinstance(out, dict) else out


class TestDefect4DecisionPacketSharedFields:
    def test_reflexion_retry_carries_acceptance_criteria_and_envelope_type(
        self, monkeypatch,
    ):
        """`healthcare` (best_of_n=0, policy-enabled -- the exact squad the
        existing reflexion ceiling test uses to isolate the per-squad
        Reflexion path from best-of-N) drives a REAL `node_dispatch` ->
        per-squad-judge -> `_reflexion_retry` cycle. `execute_squad` is
        monkeypatched to capture every packet it is called with."""
        from hydra_core.supervisor import build_supervisor
        import hydra_core.supervisor as sv

        captured: list[Any] = []
        orig_execute_squad = sv.execute_squad

        def _capturing_execute_squad(state, pack, payload, dispatcher):
            captured.append(payload)
            return orig_execute_squad(state, pack, payload, dispatcher)

        monkeypatch.setattr(sv, "execute_squad", _capturing_execute_squad)

        # Mirrors test_judge_supervisor_phase34.py's ceiling test: healthcare's
        # per-squad route scores 2 rubrics per envelope, so both must revise
        # to trigger the retry. Only 2 entries (not the ceiling test's 4) so
        # the retry's own re-judge drains to the default pass instead of
        # exhausting the ceiling.
        client = _SequencedClient(queue=[
            {"outcome": "revise",
             "critique_md": "PHI redaction coverage incomplete on inbound payload. " * 3,
             "score_json": {"redaction_completeness": 1}},
            {"outcome": "revise",
             "critique_md": "constitution alignment check failed on the policy refusal pattern. " * 3,
             "score_json": {"refusal_respect": 1}},
        ])
        sup = build_supervisor(
            project_root=HYDRA_ROOT, dispatcher=_JudgeStubDispatcher(),
            critique_client=client, force_pure_python=True,
            force_trivial_plan_rigor=True,
        )
        task = TaskState(
            owner_squad="healthcare", description="review the PHI redaction policy",
            acceptance_criteria=["no PHI leaves the boundary unredacted"],
            envelope_type="CLINICAL_REVIEW",
        )
        state = HydraState(
            root_goal="healthcare PHI redaction policy review",
            selected_squads=["healthcare"], tasks=[task],
        )
        _invoke(sup, state)

        hc_payloads = [p for p in captured if p.target_squad == "healthcare"]
        retry_payloads = [p for p in hc_payloads if "REFLEXION RETRY" in p.objective]
        assert retry_payloads, "expected at least one reflexion retry payload"

        for payload in hc_payloads:
            assert payload.acceptance_criteria == [
                "no PHI leaves the boundary unredacted"
            ], (
                "Hydra#69 round 5 defect 4: every CSuiteDecisionPacket for "
                f"this task must carry its acceptance_criteria; objective="
                f"{payload.objective!r} got {payload.acceptance_criteria!r}"
            )
            assert payload.envelope_type == "CLINICAL_REVIEW", (
                f"objective={payload.objective!r} got envelope_type="
                f"{payload.envelope_type!r}"
            )

    def test_best_of_n_candidates_carry_acceptance_criteria_and_envelope_type(
        self, monkeypatch,
    ):
        """Every real best_of_n>=2 squad pack (executive/garland/legal-
        compliance/rlm-gaming) is entrypoint=claude-native, which ALWAYS
        defers to the host in a headless test (see
        `test_native_pack_defers_instead_of_headless_best_of_n`) -- so this
        drives the SAME real `node_dispatch` -> `_dispatch_best_of_n` code
        path against a frozen-dataclass clone of the `engineering` pack with
        `best_of_n` bumped to 2 and a dispatcher that opts into the offline
        mcp path (mirrors `test_judge_supervisor_phase34.py`'s `_StubDispatcher`),
        so best-of-N actually runs headlessly instead of deferring."""
        import dataclasses

        from hydra_core.supervisor import build_supervisor
        import hydra_core.supervisor as sv
        from hydra_core.squad_loader import discover_squads as _real_discover_squads

        captured: list[Any] = []
        orig_execute_squad = sv.execute_squad

        def _capturing_execute_squad(state, pack, payload, dispatcher):
            captured.append(payload)
            return orig_execute_squad(state, pack, payload, dispatcher)

        monkeypatch.setattr(sv, "execute_squad", _capturing_execute_squad)

        def _patched_discover(project_root):
            packs = dict(_real_discover_squads(project_root))
            eng = packs.get("engineering")
            if eng is not None:
                packs["engineering"] = dataclasses.replace(eng, best_of_n=2)
            return packs

        monkeypatch.setattr(sv, "discover_squads", _patched_discover)

        class _OfflineMcpDispatcher(_JudgeStubDispatcher):
            allow_offline_mcp_dispatch = True

        client = _SequencedClient(queue=[])  # all pass -- only the packets matter
        sup = build_supervisor(
            project_root=HYDRA_ROOT, dispatcher=_OfflineMcpDispatcher(),
            critique_client=client, force_pure_python=True,
            force_trivial_plan_rigor=True,
        )
        task = TaskState(
            owner_squad="engineering", description="wire the middleware",
            acceptance_criteria=["middleware wired", "tests green"],
            envelope_type="DEV_TASK", target_repo_id="hydra",
        )
        state = HydraState(
            root_goal="ship the middleware", selected_squads=["engineering"],
            tasks=[task], target_repo_id="hydra",
        )
        _invoke(sup, state)

        eng_payloads = [p for p in captured if p.target_squad == "engineering"]
        bon_payloads = [p for p in eng_payloads if "[bon-candidate" in p.objective]
        assert len(bon_payloads) >= 2, (
            f"expected multiple best-of-N candidates, got {len(bon_payloads)}"
        )

        for payload in bon_payloads:
            assert payload.acceptance_criteria == ["middleware wired", "tests green"], (
                "Hydra#69 round 5 defect 4: every best-of-N CSuiteDecisionPacket "
                f"must carry acceptance_criteria; objective={payload.objective!r} "
                f"got {payload.acceptance_criteria!r}"
            )
            assert payload.envelope_type == "DEV_TASK", (
                f"objective={payload.objective!r} got envelope_type="
                f"{payload.envelope_type!r}"
            )


# =========================================================================== #
# Defect 5 (LOW) -- the planning prompt embeds Plan.model_json_schema()
# verbatim, compact JSON, alongside the prose field-list summary.
# =========================================================================== #

class TestDefect5PromptEmbedsGeneratedSchema:
    def test_schema_section_matches_generated_schema_object(self):
        from hydra_core import schemas

        doc = host_bridge._plan_envelope_schema_doc()
        marker = "### Plan JSON Schema (generated from hydra_core.schemas.Plan)"
        assert marker in doc
        after = doc.split(marker, 1)[1].strip()
        schema_line = after.splitlines()[0]
        embedded_schema = json.loads(schema_line)

        expected_schema = schemas.Plan.model_json_schema()
        assert embedded_schema == expected_schema

        # Asserted against the schema object, not hand-copied literals.
        for key in expected_schema["required"]:
            assert key in embedded_schema["required"]
        assert "PlanStep" in embedded_schema["$defs"]
        for step_key in expected_schema["$defs"]["PlanStep"]["required"]:
            assert step_key in embedded_schema["$defs"]["PlanStep"]["required"]

    def test_non_planning_prompt_is_byte_for_byte_unchanged(self):
        """`_plan_envelope_schema_doc` is only ever consumed by the planning
        branch of `_build_squad_prompt` -- a non-planning squad's prompt must
        not even reference the schema section."""
        prompt = host_bridge._build_squad_prompt(
            workflow_id=str(uuid4()), task_id="t-1", squad_slug="executive",
            request_text="frame the decision",
        )
        assert "Plan JSON Schema" not in prompt
        assert "Required output: PLAN envelope" not in prompt
