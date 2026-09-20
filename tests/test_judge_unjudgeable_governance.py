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
    # Item 2 MEDIUM (this round): abort-only, never `acknowledge` -- an
    # `acknowledge` resume is a guaranteed re-surface loop since the next
    # pass re-detects the identical persisted verdict. The summary names the
    # offending field directly instead of forcing the operator to parse the
    # critique excerpt.
    assert patch["pending_hitl"]["options"] == ["abort"]
    assert patch["pending_hitl"]["default_option"] == "abort"
    assert "constraints.budget_usd" in patch["pending_hitl"]["summary"]
    assert "re-ingest" in patch["pending_hitl"]["summary"]


def test_unjudgeable_envelope_re_detected_from_prior_verdicts_on_reentry():
    """Once an unjudgeable verdict is recorded, a later re-entry of
    `node_judge_per_squad` must re-detect it from `state.verdicts` and
    surface again, rather than silently advancing because the envelope
    itself is now in `already_judged`. There is no `acknowledge` resume
    to simulate any more (item 2 MEDIUM, this round) -- the gate offers
    ONLY `abort`, a terminal resolution -- but the re-detection itself
    (defense in depth against a re-entry that somehow reaches this node
    again, e.g. a future resume path) must still hold."""
    sup = _build_sup()
    judge_per_squad = _node(sup, "judge_per_squad")

    state = HydraState(root_goal="legacy checkpoint resume")
    hostile = _hostile_envelope(state.workflow_id)
    state.envelopes = [hostile]

    first = judge_per_squad(state)
    state.verdicts = list(first["verdicts"])
    state.phase = "judge_per_squad"  # simulate a re-entry

    second = judge_per_squad(state)
    assert second["phase"] == "surfaced"
    assert second["pending_hitl"]["reason"] == "unjudgeable_envelope"
    assert second["pending_hitl"]["options"] == ["abort"]


def test_already_judged_source_envelope_with_non_finite_field_still_hard_blocks():
    """The bypass this round's fix closes: a legacy pre-strict-JSON
    checkpoint holds a SOURCE envelope with a non-finite field (id=A) AND an
    old `pass` verdict already targeting A (e.g. from before strict
    serialization existed). The `already_judged` shortcut would otherwise
    `continue` past this envelope before `_judge_envelope` ever runs --
    never producing an `unjudgeable` verdict and letting the node reach
    `phase="synthesis"`. The envelope-level scan must catch it regardless."""
    sup = _build_sup()
    judge_per_squad = _node(sup, "judge_per_squad")

    state = HydraState(root_goal="legacy checkpoint with stale verdict")
    hostile = _hostile_envelope(state.workflow_id)
    hostile_id = hostile["id"]
    prior_pass_verdict = {
        "id": str(uuid4()),
        "type": "JUDGE_VERDICT",
        "workflow_id": str(state.workflow_id),
        "origin_squad": "hydra-judge",
        "target_squad": "executive",
        "target_envelope_id": hostile_id,
        "outcome": "pass",
        "rubric_id": "board-decision-quality@1",
        "judge_vendor": "codex",
        "generator_vendor": "claude",
    }
    state.envelopes = [hostile]
    state.verdicts = [prior_pass_verdict]

    patch = judge_per_squad(state)

    assert patch["phase"] == "surfaced", (
        "already_judged must not suppress the envelope-level non-finite scan"
    )
    assert patch["pending_hitl"]["reason"] == "unjudgeable_envelope"
    assert patch["pending_hitl"]["options"] == ["abort"]
    unjudgeable = [v for v in patch["verdicts"] if v.get("outcome") == "unjudgeable"]
    assert unjudgeable, "expected a fresh unjudgeable verdict from the envelope scan"
    assert str(unjudgeable[0]["target_envelope_id"]) == hostile_id
    assert "constraints.budget_usd" in patch["pending_hitl"]["summary"]


