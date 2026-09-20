"""Cross-vendor judge finding regression suite (items 1/6 CRITICAL, 2/6 HIGH).

Item 1: a legacy envelope/plan that fails STRICT SERIALIZATION must produce a
verdict outcome ("unjudgeable") DISTINCT from a legitimately routed "skip",
and every verdict-consuming site (`node_judge_per_squad`, `node_judge_synthesis`,
`node_plan_judge` in `hydra_core/supervisor.py`) must treat it as a hard
block: never advance to synthesis, never mark the workflow done, and on the
PLAN path never offer the ordinary approve gate.

Item 2: `node_plan_judge`'s own step-budget summation must route through the
same overflow-aware helper `plan_artifact.sum_finite_budgets` uses, so two
individually-valid, individually-finite step budgets near
`sys.float_info.max` never leak `inf` into `plan_detail` (checkpoint/HITL/
MCP-visible data) or the approval summary.
"""
from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from hydra_core.state import BudgetLedger, HydraState


HYDRA_ROOT = Path(__file__).resolve().parents[1]


class _StubDispatcher:
    allow_offline_mcp_dispatch = True

    def call_mcp(self, server, tool, args, **_kw):
        return {"status": "done", "tool": tool, "result": {"ok": True}}

    def spawn_subprocess(self, cmd, env=None):
        return {"status": "done", "stdout": "", "stderr": ""}

    def emit_claude_prompt(self, prompt, agent=None):
        return {"status": "done", "agent": agent, "summary": "stub"}

    def invoke_claude_skill(self, skill, args):
        return {"status": "done", "skill": skill, "summary": "stub"}


def _node(sup, name):
    """Pull one node function directly out of the `_PurePythonRunner`'s step
    list, so the exact node behavior can be exercised without driving the
    whole graph (and without needing a real pydantic-constructible envelope
    to carry the illegal non-finite field -- see module docstring)."""
    from hydra_core.supervisor import _PurePythonRunner
    assert isinstance(sup, _PurePythonRunner), "expected the pure-python runner"
    for step_name, fn in sup.steps:
        if step_name == name:
            return fn
    raise AssertionError(f"no such node: {name}")


def _build_sup(client=None):
    from hydra_core.supervisor import build_supervisor
    return build_supervisor(
        project_root=HYDRA_ROOT,
        dispatcher=_StubDispatcher(),
        critique_client=client,
        force_pure_python=True,
    )


def _hostile_envelope(workflow_id) -> dict:
    """A legacy envelope carrying a non-finite field the way a
    pre-strict-JSON checkpoint's raw dict (never revalidated by Pydantic on
    load -- see finding 6) could. Plain dict, not a constructed
    `HydraEnvelope`, exactly the shape `state.envelopes` holds at runtime."""
    return {
        "id": str(uuid4()),
        "type": "C_SUITE_DECISION_PACKET",
        "origin_squad": "executive",
        "workflow_id": str(workflow_id),
        "origin": "BOARDROOM",
        "objective": "legacy checkpoint envelope",
        "constraints": {"budget_usd": float("nan")},
    }


def _routed_skip_envelope(workflow_id) -> dict:
    """A legitimately routed skip: `route.tier == "skip"` via a passing
    `pp_verdict` (see `judge.router.route_judge` / `test_pp_verdict_skip`).
    This must behave EXACTLY as before the fix -- excluded from every gate,
    never surfaced."""
    return {
        "id": str(uuid4()),
        "type": "DECISION_RECORD",
        "origin_squad": "engineering",
        "workflow_id": str(workflow_id),
        "decision": "shipped",
        "rationale": "pp already judged this",
        "pp_verdict": {"outcome": "pass", "rubric_id": "owasp-asvs-l1@1"},
    }


# ---------------------------------------------------------------------------
# Item 1/6 CRITICAL: unjudgeable envelope hard-blocks node_judge_per_squad.
# ---------------------------------------------------------------------------

