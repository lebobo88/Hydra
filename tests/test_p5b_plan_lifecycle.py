"""P5b: complete the plan lifecycle -- authoring -> drafted -> judged ->
approved.

HYDRA_PLAN_PHASE now ships ON by default (the flip this phase always named
as its final task). Every "flag off" test below opts out explicitly via
``monkeypatch.setenv("HYDRA_PLAN_PHASE", "0")`` to keep proving the legacy
precedence; tests/conftest.py no longer pins the flag for the whole suite.

Task map (see the P5b brief):
  0. The flag gates WRITERS, not READERS -- locked in by TestTask0Asymmetry.
  1. Four envelope allow-lists gain PLAN -- TestTask1AllowLists.
  2. The ingest PLAN branch -- TestTask2IngestBranch.
  3. Graph re-entry via _reenter_graph_after_dispatch -- TestTask3GraphReentry.
  4. Step materialisation in node_plan_gate, ON APPROVAL ONLY, plus the
     plan_revision filter proven independently on all four selectors --
     TestTask4Materialisation.

Every guard here is proven as a property (remove the fix, watch the test
fail), not just exercised for coverage -- per the brief's verification
section.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from hydra_core import cli as hydra_cli
from hydra_core.cli import (
    _apply_plan_reentry,
    _apply_rejected_envelopes,
    _attended_pending_task_ids,
    _cmd_ingest_locked,
    _ingest_item_should_release_claim,
    _next_attended_task,
    _next_stub_attended_task,
    _reenter_graph_after_dispatch,
)
from hydra_core.ingest import (
    IngestItemResult,
    claim_ingested_ids,
    dispatch_ingested_envelopes,
    load_ingested_ids,
    release_ingested_ids,
)
from hydra_core.squad_loader import SquadPack, discover_squads
from hydra_core.squad_node import Dispatcher
from hydra_core.state import HydraState, TaskState, plan_barrier_active

HYDRA_ROOT = Path(__file__).resolve().parents[1]

try:
    from langgraph.checkpoint.memory import MemorySaver as _MemSaver  # noqa: F401
    _HAS_LANGGRAPH = True
except ImportError:
    _HAS_LANGGRAPH = False


@pytest.fixture
def packs():
    return discover_squads(HYDRA_ROOT)


def _stub_pack(slug: str) -> SquadPack:
    return SquadPack(slug=slug, name=slug, description=slug, entrypoint="stub")


def _minimal_plan_dict(workflow_id, *, plan_id=None, revision=1, steps=None,
                        supersedes=None):
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


# =========================================================================== #
# Task 0 -- the flag gates WRITERS, not READERS
# =========================================================================== #

class TestTask0Asymmetry:
    """P5b: HYDRA_PLAN_PHASE now ships ON by default; every flag-off test
    below opts out explicitly (monkeypatch.setenv(..., "0")) so it continues
    to prove the READ side of plan_status is unconditional regardless of the
    flag's write-side default."""

    def _runner(self, monkeypatch):
        from hydra_core.supervisor import build_supervisor, _PurePythonRunner

        def _boom(*_a, **_k):
            raise AssertionError("execute_squad must not run in these routing tests")
        monkeypatch.setattr("hydra_core.supervisor.execute_squad", _boom)

        class _OfflineDispatcher:
            allow_offline_mcp_dispatch = True

        runner = build_supervisor(
            project_root=HYDRA_ROOT, dispatcher=_OfflineDispatcher(),
            force_pure_python=True,
        )
        assert isinstance(runner, _PurePythonRunner)
        return runner

    def test_flag_off_injected_drafted_routes_to_plan_judge(self, monkeypatch):
        """Flag OFF (conftest) + plan_status='drafted' injected on the initial
        state -> after_dispatch's mirror in _PurePythonRunner still runs
        node_plan_judge (proven by plan_status becoming 'judged'), because
        the read side never checks HYDRA_PLAN_PHASE. Removing the asymmetry
        (e.g. gating plan_judge's step-skip on the flag) would leave
        plan_status=='drafted' and fail this assertion."""
        # P5b: the flag ships ON by default now -- explicit opt-out.
        monkeypatch.setenv("HYDRA_PLAN_PHASE", "0")
        import os
        assert os.environ.get("HYDRA_PLAN_PHASE") != "1"
        runner = self._runner(monkeypatch)
        state = HydraState(
            root_goal="x", selected_squads=["planning"], target_repo_id="hydra",
            plan_status="drafted",
        )
        final = runner.invoke(state, stop_before="plan_gate")
        assert final.plan_status == "judged", (
            "flag OFF must not prevent an already-drafted plan from reaching "
            "plan_judge -- the flag gates writers, not readers"
        )

    def test_flag_off_plan_status_none_never_reaches_plan_judge(self, monkeypatch):
        """Counterpart: plan_status='none' (the default, matching every
        pre-P5a workflow) must NEVER be routed into plan_judge/plan_gate --
        it stays 'none' through to the end of the run."""
        # P5b: the flag ships ON by default now -- explicit opt-out.
        monkeypatch.setenv("HYDRA_PLAN_PHASE", "0")
        import os
        assert os.environ.get("HYDRA_PLAN_PHASE") != "1"
        runner = self._runner(monkeypatch)
        state = HydraState(
            root_goal="x", selected_squads=["planning"], target_repo_id="hydra",
        )
        assert state.plan_status == "none"
        final = runner.invoke(state)
        assert final.plan_status == "none", (
            "plan_status='none' must never be routed into plan_judge/plan_gate "
            "(both would advance it away from 'none')"
        )


# =========================================================================== #
# Task 1 -- four envelope allow-lists
# =========================================================================== #

class TestTask1AllowLists:
    def test_plan_extra_fields_match_model(self):
        """_ENVELOPE_EXTRA_FIELDS['PLAN'] is hand-typed (like every other
        entry in that dict) -- pin it to hydra_core.schemas.Plan's own
        fields so it cannot drift silently."""
        import importlib
        server_mod = importlib.import_module("mcp_servers.hydra_control.server")
        from hydra_core.schemas import HydraEnvelope, Plan

        base_fields = set(HydraEnvelope.model_fields)
        plan_extra = (set(Plan.model_fields) - base_fields
                      - server_mod._RESERVED_ENVELOPE_KEYS - {"type"})
        assert server_mod._ENVELOPE_EXTRA_FIELDS["PLAN"] == plan_extra

    def test_plan_through_mcp_verb_retains_every_field(self):
        """Gate-2 regression: a PLAN submitted through hydra.envelope.record
        (the MCP verb) must retain every plan-specific field. Before Task 1,
        PLAN was absent from _ENVELOPE_EXTRA_FIELDS, so every one of these
        fields (other than the reserved outer ones) was silently stripped
        before validate_envelope ever saw them."""
        import importlib
        server_mod = importlib.import_module("mcp_servers.hydra_control.server")
        handlers = server_mod._tool_handlers()
        envelope_record = handlers["hydra.envelope.record"]

        captured: list[dict] = []

        class _CapturingAttestor:
            def pending_count(self): return 0
            def envelope_record(self, envelope):
                captured.append(dict(envelope))

        original = server_mod._get_attestor
        server_mod._get_attestor = lambda: _CapturingAttestor()
        try:
            result = envelope_record({
                "kind": "PLAN",
                "from_squad": "planning",
                "to_squad": "hydra",
                "workflow_id": str(uuid4()),
                "rigor": "standard",
                "goal_restatement": "ship the thing",
                "summary": "ship the thing",
                "steps": [],
                "non_goals": ["scope creep"],
                "open_questions": ["who owns rollback"],
                "risks": ["budget overrun"],
                "plan_revision": 2,
            })
        finally:
            server_mod._get_attestor = original

        assert result.get("ok") is True, result
        assert len(captured) == 1
        env = captured[0]
        for field, expected in (
            ("rigor", "standard"),
            ("goal_restatement", "ship the thing"),
            ("summary", "ship the thing"),
            ("non_goals", ["scope creep"]),
            ("open_questions", ["who owns rollback"]),
            ("risks", ["budget overrun"]),
            ("plan_revision", 2),
        ):
            assert env.get(field) == expected, (
                f"PLAN field {field!r} was stripped at the MCP boundary "
                f"(got {env.get(field)!r}, expected {expected!r})"
            )

    def test_envelope_type_to_kind_maps_plan_to_spec(self):
        from hydra_core.squad_node import _ENVELOPE_TYPE_TO_KIND, _gate_type_for_envelope
        assert _ENVELOPE_TYPE_TO_KIND["PLAN"] == "spec"
        # Not the code_style default a missing/unrecognized type would fall to.
        assert _gate_type_for_envelope("PLAN") == _gate_type_for_envelope("PRD")

    def test_delegation_emit_types_includes_plan(self):
        from hydra_core.squad_node import _DELEGATION_EMIT_TYPES
        assert "PLAN" in _DELEGATION_EMIT_TYPES

    def test_classifier_type_defaults_plan(self):
        from hydra_core.eights.classifier import classify
        cells = classify(envelope_type="PLAN")
        assert set(cells) >= {"li", "qian", "gen"}

    def test_forward_target_by_type_excludes_plan(self):
        """PLAN is a DECISION, not an oversight: it must never gain an entry
        in _FORWARD_TARGET_BY_TYPE (the ingest branch returns before that map
        is ever consulted -- see TestTask2IngestBranch)."""
        from hydra_core.supervisor import _FORWARD_TARGET_BY_TYPE
        assert "PLAN" not in _FORWARD_TARGET_BY_TYPE


