"""Hydra#69 part 2 (defects C and G).

C. A rejected PLAN must NOT consume the planning leg: `_cmd_attended_submit`
   only marks a `planning`-owned task attended-complete/done AFTER the PLAN it
   emitted is durably accepted (ingested, artifact written, graph re-entered).
   Every rejection kind (missing PLAN, schema-invalid PLAN,
   HYDRA_PLAN_PHASE disabled, artifact write failure, re-entry failure)
   leaves the task open, returns top-level status "plan_rejected" with a
   structured `plan_rejection`, and bumps a durable per-task attempt counter
   (`state.plan_submit_attempts`) so the next `hydra step` re-issues a fresh
   `squad-{task_id}-{attempt}` call_key. A stale call_key against a re-issued
   cursor is refused with a structured `stale_attempt` marker. Budget
   charging stays exactly-once per cursor and independent of acceptance.

G. `dispatch_ingested_envelopes`'s PLAN branch validates the submitted PLAN
   against `HydraState` BEFORE adopting its `plan_revision`: `workflow_id`
   must match, `plan_revision` must equal the expected revision for the
   revision currently being authored, and (once a plan has already been
   authored once, i.e. expected revision > 1) `supersedes` must name the
   prior `plan_envelope_id`. A mismatch is a structured `failed` item whose
   claim is released via the existing `_ingest_item_should_release_claim`
   path, never a silent adoption that could roll `state.plan_revision`
   backward.

Every test here is proven as a property: reverting the corresponding fix
makes the test fail (see the MUTATION PROOF note on each class).
"""
from __future__ import annotations

import argparse
import json as _json
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from hydra_core import cli
from hydra_core.cli import _classify_plan_rejection
from hydra_core.ingest import dispatch_ingested_envelopes
from hydra_core.squad_loader import discover_squads
from hydra_core.state import HydraState, TaskState

HYDRA_ROOT = Path(__file__).resolve().parents[1]


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
    """Minimal dispatcher stand-in exposing only what the PLAN branch reads."""

    def __init__(self, project_root: Path):
        self.project_root = project_root


# =========================================================================== #
# _classify_plan_rejection -- pure, exercised directly.
# =========================================================================== #

class TestClassifyPlanRejection:
    def test_missing_plan_when_nothing_emitted(self):
        result = _classify_plan_rejection([], [], {"status": "complete"})
        assert result["reason"] == "missing_plan"

    def test_missing_plan_when_emitted_has_no_plan_item(self):
        result = _classify_plan_rejection(
            [{"type": "HANDOFF"}], [{"envelope_type": "HANDOFF", "status": "done"}],
            {"status": "complete"},
        )
        assert result["reason"] == "missing_plan"

    def test_plan_phase_disabled(self):
        outcomes = [{"envelope_type": "PLAN", "status": "plan_phase_disabled",
                      "detail": "HYDRA_PLAN_PHASE is off"}]
        result = _classify_plan_rejection([{"type": "PLAN"}], outcomes, {"status": "complete"})
        assert result["reason"] == "plan_phase_disabled"

    def test_invalid_plan_schema_failure(self):
        outcomes = [{"envelope_type": "PLAN", "status": "failed",
                      "detail": "invalid envelope: bad field",
                      "errors": [{"field": "rigor", "msg": "required"}]}]
        result = _classify_plan_rejection([{"type": "PLAN"}], outcomes, {"status": "complete"})
        assert result["reason"] == "invalid_plan"
        assert result["errors"]

    def test_artifact_write_failed(self):
        outcomes = [{"envelope_type": "PLAN", "status": "failed",
                      "detail": "plan artifact write failed: disk full", "errors": []}]
        result = _classify_plan_rejection([{"type": "PLAN"}], outcomes, {"status": "complete"})
        assert result["reason"] == "artifact_write_failed"

    def test_plan_reentry_failed_reads_off_res(self):
        res = {"status": "plan_reentry_failed", "plan_reentry_error": "checkpoint write failed"}
        result = _classify_plan_rejection([{"type": "PLAN"}], [], res)
        assert result["reason"] == "plan_reentry_failed"
        assert "checkpoint write failed" in result["detail"]


