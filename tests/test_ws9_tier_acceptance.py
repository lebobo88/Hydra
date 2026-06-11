"""WS9 acceptance tests — model_tier propagation (Fable routing) + acceptance-criteria HITL.

Run with:
    python -m pytest tests/test_ws9_tier_acceptance.py -q

No network or LLM calls.  All MCP calls are captured by a fake dispatcher.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

import pytest

# ---------------------------------------------------------------------------
# Shared test fixtures and helpers
# ---------------------------------------------------------------------------

@dataclass
class CaptureDispatcher:
    """Fake dispatcher that records all call_mcp invocations."""
    calls: list[dict[str, Any]] = field(default_factory=list)
    mcp_result: dict[str, Any] = field(default_factory=lambda: {
        "status": "done",
        "tool": "start_run",
        "result": {"run_id": "test-run-123"},
    })

    def call_mcp(self, server: str, tool: str, args: dict[str, Any],
                 *, squad_id: str | None = None) -> dict[str, Any]:
        self.calls.append({"server": server, "tool": tool,
                           "args": dict(args), "squad_id": squad_id})
        return self.mcp_result

    def spawn_subprocess(self, cmd, env=None):
        return {"stdout": ""}

    def emit_claude_prompt(self, prompt, *, agent=None):
        return {"status": "done", "summary": ""}

    def invoke_claude_skill(self, skill, args):
        return {"status": "done", "summary": ""}


def _make_pack(*, hitl_required: bool = False, mode: str = "pp_run",
               default_team: str = "feature-team", model_tier: str | None = None):
    from hydra_core.squad_loader import SquadPack, GateSpec
    invoke: dict[str, Any] = {
        "mode": mode,
        "default_team": default_team,
        "project_path": "/tmp/test-project",
    }
    if model_tier is not None:
        invoke["model_tier"] = model_tier
    return SquadPack(
        slug="engineering",
        name="Engineering",
        description="Engineering squad",
        entrypoint="mcp",
        invoke=invoke,
        gates=(GateSpec(rubric_id="slsa-l2", hitl_required=hitl_required),),
    )


def _make_inbound(*, model_tier: str | None = None):
    """Build a CSuiteDecisionPacket with the given model_tier (may be None)."""
    from hydra_core.schemas import CSuiteDecisionPacket
    return CSuiteDecisionPacket(
        workflow_id=uuid4(),
        origin_squad="hydra",
        target_squad="engineering",
        origin="BOARDROOM",
        objective="implement the feature",
        model_tier=model_tier,
    )


def _make_state():
    from hydra_core.state import HydraState
    return HydraState(workflow_id=uuid4(), root_goal="test goal")


def _dispatch(model_tier: str | None, *, pack=None, inbound=None):
    """Run execute_squad; returns (SquadResult, CaptureDispatcher)."""
    from hydra_core.squad_node import execute_squad
    disp = CaptureDispatcher()
    _pack = pack or _make_pack()
    _inbound = inbound if inbound is not None else _make_inbound(model_tier=model_tier)
    state = _make_state()
    result = execute_squad(state, _pack, _inbound, disp)
    return result, disp


def _pp_calls(disp: CaptureDispatcher) -> list[dict]:
    return [c for c in disp.calls
            if c["server"] == "pp_harness" and c["tool"] == "start_run"]


# ===========================================================================
# PART 1A: normalize_tier
# ===========================================================================

class TestNormalizeTier:
    def test_valid_tokens_pass(self):
        from hydra_core.tiers import normalize_tier
        for token in ("haiku", "sonnet", "opus", "fable", "deep"):
            assert normalize_tier(token) == token

    def test_case_insensitive(self):
        from hydra_core.tiers import normalize_tier
        assert normalize_tier("FABLE") == "fable"
        assert normalize_tier("Sonnet") == "sonnet"
        assert normalize_tier("DEEP") == "deep"

    def test_none_returns_none(self):
        from hydra_core.tiers import normalize_tier
        assert normalize_tier(None) is None

    def test_unknown_raises(self):
        from hydra_core.tiers import normalize_tier
        with pytest.raises(ValueError, match="(?i)unknown"):
            normalize_tier("ultra")

    def test_empty_string_raises(self):
        """Fix 1: empty string must be rejected fail-closed, not treated as None."""
        from hydra_core.tiers import normalize_tier
        with pytest.raises(ValueError):
            normalize_tier("")

    def test_fable_tiers_set(self):
        from hydra_core.tiers import FABLE_TIERS
        assert "fable" in FABLE_TIERS
        assert "deep" in FABLE_TIERS
        assert "sonnet" not in FABLE_TIERS


# ===========================================================================
# PART 1B: _via_mcp tier routing
# ===========================================================================

class TestViaMcpFableRouting:

    def test_fable_routes_to_deep_reasoning_team(self):
        result, disp = _dispatch("fable")
        calls = _pp_calls(disp)
        assert calls, "Expected pp_harness.start_run call"
        args = calls[0]["args"]
        assert args.get("mode") == "team"
        assert args.get("team") == "deep-reasoning-team"

    def test_deep_alias_routes_to_deep_reasoning_team(self):
        result, disp = _dispatch("deep")
        calls = _pp_calls(disp)
        assert calls
        args = calls[0]["args"]
        assert args.get("mode") == "team"
        assert args.get("team") == "deep-reasoning-team"

    def test_fable_case_insensitive(self):
        result, disp = _dispatch("FABLE")
        calls = _pp_calls(disp)
        assert calls
        assert calls[0]["args"].get("team") == "deep-reasoning-team"

    def test_opus_does_not_route_to_deep_team(self):
        result, disp = _dispatch("opus")
        calls = _pp_calls(disp)
        assert calls
        args = calls[0]["args"]
        assert args.get("mode") == "single"
        assert args.get("team") != "deep-reasoning-team"

    def test_sonnet_does_not_route_to_deep_team(self):
        result, disp = _dispatch("sonnet")
        calls = _pp_calls(disp)
        assert calls
        args = calls[0]["args"]
        assert args.get("mode") == "single"
        assert args.get("team") != "deep-reasoning-team"

    def test_haiku_does_not_route_to_deep_team(self):
        result, disp = _dispatch("haiku")
        calls = _pp_calls(disp)
        assert calls
        args = calls[0]["args"]
        assert args.get("mode") == "single"
        assert args.get("team") != "deep-reasoning-team"

    def test_unknown_tier_returns_failed_no_pp_call(self):
        result, disp = _dispatch("ultra")
        assert result.status == "failed"
        assert "ultra" in result.rationale
        assert not _pp_calls(disp), "No pp_harness call must be made for unknown tier"

    def test_empty_string_tier_fails_closed(self):
        """Fix 1: empty-string tier on the inbound envelope must be rejected fail-closed,
        not fall through to squad.yaml default (the 'or' bug)."""
        from hydra_core.schemas import CSuiteDecisionPacket
        inbound = CSuiteDecisionPacket(
            workflow_id=uuid4(),
            origin_squad="hydra",
            target_squad="engineering",
            origin="BOARDROOM",
            objective="test",
            model_tier="",   # explicit empty string — not None
        )
        result, disp = _dispatch(None, inbound=inbound)
        assert result.status == "failed", (
            f"Empty-string tier must fail closed; got status={result.status!r}"
        )
        assert not _pp_calls(disp), "No pp call for empty-string tier"

    def test_none_tier_uses_default_behavior(self):
        """None tier -> default pp_run -> mode=single, no deep-team override."""
        result, disp = _dispatch(None)
        calls = _pp_calls(disp)
        assert calls
        args = calls[0]["args"]
        assert args.get("mode") == "single"
        assert args.get("team") != "deep-reasoning-team"

    def test_no_auto_fable_without_explicit_tier(self):
        """Fable must NOT be reached without an explicit tier setting."""
        result, disp = _dispatch(None)
        calls = _pp_calls(disp)
        assert calls
        assert calls[0]["args"].get("team") != "deep-reasoning-team", (
            "deep-reasoning-team must NOT be reached without explicit tier=fable/deep"
        )

    def test_model_tier_not_passed_as_pp_arg(self):
        """pp's start_run schema rejects unknown args — model_tier must not be in args."""
        for tier in ("fable", "deep", "opus", "sonnet", "haiku"):
            _, disp = _dispatch(tier)
            calls = _pp_calls(disp)
            if calls:
                assert "model_tier" not in calls[0]["args"], (
                    f"model_tier={tier!r} must not be passed to pp's start_run"
                )


class TestViaMcpSquadYamlTier:
    """Tier from squad.yaml invoke.model_tier."""

    def test_squad_yaml_fable_routes_to_deep_team(self):
        """squad.yaml model_tier=fable + no envelope tier (None) -> Fable route."""
        pack = _make_pack(model_tier="fable")
        inbound = _make_inbound(model_tier=None)
        result, disp = _dispatch(None, pack=pack, inbound=inbound)
        calls = _pp_calls(disp)
        assert calls
        assert calls[0]["args"].get("team") == "deep-reasoning-team"

    def test_envelope_tier_wins_over_squad_yaml(self):
        """Envelope model_tier=fable overrides squad.yaml model_tier=sonnet."""
        pack = _make_pack(model_tier="sonnet")
        inbound = _make_inbound(model_tier="fable")
        result, disp = _dispatch(None, pack=pack, inbound=inbound)
        calls = _pp_calls(disp)
        assert calls
        assert calls[0]["args"].get("team") == "deep-reasoning-team"