# =========================================================================== #
# Task 2 -- the ingest PLAN branch
# =========================================================================== #

class _ProjectRootDispatcher:
    """Minimal dispatcher stand-in exposing only what the PLAN branch reads."""
    def __init__(self, project_root: Path):
        self.project_root = project_root


class TestTask2IngestBranch:
    """Cross-vendor judge finding (P5b revise round): the PLAN branch is a
    SECOND writer of plan_status, and it shipped ungated -- with
    HYDRA_PLAN_PHASE off, a submitted PLAN still raised the barrier
    (`plan_status="drafted"`) even though nothing flag-gated would ever
    drive it to judged/approved, producing exactly the permanent stall the
    flag exists to prevent. Every test that exercises the "drafted" path
    below explicitly turns the flag ON; the flag-OFF refusal is its own
    test, and is the property that matters most in this class."""

    def test_flag_off_refuses_loudly_not_silently(self, packs, monkeypatch, tmp_path):
        """The regression this whole finding is about: with the flag off
        (the module-wide default — conftest pins it), a PLAN must be
        refused with an explicit, visible status — never silently dropped,
        and never allowed to write plan_status="drafted" and raise the
        barrier with no flag-gated code able to ever clear it."""
        # P5b: the flag ships ON by default now -- explicit opt-out.
        monkeypatch.setenv("HYDRA_PLAN_PHASE", "0")
        import os
        assert os.environ.get("HYDRA_PLAN_PHASE") != "1"

        def _boom(*_a, **_k):
            raise AssertionError("execute_squad must not run for a PLAN")
        monkeypatch.setattr("hydra_core.ingest.execute_squad", _boom)

        state = HydraState(root_goal="x")
        plan = _minimal_plan_dict(state.workflow_id)
        outcome = dispatch_ingested_envelopes(
            state, [plan], packs=packs, dispatcher=_ProjectRootDispatcher(tmp_path),
        )
        assert [it.status for it in outcome.items] == ["plan_phase_disabled"]
        assert not outcome.plan_patch, (
            "a refused PLAN must never populate plan_patch -- that is the "
            "exact write that would raise the barrier with no way to clear it"
        )
        assert state.plan_status == "none", "state itself must be untouched"
        # No artifact must be written either — a refused PLAN does nothing.
        assert not (tmp_path / "docs" / "plans").exists()

    def test_ingest_plan_writes_artifact_and_drafts(self, packs, monkeypatch, tmp_path):
        monkeypatch.setenv("HYDRA_PLAN_PHASE", "1")

        def _boom(*_a, **_k):
            raise AssertionError("execute_squad must not run for a PLAN")
        monkeypatch.setattr("hydra_core.ingest.execute_squad", _boom)

        state = HydraState(root_goal="x")
        plan = _minimal_plan_dict(state.workflow_id)
        outcome = dispatch_ingested_envelopes(
            state, [plan], packs=packs, dispatcher=_ProjectRootDispatcher(tmp_path),
        )
        assert [it.status for it in outcome.items] == ["drafted"]
        assert outcome.items[0].target is None, "PLAN must never resolve a forward target"
        assert outcome.plan_patch["plan_status"] == "drafted"
        assert outcome.plan_patch["plan_envelope_id"] == plan["id"]
        assert outcome.plan_patch["plan_revision"] == 1
        location = outcome.plan_patch["plan_artifact_location"]
        assert location, "plan_artifact_location must be set"
        # write_repo_artifact's docs/plans allow-list — the artifact must
        # actually be on disk, not just claimed.
        written = list((tmp_path / "docs" / "plans").glob("*.html"))
        assert len(written) == 1
        # PLAN creates no TaskState — step materialisation is node_plan_gate's
        # job (Task 4), on approval only.
        assert outcome.new_tasks == []

    def test_ingest_plan_never_reports_unknown_target(self, packs, monkeypatch, tmp_path):
        monkeypatch.setenv("HYDRA_PLAN_PHASE", "1")
        state = HydraState(root_goal="x")
        plan = _minimal_plan_dict(state.workflow_id)
        outcome = dispatch_ingested_envelopes(
            state, [plan], packs=packs, dispatcher=_ProjectRootDispatcher(tmp_path),
        )
        assert all(it.status != "unknown_target" for it in outcome.items)

    def test_ingest_plan_two_huge_finite_step_budgets_report_structured_failure(
        self, packs, monkeypatch, tmp_path,
    ):
        """Cross-vendor judge finding (revise round, item 2/4): two
        individually-finite `1e308` step budgets overflow the HTML renderer's
        summed total to `inf`. Before the fix this raised `ValueError` from
        deep inside `render_plan_html`, and the ingest PLAN branch only
        caught `(ArtifactStoreError, OSError)` -- so the whole batch crashed
        with an unhandled exception. The render side now degrades
        gracefully (an "unavailable" note, not a raise) so the batch
        succeeds end to end -- this is the READ-of-already-validated-data
        half of the fix. The broadened ingest exception handling (below,
        `test_ingest_plan_artifact_rendering_error_is_a_structured_failure`)
        is the remaining defense-in-depth half, for any rendering error the
        render-side guard doesn't itself catch.
        """
        monkeypatch.setenv("HYDRA_PLAN_PHASE", "1")
        state = HydraState(root_goal="x")
        steps = [
            {
                "step_id": "a", "target_squad": "engineering",
                "envelope_type": "DEV_TASK", "description": "step a",
                "estimated_budget_usd": 1e308,
            },
            {
                "step_id": "b", "target_squad": "engineering",
                "envelope_type": "DEV_TASK", "description": "step b",
                "depends_on": ["a"], "estimated_budget_usd": 1e308,
            },
        ]
        plan = _minimal_plan_dict(state.workflow_id, steps=steps)
        outcome = dispatch_ingested_envelopes(
            state, [plan], packs=packs, dispatcher=_ProjectRootDispatcher(tmp_path),
        )
        assert [it.status for it in outcome.items] == ["drafted"], (
            "the plan-level total overflow must be absorbed by the "
            "render-side guard -- the artifact still writes and drafts"
        )
        assert outcome.plan_patch["plan_status"] == "drafted"
        location = outcome.plan_patch["plan_artifact_location"]
        written = list((tmp_path / "docs" / "plans").glob("*.html"))
        assert len(written) == 1
        html_text = written[0].read_text(encoding="utf-8")
        assert "unavailable" in html_text
        assert "$nan" not in html_text and "$inf" not in html_text

    def test_ingest_plan_artifact_rendering_error_is_a_structured_failure(
        self, packs, monkeypatch, tmp_path,
    ):
        """Defense-in-depth half of item 2/4: even after the render-side
        overflow guard, some other rendering error could still reach the
        ingest PLAN branch. It must become one structured "failed"
        `IngestItemResult`, never an unhandled exception that crashes the
        whole ingest batch -- and it must not write `plan_status="drafted"`
        (the barrier must not rise on a broken artifact).

        Mutation proof (revert immediately): narrow the except back to
        `(ArtifactStoreError, OSError)` and this test fails with the
        `ValueError` propagating instead of being caught.
        """
        monkeypatch.setenv("HYDRA_PLAN_PHASE", "1")

        def _boom(*_a, **_k):
            raise ValueError("simulated rendering error")
        monkeypatch.setattr("hydra_core.plan_artifact.render_plan_html", _boom)

        state = HydraState(root_goal="x")
        plan = _minimal_plan_dict(state.workflow_id)
        outcome = dispatch_ingested_envelopes(
            state, [plan], packs=packs, dispatcher=_ProjectRootDispatcher(tmp_path),
        )
        assert [it.status for it in outcome.items] == ["failed"]
        assert "simulated rendering error" in outcome.items[0].detail
        assert not outcome.plan_patch
        assert state.plan_status == "none"

    def test_ingest_plan_unrelated_runtime_error_is_not_swallowed(
        self, packs, monkeypatch, tmp_path,
    ):
        """Cross-vendor judge finding (item 5/6, MEDIUM): the ingest PLAN
        branch's except clause previously caught bare `RuntimeError` too --
        not a documented failure mode of `render_plan_html` (raises
        `ValueError`/`PlanFigureError`, a `ValueError` subclass) or
        `write_repo_artifact` (raises only `ArtifactStoreError`, also a
        `ValueError` subclass, or lets a genuine `OSError` propagate). A
        `RuntimeError` from an UNRELATED engine bug transiting this code
        path must surface as the real defect it is, not be silently
        relabeled as an ordinary "plan artifact write failed" user-input
        item failure.

        Mutation proof (revert immediately): re-add `RuntimeError` to the
        except tuple and this test fails because the exception is caught
        and folded into a structured "failed" item instead of propagating.
        """
        monkeypatch.setenv("HYDRA_PLAN_PHASE", "1")

        def _boom(*_a, **_k):
            raise RuntimeError("unrelated engine bug, not a render/serialization failure")
        monkeypatch.setattr("hydra_core.plan_artifact.render_plan_html", _boom)

        state = HydraState(root_goal="x")
        plan = _minimal_plan_dict(state.workflow_id)
        with pytest.raises(RuntimeError, match="unrelated engine bug"):
            dispatch_ingested_envelopes(
                state, [plan], packs=packs, dispatcher=_ProjectRootDispatcher(tmp_path),
            )
        # And the barrier must not have risen on the crashed attempt.
        assert state.plan_status == "none"

    def test_resubmitted_identical_plan_is_skipped_fresh_revision_is_not(
        self, packs, monkeypatch, tmp_path,
    ):
        monkeypatch.setenv("HYDRA_PLAN_PHASE", "1")
        state = HydraState(root_goal="x")
        plan = _minimal_plan_dict(state.workflow_id)

        # First submit: drafted.
        first = dispatch_ingested_envelopes(
            state, [plan], packs=packs, dispatcher=_ProjectRootDispatcher(tmp_path),
        )
        assert first.items[0].status == "drafted"

        # Re-submit the SAME id (simulating a retried CLI call) with the
        # ledger already carrying it (mirrors _cmd_attended_submit's
        # claim_ingested_ids-before-dispatch discipline).
        again = dispatch_ingested_envelopes(
            state, [plan], packs=packs, dispatcher=_ProjectRootDispatcher(tmp_path),
            already_ingested={plan["id"]},
        )
        assert again.items[0].status == "skipped_duplicate"
        assert not again.plan_patch

        # A revision with a FRESH envelope id is not suppressed. Hydra#69
        # defect G: a real `--modify-plan` re-entry bumps state.plan_revision
        # to the NEW revision (and stamps plan_envelope_id to the prior
        # plan's id) BEFORE the revised PLAN is ever authored/submitted
        # (mirrors `_cmd_resume_locked`'s modify-plan branch) -- the revised
        # PLAN must carry that same expected revision and name the prior
        # plan as its `supersedes`, or the state-validation check this
        # defect adds refuses it.
        state.plan_revision = 2
        state.plan_envelope_id = plan["id"]
        # Hydra#69 follow-up defect 3: validated against
        # `plan_supersedes_expected` now, not `plan_envelope_id` -- see that
        # field's docstring (state.py).
        state.plan_supersedes_expected = plan["id"]
        revised = _minimal_plan_dict(
            state.workflow_id, revision=2, supersedes=plan["id"])
        third = dispatch_ingested_envelopes(
            state, [revised], packs=packs, dispatcher=_ProjectRootDispatcher(tmp_path),
            already_ingested={plan["id"]},
        )
        assert third.items[0].status == "drafted"
        assert third.plan_patch["plan_revision"] == 2

    def test_ingest_plan_still_exempt_from_its_own_barrier(self, packs, monkeypatch, tmp_path):
        """P1 already exempted PLAN from plan_barrier_active (etype != "PLAN"
        guard runs before dedup); prove the PLAN branch still reaches
        'drafted' even while a plan is mid-authoring, since a PLAN is what
        clears the barrier."""
        monkeypatch.setenv("HYDRA_PLAN_PHASE", "1")
        state = HydraState(root_goal="x", plan_status="authoring")
        plan = _minimal_plan_dict(state.workflow_id)
        outcome = dispatch_ingested_envelopes(
            state, [plan], packs=packs, dispatcher=_ProjectRootDispatcher(tmp_path),
        )
        assert outcome.items[0].status == "drafted"

    def test_flag_off_refusal_survives_even_mid_authoring_barrier(self, packs, tmp_path,
                                                                    monkeypatch):
        """Counterpart to the exemption test above: the barrier exemption
        (etype != "PLAN") is not itself the gate. With the flag off, a PLAN
        submitted while a plan is already "authoring" is STILL refused, not
        let through because it is exempt from the (separate) barrier check."""
        monkeypatch.setenv("HYDRA_PLAN_PHASE", "0")
        state = HydraState(root_goal="x", plan_status="authoring")
        plan = _minimal_plan_dict(state.workflow_id)
        outcome = dispatch_ingested_envelopes(
            state, [plan], packs=packs, dispatcher=_ProjectRootDispatcher(tmp_path),
        )
        assert outcome.items[0].status == "plan_phase_disabled"