# =========================================================================== #
# Defect G -- ingest.py's PLAN branch validates against state before adopting
# plan_revision. MUTATION PROOF: delete the workflow_id/plan_revision/
# supersedes checks this fix adds (the block right after the
# `_plan_phase_enabled()` gate in ingest.py) and every test below fails
# (each mismatched PLAN would instead reach "drafted").
# =========================================================================== #

class TestIngestPlanStateValidationDefectG:
    @pytest.fixture
    def packs(self):
        return discover_squads(HYDRA_ROOT)

    def test_wrong_workflow_id_is_rejected(self, packs, monkeypatch, tmp_path):
        monkeypatch.setenv("HYDRA_PLAN_PHASE", "1")
        state = HydraState(root_goal="x")
        plan = _plan_dict(uuid4())  # a DIFFERENT workflow_id than state's
        outcome = dispatch_ingested_envelopes(
            state, [plan], packs=packs, dispatcher=_ProjectRootDispatcher(tmp_path),
        )
        item = outcome.items[0]
        assert item.status == "failed"
        assert any(e.get("field") == "workflow_id" for e in item.errors)
        assert not outcome.plan_patch
        assert state.plan_revision == 0, "a rejected PLAN must never advance state"
        # G's claim-release requirement: a "failed" item WITH structured
        # errors must be releasable (same shared decision function C's fix
        # relies on).
        from hydra_core.cli import _ingest_item_should_release_claim
        assert _ingest_item_should_release_claim(item)

    def test_wrong_revision_is_rejected_not_adopted(self, packs, monkeypatch, tmp_path):
        """The exact regression G describes: a resubmission with the schema
        DEFAULT revision (1) while the engine expects a later revision must
        not roll state.plan_revision backward."""
        monkeypatch.setenv("HYDRA_PLAN_PHASE", "1")
        state = HydraState(root_goal="x", plan_revision=3, plan_envelope_id=uuid4())
        plan = _plan_dict(state.workflow_id)  # default plan_revision=1
        outcome = dispatch_ingested_envelopes(
            state, [plan], packs=packs, dispatcher=_ProjectRootDispatcher(tmp_path),
        )
        item = outcome.items[0]
        assert item.status == "failed"
        assert any(e.get("field") == "plan_revision" for e in item.errors)
        assert not outcome.plan_patch

    def test_first_draft_expects_revision_one(self, packs, monkeypatch, tmp_path):
        """Counterpart / control: state.plan_revision defaults to 0 (no plan
        authored yet) -- the expected revision for a first draft is 1, and a
        plan carrying the schema default (1) is accepted."""
        monkeypatch.setenv("HYDRA_PLAN_PHASE", "1")
        state = HydraState(root_goal="x")
        assert state.plan_revision == 0
        plan = _plan_dict(state.workflow_id)
        outcome = dispatch_ingested_envelopes(
            state, [plan], packs=packs, dispatcher=_ProjectRootDispatcher(tmp_path),
        )
        assert outcome.items[0].status == "drafted"
        assert outcome.plan_patch["plan_revision"] == 1

    def test_wrong_supersedes_is_rejected(self, packs, monkeypatch, tmp_path):
        monkeypatch.setenv("HYDRA_PLAN_PHASE", "1")
        prior_id = uuid4()
        state = HydraState(root_goal="x", plan_revision=2, plan_envelope_id=prior_id,
                            plan_supersedes_expected=str(prior_id))
        plan = _plan_dict(state.workflow_id, revision=2, supersedes=uuid4())  # wrong prior id
        outcome = dispatch_ingested_envelopes(
            state, [plan], packs=packs, dispatcher=_ProjectRootDispatcher(tmp_path),
        )
        item = outcome.items[0]
        assert item.status == "failed"
        assert any(e.get("field") == "supersedes" for e in item.errors)
        assert not outcome.plan_patch

    def test_correct_revision_and_supersedes_is_accepted(self, packs, monkeypatch, tmp_path):
        monkeypatch.setenv("HYDRA_PLAN_PHASE", "1")
        prior_id = uuid4()
        # Hydra#69 follow-up defect 3: validated against
        # `plan_supersedes_expected` (set by --modify-plan when it opens a
        # revision), not `state.plan_envelope_id` — see that field's
        # docstring (state.py) for why the two can diverge after a failed
        # re-entry.
        state = HydraState(root_goal="x", plan_revision=2, plan_envelope_id=prior_id,
                            plan_supersedes_expected=str(prior_id))
        plan = _plan_dict(state.workflow_id, revision=2, supersedes=prior_id)
        outcome = dispatch_ingested_envelopes(
            state, [plan], packs=packs, dispatcher=_ProjectRootDispatcher(tmp_path),
        )
        assert outcome.items[0].status == "drafted"
        assert outcome.plan_patch["plan_revision"] == 2

    def test_missing_supersedes_on_a_revision_is_rejected(self, packs, monkeypatch, tmp_path):
        """expected revision > 1 but the submitted PLAN has no `supersedes`
        at all -- must fail, not silently pass."""
        monkeypatch.setenv("HYDRA_PLAN_PHASE", "1")
        state = HydraState(root_goal="x", plan_revision=2, plan_envelope_id=uuid4(),
                            plan_supersedes_expected=str(uuid4()))
        plan = _plan_dict(state.workflow_id, revision=2)  # no supersedes
        outcome = dispatch_ingested_envelopes(
            state, [plan], packs=packs, dispatcher=_ProjectRootDispatcher(tmp_path),
        )
        item = outcome.items[0]
        assert item.status == "failed"
        assert any(e.get("field") == "supersedes" for e in item.errors)