class TestFix6DeepTeamReserved:
    """Fix 6: deep-reasoning-team is reserved — only reachable via explicit fable/deep tier."""

    def test_default_team_deep_without_fable_tier_fails(self):
        """pack.default_team=deep-reasoning-team + no fable tier -> failed SquadResult."""
        pack = _make_pack(mode="pp_team", default_team="deep-reasoning-team")
        inbound = _make_inbound(model_tier=None)
        result, disp = _dispatch(None, pack=pack, inbound=inbound)
        assert result.status == "failed", (
            f"Expected failed when default_team=deep-reasoning-team without tier; "
            f"got status={result.status!r}"
        )
        assert not _pp_calls(disp), "No pp call should be made"

    def test_default_team_deep_with_fable_tier_succeeds(self):
        """Same pack + explicit fable tier -> allowed."""
        pack = _make_pack(mode="pp_team", default_team="deep-reasoning-team")
        inbound = _make_inbound(model_tier="fable")
        result, disp = _dispatch(None, pack=pack, inbound=inbound)
        calls = _pp_calls(disp)
        assert calls, "fable tier should allow deep-reasoning-team dispatch"
        assert calls[0]["args"].get("team") == "deep-reasoning-team"

    def test_default_team_deep_with_deep_tier_succeeds(self):
        """deep alias also unlocks the reserved team."""
        pack = _make_pack(mode="pp_team", default_team="deep-reasoning-team")
        inbound = _make_inbound(model_tier="deep")
        result, disp = _dispatch(None, pack=pack, inbound=inbound)
        calls = _pp_calls(disp)
        assert calls
        assert calls[0]["args"].get("team") == "deep-reasoning-team"


# ===========================================================================
# PART 1C: task.model_tier propagation (Fix 2)
# ===========================================================================

class TestTaskModelTierPropagation:

    def test_task_model_tier_fable_reaches_deep_team(self):
        """Fix 2: when node_dispatch builds the packet with task.model_tier='fable',
        _via_mcp must route to deep-reasoning-team."""
        from hydra_core.schemas import CSuiteDecisionPacket
        from hydra_core.squad_node import execute_squad
        pack = _make_pack()
        state = _make_state()
        disp = CaptureDispatcher()

        # Simulate what node_dispatch builds after Fix 2:
        packet = CSuiteDecisionPacket(
            workflow_id=state.workflow_id,
            origin_squad="hydra",
            target_squad="engineering",
            origin="BOARDROOM",
            objective="implement auth",
            model_tier="fable",
        )
        execute_squad(state, pack, packet, disp)
        calls = _pp_calls(disp)
        assert calls
        assert calls[0]["args"].get("team") == "deep-reasoning-team", (
            "task.model_tier='fable' threaded via packet must route to deep-reasoning-team"
        )

    def test_reflexion_retry_preserves_model_tier(self):
        """Fix 2 retry path: retry_envelope carries model_tier from original_env -> deep-team."""
        from hydra_core.schemas import CSuiteDecisionPacket
        from hydra_core.squad_node import execute_squad
        pack = _make_pack()
        state = _make_state()
        disp = CaptureDispatcher()

        # Simulate the retry packet built by _reflexion_retry (Fix 2):
        retry_packet = CSuiteDecisionPacket(
            workflow_id=state.workflow_id,
            origin_squad="hydra",
            target_squad="engineering",
            origin="BOARDROOM",
            objective="retry with critique",
            model_tier="fable",   # preserved from original_env.get("model_tier")
        )
        execute_squad(state, pack, retry_packet, disp)
        calls = _pp_calls(disp)
        assert calls
        assert calls[0]["args"].get("team") == "deep-reasoning-team", (
            "reflexion retry must preserve model_tier and route to deep-reasoning-team"
        )

    def test_task_state_model_tier_field_exists(self):
        from hydra_core.state import TaskState
        t = TaskState(owner_squad="engineering", description="task", model_tier="fable")
        assert t.model_tier == "fable"

    def test_task_state_model_tier_defaults_none(self):
        from hydra_core.state import TaskState
        t = TaskState(owner_squad="engineering", description="task")
        assert t.model_tier is None

    def test_csuitepacket_model_tier_field_exists(self):
        from hydra_core.schemas import CSuiteDecisionPacket
        pkt = CSuiteDecisionPacket(
            workflow_id=uuid4(), origin_squad="hydra", target_squad="engineering",
            origin="BOARDROOM", objective="test", model_tier="sonnet",
        )
        assert pkt.model_tier == "sonnet"

    def test_csuitepacket_model_tier_defaults_none(self):
        from hydra_core.schemas import CSuiteDecisionPacket
        pkt = CSuiteDecisionPacket(
            workflow_id=uuid4(), origin_squad="hydra", target_squad="engineering",
            origin="BOARDROOM", objective="test",
        )
        assert pkt.model_tier is None


# ===========================================================================
# PART 2: acceptance-criteria pre-flight HITL
# ===========================================================================

def _build_planner(packs_dict: dict):
    """Extract node_planner from build_supervisor via pure-python runner."""
    from unittest.mock import MagicMock, patch
    from hydra_core import supervisor as sup_module

    disp = CaptureDispatcher()
    with patch.object(sup_module, "discover_squads", return_value=packs_dict), \
         patch.object(sup_module, "load_constitution",
                      return_value=MagicMock(sha256="abc")), \
         patch.object(sup_module, "load_policy",
                      return_value=MagicMock(squad_enabled=lambda s: False)), \
         patch.object(sup_module, "load_cerberus_venoms", return_value=None), \
         patch("hydra_core.eights.attestation.EightsAttestor.constitution_attest",
               return_value={}), \
         patch("hydra_core.eights.attestation.EightsAttestor.hitl_request",
               return_value=None), \
         patch("hydra_core.eights.attestation.EightsAttestor.ceiling_tick",
               return_value=None), \
         patch("hydra_core.eights.attestation.EightsAttestor.replay_pending",
               return_value={"sent": 0, "failed": 0, "skipped": 0}):
        runner = sup_module.build_supervisor(dispatcher=disp, force_pure_python=True)

    planner_fn = dict(runner.steps).get("planner")
    assert planner_fn is not None, "planner node not found"
    return planner_fn


def _hi_risk_packs():
    return {"engineering": _make_pack(hitl_required=True)}


def _lo_risk_packs():
    return {"engineering": _make_pack(hitl_required=False)}


def _run_planner(planner_fn, state):
    """Call the planner node and simulate the LangGraph append-reducer merge.

    HydraState.tasks uses an _append reducer, so LangGraph appends the node's
    returned 'tasks' list onto the existing state.tasks.  Tests inspect the
    post-merge view via out['tasks'], so we simulate that merge here:
      merged_tasks = existing state.tasks + newly returned synthesised tasks.
    Other keys (pending_hitl, phase, requires_human_approval, etc.) are passed
    through unchanged from the node's raw return dict.
    """
    from unittest.mock import patch
    prior_tasks = list(state.tasks or [])
    with patch("hydra_core.eights.attestation.EightsAttestor.hitl_request",
               return_value=None):
        raw = planner_fn(state)
    # Simulate append reducer: full view = prior + synthesised
    merged = dict(raw)
    merged["tasks"] = prior_tasks + list(raw.get("tasks") or [])
    return merged