# =========================================================================== #
# Task 3 -- graph re-entry
# =========================================================================== #

class _FakeSnapshot:
    def __init__(self, next_):
        self.next = next_


class _FakeSup:
    """Never reaches target_next and never empties `next` -- proves the loop
    bound by construction, not by observing a real graph terminate."""
    def __init__(self):
        self.invoke_calls = 0
        self.updated_with = None

    def update_state(self, config, patch, as_node=None):
        self.updated_with = (dict(patch), as_node)

    def get_state(self, config):
        return _FakeSnapshot(("some_other_node",))

    def invoke(self, arg, config=None):
        self.invoke_calls += 1


class TestTask3GraphReentry:
    def test_reentry_loop_is_bounded_not_infinite(self):
        """A graph that never reaches plan_gate and never empties `next`
        must not hang -- invoke() is called exactly max_iterations times,
        never more. Proven by an iteration ceiling assertion, not by
        observing termination (a broken bound could still "terminate" by
        luck on a small N)."""
        sup = _FakeSup()
        result = _reenter_graph_after_dispatch(
            sup, {"configurable": {"thread_id": "x"}},
            {"plan_status": "drafted"}, max_iterations=6,
        )
        assert sup.invoke_calls == 6, (
            f"expected exactly the max_iterations bound (6), got {sup.invoke_calls}"
        )
        assert result == ["some_other_node"]
        assert sup.updated_with == ({"plan_status": "drafted"}, "dispatch")

    def test_reentry_stops_early_once_parked_at_target(self):
        class _StopsAtTarget(_FakeSup):
            def get_state(self, config):
                # Parks at plan_gate on the very first check.
                return _FakeSnapshot(("plan_gate",))
        sup = _StopsAtTarget()
        _reenter_graph_after_dispatch(sup, {}, {"plan_status": "drafted"})
        assert sup.invoke_calls == 0, "must not invoke() once already parked at target"

    # ----------------------------------------------------------------- #
    # _apply_plan_reentry: the wrapper's exception path (cross-vendor
    # judge finding, revise round). Its very first statement,
    # sup.update_state(...), is fallible; on failure the claimed
    # envelope_id must be released and the caller must see a real failure
    # status -- never the optimistic "drafted".
    # ----------------------------------------------------------------- #

    def test_reentry_failure_releases_claim_and_reports_failure(self):
        class _ExplodesOnUpdateState:
            def update_state(self, config, patch, as_node=None):
                raise RuntimeError("checkpoint write failed")
            def get_state(self, config):
                raise AssertionError("get_state must not be reached after update_state raises")
            def invoke(self, arg, config=None):
                raise AssertionError("invoke must not be reached after update_state raises")

        released: list[tuple] = []
        emitted: list[tuple] = []
        res: dict[str, object] = {"status": "complete"}  # optimistic pre-existing value

        _apply_plan_reentry(
            _ExplodesOnUpdateState(), {"configurable": {"thread_id": "x"}},
            Path("."), "wf-1",
            {"plan_status": "drafted", "plan_envelope_id": "e-1"}, "e-1", res,
            emit_fn=lambda *a: emitted.append(a),
            release_fn=lambda project, wf, ids: released.append((project, wf, tuple(ids))),
        )

        assert released == [(Path("."), "wf-1", ("e-1",))], (
            "the claimed envelope_id must be released so a retry is possible "
            "instead of being permanently skipped as skipped_duplicate"
        )
        assert res["plan_status"] != "drafted", (
            "must never claim 'drafted' when the graph re-entry itself failed"
        )
        assert res["status"] == "plan_reentry_failed", (
            "the caller-visible top-level status must report the failure, "
            "not silently keep the pre-existing 'complete'"
        )
        assert "plan_reentry_error" in res
        assert emitted, "the failure must be traced, not swallowed silently"

    def test_reentry_success_does_not_release_and_reports_drafted(self):
        """Counterpart: on success, nothing is released and the real
        (not optimistic-in-advance) plan_status is reported."""
        class _SucceedsImmediately:
            def update_state(self, config, patch, as_node=None):
                pass
            def get_state(self, config):
                return _FakeSnapshot(("plan_gate",))
            def invoke(self, arg, config=None):
                raise AssertionError("already parked at target; must not invoke")

        released: list[tuple] = []
        res: dict[str, object] = {"status": "complete"}

        _apply_plan_reentry(
            _SucceedsImmediately(), {}, Path("."), "wf-1",
            {"plan_status": "drafted", "plan_envelope_id": "e-1"}, "e-1", res,
            emit_fn=lambda *a: None,
            release_fn=lambda *a: released.append(a),
        )

        assert released == []
        assert res["plan_status"] == "drafted"
        assert res["plan_parked_at"] == ["plan_gate"]
        assert res["status"] == "complete", "success must not touch the top-level status"

    @pytest.mark.skipif(not _HAS_LANGGRAPH, reason="langgraph not installed")
    def test_real_graph_parks_at_plan_gate(self, monkeypatch, tmp_path):
        """End-to-end with the compiled graph: after_dispatch really does
        route plan_status='drafted' to plan_judge, which files the HITL and
        edges to plan_gate -- an interrupt_before node -- so the graph parks
        there rather than running past it or reaching END."""
        monkeypatch.setenv("HYDRA_CHECKPOINT_DB", str(tmp_path / "cp.db"))
        monkeypatch.setattr(
            "hydra_core.governance.enforce_constitution",
            lambda *_a, **_k: type("V", (), {"aligned": True, "rationale": ""})(),
        )
        from hydra_core.supervisor import build_supervisor, _PurePythonRunner

        class _NullDispatcher:
            live_execution = False
            def call_mcp(self, *a, **k):
                return {"status": "done", "result": {}}
            def set_squad_packs(self, packs):
                pass

        sup = build_supervisor(project_root=HYDRA_ROOT, dispatcher=_NullDispatcher())
        if isinstance(sup, _PurePythonRunner):
            pytest.skip("compiled graph unavailable")

        wf = uuid4()
        config = {"configurable": {"thread_id": str(wf)}}
        sup.update_state(config, HydraState(root_goal="x", workflow_id=wf).model_dump(mode="json"))

        parked = _reenter_graph_after_dispatch(
            sup, config, {"plan_status": "drafted", "plan_revision": 1},
        )
        assert parked == ["plan_gate"], f"expected to park at plan_gate, got {parked}"
        final_state = HydraState.model_validate(sup.get_state(config).values)
        assert final_state.plan_status == "judged"