def test_unjudgeable_envelope_surfaces_and_never_reaches_synthesis():
    sup = _build_sup()
    judge_per_squad = _node(sup, "judge_per_squad")

    state = HydraState(root_goal="legacy checkpoint resume")
    state.envelopes = [_hostile_envelope(state.workflow_id)]

    patch = judge_per_squad(state)

    assert patch["phase"] == "surfaced", (
        "an unjudgeable envelope must surface the workflow, not advance to synthesis"
    )
    assert patch["pending_hitl"] is not None
    assert patch["pending_hitl"]["reason"] == "unjudgeable_envelope"
    # The envelope id is named in the HITL summary so the operator can act.
    hostile_id = state.envelopes[0]["id"]
    verdicts = patch["verdicts"]
    unjudgeable = [v for v in verdicts if v.get("outcome") == "unjudgeable"]
    assert unjudgeable, f"expected an unjudgeable verdict, got outcomes: {[v.get('outcome') for v in verdicts]}"
    assert str(unjudgeable[0]["target_envelope_id"]) == hostile_id
    # Never advances to synthesis.
    assert patch.get("phase") != "synthesis"


def test_unjudgeable_envelope_re_detected_from_prior_verdicts_on_reentry():
    """Once an unjudgeable verdict is recorded, a later re-entry of
    `node_judge_per_squad` (e.g. after an `acknowledge` resume) must
    re-detect it from `state.verdicts` and surface again, rather than
    silently advancing because the envelope itself is now in
    `already_judged`."""
    sup = _build_sup()
    judge_per_squad = _node(sup, "judge_per_squad")

    state = HydraState(root_goal="legacy checkpoint resume")
    hostile = _hostile_envelope(state.workflow_id)
    state.envelopes = [hostile]

    first = judge_per_squad(state)
    state.verdicts = list(first["verdicts"])
    state.phase = "judge_per_squad"  # simulate re-entry after a non-terminal resume

    second = judge_per_squad(state)
    assert second["phase"] == "surfaced"
    assert second["pending_hitl"]["reason"] == "unjudgeable_envelope"


def test_unjudgeable_final_record_blocks_postcheck_from_marking_done():
    """`node_judge_synthesis` must surface (not advance to postcheck→done)
    when the final DecisionRecord itself fails strict serialization."""
    sup = _build_sup()
    judge_synthesis = _node(sup, "judge_synthesis")

    state = HydraState(root_goal="legacy checkpoint resume")
    record = {
        "id": str(uuid4()),
        "type": "DECISION_RECORD",
        "origin_squad": "hydra",
        "workflow_id": str(state.workflow_id),
        "decision": "synthesized",
        "rationale": "x",
        # A non-finite field a legacy DecisionRecord dict could carry.
        "constraints": {"budget_usd": float("inf")},
    }
    state.envelopes = [record]

    patch = judge_synthesis(state)
    assert patch["phase"] == "surfaced"
    assert patch["pending_hitl"]["reason"] == "unjudgeable_envelope"
    assert patch["phase"] != "postcheck"


# ---------------------------------------------------------------------------
# Legitimately routed skip: unaffected by the fix.
# ---------------------------------------------------------------------------

def test_routed_skip_still_behaves_exactly_as_before():
    """`route.tier == "skip"` (here via a passing `pp_verdict`) must still
    return zero verdicts for that envelope and never trip the new
    unjudgeable/breach gates -- the workflow proceeds to synthesis
    normally."""
    sup = _build_sup()
    judge_per_squad = _node(sup, "judge_per_squad")

    state = HydraState(root_goal="pp already judged this task")
    state.envelopes = [_routed_skip_envelope(state.workflow_id)]

    patch = judge_per_squad(state)

    assert patch["phase"] == "synthesis"
    assert patch.get("pending_hitl") is None
    # No verdict at all was produced for the routed-skip envelope (route_judge
    # returns rubric_ids=[] for it, and `_judge_envelope` returns [] before
    # ever calling `dispatch_judge_with_fallback`).
    assert patch["verdicts"] == []


# ---------------------------------------------------------------------------
# Item 1/6, PLAN path: node_plan_judge never offers the ordinary approve gate.
# ---------------------------------------------------------------------------