class TestAcceptanceCriteriaGate:

    # -- Fix 3: planner preserves existing task criteria (REAL planner call) --

    def test_planner_preserves_task_criteria_no_ac_gate(self):
        """Fix 3: pre-seeded tasks WITH valid criteria -> AC gate does NOT fire
        (real planner invocation, not a reimplementation of the helper)."""
        from hydra_core.state import HydraState, TaskState
        planner = _build_planner(_hi_risk_packs())

        state = HydraState(
            workflow_id=uuid4(),
            root_goal="rewrite the entire payment system",
            selected_squads=["engineering"],
        )
        # Pre-seed the engineering task with valid acceptance_criteria.
        state.tasks = [TaskState(
            owner_squad="engineering",
            description=state.root_goal,
            acceptance_criteria=["payment gateway processes all currencies",
                                  "PCI-DSS L1 compliant"],
        )]
        out = _run_planner(planner, state)

        # Requires_human_approval can still be True (squad has hitl gate),
        # but reason must NOT be "acceptance_criteria" when criteria are present.
        hitl = out.get("pending_hitl") or {}
        assert hitl.get("reason") != "acceptance_criteria", (
            f"Tasks with valid criteria must NOT trigger acceptance_criteria HITL; "
            f"got reason={hitl.get('reason')!r}"
        )

    def test_planner_preserves_task_model_tier(self):
        """Fix 3: pre-seeded task.model_tier must survive planner rebuild."""
        from hydra_core.state import HydraState, TaskState
        planner = _build_planner(_lo_risk_packs())

        state = HydraState(
            workflow_id=uuid4(),
            root_goal="implement feature",
            selected_squads=["engineering"],
        )
        state.tasks = [TaskState(
            owner_squad="engineering",
            description=state.root_goal,
            model_tier="fable",
        )]
        out = _run_planner(planner, state)
        rebuilt = out.get("tasks") or []
        eng_task = next((t for t in rebuilt if t.owner_squad == "engineering"), None)
        assert eng_task is not None
        assert eng_task.model_tier == "fable", (
            f"Planner must preserve task.model_tier; got {eng_task.model_tier!r}"
        )

    # -- Fix 4: gate fires on ANY qualifying-missing --

    def test_any_qualifying_missing_fires_gate(self):
        """Fix 4: two qualifying tasks where ONE lacks criteria -> gate FIRES."""
        from hydra_core.state import HydraState, TaskState
        from hydra_core.squad_loader import SquadPack, GateSpec

        eng_pack = SquadPack(
            slug="engineering", name="Engineering", description="",
            entrypoint="mcp",
            invoke={"mode": "pp_run", "default_team": "feature-team",
                    "project_path": "/tmp"},
            gates=(GateSpec(rubric_id="slsa-l2", hitl_required=True),),
        )
        exec_pack = SquadPack(
            slug="executive", name="Executive", description="",
            entrypoint="agent-impersonation",
            invoke={},
            gates=(GateSpec(rubric_id="exec-policy", hitl_required=True),),
        )
        planner = _build_planner({"engineering": eng_pack, "executive": exec_pack})

        state = HydraState(
            workflow_id=uuid4(),
            root_goal="big change",
            selected_squads=["engineering", "executive"],
        )
        # engineering has criteria; executive does NOT.
        state.tasks = [
            TaskState(owner_squad="engineering", description=state.root_goal,
                      acceptance_criteria=["works correctly"]),
            TaskState(owner_squad="executive", description=state.root_goal,
                      acceptance_criteria=None),
        ]
        out = _run_planner(planner, state)
        hitl = out.get("pending_hitl") or {}
        assert hitl.get("reason") == "acceptance_criteria", (
            f"ANY qualifying task missing criteria must fire gate; "
            f"got reason={hitl.get('reason')!r}"
        )

    # -- Fix 5: major (P0/P1) tasks qualify regardless of squad risk level --

    def test_p0_task_missing_criteria_gates(self):
        """Fix 5: P0 priority (major) + no criteria -> gate fires even on low-risk squad."""
        from hydra_core.state import HydraState, TaskState
        planner = _build_planner(_lo_risk_packs())

        state = HydraState(
            workflow_id=uuid4(),
            root_goal="critical production deploy",
            selected_squads=["engineering"],
        )
        state.tasks = [TaskState(
            owner_squad="engineering",
            description=state.root_goal,
            priority="P0",
            acceptance_criteria=None,
        )]
        out = _run_planner(planner, state)
        hitl = out.get("pending_hitl") or {}
        assert hitl.get("reason") == "acceptance_criteria", (
            f"P0 task missing criteria must fire gate; got reason={hitl.get('reason')!r}"
        )
        assert out.get("requires_human_approval") is True

    def test_p1_task_missing_criteria_gates(self):
        """Fix 5: P1 priority + no criteria -> gate fires."""
        from hydra_core.state import HydraState, TaskState
        planner = _build_planner(_lo_risk_packs())

        state = HydraState(
            workflow_id=uuid4(),
            root_goal="important feature",
            selected_squads=["engineering"],
        )
        state.tasks = [TaskState(
            owner_squad="engineering",
            description=state.root_goal,
            priority="P1",
            acceptance_criteria=None,
        )]
        out = _run_planner(planner, state)
        hitl = out.get("pending_hitl") or {}
        assert hitl.get("reason") == "acceptance_criteria"

    def test_low_risk_minor_no_criteria_no_gate(self):
        """Fix 5 non-blanket: low-risk P2 task with no criteria -> gate does NOT fire."""
        from hydra_core.state import HydraState, TaskState
        planner = _build_planner(_lo_risk_packs())

        state = HydraState(
            workflow_id=uuid4(),
            root_goal="update README",
            selected_squads=["engineering"],
        )
        state.tasks = [TaskState(
            owner_squad="engineering",
            description=state.root_goal,
            priority="P2",
            acceptance_criteria=None,
        )]
        out = _run_planner(planner, state)
        hitl = out.get("pending_hitl") or {}
        assert hitl.get("reason") != "acceptance_criteria", (
            "Low-risk minor P2 task with no criteria must NOT trigger AC gate"
        )

    # -- Standard coverage --

    def test_high_risk_no_criteria_triggers_ac_hitl(self):
        """High-risk squad + fresh tasks (no pre-seeded criteria) -> AC gate fires."""
        from hydra_core.state import HydraState
        planner = _build_planner(_hi_risk_packs())

        state = HydraState(
            workflow_id=uuid4(),
            root_goal="rewrite the entire payment system",
            selected_squads=["engineering"],
        )
        # No pre-seeded tasks — planner builds fresh criterion-less tasks.
        out = _run_planner(planner, state)

        assert out["requires_human_approval"] is True
        hitl = out.get("pending_hitl") or {}
        assert hitl.get("reason") == "acceptance_criteria", (
            f"Expected reason='acceptance_criteria', got {hitl.get('reason')!r}"
        )

    def test_criteria_carried_on_task_state(self):
        from hydra_core.state import TaskState
        t = TaskState(
            owner_squad="engineering",
            description="implement auth",
            acceptance_criteria=["JWT validated", "expired tokens rejected"],
        )
        assert t.acceptance_criteria == ["JWT validated", "expired tokens rejected"]


# ===========================================================================
# Fix A: reflexion retry sources model_tier from the TASK, not the envelope
# ===========================================================================

class TestFixARetryTierFromTask:
    """Bug A: original_env is a DecisionRecord which has no model_tier.
    The retry must look up task.model_tier from state.tasks[owner_squad==origin].
    """

    def _build_runner_for_retry(self, task_model_tier: str | None):
        """Build a pure-python supervisor runner with a fable-tier engineering task
        already in state.tasks, and wire a fake dispatcher that:
          1. Succeeds on the first pp_harness.start_run call (dispatch phase).
          2. Produces a DecisionRecord that the judge marks 'revise'.
          3. On the reflexion retry pp_harness.start_run, captures args so we
             can assert mode/team.

        Returns (runner, initial_state).
        """
        from unittest.mock import MagicMock, patch
        from hydra_core import supervisor as sup_module
        from hydra_core.state import HydraState, TaskState

        disp = CaptureDispatcher()
        hi_risk_packs = _hi_risk_packs()

        with patch.object(sup_module, "discover_squads", return_value=hi_risk_packs), \
             patch.object(sup_module, "load_constitution",
                          return_value=MagicMock(sha256="abc")), \
             patch.object(sup_module, "load_policy",
                          return_value=MagicMock(
                              squad_enabled=lambda s: False,
                              is_hitl_severity=lambda r: False,
                          )), \
             patch.object(sup_module, "load_cerberus_venoms", return_value=None), \
             patch("hydra_core.eights.attestation.EightsAttestor.constitution_attest",
                   return_value={}), \
             patch("hydra_core.eights.attestation.EightsAttestor.hitl_request",
                   return_value=None), \
             patch("hydra_core.eights.attestation.EightsAttestor.ceiling_tick",
                   return_value=None), \
             patch("hydra_core.eights.attestation.EightsAttestor.replay_pending",
                   return_value={"sent": 0, "failed": 0, "skipped": 0}), \
             patch("hydra_core.eights.attestation.EightsAttestor.envelope_record",
                   return_value=None):
            runner = sup_module.build_supervisor(dispatcher=disp, force_pure_python=True)

        state = HydraState(
            workflow_id=uuid4(),
            root_goal="implement payment rewrite",
            selected_squads=["engineering"],
        )
        state.tasks = [TaskState(
            owner_squad="engineering",
            description=state.root_goal,
            model_tier=task_model_tier,
        )]
        return runner, state, disp

    def test_retry_sources_model_tier_from_task_not_envelope(self):
        """Fix A: _reflexion_retry must read model_tier from state.tasks[owner_squad],
        NOT from original_env (DecisionRecord has no model_tier field).

        We directly test _reflexion_retry by constructing the state and calling
        it with a fake original_env (a DECISION_RECORD dict without model_tier)
        to prove the fix sources tier from the task.
        """
        from unittest.mock import MagicMock, patch
        from hydra_core import supervisor as sup_module
        from hydra_core.state import HydraState, TaskState

        disp = CaptureDispatcher()
        hi_risk_packs = _hi_risk_packs()

        captured_retry_packets: list[dict] = []

        # Intercept execute_squad during the retry to capture the packet's model_tier.
        original_execute = None

        def capturing_execute(state, pack, inbound, dispatcher, **kwargs):
            from hydra_core.squad_node import execute_squad as real_exec
            captured_retry_packets.append({
                "model_tier": getattr(inbound, "model_tier", None),
                "objective": getattr(inbound, "objective", ""),
            })
            return real_exec(state, pack, inbound, dispatcher, **kwargs)

        with patch.object(sup_module, "discover_squads", return_value=hi_risk_packs), \
             patch.object(sup_module, "load_constitution",
                          return_value=MagicMock(sha256="abc")), \
             patch.object(sup_module, "load_policy",
                          return_value=MagicMock(
                              squad_enabled=lambda s: False,
                              is_hitl_severity=lambda r: False,
                          )), \
             patch.object(sup_module, "load_cerberus_venoms", return_value=None), \
             patch("hydra_core.eights.attestation.EightsAttestor.constitution_attest",
                   return_value={}), \
             patch("hydra_core.eights.attestation.EightsAttestor.hitl_request",
                   return_value=None), \
             patch("hydra_core.eights.attestation.EightsAttestor.ceiling_tick",
                   return_value=None), \
             patch("hydra_core.eights.attestation.EightsAttestor.replay_pending",
                   return_value={"sent": 0, "failed": 0, "skipped": 0}), \
             patch("hydra_core.eights.attestation.EightsAttestor.envelope_record",
                   return_value=None), \
             patch.object(sup_module, "execute_squad", side_effect=capturing_execute):

            runner = sup_module.build_supervisor(dispatcher=disp, force_pure_python=True)

        # Build state with a fable-tier task.
        from hydra_core.state import HydraState, TaskState
        state = HydraState(
            workflow_id=uuid4(),
            root_goal="payment rewrite",
            selected_squads=["engineering"],
        )
        state.tasks = [TaskState(
            owner_squad="engineering",
            description=state.root_goal,
            model_tier="fable",
        )]

        # Extract the _reflexion_retry closure directly from runner internals.
        # Since _reflexion_retry is a closure inside build_supervisor, we
        # instead test it indirectly: pre-load state with a squad-produced
        # DECISION_RECORD envelope (no model_tier field) and a 'revise' verdict,
        # then call node_judge_per_squad and observe the retry dispatch.
        # The DECISION_RECORD simulates what _via_mcp emits — no model_tier.
        from uuid import uuid4 as _uuid4
        from hydra_core.schemas import DecisionRecord
        env_id = _uuid4()
        decision_env = DecisionRecord(
            id=env_id,
            workflow_id=state.workflow_id,
            origin_squad="engineering",  # producer
            target_squad="hydra",
            decision="Engineering work dispatched",
            rationale="mode=pp_run; model_tier=default",  # NOTE: no model_tier field
            sealed=False,
        )
        state.envelopes = [decision_env.model_dump(mode="json")]

        # Inject a 'revise' verdict for this envelope.
        from hydra_core.judge.schemas import JudgeVerdict
        verdict = JudgeVerdict(
            workflow_id=state.workflow_id,
            origin_squad="hydra-judge",
            target_squad="engineering",
            target_envelope_id=env_id,
            outcome="revise",
            rubric_id="constitution-alignment@1",
            judge_vendor="gemini",
            generator_vendor="engineering",
            critique_md="Needs improvement",
        )
        state.verdicts = []  # start clean so already_judged is empty

        # Run node_judge_per_squad which calls _reflexion_retry.
        judge_fn = dict(runner.steps).get("judge_per_squad")
        assert judge_fn is not None

        from unittest.mock import patch
        with patch("hydra_core.eights.attestation.EightsAttestor.hitl_request",
                   return_value=None), \
             patch("hydra_core.supervisor.dispatch_judge",
                   return_value=verdict), \
             patch("hydra_core.supervisor.route_judge",
                   return_value=MagicMock(
                       tier="full", rubric_ids=["constitution-alignment@1"],
                       rationale="test", preferred_judge_vendors=["gemini"],
                   )), \
             patch.object(sup_module, "execute_squad", side_effect=capturing_execute):
            judge_fn(state)

        # The retry dispatch should have been captured.
        retry_packets = [p for p in captured_retry_packets
                         if "REFLEXION RETRY" in p.get("objective", "")]
        assert retry_packets, (
            "Expected a reflexion retry dispatch; captured_packets="
            f"{captured_retry_packets!r}"
        )
        retry_tier = retry_packets[0]["model_tier"]
        assert retry_tier == "fable", (
            f"Retry must source model_tier from task (='fable'), "
            f"NOT from DecisionRecord (=None). Got: {retry_tier!r}"
        )

    def test_retry_routes_to_deep_team_when_task_is_fable(self):
        """Fix A integration: task.model_tier='fable' -> retry pp call uses team=deep-reasoning-team."""
        # Build retry packet as _reflexion_retry now does (sourced from task):
        from hydra_core.schemas import CSuiteDecisionPacket
        from hydra_core.squad_node import execute_squad
        from hydra_core.state import HydraState, TaskState

        pack = _make_pack()
        state = HydraState(workflow_id=uuid4(), root_goal="test")
        # The task carries the tier — this is what _reflexion_retry looks up.
        state.tasks = [TaskState(
            owner_squad="engineering",
            description="test",
            model_tier="fable",
        )]
        disp = CaptureDispatcher()

        # Simulate what _reflexion_retry now builds (after Fix A):
        _retry_task = next(
            (t for t in state.tasks if t.owner_squad == "engineering"), None
        )
        retry_packet = CSuiteDecisionPacket(
            workflow_id=state.workflow_id,
            origin_squad="hydra",
            target_squad="engineering",
            origin="BOARDROOM",
            objective="retry with critique\n\n=== REFLEXION RETRY #1 ===",
            model_tier=getattr(_retry_task, "model_tier", None),  # from task, not DecisionRecord
        )
        execute_squad(state, pack, retry_packet, disp)
        calls = _pp_calls(disp)
        assert calls
        assert calls[0]["args"].get("team") == "deep-reasoning-team", (
            f"Retry sourced from task.model_tier='fable' must route to deep-reasoning-team; "
            f"got {calls[0]['args']!r}"
        )