def test_already_judged_finite_envelope_takes_fast_path_no_rejudge():
    """Control for the fix above: an envelope with NO non-finite value and a
    prior verdict targeting it must still take the ordinary `already_judged`
    fast path -- no re-judge, no new verdict emitted for it, no HITL."""
    sup = _build_sup()
    judge_per_squad = _node(sup, "judge_per_squad")

    state = HydraState(root_goal="finite envelope already judged")
    finite_envelope = {
        "id": str(uuid4()),
        "type": "C_SUITE_DECISION_PACKET",
        "origin_squad": "executive",
        "workflow_id": str(state.workflow_id),
        "origin": "BOARDROOM",
        "objective": "ordinary envelope",
        "constraints": {"budget_usd": 1000.0},
    }
    finite_id = finite_envelope["id"]
    prior_pass_verdict = {
        "id": str(uuid4()),
        "type": "JUDGE_VERDICT",
        "workflow_id": str(state.workflow_id),
        "origin_squad": "hydra-judge",
        "target_squad": "executive",
        "target_envelope_id": finite_id,
        "outcome": "pass",
        "rubric_id": "board-decision-quality@1",
        "judge_vendor": "codex",
        "generator_vendor": "claude",
    }
    state.envelopes = [finite_envelope]
    state.verdicts = [prior_pass_verdict]

    patch = judge_per_squad(state)

    assert patch["phase"] == "synthesis"
    assert patch.get("pending_hitl") is None
    retargeting = [
        v for v in patch["verdicts"] if v.get("target_envelope_id") == finite_id
    ]
    assert not retargeting, (
        "an already-judged, finite envelope must not be re-judged: "
        f"got fresh verdicts {retargeting}"
    )


def test_imported_persisted_unjudgeable_verdict_envelope_blocks_without_verdicts_entry():
    """Cross-vendor judge finding (this round, item 1 HIGH): an
    imported/replayed state can hold a JUDGE_VERDICT envelope (in
    `state.envelopes`) with outcome="unjudgeable" while `state.verdicts` was
    NOT reconstructed in lockstep -- e.g. a checkpoint import that ingests
    the raw envelope list but not the verdict ledger, or a replay that
    resumes mid-run from a snapshot of `state.envelopes` alone. Before this
    fix, `node_judge_per_squad` unconditionally `continue`d on any
    `type == "JUDGE_VERDICT"` envelope, so this scenario silently advanced
    to synthesis with zero signal that the verdict was ever unjudgeable."""
    sup = _build_sup()
    judge_per_squad = _node(sup, "judge_per_squad")

    state = HydraState(root_goal="imported checkpoint resume")
    persisted_verdict_id = str(uuid4())
    persisted_verdict = {
        "id": persisted_verdict_id,
        "type": "JUDGE_VERDICT",
        "workflow_id": str(state.workflow_id),
        "origin_squad": "hydra-judge",
        "target_squad": "executive",
        "target_envelope_id": str(uuid4()),
        "outcome": "unjudgeable",
        "rubric_id": "constitution-alignment@1",
        "judge_vendor": "codex",
        "critique_md": (
            "[UNJUDGEABLE — envelope failed strict serialization, cannot be "
            "evaluated by any vendor] codex:non_finite_envelope. Last error: "
            "envelope failed strict serialization (rubric=constitution-alignment@1): "
            "envelope abc contains a non-finite value at $.constraints.budget_usd; "
            "refusing to write invalid JSON"
        ),
    }
    # NOTE: this envelope is imported directly into `state.envelopes` -- it
    # is NEVER added to `state.verdicts`, exactly modeling the import/replay
    # gap this fix closes.
    state.envelopes = [persisted_verdict]
    assert state.verdicts == []

    patch = judge_per_squad(state)

    assert patch["phase"] == "surfaced", (
        "a persisted unjudgeable JUDGE_VERDICT envelope with no matching "
        "state.verdicts entry must still hard-block, not silently advance"
    )
    assert patch["pending_hitl"]["reason"] == "unjudgeable_envelope"
    assert patch["pending_hitl"]["options"] == ["abort"]
    assert "constraints.budget_usd" in patch["pending_hitl"]["summary"]
    assert patch.get("phase") != "synthesis"