# =========================================================================== #
# P5b revise round 2 -- status clobber + claim release on refusal
# =========================================================================== #

class TestReviseRoundStatusComposition:
    """Item 1: `_apply_rejected_envelopes` must never downgrade an existing
    `plan_reentry_failed` status back to `envelopes_rejected` -- both
    failures are real and neither should silently replace the other in the
    field a caller actually switches on."""

    def _failed_plan_reentry_res(self) -> dict[str, object]:
        class _ExplodesOnUpdateState:
            def update_state(self, config, patch, as_node=None):
                raise RuntimeError("checkpoint write failed")
        res: dict[str, object] = {"status": "complete"}
        _apply_plan_reentry(
            _ExplodesOnUpdateState(), {}, Path("."), "wf-1",
            {"plan_status": "drafted", "plan_envelope_id": "e-1"}, "e-1", res,
            emit_fn=lambda *a: None, release_fn=lambda *a: None,
        )
        assert res["status"] == "plan_reentry_failed"  # sanity on the fixture itself
        return res

    def test_rejected_envelopes_does_not_clobber_plan_reentry_failed(self):
        res = self._failed_plan_reentry_res()
        recorded: list[tuple] = []
        emitted: list[tuple] = []

        _apply_rejected_envelopes(
            res, [{"envelope_id": "e-2", "status": "failed"}],
            record_fn=lambda cfile, rej: recorded.append((cfile, rej)),
            emit_fn=lambda *a: emitted.append(a),
            project=Path("."), wf="wf-1", cfile="cursor.json", run_id="run-1",
        )

        assert res["status"] == "plan_reentry_failed", (
            "a real rejected-envelope failure must not silently replace an "
            "already-recorded plan_reentry_failed status"
        )
        # Both signals must still be recoverable -- the rejected-envelope
        # signal lives at its own key regardless of which status wins.
        assert res["rejected_envelopes"] == [{"envelope_id": "e-2", "status": "failed"}]
        assert res["plan_status"] == "plan_reentry_failed"
        assert "plan_reentry_error" in res
        assert recorded and emitted, "rejected-envelope side effects must still run"

    def test_rejected_envelopes_sets_status_when_no_plan_failure(self):
        """Counterpart: with no competing plan-reentry failure, the existing
        behaviour (envelopes_rejected wins) is unchanged."""
        res: dict[str, object] = {"status": "complete"}
        _apply_rejected_envelopes(
            res, [{"envelope_id": "e-2", "status": "failed"}],
            record_fn=lambda *a: None, emit_fn=lambda *a: None,
            project=Path("."), wf="wf-1", cfile="cursor.json", run_id="run-1",
        )
        assert res["status"] == "envelopes_rejected"