# =========================================================================== #
# Defect C -- end-to-end attended CLI flow. A tiny stateful fake supervisor
# applies the same reducer semantics LangGraph would (append for `tasks`,
# merge for `plan_submit_attempts`, replace-by-default otherwise) so
# `_cmd_attended_step`/`_cmd_attended_submit` see a checkpoint that actually
# evolves across calls, exactly like the real compiled graph would.
# =========================================================================== #

_APPEND_KEYS = {"tasks", "envelopes", "artifacts", "episodic_refs",
                "semantic_queries", "verdicts", "hitl_history"}
_MERGE_DICT_KEYS = {"error_counters", "plan_submit_attempts"}


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
    """Minimal LangGraph stand-in: persists state across calls with real
    reducer semantics for the keys this flow touches, and reports `next` as
    already-parked-at-plan_gate the instant a PLAN is durably drafted (state
    the CLI/ingest just wrote, not the raw patch) -- mirrors the
    `_StopsAtTarget` fixture in test_p5b_plan_lifecycle.py's graph-reentry
    tests, so `_apply_plan_reentry` short-circuits without needing a real
    compiled graph or any `invoke()` semantics."""

    def __init__(self, initial_values: dict):
        self._values = dict(initial_values)
        self.update_calls: list[tuple[dict, str | None]] = []
        self.invoke_calls = 0

    def get_state(self, config):
        next_ = ("plan_gate",) if self._values.get("plan_status") == "drafted" else ()
        return _Snap(dict(self._values), next_)

    def update_state(self, config, patch, as_node=None):
        self.update_calls.append((dict(patch), as_node))
        self._values = _merge_patch(self._values, patch)

    def invoke(self, arg, config=None):  # pragma: no cover -- never reached
        self.invoke_calls += 1


@pytest.fixture
def plan_task_fixture(tmp_path, monkeypatch):
    """Seeds a single `planning`-owned task, wires the CLI's live dispatcher
    and supervisor to the stateful fake, and returns
    (wf_id, task_id, fake_sup, submit) for the test body to drive."""
    task = TaskState(owner_squad="planning", description="author a plan for: ship it")
    wf = uuid4()
    state = HydraState(root_goal="ship it", workflow_id=wf, tasks=[task])
    fake_sup = _StatefulFakeSup(state.model_dump(mode="json"))

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

    wf_id = str(wf)
    task_id = str(task.task_id)
    return wf_id, task_id, fake_sup, charge_calls


def _step(capsys, wf_id):
    rc = cli._cmd_attended_step(
        argparse.Namespace(project=str(HYDRA_ROOT), workflow_id=wf_id, verbose=False))
    out = capsys.readouterr().out
    assert rc == 0, out
    return _json.loads(out)