def test_persisted_unjudgeable_verdict_already_in_state_verdicts_not_double_counted():
    """Companion mutation-proof guard: when the persisted JUDGE_VERDICT
    envelope's id IS already present in `state.verdicts` (the normal case --
    it was folded into `state.verdicts` in the SAME pass that produced it),
    the loop must not re-treat it as a fresh, previously-undetected hit.
    It is still caught (via the `state.verdicts` scan earlier in the node),
    just not through the persisted-envelope path this fix adds."""
    sup = _build_sup()
    judge_per_squad = _node(sup, "judge_per_squad")

    state = HydraState(root_goal="normal same-pass resume")
    verdict_id = str(uuid4())
    target_id = str(uuid4())
    verdict = {
        "id": verdict_id,
        "type": "JUDGE_VERDICT",
        "workflow_id": str(state.workflow_id),
        "origin_squad": "hydra-judge",
        "target_envelope_id": target_id,
        "outcome": "unjudgeable",
        "rubric_id": "constitution-alignment@1",
        "judge_vendor": "codex",
        "critique_md": "non-finite value at $.constraints.budget_usd; refusing to write invalid JSON",
    }
    state.verdicts = [verdict]
    state.envelopes = [dict(verdict)]  # same verdict also persisted as an envelope

    patch = judge_per_squad(state)

    # Still hard-blocks (via the `state.verdicts` scan), and there is
    # exactly one unjudgeable_hit driving the HITL -- not a crash from
    # double-processing the same verdict via two different code paths.
    assert patch["phase"] == "surfaced"
    assert patch["pending_hitl"]["reason"] == "unjudgeable_envelope"


def test_unrelated_verdict_sharing_id_does_not_suppress_block():
    """Cross-vendor judge finding (this round, item 1 HIGH, follow-up): an
    id match ALONE must never suppress the hard block. An unrelated verdict
    that happens to share the same `id` as the persisted unjudgeable
    envelope (but a DIFFERENT outcome) is not "the same record already
    folded in" -- the block must still fire."""
    sup = _build_sup()
    judge_per_squad = _node(sup, "judge_per_squad")

    state = HydraState(root_goal="id collision resume")
    shared_id = str(uuid4())
    unrelated_pass_verdict = {
        "id": shared_id,
        "type": "JUDGE_VERDICT",
        "workflow_id": str(state.workflow_id),
        "origin_squad": "hydra-judge",
        "target_envelope_id": str(uuid4()),
        "outcome": "pass",
        "rubric_id": "owasp-asvs-l1@1",
    }
    persisted_unjudgeable_envelope = {
        "id": shared_id,
        "type": "JUDGE_VERDICT",
        "workflow_id": str(state.workflow_id),
        "origin_squad": "hydra-judge",
        "target_envelope_id": str(uuid4()),
        "outcome": "unjudgeable",
        "rubric_id": "constitution-alignment@1",
        "judge_vendor": "codex",
        "critique_md": "non-finite value at $.constraints.budget_usd; refusing to write invalid JSON",
    }
    state.verdicts = [unrelated_pass_verdict]
    state.envelopes = [persisted_unjudgeable_envelope]

    patch = judge_per_squad(state)

    assert patch["phase"] == "surfaced", (
        "an id collision with an unrelated verdict must never suppress a "
        "genuine unjudgeable hit"
    )
    assert patch["pending_hitl"]["reason"] == "unjudgeable_envelope"


def test_unjudgeable_envelope_missing_id_still_blocks():
    """A raw record with no `id` at all must never be treated as matching
    a persisted verdict via a `None`-to-`None` id collision."""
    sup = _build_sup()
    judge_per_squad = _node(sup, "judge_per_squad")

    state = HydraState(root_goal="missing id resume")
    verdict_missing_id = {
        # no "id" key at all
        "type": "JUDGE_VERDICT",
        "workflow_id": str(state.workflow_id),
        "origin_squad": "hydra-judge",
        "target_envelope_id": str(uuid4()),
        "outcome": "pass",
        "rubric_id": "owasp-asvs-l1@1",
    }
    persisted_unjudgeable_envelope_no_id = {
        # also no "id" key
        "type": "JUDGE_VERDICT",
        "workflow_id": str(state.workflow_id),
        "origin_squad": "hydra-judge",
        "target_envelope_id": str(uuid4()),
        "outcome": "unjudgeable",
        "rubric_id": "constitution-alignment@1",
        "judge_vendor": "codex",
        "critique_md": "non-finite value at $.constraints.budget_usd; refusing to write invalid JSON",
    }
    state.verdicts = [verdict_missing_id]
    state.envelopes = [persisted_unjudgeable_envelope_no_id]

    patch = judge_per_squad(state)

    assert patch["phase"] == "surfaced", (
        "a missing/None id must never collide with another missing/None id"
    )
    assert patch["pending_hitl"]["reason"] == "unjudgeable_envelope"