class TestReviseRoundClaimRelease:
    """Item 2: a PLAN refused with `plan_phase_disabled` must release its
    claimed envelope_id -- the flag-flip-and-resubmit path is exactly when a
    stale claim would silently swallow the resubmit as `skipped_duplicate`."""

    @pytest.mark.parametrize("status,errors,expected", [
        ("unknown_target", [], True),
        ("plan_phase_disabled", [], True),
        ("failed", [{"field": "x", "msg": "bad"}], True),
        ("failed", [], False),  # a bare "failed" with no structured errors: keep
        ("drafted", [], False),
        ("done", [], False),
        ("skipped_duplicate", [], False),
        ("deferred_to_host", [], False),
    ])
    def test_should_release_claim_table(self, status, errors, expected):
        item = IngestItemResult(
            envelope_id="e-1", envelope_type="PLAN", target=None,
            status=status, errors=errors,
        )
        assert _ingest_item_should_release_claim(item) is expected

    def test_plan_refused_with_flag_off_can_be_resubmitted_once_flag_is_on(
        self, packs, tmp_path, monkeypatch,
    ):
        """The property that actually matters: not merely that release_fn
        was called, but that the SAME envelope id, refused while the flag
        was off, dispatches successfully once the flag is on -- exactly the
        operator action of flipping HYDRA_PLAN_PHASE and resubmitting."""
        # P5b: the flag ships ON by default now -- start from the explicit
        # OFF state this test's first half needs.
        monkeypatch.setenv("HYDRA_PLAN_PHASE", "0")
        state = HydraState(root_goal="x")
        plan = _minimal_plan_dict(state.workflow_id)
        eid = plan["id"]

        # Mirrors _cmd_attended_submit's ACTUAL ordering exactly: `processed`
        # is loaded ONCE before the loop, THEN claim_ingested_ids writes the
        # disk ledger, and dispatch is called with that pre-claim in-memory
        # set -- not a freshly reloaded one (reloading after claiming would
        # make every single envelope a false "skipped_duplicate" against its
        # own claim, which is not what the real code does).
        processed_before_claim = load_ingested_ids(tmp_path, "wf-1")
        claim_ingested_ids(tmp_path, "wf-1", [eid])

        # Flag OFF: refused.
        outcome = dispatch_ingested_envelopes(
            state, [plan], packs=packs, dispatcher=_ProjectRootDispatcher(tmp_path),
            already_ingested=processed_before_claim,
        )
        item = outcome.items[0]
        assert item.status == "plan_phase_disabled"

        # The CLI's own release decision, applied exactly as _cmd_attended_submit does.
        if _ingest_item_should_release_claim(item):
            release_ingested_ids(tmp_path, "wf-1", [eid])

        assert eid not in load_ingested_ids(tmp_path, "wf-1"), (
            "a refused PLAN's id must not stay claimed"
        )

        # Flip the flag ON and resubmit the SAME plan, SAME id.
        import os
        old = os.environ.get("HYDRA_PLAN_PHASE")
        os.environ["HYDRA_PLAN_PHASE"] = "1"
        try:
            resubmit = dispatch_ingested_envelopes(
                state, [plan], packs=packs, dispatcher=_ProjectRootDispatcher(tmp_path),
                already_ingested=load_ingested_ids(tmp_path, "wf-1"),
            )
        finally:
            if old is None:
                os.environ.pop("HYDRA_PLAN_PHASE", None)
            else:
                os.environ["HYDRA_PLAN_PHASE"] = old

        assert resubmit.items[0].status == "drafted", (
            "the resubmit must actually land, not be silently skipped as "
            "skipped_duplicate because the earlier refusal left the id claimed"
        )


class TestReviseRoundIngestPathParity:
    """Item 3: `_cmd_ingest_locked` (the `hydra ingest` continuation-transport
    path host-run claude-skill squads use) used to carry its own
    hand-duplicated copy of the claim/release decision, and that copy never
    got `plan_phase_disabled` added when item 2 fixed the attended-submit
    path. Same defect, same trigger (the flag flip + resubmit), different
    call site. Proven end-to-end through the REAL `_cmd_ingest_locked`
    function (not a reimplementation of its loop), against a real compiled
    checkpoint, exactly like `hydra ingest` would run it."""

    @pytest.mark.skipif(not _HAS_LANGGRAPH, reason="langgraph not installed")
    def test_plan_via_hydra_ingest_refused_then_resubmitted_once_flag_on(
        self, monkeypatch, tmp_path, capsys,
    ):
        monkeypatch.setenv("HYDRA_CHECKPOINT_DB", str(tmp_path / "cp.db"))
        monkeypatch.setattr(
            "hydra_core.governance.enforce_constitution",
            lambda *_a, **_k: type("V", (), {"aligned": True, "rationale": ""})(),
        )
        # _cmd_ingest_locked's non-`--live` path uses cli._NullDispatcher(),
        # which (unlike MCPStdioDispatcher, the real dispatcher `hydra
        # ingest` actually runs with) carries no `project_root` — give it
        # one so the PLAN branch's artifact write has somewhere to land,
        # matching what a real dispatcher provides.
        monkeypatch.setattr(hydra_cli._NullDispatcher, "project_root", tmp_path,
                            raising=False)
        # Squad discovery needs a real `squads/<slug>/squad.yaml` tree, and
        # build_supervisor needs a real CONSTITUTION.md; `project` below is
        # deliberately tmp_path anyway, so the ledger
        # (`.hydra/<wf>/ingested.json`) and the plan artifact write both
        # land in an isolated directory rather than the real checkout.
        # CONSTITUTION.md is a single small file -- just copy it in.
        # Squads/ is a whole tree -- redirect the lookup to HYDRA_ROOT
        # instead of copying it.
        import shutil
        shutil.copy(HYDRA_ROOT / "CONSTITUTION.md", tmp_path / "CONSTITUTION.md")
        from hydra_core.squad_loader import discover_squads as _real_discover_squads
        monkeypatch.setattr("hydra_core.cli.discover_squads",
                            lambda *_a, **_k: _real_discover_squads(HYDRA_ROOT))
        monkeypatch.setattr("hydra_core.supervisor.discover_squads",
                            lambda *_a, **_k: _real_discover_squads(HYDRA_ROOT))

        from hydra_core.supervisor import build_supervisor, _PurePythonRunner
        wf = uuid4()
        wf_str = str(wf)
        seed_sup = build_supervisor(project_root=tmp_path, dispatcher=hydra_cli._NullDispatcher())
        if isinstance(seed_sup, _PurePythonRunner):
            pytest.skip("compiled graph unavailable")
        config = {"configurable": {"thread_id": wf_str}}
        seed_sup.update_state(config, HydraState(root_goal="x", workflow_id=wf).model_dump(mode="json"))

        plan = _minimal_plan_dict(wf)
        args = type("Args", (), {"live": False, "verbose": False})()

        # P5b: the flag ships ON by default now -- start from the explicit
        # OFF state this test's first half needs.
        monkeypatch.setenv("HYDRA_PLAN_PHASE", "0")

        # --- Flag OFF: refused via the REAL hydra-ingest code path. ---
        rc1 = _cmd_ingest_locked(args, tmp_path, wf_str, [dict(plan)])
        out1 = json.loads(capsys.readouterr().out)
        assert rc1 == 0
        assert out1["items"][0]["status"] == "plan_phase_disabled"
        ledger_after_refusal = load_ingested_ids(tmp_path, wf_str)
        assert plan["id"] not in ledger_after_refusal, (
            "a PLAN refused through `hydra ingest` must not leave its id "
            "claimed -- this is the exact defect item 2 fixed on the other "
            "call site, reachable here too before this fix"
        )

        # --- Flip the flag ON and resubmit the SAME plan, SAME id. ---
        monkeypatch.setenv("HYDRA_PLAN_PHASE", "1")
        rc2 = _cmd_ingest_locked(args, tmp_path, wf_str, [dict(plan)])
        out2 = json.loads(capsys.readouterr().out)
        assert rc2 == 0
        assert out2["items"][0]["status"] == "drafted", (
            "the resubmit through hydra ingest must actually land as "
            "drafted, not be silently skipped as skipped_duplicate because "
            "the earlier refusal left the id claimed"
        )

    def test_no_second_copy_of_the_decision_remains(self):
        """Guard against a fourth occurrence of this defect shape: assert
        `_cmd_ingest_locked`'s source calls the ONE shared decision function
        rather than re-deriving its own condition. AST-based (not a plain
        substring search) so this cannot false-positive on a comment that
        merely NAMES the old local variable while explaining the history --
        it fails only on an actual re-derived condition: a local variable
        assignment or an `item.status == ...` comparison living inside
        `_cmd_ingest_locked` itself, outside the one shared function call."""
        import ast
        import inspect
        import textwrap

        src = inspect.getsource(_cmd_ingest_locked)
        tree = ast.parse(textwrap.dedent(src))
        fn_node = tree.body[0]
        assert isinstance(fn_node, ast.FunctionDef)

        calls_shared_fn = any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_ingest_item_should_release_claim"
            for node in ast.walk(fn_node)
        )
        assert calls_shared_fn, "_cmd_ingest_locked must call the shared decision function"

        # Scoped to `last_item`/`item_status` specifically -- NOT every
        # `.status ==` in the function. The final `summary = {...}` dict
        # legitimately re-checks `it.status` for a DIFFERENT loop variable
        # (categorising already-decided outcomes for the JSON response,
        # e.g. `it.status in ("done", "running")`), which is not the
        # claim/release decision this test is guarding.
        reinvented_condition = any(
            isinstance(node, ast.Compare)
            and isinstance(node.left, ast.Attribute)
            and node.left.attr == "status"
            and isinstance(node.left.value, ast.Name)
            and node.left.value.id in ("last_item", "item_status", "item")
            for node in ast.walk(fn_node)
        )
        assert not reinvented_condition, (
            "found an `x.status == ...` comparison inside _cmd_ingest_locked "
            "itself -- the release decision must live ONLY in "
            "_ingest_item_should_release_claim, not be re-derived at this "
            "call site (that is exactly how the hand-duplicated copy "
            "drifted out of sync last time)"
        )

        reinvented_local_set = any(
            isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id.startswith("_NOT_DISPATCHED")
                    for t in node.targets)
            for node in ast.walk(fn_node)
        )
        assert not reinvented_local_set, (
            "a local _NOT_DISPATCHED-shaped variable must not come back"
        )