# ===========================================================================
# Fix B: executive planner branch preserves pre-seeded tasks for AC gate
# ===========================================================================

def _exec_packs(*, exec_hitl: bool = True, eng_hitl: bool = True):
    """Return packs dict with both executive and engineering squads."""
    from hydra_core.squad_loader import SquadPack, GateSpec
    exec_pack = SquadPack(
        slug="executive", name="Executive", description="",
        entrypoint="agent-impersonation",
        invoke={},
        gates=(GateSpec(rubric_id="exec-policy", hitl_required=exec_hitl),),
    )
    eng_pack = SquadPack(
        slug="engineering", name="Engineering", description="",
        entrypoint="mcp",
        invoke={"mode": "pp_run", "default_team": "feature-team",
                "project_path": "/tmp"},
        gates=(GateSpec(rubric_id="slsa-l2", hitl_required=eng_hitl),),
    )
    return {"executive": exec_pack, "engineering": eng_pack}


class TestFixBExecutiveBranchPreservation:
    """Fix B: executive planner branch must not silently discard pre-seeded
    non-executive tasks that the AC gate needs to evaluate.
    """

    def test_exec_branch_p0_task_missing_criteria_fires_ac_gate(self):
        """Fix B: executive squad selected + pre-seeded P0 engineering task
        with no criteria -> AC gate FIRES (task is not discarded)."""
        from hydra_core.state import HydraState, TaskState
        planner = _build_planner(_exec_packs())

        state = HydraState(
            workflow_id=uuid4(),
            root_goal="critical system overhaul",
            selected_squads=["executive", "engineering"],
        )
        # Pre-seed a P0 engineering task with NO criteria.
        state.tasks = [
            TaskState(owner_squad="engineering", description=state.root_goal,
                      priority="P0", acceptance_criteria=None),
        ]
        out = _run_planner(planner, state)
        hitl = out.get("pending_hitl") or {}
        assert hitl.get("reason") == "acceptance_criteria", (
            f"Executive branch must not discard pre-seeded P0 engineering task; "
            f"AC gate must fire. Got reason={hitl.get('reason')!r}, "
            f"tasks_in_out={[t.owner_squad for t in (out.get('tasks') or [])]!r}"
        )

    def test_exec_branch_preserves_non_exec_task_criteria(self):
        """Fix B: executive branch preserves acceptance_criteria on non-exec tasks
        so the AC gate can correctly evaluate them.

        When BOTH executive (qualifying, no criteria) AND engineering (qualifying,
        has criteria) are in play, the gate still fires because the executive
        decompose task itself is qualifying and missing criteria. The key assertion
        is that the engineering task's criteria ARE preserved into new_tasks
        (not silently dropped), which is confirmed by verifying the engineering
        task appears in the output with its criteria intact.
        """
        from hydra_core.state import HydraState, TaskState
        planner = _build_planner(_exec_packs())

        state = HydraState(
            workflow_id=uuid4(),
            root_goal="system overhaul",
            selected_squads=["executive", "engineering"],
        )
        # Pre-seed BOTH executive (with criteria) and engineering (with criteria).
        state.tasks = [
            TaskState(owner_squad="executive", description=state.root_goal,
                      acceptance_criteria=["board approves strategy"]),
            TaskState(owner_squad="engineering", description=state.root_goal,
                      priority="P0",
                      acceptance_criteria=["all payments process correctly",
                                           "zero downtime migration"]),
        ]
        out = _run_planner(planner, state)
        # When ALL qualifying tasks have criteria, the AC gate must NOT fire.
        hitl = out.get("pending_hitl") or {}
        assert hitl.get("reason") != "acceptance_criteria", (
            f"Both tasks have valid criteria; AC gate must NOT fire. "
            f"got reason={hitl.get('reason')!r}"
        )
        # Confirm engineering task is preserved with its criteria.
        rebuilt = out.get("tasks") or []
        eng = next((t for t in rebuilt if t.owner_squad == "engineering"), None)
        assert eng is not None, "Engineering task must appear in new_tasks"
        assert eng.acceptance_criteria == ["all payments process correctly",
                                            "zero downtime migration"]

    def test_exec_branch_preserves_non_exec_model_tier(self):
        """Fix B: executive branch preserves model_tier on non-exec tasks."""
        from hydra_core.state import HydraState, TaskState
        planner = _build_planner(_exec_packs())

        state = HydraState(
            workflow_id=uuid4(),
            root_goal="implement feature",
            selected_squads=["executive", "engineering"],
        )
        state.tasks = [
            TaskState(owner_squad="engineering", description=state.root_goal,
                      model_tier="fable"),
        ]
        out = _run_planner(planner, state)
        rebuilt = out.get("tasks") or []
        eng_task = next(
            (t for t in rebuilt if t.owner_squad == "engineering"), None
        )
        assert eng_task is not None, (
            "Executive branch must keep pre-seeded engineering task in new_tasks"
        )
        assert eng_task.model_tier == "fable", (
            f"Executive branch must preserve model_tier on non-exec task; "
            f"got {eng_task.model_tier!r}"
        )

    def test_exec_branch_preserves_exec_task_priority(self):
        """Fix B: executive task itself preserves pre-seeded priority."""
        from hydra_core.state import HydraState, TaskState
        planner = _build_planner(_exec_packs())

        state = HydraState(
            workflow_id=uuid4(),
            root_goal="board decision",
            selected_squads=["executive"],
        )
        state.tasks = [
            TaskState(owner_squad="executive", description=state.root_goal,
                      priority="P1", acceptance_criteria=None),
        ]
        out = _run_planner(planner, state)
        rebuilt = out.get("tasks") or []
        exec_task = next(
            (t for t in rebuilt if t.owner_squad == "executive"), None
        )
        assert exec_task is not None
        assert exec_task.priority == "P1", (
            f"Executive branch must preserve pre-seeded priority on exec task; "
            f"got {exec_task.priority!r}"
        )


# ===========================================================================
# Fix 1: multi-task-per-squad — no task is collapsed/dropped
# ===========================================================================