def _submit(capsys, wf_id, run_id, call_key, result_path):
    rc = cli._cmd_attended_submit(argparse.Namespace(
        project=str(HYDRA_ROOT), workflow_id=wf_id, run_id=run_id,
        call_key=call_key, result=str(result_path),
    ))
    out = capsys.readouterr().out
    return rc, _json.loads(out)


def _write_result(tmp_path, name, payload):
    p = tmp_path / name
    p.write_text(_json.dumps(payload), encoding="utf-8")
    return p


def _task_status(values: dict, task_id: str) -> Any:
    """Reads a task's persisted `TaskState.status` back out of a checkpoint
    `values` dict -- shape-defensive like `_cmd_replay`/`_task_plan_step_id`
    (cli.py), since `values["tasks"]` may hold either plain dicts (as here,
    a hand-seeded `model_dump(mode="json")` fixture) or real `TaskState`
    Pydantic instances depending on the read path."""
    for t in values.get("tasks") or []:
        tid = t.get("task_id") if isinstance(t, dict) else getattr(t, "task_id", None)
        if str(tid) == str(task_id):
            return t.get("status") if isinstance(t, dict) else getattr(t, "status", None)
    raise AssertionError(f"task {task_id} not found in checkpoint tasks")


class TestAttendedPlanRejectionDefectC:
    """MUTATION PROOF for this whole class: revert the `is_planning_task`
    split in `_cmd_attended_submit` (cli.py) back to writing
    attended_completed_task_ids/attended_done_task_ids/attended_results
    unconditionally before the emitted-envelope loop runs, and
    `test_missing_plan_leaves_task_open_and_reissues_new_call_key` fails
    (the task would be attended-complete despite no PLAN ever landing)."""

    def test_missing_plan_leaves_task_open_and_reissues_new_call_key(
        self, plan_task_fixture, tmp_path, capsys,
    ):
        wf_id, task_id, fake_sup, charge_calls = plan_task_fixture

        step1 = _step(capsys, wf_id)
        assert step1["state"] == "await_squad_agent"
        call_key_0 = step1["host_action"]["call_key"]
        assert call_key_0 == f"squad-{task_id}-0"

        result_path = _write_result(tmp_path, "r0.json", {
            "text": "no plan emitted", "cost_usd": 0.01,
            "tokens_in": 10, "tokens_out": 10,
        })
        rc, payload = _submit(capsys, wf_id, task_id, call_key_0, result_path)
        assert rc == 0
        assert payload["status"] == "plan_rejected", payload
        assert payload["plan_rejection"]["reason"] == "missing_plan"
        assert charge_calls == [0.01], "charging must still happen on a rejected attempt"

        # The task must NOT be attended-complete/done anywhere in the
        # checkpoint after a rejection.
        values = fake_sup.get_state({}).values
        assert task_id not in (values.get("attended_completed_task_ids") or [])
        assert task_id not in (values.get("attended_done_task_ids") or [])
        assert not any(
            r.get("task_id") == task_id for r in (values.get("attended_results") or [])
        )
        assert values.get("plan_submit_attempts", {}).get(task_id) == 1
        # Left OPEN also at the state layer: TaskState.status is untouched.
        assert _task_status(values, task_id) == "pending"

        # The NEXT step re-issues a fresh cursor with an incremented attempt
        # number in its call_key -- never the same key as the rejected one.
        step2 = _step(capsys, wf_id)
        assert step2["state"] == "await_squad_agent"
        call_key_1 = step2["host_action"]["call_key"]
        assert call_key_1 == f"squad-{task_id}-1"
        assert call_key_1 != call_key_0

    def test_stale_call_key_refused(self, plan_task_fixture, tmp_path, capsys):
        wf_id, task_id, fake_sup, charge_calls = plan_task_fixture

        step1 = _step(capsys, wf_id)
        call_key_0 = step1["host_action"]["call_key"]
        result_path = _write_result(tmp_path, "r0.json", {"text": "no plan", "cost_usd": 0.0})
        _submit(capsys, wf_id, task_id, call_key_0, result_path)

        step2 = _step(capsys, wf_id)
        call_key_1 = step2["host_action"]["call_key"]
        assert call_key_1 != call_key_0

        # A late response carrying the STALE (attempt-0) call_key against the
        # freshly re-issued (attempt-1) cursor must be refused structurally.
        stale_result = _write_result(tmp_path, "stale.json", {"text": "late", "cost_usd": 0.0})
        rc, payload = _submit(capsys, wf_id, task_id, call_key_0, stale_result)
        assert rc == 0
        assert payload.get("stale_attempt") is True, payload
        assert payload.get("error_code") == "stale_attempt", payload
        # Must not have been silently accepted as complete.
        assert payload.get("status") != "complete"

    def test_charge_then_reject_not_double_charged_next_attempt_ingested(
        self, plan_task_fixture, tmp_path, capsys,
    ):
        wf_id, task_id, fake_sup, charge_calls = plan_task_fixture

        step1 = _step(capsys, wf_id)
        call_key_0 = step1["host_action"]["call_key"]
        r0 = _write_result(tmp_path, "r0.json", {
            "text": "no plan", "cost_usd": 0.05, "tokens_in": 1, "tokens_out": 1,
        })
        rc0, p0 = _submit(capsys, wf_id, task_id, call_key_0, r0)
        assert p0["status"] == "plan_rejected"
        assert charge_calls == [0.05]

        # A DUPLICATE submit against the SAME (still-open, rejected) cursor
        # must be idempotent -- already_charged, no second charge call.
        rc0b, p0b = _submit(capsys, wf_id, task_id, call_key_0, r0)
        assert p0b.get("already_charged") is True
        assert charge_calls == [0.05], "a repeat submit on the SAME cursor must not re-charge"

        # The NEXT attempt (a fresh cursor at the same path) is charged AND
        # ingested normally -- already_charged must never leak across attempts.
        step2 = _step(capsys, wf_id)
        call_key_1 = step2["host_action"]["call_key"]
        plan = _plan_dict(wf_id)
        r1 = _write_result(tmp_path, "r1.json", {
            "text": "plan authored", "cost_usd": 0.07, "tokens_in": 5, "tokens_out": 5,
            "emitted_envelopes": [plan],
        })
        rc1, p1 = _submit(capsys, wf_id, task_id, call_key_1, r1)
        assert not p1.get("already_charged")
        assert charge_calls == [0.05, 0.07]
        assert p1["status"] != "plan_rejected"

    def test_accepted_plan_completes_task_and_parks_at_plan_gate(
        self, plan_task_fixture, tmp_path, capsys,
    ):
        wf_id, task_id, fake_sup, charge_calls = plan_task_fixture

        step1 = _step(capsys, wf_id)
        call_key_0 = step1["host_action"]["call_key"]
        assert call_key_0 == f"squad-{task_id}-0"

        plan = _plan_dict(wf_id)
        r0 = _write_result(tmp_path, "r0.json", {
            "text": "plan authored", "cost_usd": 0.10, "tokens_in": 5, "tokens_out": 5,
            "emitted_envelopes": [plan],
        })
        rc, payload = _submit(capsys, wf_id, task_id, call_key_0, r0)
        assert rc == 0
        assert payload["status"] != "plan_rejected", payload
        assert payload["plan_status"] == "drafted"
        assert payload["plan_parked_at"] == ["plan_gate"]

        values = fake_sup.get_state({}).values
        assert task_id in (values.get("attended_completed_task_ids") or [])
        assert any(
            r.get("task_id") == task_id for r in (values.get("attended_results") or [])
        )
        # The artifact actually landed on disk under the dispatcher's
        # project_root (tmp_path), not the real repo.
        written = list((tmp_path / "docs" / "plans").glob("*.html"))
        assert len(written) == 1

        # Nothing left open for a re-issued cursor -- the task is done, so
        # the next step does not re-issue a planning cursor for it. (It
        # instead reports the plan_gate HITL is unresolved -- a real
        # operator gate, not a stale planning task.)
        step_after = cli._cmd_attended_step(
            argparse.Namespace(project=str(HYDRA_ROOT), workflow_id=wf_id, verbose=False))
        out_after = _json.loads(capsys.readouterr().out)
        assert step_after == 0, out_after
        assert out_after.get("state") != "await_squad_agent"

    def test_invalid_plan_schema_failure_leaves_task_open_and_reissues_cursor(
        self, plan_task_fixture, tmp_path, capsys,
    ):
        """MUTATION PROOF: revert the acceptance predicate so a `failed` PLAN
        item is still treated as accepted (or revert the C fix generally) and
        this fails -- the task would wrongly become attended-complete despite
        the PLAN failing schema validation."""
        wf_id, task_id, fake_sup, charge_calls = plan_task_fixture

        step1 = _step(capsys, wf_id)
        call_key_0 = step1["host_action"]["call_key"]
        assert call_key_0 == f"squad-{task_id}-0"

        bad_plan = _plan_dict(wf_id)
        bad_plan["rigor"] = "not-a-real-rigor"  # fails Literal[...] validation
        result_path = _write_result(tmp_path, "r0.json", {
            "text": "plan authored", "cost_usd": 0.02, "tokens_in": 1, "tokens_out": 1,
            "emitted_envelopes": [bad_plan],
        })
        rc, payload = _submit(capsys, wf_id, task_id, call_key_0, result_path)
        assert rc == 0
        assert payload["status"] == "plan_rejected", payload
        assert payload["plan_rejection"]["reason"] == "invalid_plan", payload

        values = fake_sup.get_state({}).values
        assert task_id not in (values.get("attended_completed_task_ids") or [])
        assert _task_status(values, task_id) == "pending"
        assert task_id not in (values.get("attended_done_task_ids") or [])

        step2 = _step(capsys, wf_id)
        assert step2["host_action"]["call_key"] == f"squad-{task_id}-1"

    def test_invalid_plan_normalization_failure_is_invalid_not_missing(
        self, plan_task_fixture, tmp_path, capsys, monkeypatch,
    ):
        """Cross-vendor judge finding (this round): a normalization-failure
        record used to omit `envelope_type`, so `_classify_plan_rejection`
        (which matches outcomes on `envelope_type == "PLAN"`) fell through
        to `missing_plan` instead of `invalid_plan`. MUTATION PROOF: drop the
        `"envelope_type": raw.get("type")` key this fix adds to the
        normalization-failure record in `_cmd_attended_submit` and this test
        fails (reason flips back to `missing_plan`)."""
        wf_id, task_id, fake_sup, charge_calls = plan_task_fixture

        step1 = _step(capsys, wf_id)
        call_key_0 = step1["host_action"]["call_key"]

        from hydra_core import ingest as ingest_mod
        real_normalize = ingest_mod.normalize_for_ingest

        def _raise_for_plan(raw, emit_fn=None):
            if isinstance(raw, dict) and raw.get("type") == "PLAN":
                raise ValueError("synthetic normalization failure")
            return real_normalize(raw, emit_fn)

        monkeypatch.setattr(ingest_mod, "normalize_for_ingest", _raise_for_plan)

        plan = _plan_dict(wf_id)
        result_path = _write_result(tmp_path, "r0.json", {
            "text": "plan authored", "cost_usd": 0.02, "tokens_in": 1, "tokens_out": 1,
            "emitted_envelopes": [plan],
        })
        rc, payload = _submit(capsys, wf_id, task_id, call_key_0, result_path)
        assert rc == 0
        assert payload["status"] == "plan_rejected", payload
        assert payload["plan_rejection"]["reason"] == "invalid_plan", payload

        values = fake_sup.get_state({}).values
        assert task_id not in (values.get("attended_completed_task_ids") or [])
        assert _task_status(values, task_id) == "pending"

        step2 = _step(capsys, wf_id)
        assert step2["host_action"]["call_key"] == f"squad-{task_id}-1"

    def test_plan_phase_disabled_leaves_task_open_and_reissues_cursor(
        self, plan_task_fixture, tmp_path, capsys, monkeypatch,
    ):
        wf_id, task_id, fake_sup, charge_calls = plan_task_fixture

        step1 = _step(capsys, wf_id)
        call_key_0 = step1["host_action"]["call_key"]

        monkeypatch.setenv("HYDRA_PLAN_PHASE", "0")
        plan = _plan_dict(wf_id)
        result_path = _write_result(tmp_path, "r0.json", {
            "text": "plan authored", "cost_usd": 0.02, "tokens_in": 1, "tokens_out": 1,
            "emitted_envelopes": [plan],
        })
        rc, payload = _submit(capsys, wf_id, task_id, call_key_0, result_path)
        assert rc == 0
        assert payload["status"] == "plan_rejected", payload
        assert payload["plan_rejection"]["reason"] == "plan_phase_disabled", payload

        values = fake_sup.get_state({}).values
        assert task_id not in (values.get("attended_completed_task_ids") or [])
        assert _task_status(values, task_id) == "pending"

        step2 = _step(capsys, wf_id)
        assert step2["host_action"]["call_key"] == f"squad-{task_id}-1"

    def test_artifact_write_failure_leaves_task_open_and_reissues_cursor(
        self, plan_task_fixture, tmp_path, capsys, monkeypatch,
    ):
        wf_id, task_id, fake_sup, charge_calls = plan_task_fixture

        step1 = _step(capsys, wf_id)
        call_key_0 = step1["host_action"]["call_key"]

        # Strip the dispatcher's project_root so `write_repo_artifact` can
        # never be reached -- `dispatch_ingested_envelopes` reports that as
        # `ArtifactStoreError("dispatcher has no project_root...")`, a real
        # "plan artifact write failed" item, not a synthetic one.
        class _NoRootDispatcher:
            project_root = None

            def call_mcp(self, *a, **k):
                raise AssertionError("PLAN flow must never call an MCP tool")

            def set_squad_packs(self, packs):
                pass

        monkeypatch.setattr(cli, "_attended_live_dispatcher",
                            lambda *a, **k: _NoRootDispatcher())

        plan = _plan_dict(wf_id)
        result_path = _write_result(tmp_path, "r0.json", {
            "text": "plan authored", "cost_usd": 0.02, "tokens_in": 1, "tokens_out": 1,
            "emitted_envelopes": [plan],
        })
        rc, payload = _submit(capsys, wf_id, task_id, call_key_0, result_path)
        assert rc == 0
        assert payload["status"] == "plan_rejected", payload
        assert payload["plan_rejection"]["reason"] == "artifact_write_failed", payload

        values = fake_sup.get_state({}).values
        assert task_id not in (values.get("attended_completed_task_ids") or [])
        assert _task_status(values, task_id) == "pending"

        step2 = _step(capsys, wf_id)
        assert step2["host_action"]["call_key"] == f"squad-{task_id}-1"

    def test_reentry_exception_leaves_task_open_and_reissues_cursor(
        self, tmp_path, capsys, monkeypatch,
    ):
        """MUTATION PROOF for the whole re-entry family: revert
        `_apply_plan_reentry` to treat any non-raising
        `_reenter_graph_after_dispatch` call as success and this fails."""
        task = TaskState(owner_squad="planning", description="author a plan for: ship it")
        wf = uuid4()
        state = HydraState(root_goal="ship it", workflow_id=wf, tasks=[task])

        class _RaisingReentryFakeSup(_StatefulFakeSup):
            def update_state(self, config, patch, as_node=None):
                if as_node == "dispatch":
                    raise RuntimeError("checkpoint write failed")
                super().update_state(config, patch, as_node=as_node)

        fake_sup = _RaisingReentryFakeSup(state.model_dump(mode="json"))

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
        monkeypatch.setattr(
            "hydra_core.governance.charge_and_gate",
            lambda state, cost, toks, **_kw: (False, False),
        )
        monkeypatch.setenv("HYDRA_PLAN_PHASE", "1")

        wf_id = str(wf)
        task_id = str(task.task_id)

        step1 = _step(capsys, wf_id)
        call_key_0 = step1["host_action"]["call_key"]

        plan = _plan_dict(wf_id)
        result_path = _write_result(tmp_path, "r0.json", {
            "text": "plan authored", "cost_usd": 0.02, "tokens_in": 1, "tokens_out": 1,
            "emitted_envelopes": [plan],
        })
        rc, payload = _submit(capsys, wf_id, task_id, call_key_0, result_path)
        assert rc == 0
        assert payload["status"] == "plan_rejected", payload
        assert payload["plan_rejection"]["reason"] == "plan_reentry_failed", payload

        values = fake_sup.get_state({}).values
        assert task_id not in (values.get("attended_completed_task_ids") or [])
        assert _task_status(values, task_id) == "pending"

        step2 = _step(capsys, wf_id)
        assert step2["host_action"]["call_key"] == f"squad-{task_id}-1"

    def test_reentry_non_target_next_leaves_task_open_and_reissues_cursor(
        self, tmp_path, capsys, monkeypatch,
    ):
        """Cross-vendor judge finding (this round, HIGH): the bounded
        re-entry loop can exhaust `max_iterations` with `next` parked at
        something OTHER than `plan_gate` without ever raising --
        `_apply_plan_reentry` used to report that as success. MUTATION
        PROOF: revert the `tuple(parked_at) != target_next` check added to
        `_apply_plan_reentry` and this test fails (status would be something
        other than `plan_rejected`/`plan_reentry_failed`, and the task would
        wrongly complete)."""
        task = TaskState(owner_squad="planning", description="author a plan for: ship it")
        wf = uuid4()
        state = HydraState(root_goal="ship it", workflow_id=wf, tasks=[task])

        class _StuckFakeSup(_StatefulFakeSup):
            """Never reaches plan_gate -- simulates a routing bug so the
            bounded loop exhausts without ever raising."""
            def get_state(self, config):
                return _Snap(dict(self._values), ("await_host",))

        fake_sup = _StuckFakeSup(state.model_dump(mode="json"))

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
        monkeypatch.setattr(
            "hydra_core.governance.charge_and_gate",
            lambda state, cost, toks, **_kw: (False, False),
        )
        monkeypatch.setenv("HYDRA_PLAN_PHASE", "1")

        wf_id = str(wf)
        task_id = str(task.task_id)

        step1 = _step(capsys, wf_id)
        call_key_0 = step1["host_action"]["call_key"]

        plan = _plan_dict(wf_id)
        result_path = _write_result(tmp_path, "r0.json", {
            "text": "plan authored", "cost_usd": 0.02, "tokens_in": 1, "tokens_out": 1,
            "emitted_envelopes": [plan],
        })
        rc, payload = _submit(capsys, wf_id, task_id, call_key_0, result_path)
        assert rc == 0
        assert payload["status"] == "plan_rejected", payload
        assert payload["plan_rejection"]["reason"] == "plan_reentry_failed", payload
        assert "await_host" in payload["plan_rejection"]["detail"], payload

        values = fake_sup.get_state({}).values
        assert task_id not in (values.get("attended_completed_task_ids") or [])
        assert _task_status(values, task_id) == "pending"
        assert task_id not in (values.get("attended_done_task_ids") or [])

        step2 = _step(capsys, wf_id)
        assert step2["host_action"]["call_key"] == f"squad-{task_id}-1"

    def test_duplicate_replay_of_accepted_plan_is_idempotent(
        self, plan_task_fixture, tmp_path, capsys,
    ):
        wf_id, task_id, fake_sup, charge_calls = plan_task_fixture

        step1 = _step(capsys, wf_id)
        call_key_0 = step1["host_action"]["call_key"]
        plan = _plan_dict(wf_id)
        r0 = _write_result(tmp_path, "r0.json", {
            "text": "plan authored", "cost_usd": 0.10, "tokens_in": 5, "tokens_out": 5,
            "emitted_envelopes": [plan],
        })
        _submit(capsys, wf_id, task_id, call_key_0, r0)
        assert charge_calls == [0.10]

        # A duplicate submit against the terminal cursor is a pure replay --
        # already_charged, no second charge, task completion untouched.
        rc, payload = _submit(capsys, wf_id, task_id, call_key_0, r0)
        assert rc == 0
        assert payload.get("already_charged") is True
        assert charge_calls == [0.10]
        values = fake_sup.get_state({}).values
        assert (values.get("attended_completed_task_ids") or []).count(task_id) == 1