# =========================================================================== #
# Task 4 -- step materialisation on approval + the plan_revision filter
# =========================================================================== #

def _plan_gate_fn():
    from hydra_core.squad_node import Dispatcher as _Dispatcher
    from hydra_core.supervisor import build_supervisor, _PurePythonRunner
    stub_dispatcher = MagicMock(spec=_Dispatcher)
    stub_dispatcher._tool_tracker = None
    runner = build_supervisor(
        project_root=HYDRA_ROOT, dispatcher=stub_dispatcher, force_pure_python=True,
    )
    fn = dict(runner.steps).get("plan_gate")
    assert fn is not None
    return fn


class TestTask4Materialisation:
    def test_materialises_one_task_per_step_with_dependency_translation(self):
        node_plan_gate = _plan_gate_fn()
        wf = uuid4()
        plan = _minimal_plan_dict(wf, steps=[
            {"step_id": "a", "target_squad": "engineering", "envelope_type": "DEV_TASK",
             "description": "wire the middleware", "depends_on": []},
            {"step_id": "b", "target_squad": "engineering", "envelope_type": "DEV_TASK",
             "description": "add the tests", "depends_on": ["a"]},
        ])
        state = HydraState(
            root_goal="x", workflow_id=wf, plan_status="judged", plan_revision=1,
            plan_ref=plan, pending_hitl={"gate_node": "plan_gate"},
        )
        patch = node_plan_gate(state)
        assert patch["plan_status"] == "approved"
        tasks = patch["tasks"]
        assert len(tasks) == 2
        by_step = {t.plan_step_id: t for t in tasks}
        assert by_step["a"].depends_on == []
        assert by_step["b"].depends_on == [str(by_step["a"].task_id)]
        assert all(t.plan_revision == 1 for t in tasks)

    def test_no_materialisation_without_plan_ref(self):
        node_plan_gate = _plan_gate_fn()
        state = HydraState(
            root_goal="x", plan_status="judged", plan_revision=1,
            pending_hitl={"gate_node": "plan_gate"},
        )
        patch = node_plan_gate(state)
        assert "tasks" not in patch

    # ----------------------------------------------------------------- #
    # Idempotency: node_plan_gate must not double-materialise on replay
    # or on any re-run against the same revision (reachable via `hydra
    # replay --from-phase` re-invoking the graph from a checkpoint
    # snapshot that already carries the first materialisation's tasks).
    # ----------------------------------------------------------------- #

    def _two_step_plan(self, wf):
        return _minimal_plan_dict(wf, steps=[
            {"step_id": "a", "target_squad": "engineering", "envelope_type": "DEV_TASK",
             "description": "wire the middleware", "depends_on": []},
            {"step_id": "b", "target_squad": "engineering", "envelope_type": "DEV_TASK",
             "description": "add the tests", "depends_on": ["a"]},
        ])

    def test_second_execution_against_same_state_materialises_nothing_new(self):
        """First call: 2 new tasks. Apply the append reducer by hand (mirrors
        what update_state actually does), then re-run the SAME node against
        the resulting state (simulating a replay/re-raised-gate re-entry) --
        the second call must materialise NOTHING, not a second copy."""
        node_plan_gate = _plan_gate_fn()
        wf = uuid4()
        plan = self._two_step_plan(wf)
        state = HydraState(
            root_goal="x", workflow_id=wf, plan_status="judged", plan_revision=1,
            plan_ref=plan, pending_hitl={"gate_node": "plan_gate"},
        )
        first_patch = node_plan_gate(state)
        assert len(first_patch["tasks"]) == 2

        # Apply the append reducer + re-raise the gate, exactly as a replay
        # landing back on this node would present it.
        state.tasks.extend(first_patch["tasks"])
        state.pending_hitl = {"gate_node": "plan_gate"}

        second_patch = node_plan_gate(state)
        assert "tasks" not in second_patch, (
            "a second execution against a state that already carries this "
            "revision's step tasks must not re-materialise them — this is "
            "exactly the doubling a replay through plan_gate would trigger"
        )

        # A third execution converges the same way (not "eventually stops
        # growing" — never grows past the first materialisation at all).
        third_patch = node_plan_gate(state)
        assert "tasks" not in third_patch

        by_step = {t.plan_step_id: t for t in state.tasks}
        assert len(state.tasks) == 2
        assert set(by_step) == {"a", "b"}

    def test_partial_materialisation_resolves_dependency_to_existing_task(self):
        """If step 'a' already has a same-revision TaskState (a partial prior
        materialisation) and step 'b' (which depends on 'a') does not yet,
        this call must create ONLY b's task, and b's depends_on must resolve
        to the EXISTING a-task's id -- not be dropped, and not duplicate a."""
        node_plan_gate = _plan_gate_fn()
        wf = uuid4()
        plan = self._two_step_plan(wf)
        existing_a = TaskState(
            owner_squad="engineering", description="wire the middleware",
            plan_step_id="a", plan_revision=1,
        )
        state = HydraState(
            root_goal="x", workflow_id=wf, plan_status="judged", plan_revision=1,
            plan_ref=plan, pending_hitl={"gate_node": "plan_gate"},
        )
        state.tasks.append(existing_a)

        patch = node_plan_gate(state)
        assert "tasks" in patch
        new_tasks = patch["tasks"]
        assert len(new_tasks) == 1, "only step b should be newly materialised"
        assert new_tasks[0].plan_step_id == "b"
        assert new_tasks[0].depends_on == [str(existing_a.task_id)], (
            "b's dependency on the already-existing a-task must resolve to "
            "a's real task_id, not be silently dropped"
        )

    def test_older_revision_step_still_materialises_fresh_at_new_revision(self):
        """The idempotency guard must not suppress a step at a NEWER
        revision just because an older-revision TaskState for the same
        step_id exists — the existing_by_step_id lookup is revision-scoped."""
        node_plan_gate = _plan_gate_fn()
        wf = uuid4()
        plan = self._two_step_plan(wf)
        stale_a = TaskState(
            owner_squad="engineering", description="wire the middleware (rev 1)",
            plan_step_id="a", plan_revision=1,
        )
        state = HydraState(
            root_goal="x", workflow_id=wf, plan_status="judged", plan_revision=2,
            plan_ref=plan, pending_hitl={"gate_node": "plan_gate"},
        )
        state.tasks.append(stale_a)

        patch = node_plan_gate(state)
        by_step = {t.plan_step_id: t for t in patch["tasks"]}
        assert set(by_step) == {"a", "b"}
        assert by_step["a"].plan_revision == 2
        assert by_step["a"].task_id != stale_a.task_id

    def test_clobber_guard_still_holds_with_materialisation_added(self):
        """A foreign gate that landed between the operator's clear and this
        continuation must still short-circuit BEFORE any materialisation."""
        node_plan_gate = _plan_gate_fn()
        wf = uuid4()
        plan = _minimal_plan_dict(wf, steps=[
            {"step_id": "a", "target_squad": "engineering", "envelope_type": "DEV_TASK",
             "description": "x", "depends_on": []},
        ])
        state = HydraState(
            root_goal="x", workflow_id=wf, plan_status="judged", plan_revision=1,
            plan_ref=plan, pending_hitl={"gate_node": "some_other_gate"},
        )
        patch = node_plan_gate(state)
        assert "tasks" not in patch
        assert patch == {"phase": "approval"}

    # ----------------------------------------------------------------- #
    # The plan_revision filter, proven independently on EACH selector.
    # ----------------------------------------------------------------- #

    def test_next_attended_task_skips_stale_revision(self, packs):
        state = HydraState(root_goal="x", plan_revision=2)
        stale = TaskState(owner_squad="engineering", description="stale", plan_revision=1)
        fresh = TaskState(owner_squad="engineering", description="fresh", plan_revision=2)
        state.tasks.extend([stale, fresh])
        task, kind, pack = _next_attended_task(state, packs)
        assert task is not None and task.description == "fresh"

    def test_next_stub_attended_task_skips_stale_revision(self):
        packs = {"stubby": _stub_pack("stubby")}
        state = HydraState(root_goal="x", plan_revision=2)
        stale = TaskState(owner_squad="stubby", description="stale", plan_revision=1)
        fresh = TaskState(owner_squad="stubby", description="fresh", plan_revision=2)
        state.tasks.extend([stale, fresh])
        task, pack = _next_stub_attended_task(state, packs)
        assert task is not None and task.description == "fresh"

    def test_attended_pending_task_ids_excludes_stale_revision(self):
        state = HydraState(root_goal="x", plan_revision=2)
        stale = TaskState(owner_squad="engineering", description="stale", plan_revision=1)
        fresh = TaskState(owner_squad="engineering", description="fresh", plan_revision=2)
        state.tasks.extend([stale, fresh])
        pending = _attended_pending_task_ids(state)
        assert str(fresh.task_id) in pending
        assert str(stale.task_id) not in pending

    def test_node_dispatch_sequential_loop_skips_stale_revision(self, monkeypatch, packs):
        """Drive the real node_dispatch (via the pure-python runner) and prove
        a stale-revision task never reaches execute_squad while a
        same-revision task does."""
        from hydra_core.supervisor import build_supervisor, _PurePythonRunner

        calls: list[str] = []

        def _fake_execute_squad(state, pack, env, dispatcher, **kw):
            calls.append(env.objective if hasattr(env, "objective") else "?")
            from hydra_core.squad_node import SquadResult
            return SquadResult(envelopes=[], artifacts=[], status="done", rationale="ok")

        monkeypatch.setattr("hydra_core.supervisor.execute_squad", _fake_execute_squad)

        class _OfflineDispatcher:
            allow_offline_mcp_dispatch = True
            live_execution = False

        runner = build_supervisor(
            project_root=HYDRA_ROOT, dispatcher=_OfflineDispatcher(),
            force_pure_python=True,
            # P5b hostless-path audit: this test's concern is the stale-
            # revision skip, not plan-phase seeding -- one-shot invoke, no
            # attended host.
            force_trivial_plan_rigor=True,
        )
        assert isinstance(runner, _PurePythonRunner)

        state = HydraState(
            root_goal="x", selected_squads=["engineering"], target_repo_id="hydra",
            plan_revision=2,
        )
        stale = TaskState(owner_squad="engineering", description="stale-task",
                          plan_revision=1)
        fresh = TaskState(owner_squad="engineering", description="fresh-task",
                          plan_revision=2)
        state.tasks.extend([stale, fresh])

        final = runner.invoke(state, stop_before="judge_per_squad")
        by_desc = {t.description: t for t in final.tasks}
        assert by_desc["stale-task"].status == "pending", (
            "a stale-revision task must never be dispatched by node_dispatch's "
            "sequential loop"
        )
        assert by_desc["fresh-task"].status != "pending", (
            "a same-revision task must still dispatch normally"
        )