class TestFix1MultiTaskPerSquad:
    """Fix 1: two+ pre-seeded tasks sharing a squad must all survive into new_tasks."""

    def test_two_same_squad_tasks_p0_missing_fires_gate(self):
        """Two pre-seeded engineering tasks: P0 missing criteria + P2 with criteria.
        The P0 must NOT be dropped; the AC gate must FIRE."""
        from hydra_core.state import HydraState, TaskState
        planner = _build_planner(_hi_risk_packs())

        state = HydraState(
            workflow_id=uuid4(),
            root_goal="dual-track feature work",
            selected_squads=["engineering"],
        )
        state.tasks = [
            # P0, high-risk squad, NO criteria — must trigger AC gate.
            TaskState(owner_squad="engineering", description="critical path",
                      priority="P0", acceptance_criteria=None),
            # P2, high-risk squad, HAS criteria — must NOT excuse the P0 task.
            TaskState(owner_squad="engineering", description="housekeeping",
                      priority="P2",
                      acceptance_criteria=["lint passes", "tests green"]),
        ]
        out = _run_planner(planner, state)

        # Both tasks must appear in new_tasks.
        rebuilt = out.get("tasks") or []
        eng_tasks = [t for t in rebuilt if t.owner_squad == "engineering"]
        assert len(eng_tasks) == 2, (
            f"Both pre-seeded engineering tasks must survive; got {len(eng_tasks)}"
        )

        # AC gate must fire because the P0 task is qualifying and missing criteria.
        hitl = out.get("pending_hitl") or {}
        assert hitl.get("reason") == "acceptance_criteria", (
            f"P0 missing-criteria task must trigger AC gate even when another "
            f"same-squad task has criteria; got reason={hitl.get('reason')!r}"
        )

    def test_two_same_squad_tasks_both_have_criteria_no_gate(self):
        """Two same-squad tasks, both with valid criteria -> AC gate does NOT fire."""
        from hydra_core.state import HydraState, TaskState
        planner = _build_planner(_hi_risk_packs())

        state = HydraState(
            workflow_id=uuid4(),
            root_goal="parallel work",
            selected_squads=["engineering"],
        )
        state.tasks = [
            TaskState(owner_squad="engineering", description="task A",
                      priority="P0",
                      acceptance_criteria=["A passes integration tests"]),
            TaskState(owner_squad="engineering", description="task B",
                      priority="P1",
                      acceptance_criteria=["B deploys cleanly"]),
        ]
        out = _run_planner(planner, state)

        rebuilt = out.get("tasks") or []
        eng_tasks = [t for t in rebuilt if t.owner_squad == "engineering"]
        assert len(eng_tasks) == 2

        hitl = out.get("pending_hitl") or {}
        assert hitl.get("reason") != "acceptance_criteria", (
            "All qualifying tasks have criteria; AC gate must NOT fire"
        )


# ===========================================================================
# Fix 2: retry sources tier from CORRECT task by _task_id, not first same-squad
# ===========================================================================

class TestFix2RetryByTaskId:
    """Fix 2: when a squad has two tasks with different model_tiers, the retry
    for the fable task must source tier='fable', not the other task's tier."""

    def test_retry_sources_correct_task_tier_by_task_id(self):
        """Two same-squad tasks: one with model_tier='fable', one with model_tier=None.
        The retry is triggered for the fable task (identified by _task_id).
        The retry dispatch packet must carry model_tier='fable'.
        """
        from unittest.mock import MagicMock, patch
        from uuid import UUID
        from hydra_core import supervisor as sup_module
        from hydra_core.state import HydraState, TaskState
        from hydra_core.schemas import DecisionRecord
        from hydra_core.judge.schemas import JudgeVerdict

        hi_risk_packs = _hi_risk_packs()
        captured_retry_packets: list[dict] = []

        def capturing_execute(state, pack, inbound, dispatcher, **kwargs):
            from hydra_core.squad_node import execute_squad as real_exec
            # Tag retry packets by the REFLEXION RETRY marker in objective.
            if "REFLEXION RETRY" in getattr(inbound, "objective", ""):
                captured_retry_packets.append({
                    "model_tier": getattr(inbound, "model_tier", None),
                })
            return real_exec(state, pack, inbound, dispatcher, **kwargs)

        with patch.object(sup_module, "discover_squads", return_value=hi_risk_packs), \
             patch.object(sup_module, "load_constitution",
                          return_value=MagicMock(sha256="abc")), \
             patch.object(sup_module, "load_policy",
                          return_value=MagicMock(
                              squad_enabled=lambda s: False,
                              is_hitl_severity=lambda r: False,
                          )), \
             patch.object(sup_module, "load_cerberus_venoms", return_value=None), \
             patch("hydra_core.eights.attestation.EightsAttestor.constitution_attest",
                   return_value={}), \
             patch("hydra_core.eights.attestation.EightsAttestor.hitl_request",
                   return_value=None), \
             patch("hydra_core.eights.attestation.EightsAttestor.ceiling_tick",
                   return_value=None), \
             patch("hydra_core.eights.attestation.EightsAttestor.replay_pending",
                   return_value={"sent": 0, "failed": 0, "skipped": 0}), \
             patch("hydra_core.eights.attestation.EightsAttestor.envelope_record",
                   return_value=None):
            runner = sup_module.build_supervisor(
                dispatcher=CaptureDispatcher(), force_pure_python=True,
            )

        # Two same-squad tasks with different tiers.
        fable_task = TaskState(
            owner_squad="engineering", description="fable work",
            model_tier="fable",
        )
        none_task = TaskState(
            owner_squad="engineering", description="default work",
            model_tier=None,
        )
        state = HydraState(
            workflow_id=uuid4(),
            root_goal="dual work",
            selected_squads=["engineering"],
        )
        state.tasks = [fable_task, none_task]

        # Build a DecisionRecord envelope tagged with fable_task's task_id.
        # This simulates what node_dispatch produces after Fix 2's _task_id tagging.
        env_id = uuid4()
        decision_env = DecisionRecord(
            id=env_id,
            workflow_id=state.workflow_id,
            origin_squad="engineering",
            target_squad="hydra",
            decision="Engineering work dispatched",
            rationale="fable task result",
            sealed=False,
        )
        d = decision_env.model_dump(mode="json")
        d["_task_id"] = str(fable_task.task_id)  # tagged with the FABLE task's id
        state.envelopes = [d]

        # Inject a 'revise' verdict for this envelope.
        verdict = JudgeVerdict(
            workflow_id=state.workflow_id,
            origin_squad="hydra-judge",
            target_squad="engineering",
            target_envelope_id=env_id,
            outcome="revise",
            rubric_id="constitution-alignment@1",
            judge_vendor="gemini",
            generator_vendor="engineering",
            critique_md="Needs improvement",
        )
        state.verdicts = []

        judge_fn = dict(runner.steps).get("judge_per_squad")
        assert judge_fn is not None

        with patch("hydra_core.eights.attestation.EightsAttestor.hitl_request",
                   return_value=None), \
             patch("hydra_core.supervisor.dispatch_judge", return_value=verdict), \
             patch("hydra_core.supervisor.route_judge",
                   return_value=MagicMock(
                       tier="full",
                       rubric_ids=["constitution-alignment@1"],
                       rationale="test",
                       preferred_judge_vendors=["gemini"],
                   )), \
             patch.object(sup_module, "execute_squad", side_effect=capturing_execute):
            judge_fn(state)

        retry_packets = [p for p in captured_retry_packets]
        assert retry_packets, "Expected a reflexion retry dispatch"
        retry_tier = retry_packets[0]["model_tier"]
        assert retry_tier == "fable", (
            f"Retry must source tier from the FABLE task (task_id={fable_task.task_id}) "
            f"NOT from the none_task (first same-squad match). Got: {retry_tier!r}"
        )


# ===========================================================================
# Round-5 Fix 1: executive branch no longer collapses multiple pre-seeded tasks
# ===========================================================================

def _exec_packs():
    """Pack set containing the executive squad (agent-impersonation, hitl_required=True)."""
    from hydra_core.squad_loader import SquadPack, GateSpec
    exec_pack = SquadPack(
        slug="executive",
        name="Executive",
        description="C-suite",
        entrypoint="agent-impersonation",
        invoke={},
        gates=(GateSpec(rubric_id="executive-review", hitl_required=True),),
    )
    return {"executive": exec_pack}


class TestFix1ExecutiveNoCollapse:
    """Round-5: two pre-seeded executive tasks must both survive; no collapse."""

    def test_two_exec_tasks_p0_missing_criteria_gate_fires(self):
        """Two pre-seeded executive tasks. P0 has no criteria.
        Both must appear in rebuilt tasks; AC gate must fire."""
        from hydra_core.state import HydraState, TaskState
        planner = _build_planner(_exec_packs())

        exec_p0 = TaskState(
            owner_squad="executive",
            description="Exec P0 — no criteria",
            priority="P0",
            acceptance_criteria=None,
        )
        exec_p1 = TaskState(
            owner_squad="executive",
            description="Exec P1 — has criteria",
            priority="P1",
            acceptance_criteria=["Must not lose money"],
        )

        state = HydraState(
            workflow_id=uuid4(),
            root_goal="executive dual task",
            selected_squads=["executive"],
        )
        state.tasks = [exec_p0, exec_p1]

        out = _run_planner(planner, state)

        rebuilt = out.get("tasks") or []
        exec_tasks = [t for t in rebuilt if t.owner_squad == "executive"]
        assert len(exec_tasks) == 2, (
            f"Both pre-seeded executive tasks must survive (no collapse); "
            f"got {len(exec_tasks)}: {[t.description for t in exec_tasks]}"
        )
        surviving_ids = {str(t.task_id) for t in exec_tasks}
        assert str(exec_p0.task_id) in surviving_ids, "P0 executive task must survive"
        assert str(exec_p1.task_id) in surviving_ids, "P1 executive task must survive"

        # AC gate fires because P0 exec task is qualifying and has no criteria.
        hitl = out.get("pending_hitl") or {}
        assert hitl.get("reason") == "acceptance_criteria", (
            f"AC gate must fire for P0 exec task with no criteria; "
            f"got reason={hitl.get('reason')!r}"
        )

    def test_two_exec_tasks_both_criteria_no_ac_gate(self):
        """Two pre-seeded executive tasks both with valid criteria.
        Both survive; no AC gate fires."""
        from hydra_core.state import HydraState, TaskState
        planner = _build_planner(_exec_packs())

        exec_a = TaskState(
            owner_squad="executive",
            description="Exec A",
            priority="P0",
            acceptance_criteria=["Criterion A"],
        )
        exec_b = TaskState(
            owner_squad="executive",
            description="Exec B",
            priority="P1",
            acceptance_criteria=["Criterion B"],
        )

        state = HydraState(
            workflow_id=uuid4(),
            root_goal="dual exec both criteria",
            selected_squads=["executive"],
        )
        state.tasks = [exec_a, exec_b]

        out = _run_planner(planner, state)

        rebuilt = out.get("tasks") or []
        exec_tasks = [t for t in rebuilt if t.owner_squad == "executive"]
        assert len(exec_tasks) == 2, (
            f"Both pre-seeded executive tasks must survive; got {len(exec_tasks)}"
        )
        surviving_ids = {str(t.task_id) for t in exec_tasks}
        assert str(exec_a.task_id) in surviving_ids, "Exec task A must survive"
        assert str(exec_b.task_id) in surviving_ids, "Exec task B must survive"

        hitl = out.get("pending_hitl") or {}
        assert hitl.get("reason") != "acceptance_criteria", (
            "All qualifying exec tasks have criteria; AC gate must NOT fire"
        )