def test_nested_payload_outcome_unjudgeable_still_blocks():
    """A persisted JUDGE_VERDICT envelope carrying its outcome nested under
    `payload.outcome` (rather than flattened at the top level) must still
    hard-block -- the top-level `outcome` read must not silently miss it."""
    sup = _build_sup()
    judge_per_squad = _node(sup, "judge_per_squad")

    state = HydraState(root_goal="nested payload outcome resume")
    nested_unjudgeable_envelope = {
        "id": str(uuid4()),
        "type": "JUDGE_VERDICT",
        "workflow_id": str(state.workflow_id),
        "origin_squad": "hydra-judge",
        "target_envelope_id": str(uuid4()),
        "rubric_id": "constitution-alignment@1",
        "judge_vendor": "codex",
        "critique_md": "non-finite value at $.constraints.budget_usd; refusing to write invalid JSON",
        "payload": {"outcome": "unjudgeable"},
    }
    state.envelopes = [nested_unjudgeable_envelope]

    patch = judge_per_squad(state)

    assert patch["phase"] == "surfaced"
    assert patch["pending_hitl"]["reason"] == "unjudgeable_envelope"


def test_id_match_with_differing_outcome_still_hard_blocks_via_envelope_scan():
    """Isolates the envelope-scan's `already_folded` predicate (supervisor.py
    ``already_folded = matched is not None and _verdict_outcome(matched) ==
    env_outcome``) from the earlier `state.verdicts` scan.

    Cross-vendor judge finding (this round, item 2 LOW): the previous version
    of this test put a genuinely-matching (same id AND same outcome
    "unjudgeable") record in `state.verdicts`. But the `state.verdicts` scan
    (lines ~3044-3046) runs BEFORE the envelope loop and unconditionally sets
    `unjudgeable_hit` from ANY `outcome == "unjudgeable"` entry in
    `state.verdicts`, regardless of whether a matching envelope exists. So
    that test passed even with the id-and-outcome match check deleted
    entirely -- the earlier scan already supplied the block, and the
    envelope-scan's own predicate was never exercised at all.

    Here, `state.verdicts` holds a record with the SAME id as the envelope
    but outcome="pass" -- which cannot itself trip the `state.verdicts` scan
    (only `outcome == "unjudgeable"` does). The envelope carries
    outcome="unjudgeable" under that same id. Because the outcomes differ,
    `already_folded` must evaluate False (an id match alone is NOT proof of
    "the same record"), so the block must come from the envelope scan --
    the only remaining signal available to this state.
    """
    sup = _build_sup()
    judge_per_squad = _node(sup, "judge_per_squad")

    state = HydraState(root_goal="id match, differing outcome resume")
    shared_id = str(uuid4())
    non_unjudgeable_verdict = {
        "id": shared_id,
        "type": "JUDGE_VERDICT",
        "workflow_id": str(state.workflow_id),
        "origin_squad": "hydra-judge",
        "target_envelope_id": str(uuid4()),
        "outcome": "pass",
        "rubric_id": "owasp-asvs-l1@1",
    }
    unjudgeable_envelope_same_id = {
        "id": shared_id,
        "type": "JUDGE_VERDICT",
        "workflow_id": str(state.workflow_id),
        "origin_squad": "hydra-judge",
        "target_envelope_id": str(uuid4()),
        "outcome": "unjudgeable",
        "rubric_id": "constitution-alignment@1",
        "judge_vendor": "codex",
        "critique_md": "non-finite value at $.constraints.budget_usd; refusing to write invalid JSON",
    }
    state.verdicts = [non_unjudgeable_verdict]
    state.envelopes = [unjudgeable_envelope_same_id]

    patch = judge_per_squad(state)

    assert patch["phase"] == "surfaced", (
        "an id match with a DIFFERING outcome must never suppress a genuine "
        "unjudgeable hit -- the block must come from the envelope scan since "
        "state.verdicts alone (outcome='pass') cannot trigger it"
    )
    assert patch["pending_hitl"]["reason"] == "unjudgeable_envelope"


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
    # Item 2 MEDIUM (this round): same abort-only + field-naming contract as
    # `node_judge_per_squad`.
    assert patch["pending_hitl"]["options"] == ["abort"]
    assert patch["pending_hitl"]["default_option"] == "abort"
    assert "constraints.budget_usd" in patch["pending_hitl"]["summary"]
    assert "re-ingest" in patch["pending_hitl"]["summary"]


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