# =========================================================================== #
# Hydra#69 (part 1) defects B, E, F, H:
#   B: node_planner's whole-goal placeholder tasks are superseded on approval
#      (plan_placeholder_task_ids / plan_superseded_task_ids), closing the
#      revision-0 gap a stale-revision-only check could never catch.
#   E: materialise_plan_steps copies each PlanStep's acceptance_criteria and
#      envelope_type onto the materialised TaskState.
#   F: task_eligible_for_dispatch is the ONE predicate every selector uses
#      (stale revision + explicit supersession + plan_deps_satisfied).
#   H: materialise_plan_steps is a no-op once plan_status == "bypassed".
# =========================================================================== #

from hydra_core.supervisor import materialise_plan_steps
from hydra_core.state import task_eligible_for_dispatch


class TestDefectEAcceptanceCriteriaEnvelopeType:
    def test_materialised_task_carries_step_acceptance_criteria_and_envelope_type(self):
        wf = uuid4()
        plan = _minimal_plan_dict(wf, steps=[
            {"step_id": "a", "target_squad": "engineering", "envelope_type": "DEV_TASK",
             "description": "wire the middleware", "depends_on": [],
             "acceptance_criteria": ["middleware wired", "tests green"]},
        ])
        state = HydraState(
            root_goal="x", workflow_id=wf, plan_status="judged", plan_revision=1,
            plan_ref=plan, pending_hitl={"gate_node": "plan_gate"},
        )
        patch = materialise_plan_steps(state)
        task = patch["tasks"][0]
        assert task.envelope_type == "DEV_TASK"
        assert task.acceptance_criteria == ["middleware wired", "tests green"]

    def test_step_without_acceptance_criteria_leaves_task_field_none(self):
        wf = uuid4()
        plan = _minimal_plan_dict(wf, steps=[
            {"step_id": "a", "target_squad": "engineering", "envelope_type": "DEV_TASK",
             "description": "x", "depends_on": []},
        ])
        state = HydraState(
            root_goal="x", workflow_id=wf, plan_status="judged", plan_revision=1,
            plan_ref=plan, pending_hitl={"gate_node": "plan_gate"},
        )
        patch = materialise_plan_steps(state)
        assert patch["tasks"][0].acceptance_criteria is None