def test_unjudgeable_plan_never_offers_approval():
    sup = _build_sup()
    plan_judge = _node(sup, "plan_judge")

    state = HydraState(root_goal="legacy plan resume")
    state.plan_status = "drafted"
    state.plan_ref = {
        "id": str(uuid4()),
        "type": "PLAN",
        "workflow_id": str(state.workflow_id),
        "origin_squad": "planning",
        "target_squad": "hydra",
        "rigor": "standard",
        "goal_restatement": "legacy plan resume",
        "summary": "legacy plan resume",
        "steps": [],
        # A non-finite field the plan dict could carry from a legacy
        # checkpoint (Plan/PlanStep construction rejects NaN/Infinity, but a
        # RAW DICT loaded from disk without revalidation does not).
        "constraints": {"budget_usd": float("nan")},
    }

    patch = plan_judge(state)

    assert patch["pending_hitl"]["reason"] == "unjudgeable_plan"
    options = patch["pending_hitl"]["options"]
    assert "approve" not in options, (
        f"an unjudgeable plan must never offer 'approve', got options={options}"
    )
    assert options == ["abort"], (
        "unjudgeable_plan must offer ONLY the terminal 'abort' option -- "
        "node_plan_gate materialises tasks on ANY non-terminal resume "
        "regardless of chosen option, so 'acknowledge' would silently "
        "behave like 'approve'"
    )
    assert patch["pending_hitl"]["default_option"] == "abort"
    assert patch["verdicts"][0]["outcome"] == "unjudgeable"


# ---------------------------------------------------------------------------
# Item 2/6 HIGH: plan budget overflow reports unavailable, never Infinity.
# ---------------------------------------------------------------------------

def test_two_huge_step_budgets_report_unavailable_not_infinity():
    import math

    sup = _build_sup()
    plan_judge = _node(sup, "plan_judge")

    state = HydraState(root_goal="huge budget plan", budget=BudgetLedger(budget_usd=100.0))
    state.plan_status = "drafted"
    state.plan_ref = {
        "id": str(uuid4()),
        "type": "PLAN",
        "workflow_id": str(state.workflow_id),
        "origin_squad": "planning",
        "target_squad": "hydra",
        "rigor": "standard",
        "goal_restatement": "huge budget plan",
        "summary": "huge budget plan",
        "steps": [
            {"step_id": "s1", "description": "a", "target_squad": "engineering",
             "envelope_type": "DEV_TASK", "priority": "P2",
             "estimated_budget_usd": 1e308},
            {"step_id": "s2", "description": "b", "target_squad": "engineering",
             "envelope_type": "DEV_TASK", "priority": "P2",
             "estimated_budget_usd": 1e308},
        ],
    }

    patch = plan_judge(state)
    plan_detail = patch["pending_hitl"]["plan_detail"]

    assert plan_detail["estimated_total_budget_usd"] is None, (
        "overflowed sum must be reported as None/unavailable, never a bare float"
    )
    assert plan_detail["estimated_total_budget_overflowed"] is True
    # No Infinity anywhere in the checkpoint/HITL-visible plan_detail dict.
    import json
    text = json.dumps(plan_detail, default=str)
    assert "Infinity" not in text
    # And the operator-facing summary says "unavailable", not "$inf".
    assert "unavailable" in patch["pending_hitl"]["summary"]
    assert not any(
        isinstance(v, float) and math.isinf(v)
        for v in _flatten(plan_detail)
    )


def _flatten(obj):
    if isinstance(obj, dict):
        for v in obj.values():
            yield from _flatten(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _flatten(v)
    else:
        yield obj


def test_single_normal_step_budget_still_sums_correctly():
    """Mutation proof companion: the overflow-aware helper must not change
    ordinary, non-overflowing sums."""
    sup = _build_sup()
    plan_judge = _node(sup, "plan_judge")

    state = HydraState(root_goal="normal budget plan", budget=BudgetLedger(budget_usd=100.0))
    state.plan_status = "drafted"
    state.plan_ref = {
        "id": str(uuid4()),
        "type": "PLAN",
        "workflow_id": str(state.workflow_id),
        "origin_squad": "planning",
        "target_squad": "hydra",
        "rigor": "standard",
        "goal_restatement": "normal budget plan",
        "summary": "normal budget plan",
        "steps": [
            {"step_id": "s1", "description": "a", "target_squad": "engineering",
             "envelope_type": "DEV_TASK", "priority": "P2",
             "estimated_budget_usd": 10.5},
            {"step_id": "s2", "description": "b", "target_squad": "engineering",
             "envelope_type": "DEV_TASK", "priority": "P2",
             "estimated_budget_usd": 4.5},
        ],
    }

    patch = plan_judge(state)
    plan_detail = patch["pending_hitl"]["plan_detail"]
    assert plan_detail["estimated_total_budget_usd"] == 15.0
    assert plan_detail["estimated_total_budget_overflowed"] is False