# ---------------------------------------------------------------------------
# Cross-vendor judge finding (2026-09-20, item 1 HIGH): a persisted
# JUDGE_VERDICT envelope with a normal outcome (e.g. "pass") but a
# NaN-poisoned score_json must still hard-block -- the unconditional
# `continue` in the JUDGE_VERDICT branch must never run before the
# envelope-wide non-finite scan.
# ---------------------------------------------------------------------------

def test_persisted_pass_verdict_with_nan_score_json_hard_blocks():
    """The bug this round's structural fix closes: a legacy checkpoint holds
    a JUDGE_VERDICT envelope with outcome="pass" (not "unjudgeable") whose
    `score_json` contains NaN, alongside its finite source envelope which is
    ALREADY judged (a prior verdict targets it). Before the fix, the
    JUDGE_VERDICT branch's own `continue` fired before the non-finite scan
    ever ran for this envelope, so the poisoned score_json reached synthesis
    untouched. The scan must now be the FIRST statement in the loop body, so
    it catches this JUDGE_VERDICT envelope regardless of its outcome."""
    sup = _build_sup()
    judge_per_squad = _node(sup, "judge_per_squad")

    state = HydraState(root_goal="legacy pass verdict with poisoned score_json")
    source_id = str(uuid4())
    finite_source_envelope = {
        "id": source_id,
        "type": "C_SUITE_DECISION_PACKET",
        "origin_squad": "executive",
        "workflow_id": str(state.workflow_id),
        "origin": "BOARDROOM",
        "objective": "already-judged source",
        "constraints": {"budget_usd": 1000.0},
    }
    poisoned_pass_verdict = {
        "id": str(uuid4()),
        "type": "JUDGE_VERDICT",
        "workflow_id": str(state.workflow_id),
        "origin_squad": "hydra-judge",
        "target_squad": "executive",
        "target_envelope_id": source_id,
        "outcome": "pass",
        "rubric_id": "board-decision-quality@1",
        "judge_vendor": "codex",
        "generator_vendor": "claude",
        # Poisoned nested payload -- not the top-level `outcome` field.
        "score_json": {"overall": float("nan")},
    }
    state.envelopes = [finite_source_envelope, poisoned_pass_verdict]
    # The finite source is already judged -- proves the block comes from the
    # scan on the JUDGE_VERDICT envelope itself, not from the source ever
    # being unjudged.
    state.verdicts = [dict(poisoned_pass_verdict)]

    patch = judge_per_squad(state)

    assert patch["phase"] == "surfaced", (
        "a persisted pass verdict whose score_json contains NaN must hard-"
        "block, not silently reach synthesis"
    )
    assert patch["pending_hitl"]["reason"] == "unjudgeable_envelope"
    assert patch["pending_hitl"]["options"] == ["abort"]
    unjudgeable = [v for v in patch["verdicts"] if v.get("outcome") == "unjudgeable"]
    assert unjudgeable, "expected a fresh unjudgeable verdict from the envelope scan"
    assert "score_json" in patch["pending_hitl"]["summary"]