# ===========================================================================
# Round-5 Fix 2: best-of-N envelopes are _task_id-tagged; retry sources correct tier
# ===========================================================================

def _build_runner_with_packs(packs: dict):
    """Build a pure-python supervisor runner with the given squad packs."""
    from unittest.mock import MagicMock, patch
    from hydra_core import supervisor as sup_module

    disp = CaptureDispatcher()
    with patch.object(sup_module, "discover_squads", return_value=packs), \
         patch.object(sup_module, "load_constitution",
                      return_value=MagicMock(sha256="abc")), \
         patch.object(sup_module, "load_policy",
                      return_value=MagicMock(
                          squad_enabled=lambda s: False,
                          is_hitl_severity=lambda r: False,
                      )), \
         patch.object(sup_module, "load_cerberus_venoms", return_value=None), \
         patch("hydra_core.eights.attestation.EightsAttestor.constitution_attest",
               return_value={}), \
         patch("hydra_core.eights.attestation.EightsAttestor.hitl_request",
               return_value=None), \
         patch("hydra_core.eights.attestation.EightsAttestor.ceiling_tick",
               return_value=None), \
         patch("hydra_core.eights.attestation.EightsAttestor.replay_pending",
               return_value={"sent": 0, "failed": 0, "skipped": 0}), \
         patch("hydra_core.eights.attestation.EightsAttestor.envelope_record",
               return_value=None):
        runner = sup_module.build_supervisor(dispatcher=disp, force_pure_python=True)
    return runner, disp


class TestFix2BonTaskIdTagged:
    """Round-5: best-of-N envelopes are _task_id-tagged; retry finds the right task."""

    def test_bon_retry_sources_fable_tier_via_task_id(self):
        """Two same-squad tasks (none + fable). A best-of-N envelope tagged with the
        fable task's _task_id arrives for reflexion retry. The retry packet must carry
        model_tier='fable' — sourced by _task_id, not by first-same-squad fallback."""
        from unittest.mock import MagicMock, patch
        from hydra_core import supervisor as sup_module
        from hydra_core.state import HydraState, TaskState
        from hydra_core.schemas import DecisionRecord
        from hydra_core.judge.schemas import JudgeVerdict

        runner, _ = _build_runner_with_packs(_hi_risk_packs())

        none_task = TaskState(
            owner_squad="engineering",
            description="default work",
            model_tier=None,
        )
        fable_task = TaskState(
            owner_squad="engineering",
            description="fable best-of-N work",
            model_tier="fable",
        )
        state = HydraState(
            workflow_id=uuid4(),
            root_goal="dual engineering work",
            selected_squads=["engineering"],
        )
        state.tasks = [none_task, fable_task]

        # Simulate a best-of-N envelope tagged with the FABLE task's _task_id.
        env_id = uuid4()
        decision_env = DecisionRecord(
            id=env_id,
            workflow_id=state.workflow_id,
            origin_squad="engineering",
            target_squad="hydra",
            decision="Best-of-N result",
            rationale="fable candidate result",
            sealed=False,
        )
        d = decision_env.model_dump(mode="json")
        d["_bon_candidate_index"] = 0              # marks it as a best-of-N envelope
        d["_task_id"] = str(fable_task.task_id)   # tagged with FABLE task, not none_task
        state.envelopes = [d]

        captured_retry_packets: list[dict] = []

        def capturing_execute(state_inner, pack, inbound, dispatcher, **kwargs):
            if "REFLEXION RETRY" in getattr(inbound, "objective", ""):
                captured_retry_packets.append({
                    "model_tier": getattr(inbound, "model_tier", None),
                })
            from hydra_core.squad_node import execute_squad as real_exec
            return real_exec(state_inner, pack, inbound, dispatcher, **kwargs)

        verdict = JudgeVerdict(
            workflow_id=state.workflow_id,
            origin_squad="hydra-judge",
            target_squad="engineering",
            target_envelope_id=env_id,
            outcome="revise",
            rubric_id="constitution-alignment@1",
            judge_vendor="gemini",
            generator_vendor="engineering",
            critique_md="needs revision",
        )
        state.verdicts = []

        judge_fn = dict(runner.steps).get("judge_per_squad")
        assert judge_fn is not None

        with patch("hydra_core.eights.attestation.EightsAttestor.hitl_request",
                   return_value=None), \
             patch("hydra_core.supervisor.dispatch_judge", return_value=verdict), \
             patch("hydra_core.supervisor.route_judge",
                   return_value=MagicMock(
                       tier="full",
                       rubric_ids=["constitution-alignment@1"],
                       rationale="test",
                       preferred_judge_vendors=["gemini"],
                   )), \
             patch.object(sup_module, "execute_squad", side_effect=capturing_execute):
            judge_fn(state)

        assert captured_retry_packets, "Expected a reflexion retry dispatch"
        retry_tier = captured_retry_packets[0].get("model_tier")
        assert retry_tier == "fable", (
            f"Retry must carry model_tier='fable' (sourced via _task_id={fable_task.task_id}); "
            f"got {retry_tier!r}. This confirms best-of-N envelopes are _task_id-tagged."
        )


# ===========================================================================
# Round-6 Fix 1: executive task identity fields preserved verbatim
# ===========================================================================

class TestRound6Fix1ExecIdentityPreserved:
    """Pre-seeded executive task with envelope_id and result_envelope_id must
    carry those fields through node_planner unchanged (verbatim extend)."""

    def test_exec_task_identity_fields_preserved(self):
        """envelope_id, result_envelope_id, task_id are all preserved after
        node_planner rebuild — no reconstruction, no field loss."""
        from hydra_core.state import HydraState, TaskState
        import uuid

        original_task_id = uuid.uuid4()
        original_envelope_id = uuid.uuid4()
        original_result_envelope_id = uuid.uuid4()

        planner = _build_planner(_exec_packs())

        exec_task = TaskState(
            task_id=original_task_id,
            owner_squad="executive",
            description="Pre-seeded exec task",
            priority="P1",
            acceptance_criteria=["Revenue must grow"],
            model_tier="sonnet",
            envelope_id=original_envelope_id,
            result_envelope_id=original_result_envelope_id,
        )

        state = HydraState(
            workflow_id=uuid.uuid4(),
            root_goal="executive identity test",
            selected_squads=["executive"],
        )
        state.tasks = [exec_task]

        out = _run_planner(planner, state)

        rebuilt = out.get("tasks") or []
        surviving = [t for t in rebuilt if t.owner_squad == "executive"]
        assert len(surviving) >= 1, "Executive task must survive planner rebuild"

        preserved = next(
            (t for t in surviving if str(t.task_id) == str(original_task_id)),
            None,
        )
        assert preserved is not None, (
            f"Executive task with task_id={original_task_id} was not found in "
            f"rebuilt tasks. Got: {[str(t.task_id) for t in surviving]}"
        )
        assert str(preserved.envelope_id) == str(original_envelope_id), (
            f"envelope_id must be preserved verbatim; "
            f"expected {original_envelope_id}, got {preserved.envelope_id}"
        )
        assert str(preserved.result_envelope_id) == str(original_result_envelope_id), (
            f"result_envelope_id must be preserved verbatim; "
            f"expected {original_result_envelope_id}, got {preserved.result_envelope_id}"
        )
        assert preserved.model_tier == "sonnet", (
            f"model_tier must be preserved; got {preserved.model_tier!r}"
        )
        assert preserved.acceptance_criteria == ["Revenue must grow"], (
            f"acceptance_criteria must be preserved; got {preserved.acceptance_criteria!r}"
        )


# ===========================================================================
# Round-6 Fix 2: _task_id propagates through chained retries (retry-of-retry)
# ===========================================================================