class TestDefectBPlaceholderSupersession:
    def test_planner_records_whole_goal_placeholder_ids_when_plan_active(self):
        from hydra_core.supervisor import build_supervisor
        runner = build_supervisor(
            project_root=HYDRA_ROOT, dispatcher=MagicMock(), force_pure_python=True,
        )
        node_planner = dict(runner.steps)["planner"]
        state = HydraState(
            root_goal="x", selected_squads=["engineering"], target_repo_id="hydra",
            plan_rigor_override="standard",
        )
        patch = node_planner(state)
        assert patch["plan_placeholder_task_ids"], (
            "the whole-goal engineering task must be recorded as a placeholder "
            "while the plan gate is active"
        )
        placeholder_ids = set(patch["plan_placeholder_task_ids"])
        synthesised_ids = {str(t.task_id) for t in patch["tasks"]}
        planning_ids = {str(t.task_id) for t in patch["tasks"]
                        if t.owner_squad == "planning"}
        assert placeholder_ids == synthesised_ids - planning_ids, (
            "the planning task itself must never be its own placeholder"
        )

    def test_planner_writes_empty_placeholder_list_when_plan_not_active(self):
        from hydra_core.supervisor import build_supervisor
        runner = build_supervisor(
            project_root=HYDRA_ROOT, dispatcher=MagicMock(), force_pure_python=True,
            # trivial rigor -> the plan gate never activates this pass.
            force_trivial_plan_rigor=True,
        )
        node_planner = dict(runner.steps)["planner"]
        state = HydraState(
            root_goal="x", selected_squads=["engineering"], target_repo_id="hydra",
        )
        patch = node_planner(state)
        assert patch["plan_placeholder_task_ids"] == [], (
            "the field must be explicitly written as [] (LastValue-clear), "
            "not omitted, whenever the plan gate is not active this pass"
        )

    def test_materialise_plan_steps_supersedes_placeholder_ids(self):
        wf = uuid4()
        plan = _minimal_plan_dict(wf, steps=[
            {"step_id": "a", "target_squad": "engineering", "envelope_type": "DEV_TASK",
             "description": "x", "depends_on": []},
        ])
        placeholder = TaskState(owner_squad="engineering", description="whole-goal placeholder")
        state = HydraState(
            root_goal="x", workflow_id=wf, plan_status="judged", plan_revision=1,
            plan_ref=plan, pending_hitl={"gate_node": "plan_gate"},
            plan_placeholder_task_ids=[str(placeholder.task_id)],
        )
        patch = materialise_plan_steps(state)
        assert patch["plan_superseded_task_ids"] == [str(placeholder.task_id)]

    def test_placeholder_at_revision_zero_ineligible_once_superseded(self):
        """The bug: a revision-0 placeholder is NEVER caught by a stale-
        revision-only check (``0 and ...`` is falsy). task_eligible_for_
        dispatch must exclude it once it is explicitly superseded."""
        placeholder = TaskState(owner_squad="engineering", description="placeholder")
        assert placeholder.plan_revision == 0
        state = HydraState(
            root_goal="x", plan_revision=1,
            plan_superseded_task_ids=[str(placeholder.task_id)],
        )
        assert task_eligible_for_dispatch(state, placeholder) is False

    def test_bypass_keeps_placeholder_eligible(self):
        """H: force-dispatch bypass must not supersede the placeholder — the
        placeholder is exactly what keeps the workflow running when the plan
        is bypassed."""
        placeholder = TaskState(owner_squad="engineering", description="placeholder")
        state = HydraState(root_goal="x", plan_status="bypassed",
                           plan_superseded_task_ids=[])
        assert task_eligible_for_dispatch(state, placeholder) is True


class TestDefectHBypassNoOp:
    def test_materialise_plan_steps_is_noop_when_bypassed(self):
        wf = uuid4()
        plan = _minimal_plan_dict(wf, steps=[
            {"step_id": "a", "target_squad": "engineering", "envelope_type": "DEV_TASK",
             "description": "x", "depends_on": []},
        ])
        state = HydraState(
            root_goal="x", workflow_id=wf, plan_status="bypassed", plan_revision=1,
            plan_ref=plan, pending_hitl=None,
        )
        patch = materialise_plan_steps(state)
        assert patch == {}, "a bypassed plan must never be overwritten to approved"


class TestDefectFSharedPredicate:
    def test_next_stub_attended_task_honours_unmet_dependency(self):
        packs = {"stubby": _stub_pack("stubby")}
        upstream = TaskState(owner_squad="engineering", description="upstream")
        blocked = TaskState(owner_squad="stubby", description="blocked",
                            depends_on=[str(upstream.task_id)])
        state = HydraState(root_goal="x")
        state.tasks.extend([upstream, blocked])
        task, pack = _next_stub_attended_task(state, packs)
        assert task is None, (
            "the stub selector must honour depends_on, not just stale revision "
            "-- this was the actual gap defect F closes"
        )

    def test_next_stub_attended_task_releases_once_dependency_done(self):
        packs = {"stubby": _stub_pack("stubby")}
        upstream = TaskState(owner_squad="engineering", description="upstream")
        blocked = TaskState(owner_squad="stubby", description="blocked",
                            depends_on=[str(upstream.task_id)])
        state = HydraState(root_goal="x", attended_done_task_ids=[str(upstream.task_id)])
        state.tasks.extend([upstream, blocked])
        task, pack = _next_stub_attended_task(state, packs)
        assert task is not None and task.description == "blocked"

    def test_next_attended_task_skips_superseded_even_at_revision_zero(self, packs):
        placeholder = TaskState(owner_squad="engineering", description="placeholder")
        real = TaskState(owner_squad="engineering", description="real step", plan_revision=1)
        state = HydraState(
            root_goal="x", plan_revision=1,
            plan_superseded_task_ids=[str(placeholder.task_id)],
        )
        state.tasks.extend([placeholder, real])
        task, kind, pack = _next_attended_task(state, packs)
        assert task is not None and task.description == "real step"

    def test_attended_pending_task_ids_excludes_superseded(self):
        placeholder = TaskState(owner_squad="engineering", description="placeholder")
        real = TaskState(owner_squad="engineering", description="real", plan_revision=1)
        state = HydraState(
            root_goal="x", plan_revision=1,
            plan_superseded_task_ids=[str(placeholder.task_id)],
        )
        state.tasks.extend([placeholder, real])
        pending = _attended_pending_task_ids(state)
        assert str(placeholder.task_id) not in pending
        assert str(real.task_id) in pending

    def test_dispatch_excludes_superseded_task_with_distinct_target_repos(self, monkeypatch):
        """node_dispatch (and, ahead of it, the fleet candidate list -- both
        now share `task_eligible_for_dispatch`) must never dispatch a
        superseded task, proven here with two DISTINCT target_repo_id
        engineering tasks (the shape the fleet candidate filter cares about),
        one of which is superseded."""
        from hydra_core.supervisor import build_supervisor, _PurePythonRunner

        calls: list[str] = []

        def _fake_execute_squad(state, pack, env, dispatcher, **kw):
            calls.append(env.objective if hasattr(env, "objective") else "?")
            from hydra_core.squad_node import SquadResult
            return SquadResult(envelopes=[], artifacts=[], status="done", rationale="ok")

        monkeypatch.setattr("hydra_core.supervisor.execute_squad", _fake_execute_squad)

        class _OfflineDispatcher:
            allow_offline_mcp_dispatch = True
            live_execution = False

        runner = build_supervisor(
            project_root=HYDRA_ROOT, dispatcher=_OfflineDispatcher(),
            force_pure_python=True, force_trivial_plan_rigor=True,
        )
        assert isinstance(runner, _PurePythonRunner)

        superseded = TaskState(owner_squad="engineering", description="superseded-task",
                               target_repo_id="hydra")
        live = TaskState(owner_squad="engineering", description="live-task",
                         target_repo_id="hydra-two")
        state = HydraState(
            root_goal="x", selected_squads=["engineering"], target_repo_id="hydra",
            plan_superseded_task_ids=[str(superseded.task_id)],
        )
        state.tasks.extend([superseded, live])

        final = runner.invoke(state, stop_before="judge_per_squad")
        by_desc = {t.description: t for t in final.tasks}
        assert by_desc["superseded-task"].status == "pending", (
            "a superseded task must never be dispatched, sequential or fleet"
        )
        assert by_desc["live-task"].status != "pending"