def test_persisted_clean_pass_verdict_with_finite_source_takes_fast_path():
    """Control for the fix above: a persisted `pass` JUDGE_VERDICT envelope
    with a clean (finite) score_json, alongside its already-judged finite
    source envelope, must take the ordinary fast path -- no re-judge, no
    HITL, advances straight to synthesis."""
    sup = _build_sup()
    judge_per_squad = _node(sup, "judge_per_squad")

    state = HydraState(root_goal="clean pass verdict, finite source")
    source_id = str(uuid4())
    finite_source_envelope = {
        "id": source_id,
        "type": "C_SUITE_DECISION_PACKET",
        "origin_squad": "executive",
        "workflow_id": str(state.workflow_id),
        "origin": "BOARDROOM",
        "objective": "already-judged source",
        "constraints": {"budget_usd": 1000.0},
    }
    clean_pass_verdict = {
        "id": str(uuid4()),
        "type": "JUDGE_VERDICT",
        "workflow_id": str(state.workflow_id),
        "origin_squad": "hydra-judge",
        "target_squad": "executive",
        "target_envelope_id": source_id,
        "outcome": "pass",
        "rubric_id": "board-decision-quality@1",
        "judge_vendor": "codex",
        "generator_vendor": "claude",
        "score_json": {"overall": 0.95},
    }
    state.envelopes = [finite_source_envelope, clean_pass_verdict]
    state.verdicts = [dict(clean_pass_verdict)]

    patch = judge_per_squad(state)

    assert patch["phase"] == "synthesis"
    assert patch.get("pending_hitl") is None
    retargeting = [
        v for v in patch["verdicts"] if v.get("target_envelope_id") == source_id
    ]
    assert not retargeting, (
        "an already-judged, finite source with a clean persisted verdict "
        f"must not be re-judged: got fresh verdicts {retargeting}"
    )


def test_verdict_only_nan_score_json_hard_blocks_with_no_matching_envelope():
    """This round's read-side backstop (item 1 CRITICAL): a verdict can sit
    in `state.verdicts` with NO counterpart in `state.envelopes` at all --
    e.g. a best-of-N internal verdict recorded via `node_dispatch`'s
    `verdicts_out.extend(...)`, which never mints a JUDGE_VERDICT envelope
    for each candidate rubric pass. A NaN in that verdict's `score_json`
    must still hard-block `node_judge_per_squad`, not silently take the fast
    path just because the poisoned payload never appears in `state.envelopes`
    for the unconditional envelope-loop scan to catch."""
    sup = _build_sup()
    judge_per_squad = _node(sup, "judge_per_squad")

    state = HydraState(root_goal="verdict-only NaN score, no envelope counterpart")
    source_id = str(uuid4())
    finite_source_envelope = {
        "id": source_id,
        "type": "C_SUITE_DECISION_PACKET",
        "origin_squad": "executive",
        "workflow_id": str(state.workflow_id),
        "origin": "BOARDROOM",
        "objective": "best-of-n winner, no JUDGE_VERDICT envelope minted",
        "constraints": {"budget_usd": 1000.0},
    }
    verdict_only_poisoned = {
        "id": str(uuid4()),
        "type": "JUDGE_VERDICT",
        "workflow_id": str(state.workflow_id),
        "origin_squad": "hydra-judge",
        "target_squad": "executive",
        "target_envelope_id": source_id,
        "outcome": "pass",
        "rubric_id": "board-decision-quality@1",
        "judge_vendor": "codex",
        "generator_vendor": "claude",
        "score_json": {"overall": float("nan")},
    }
    # Only the source envelope is present in `state.envelopes` -- the
    # poisoned verdict lives ONLY in `state.verdicts`, unlike the
    # `test_persisted_pass_verdict_with_nan_score_json_hard_blocks` case
    # above, which also carries the poisoned payload as an envelope.
    state.envelopes = [finite_source_envelope]
    state.verdicts = [dict(verdict_only_poisoned)]

    patch = judge_per_squad(state)

    assert patch["phase"] == "surfaced", (
        "a state.verdicts-only NaN score_json must hard-block even with no "
        "matching state.envelopes entry"
    )
    assert patch["pending_hitl"]["reason"] == "unjudgeable_envelope"
    assert patch["pending_hitl"]["options"] == ["abort"]
    assert "score_json" in patch["pending_hitl"]["summary"]