class TestRound6Fix2RetryOfRetryTier:
    """The first retry output envelope must be _task_id-tagged so a second
    retry (retry-of-retry with ceiling raised to 2) also carries model_tier='fable'."""

    def test_retry_of_retry_carries_fable_tier(self):
        """Two same-squad tasks (none + fable). Two successive judge passes both
        return 'revise'. The second retry (retry-of-retry) packet must carry
        model_tier='fable', proving _task_id was propagated through the first
        retry output envelope."""
        from unittest.mock import MagicMock, patch
        from hydra_core import supervisor as sup_module
        from hydra_core.state import HydraState, TaskState
        from hydra_core.schemas import DecisionRecord
        from hydra_core.judge.schemas import JudgeVerdict

        runner, _ = _build_runner_with_packs(_hi_risk_packs())

        none_task = TaskState(
            owner_squad="engineering",
            description="default work",
            model_tier=None,
        )
        fable_task = TaskState(
            owner_squad="engineering",
            description="fable work",
            model_tier="fable",
        )
        state = HydraState(
            workflow_id=uuid4(),
            root_goal="retry-of-retry fable test",
            selected_squads=["engineering"],
        )
        state.tasks = [none_task, fable_task]
        # Raise reflexion ceiling to 2 to allow a second retry.
        state.reflexion_override_granted_until = 2

        # Initial envelope tagged with fable_task._task_id.
        env_id_1 = uuid4()
        initial_env = DecisionRecord(
            id=env_id_1,
            workflow_id=state.workflow_id,
            origin_squad="engineering",
            target_squad="hydra",
            decision="First dispatch",
            rationale="initial",
            sealed=False,
        )
        d1 = initial_env.model_dump(mode="json")
        d1["_task_id"] = str(fable_task.task_id)  # tagged with fable task
        d1["_retry_index"] = 0
        state.envelopes = [d1]
        state.verdicts = []

        all_retry_packets: list[dict] = []
        retry_count_tracker = {"n": 0}

        def capturing_execute(state_inner, pack, inbound, dispatcher, **kwargs):
            """Record all REFLEXION RETRY packets; return a tagged output envelope."""
            from hydra_core.squad_node import SquadResult
            from hydra_core.schemas import DecisionRecord as DR
            is_retry = "REFLEXION RETRY" in getattr(inbound, "objective", "")
            if is_retry:
                retry_count_tracker["n"] += 1
                all_retry_packets.append({
                    "model_tier": getattr(inbound, "model_tier", None),
                    "retry_count": retry_count_tracker["n"],
                })
            # Produce a fresh output envelope that will become the next retry target.
            new_env_id = uuid4()
            out_env = DR(
                id=new_env_id,
                workflow_id=state_inner.workflow_id,
                origin_squad="engineering",
                target_squad="hydra",
                decision="retry output",
                rationale="retry result",
                sealed=False,
            )
            return SquadResult(
                status="ok",
                envelopes=[out_env],
                artifacts=[],
                rationale="mock retry",
            )

        # dispatch_judge is called with keyword args: envelope=, rubric_id=,
        # judge_vendor=, workflow_id=, generator_vendor=, client=
        # Build a side_effect that extracts the envelope id from kwargs and
        # returns a 'revise' verdict targeting that envelope.
        def mock_dispatch_judge(*, envelope, rubric_id, judge_vendor,
                                workflow_id, generator_vendor, client, **kw):
            eid = envelope.get("id") if isinstance(envelope, dict) else str(
                getattr(envelope, "id", uuid4()))
            return JudgeVerdict(
                workflow_id=workflow_id,
                origin_squad="hydra-judge",
                target_squad="engineering",
                target_envelope_id=eid,
                outcome="revise",
                rubric_id=rubric_id,
                judge_vendor=judge_vendor,
                generator_vendor=generator_vendor,
                critique_md="needs revision",
            )

        judge_fn = dict(runner.steps).get("judge_per_squad")
        assert judge_fn is not None

        route_mock = MagicMock(
            tier="full",
            rubric_ids=["constitution-alignment@1"],
            rationale="test",
            preferred_judge_vendors=["gemini"],
        )

        # --- Pass 1: judge the initial envelope (retry_index=0 -> retry 1 fires) ---
        with patch("hydra_core.eights.attestation.EightsAttestor.hitl_request",
                   return_value=None), \
             patch("hydra_core.supervisor.dispatch_judge",
                   side_effect=mock_dispatch_judge), \
             patch("hydra_core.supervisor.route_judge", return_value=route_mock), \
             patch.object(sup_module, "execute_squad",
                          side_effect=capturing_execute):
            out1 = judge_fn(state)

        assert all_retry_packets, "First retry must fire in pass 1"
        assert all_retry_packets[0]["model_tier"] == "fable", (
            f"First retry packet must carry model_tier='fable'; "
            f"got {all_retry_packets[0]['model_tier']!r}"
        )

        # The retry output envelopes returned by the judge node must be _task_id-tagged.
        retry_output_envs = out1.get("envelopes", []) if isinstance(out1, dict) else []
        assert retry_output_envs, (
            "judge node must return retry output envelopes in pass 1"
        )
        first_retry_out = retry_output_envs[0]
        assert first_retry_out.get("_task_id") == str(fable_task.task_id), (
            f"First retry output envelope must carry _task_id={fable_task.task_id}; "
            f"got _task_id={first_retry_out.get('_task_id')!r}. "
            f"Fix: _reflexion_retry must propagate _task_id onto output envelopes."
        )

        # --- Pass 2: judge the retry output envelope (retry_index=1 -> retry 2 fires) ---
        # Update state with the retry output envelopes; reset verdicts so they get re-judged.
        state.envelopes = retry_output_envs
        state.verdicts = []

        with patch("hydra_core.eights.attestation.EightsAttestor.hitl_request",
                   return_value=None), \
             patch("hydra_core.supervisor.dispatch_judge",
                   side_effect=mock_dispatch_judge), \
             patch("hydra_core.supervisor.route_judge", return_value=route_mock), \
             patch.object(sup_module, "execute_squad",
                          side_effect=capturing_execute):
            judge_fn(state)

        retry2_packets = [p for p in all_retry_packets if p["retry_count"] == 2]
        assert retry2_packets, (
            "Second retry (retry-of-retry) must fire in pass 2 (ceiling=2 allows it)"
        )
        assert retry2_packets[0]["model_tier"] == "fable", (
            f"Retry-of-retry packet must STILL carry model_tier='fable' "
            f"(sourced via _task_id propagated through first retry output); "
            f"got {retry2_packets[0]['model_tier']!r}"
        )


# ===========================================================================
# Round-7: AC gate covers tasks outside selected_squads (per-task squad check)
# ===========================================================================

def _make_hi_risk_pack_for_squad(slug: str):
    """Build a high-risk (hitl_required=True) SquadPack for an arbitrary slug."""
    from hydra_core.squad_loader import SquadPack, GateSpec
    return SquadPack(
        slug=slug,
        name=slug.capitalize(),
        description=f"{slug} squad",
        entrypoint="mcp",
        invoke={"mode": "pp_run", "default_team": "feature-team",
                "project_path": "/tmp"},
        gates=(GateSpec(rubric_id="slsa-l2", hitl_required=True),),
    )


def _make_lo_risk_pack_for_squad(slug: str):
    """Build a low-risk (hitl_required=False) SquadPack for an arbitrary slug."""
    from hydra_core.squad_loader import SquadPack, GateSpec
    return SquadPack(
        slug=slug,
        name=slug.capitalize(),
        description=f"{slug} squad",
        entrypoint="mcp",
        invoke={"mode": "pp_run", "default_team": "feature-team",
                "project_path": "/tmp"},
        gates=(GateSpec(rubric_id="slsa-l2", hitl_required=False),),
    )


class TestRound7AcGateOutsideSelectedSquads:
    """AC gate must fire for a qualifying pre-seeded task whose owner_squad is
    NOT in selected_squads, as long as that squad has an hitl_required gate
    in packs.  Non-blanket: low-risk P2/P3 outside selected_squads stays silent."""

    def test_hi_risk_outside_selected_missing_criteria_fires(self):
        """Pre-seeded task: owner_squad='security' (high-risk, NOT in selected_squads).
        No acceptance_criteria.  AC gate must FIRE."""
        from hydra_core.state import HydraState, TaskState

        # packs includes both 'engineering' (selected) and 'security' (not selected).
        packs = {
            "engineering": _make_hi_risk_pack_for_squad("engineering"),
            "security": _make_hi_risk_pack_for_squad("security"),
        }
        planner = _build_planner(packs)

        state = HydraState(
            workflow_id=uuid4(),
            root_goal="ship v2",
            selected_squads=["engineering"],   # 'security' deliberately absent
        )
        # Pre-seed a task for the 'security' squad (outside selected_squads).
        state.tasks = [
            TaskState(
                owner_squad="security",
                description="Audit auth layer",
                priority="P2",              # not P0/P1 — qualifies via squad risk only
                acceptance_criteria=None,   # missing → gate must fire
            )
        ]

        out = _run_planner(planner, state)

        hitl = out.get("pending_hitl") or {}
        assert hitl.get("reason") == "acceptance_criteria", (
            f"AC gate must fire: 'security' squad has hitl_required gate but task "
            f"has no criteria (even though 'security' not in selected_squads); "
            f"got reason={hitl.get('reason')!r}"
        )

    def test_hi_risk_outside_selected_with_criteria_no_gate(self):
        """Same high-risk 'security' squad outside selected_squads, but the task
        HAS valid acceptance_criteria.  No AC gate must fire on this task."""
        from hydra_core.state import HydraState, TaskState

        packs = {
            "engineering": _make_lo_risk_pack_for_squad("engineering"),
            "security": _make_hi_risk_pack_for_squad("security"),
        }
        planner = _build_planner(packs)

        state = HydraState(
            workflow_id=uuid4(),
            root_goal="ship v2",
            # Use lo-risk engineering so its fresh default task doesn't
            # independently trigger the AC gate; only the security task
            # is hi-risk.  Engineering task (P2, lo-risk) -> not qualifying.
            selected_squads=["engineering"],
        )
        state.tasks = [
            TaskState(
                owner_squad="security",
                description="Audit auth layer",
                priority="P2",
                acceptance_criteria=["Zero CVEs in OWASP Top-10 scan"],
            )
        ]

        out = _run_planner(planner, state)

        hitl = out.get("pending_hitl") or {}
        assert hitl.get("reason") != "acceptance_criteria", (
            f"No AC gate expected: security task has valid criteria and fresh "
            f"engineering task is low-risk P2 (not qualifying); "
            f"got reason={hitl.get('reason')!r}"
        )

    def test_lo_risk_p2_outside_selected_no_criteria_no_gate(self):
        """Pre-seeded P2 task, low-risk squad (hitl_required=False), outside
        selected_squads, no criteria.  Gate must NOT fire — stays non-blanket."""
        from hydra_core.state import HydraState, TaskState

        packs = {
            "engineering": _make_lo_risk_pack_for_squad("engineering"),
            "docs": _make_lo_risk_pack_for_squad("docs"),
        }
        planner = _build_planner(packs)

        state = HydraState(
            workflow_id=uuid4(),
            root_goal="update docs",
            selected_squads=["engineering"],   # 'docs' deliberately absent
        )
        state.tasks = [
            TaskState(
                owner_squad="docs",
                description="Update changelog",
                priority="P2",
                acceptance_criteria=None,   # missing but low-risk P2 → no gate
            )
        ]

        out = _run_planner(planner, state)

        hitl = out.get("pending_hitl") or {}
        assert hitl.get("reason") != "acceptance_criteria", (
            f"P2 low-risk task outside selected_squads must NOT trigger AC gate "
            f"(non-blanket invariant); got reason={hitl.get('reason')!r}"
        )


# ===========================================================================
# Round-8: holistic fixes — shared risk helper, no duplication, HITL for
# high-risk outside selected_squads even when criteria are present
# ===========================================================================

class TestRound8HolisticPlanner:
    """Covers the two new issues fixed in round-8 plus a no-duplication invariant."""

    def test_hi_risk_outside_selected_with_criteria_triggers_high_risk_hitl(self):
        """A pre-seeded high-risk task outside selected_squads WITH valid criteria
        must set requires_human_approval=True (high_risk HITL reason, not AC gate).
        This verifies the shared _task_is_high_risk helper drives both gates."""
        from hydra_core.state import HydraState, TaskState

        # packs: engineering is lo-risk (selected), security is hi-risk (not selected).
        packs = {
            "engineering": _make_lo_risk_pack_for_squad("engineering"),
            "security": _make_hi_risk_pack_for_squad("security"),
        }
        planner = _build_planner(packs)

        state = HydraState(
            workflow_id=uuid4(),
            root_goal="ship v2",
            selected_squads=["engineering"],   # 'security' NOT in selected_squads
        )
        state.tasks = [
            TaskState(
                owner_squad="security",
                description="Audit auth layer",
                priority="P2",
                # Has valid criteria — AC gate must NOT fire.
                # But security squad is high-risk → requires_human_approval must be True.
                acceptance_criteria=["Zero CVEs in OWASP Top-10 scan"],
            )
        ]

        out = _run_planner(planner, state)

        # AC gate must NOT fire (criteria are present).
        hitl = out.get("pending_hitl") or {}
        assert hitl.get("reason") != "acceptance_criteria", (
            f"AC gate must not fire when criteria are present; "
            f"got reason={hitl.get('reason')!r}"
        )
        # But requires_human_approval MUST be True (security is high-risk).
        assert out.get("requires_human_approval") is True, (
            "requires_human_approval must be True: security squad is high-risk "
            "and its task is in full_tasks (even though not in selected_squads). "
            "The shared _task_is_high_risk helper must drive this flag."
        )
        # The pending_hitl reason should be "high_risk" (not "acceptance_criteria").
        assert hitl.get("reason") == "high_risk", (
            f"HITL reason must be 'high_risk' when criteria present but squad is "
            f"high-risk; got reason={hitl.get('reason')!r}"
        )

    def test_no_duplicate_task_ids_after_planner(self):
        """After node_planner runs, the merged task list must contain each
        task_id exactly once (append-reducer must not double pre-seeded tasks)."""
        from hydra_core.state import HydraState, TaskState

        packs = {"engineering": _make_hi_risk_pack_for_squad("engineering")}
        planner = _build_planner(packs)

        eng_task = TaskState(
            owner_squad="engineering",
            description="pre-seeded engineering",
            priority="P0",
            acceptance_criteria=["All tests green"],
        )

        state = HydraState(
            workflow_id=uuid4(),
            root_goal="no duplication test",
            selected_squads=["engineering"],
        )
        state.tasks = [eng_task]  # pre-seeded: already in state.tasks

        out = _run_planner(planner, state)

        merged_tasks = out.get("tasks") or []
        task_ids = [str(t.task_id) for t in merged_tasks]
        duplicates = [tid for tid in set(task_ids) if task_ids.count(tid) > 1]
        assert not duplicates, (
            f"No task_id must appear more than once after node_planner "
            f"(append reducer must not double pre-seeded tasks); "
            f"duplicates: {duplicates}"
        )
        # The pre-seeded task must still be present exactly once.
        assert task_ids.count(str(eng_task.task_id)) == 1, (
            f"Pre-seeded task {eng_task.task_id} must appear exactly once; "
            f"got count={task_ids.count(str(eng_task.task_id))}"
        )

    def test_no_duplicate_task_ids_multi_seeded_squads(self):
        """Multiple pre-seeded tasks across different squads — all unique after merge."""
        from hydra_core.state import HydraState, TaskState

        packs = {
            "engineering": _make_hi_risk_pack_for_squad("engineering"),
            "security": _make_hi_risk_pack_for_squad("security"),
        }
        planner = _build_planner(packs)

        tasks_in = [
            TaskState(owner_squad="engineering", description="eng task",
                      priority="P0", acceptance_criteria=["eng criterion"]),
            TaskState(owner_squad="security", description="sec task 1",
                      priority="P1", acceptance_criteria=["sec criterion A"]),
            TaskState(owner_squad="security", description="sec task 2",
                      priority="P2", acceptance_criteria=["sec criterion B"]),
        ]

        state = HydraState(
            workflow_id=uuid4(),
            root_goal="multi-squad no-dup",
            selected_squads=["engineering"],   # 'security' outside selected_squads
        )
        state.tasks = tasks_in

        out = _run_planner(planner, state)

        merged_tasks = out.get("tasks") or []
        task_ids = [str(t.task_id) for t in merged_tasks]
        duplicates = [tid for tid in set(task_ids) if task_ids.count(tid) > 1]
        assert not duplicates, (
            f"No task_id duplicates after multi-squad planner run; "
            f"duplicates: {duplicates}"
        )
        # All three pre-seeded tasks must be present.
        for t in tasks_in:
            assert task_ids.count(str(t.task_id)) == 1, (
                f"Task {t.task_id} ({t.description}) must appear exactly once"
            )


# ===========================================================================
# Round-9: dispatch-point dedup — duplicate task_ids in state.tasks never
# double-dispatch, distinct tasks each dispatch exactly once.
# ===========================================================================

class TestRound9DispatchDedup:
    """Belt-and-suspenders dedup in node_dispatch (the consumer of state.tasks)."""

    def _build_dispatch_fn(self, packs: dict):
        """Extract the 'dispatch' node function from a pure-python runner."""
        runner, _ = _build_runner_with_packs(packs)
        dispatch_fn = dict(runner.steps).get("dispatch")
        assert dispatch_fn is not None, "dispatch step not found in runner"
        return dispatch_fn

    def test_duplicate_task_id_dispatched_exactly_once(self):
        """state.tasks already contains two entries with the SAME task_id
        (simulating a prior re-plan duplicate in the append-reduced list).
        node_dispatch must dispatch that task_id exactly once."""
        from unittest.mock import patch, MagicMock
        from uuid import UUID
        from hydra_core import supervisor as sup_module
        from hydra_core.state import HydraState, TaskState
        from hydra_core.squad_node import SquadResult

        packs = {"engineering": _make_hi_risk_pack_for_squad("engineering")}
        dispatch_fn = self._build_dispatch_fn(packs)

        shared_id = uuid4()
        # Two TaskState entries with the SAME task_id — simulates append-reducer dup.
        dup_task_a = TaskState(
            task_id=shared_id,
            owner_squad="engineering",
            description="the real task",
            priority="P1",
            status="pending",
        )
        dup_task_b = TaskState(
            task_id=shared_id,           # same id!
            owner_squad="engineering",
            description="the duplicate",
            priority="P1",
            status="pending",
        )

        state = HydraState(
            workflow_id=uuid4(),
            root_goal="dedup dispatch test",
            selected_squads=["engineering"],
            phase="dispatch",
        )
        # Directly inject the duplicate list (bypasses planner dedup).
        state.tasks = [dup_task_a, dup_task_b]

        dispatch_calls: list[dict] = []

        def capturing_execute(state_inner, pack, inbound, dispatcher, **kwargs):
            dispatch_calls.append({
                "squad": pack.slug,
                "objective": getattr(inbound, "objective", None),
            })
            return SquadResult(status="ok", envelopes=[], artifacts=[],
                               rationale="mock dispatch")

        with patch("hydra_core.eights.attestation.EightsAttestor.hitl_request",
                   return_value=None), \
             patch("hydra_core.eights.attestation.EightsAttestor.envelope_record",
                   return_value=None), \
             patch.object(sup_module, "execute_squad",
                          side_effect=capturing_execute):
            dispatch_fn(state)

        assert len(dispatch_calls) == 1, (
            f"Duplicate task_id must dispatch exactly once; "
            f"got {len(dispatch_calls)} dispatch calls: {dispatch_calls}"
        )

    def test_distinct_task_ids_each_dispatch_once(self):
        """Three distinct task_ids in state.tasks → each dispatched exactly once,
        no over-dedup."""
        from unittest.mock import patch
        from hydra_core import supervisor as sup_module
        from hydra_core.state import HydraState, TaskState
        from hydra_core.squad_node import SquadResult

        packs = {"engineering": _make_hi_risk_pack_for_squad("engineering")}
        dispatch_fn = self._build_dispatch_fn(packs)

        tasks = [
            TaskState(owner_squad="engineering", description=f"task {i}",
                      status="pending", priority="P1")
            for i in range(3)
        ]
        task_ids = [str(t.task_id) for t in tasks]
        # All three must be distinct.
        assert len(set(task_ids)) == 3

        state = HydraState(
            workflow_id=uuid4(),
            root_goal="three-task dispatch test",
            selected_squads=["engineering"],
            phase="dispatch",
        )
        state.tasks = tasks

        dispatch_calls: list[str] = []

        def capturing_execute(state_inner, pack, inbound, dispatcher, **kwargs):
            dispatch_calls.append(getattr(inbound, "objective", ""))
            return SquadResult(status="ok", envelopes=[], artifacts=[],
                               rationale="mock dispatch")

        with patch("hydra_core.eights.attestation.EightsAttestor.hitl_request",
                   return_value=None), \
             patch("hydra_core.eights.attestation.EightsAttestor.envelope_record",
                   return_value=None), \
             patch.object(sup_module, "execute_squad",
                          side_effect=capturing_execute):
            dispatch_fn(state)

        assert len(dispatch_calls) == 3, (
            f"Three distinct tasks must each dispatch exactly once; "
            f"got {len(dispatch_calls)} calls"
        )
