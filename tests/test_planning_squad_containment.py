"""P4 of the planning-phase build: the `planning` squad pack (squads/planning/)
is discovered but MUST NOT be reachable -- not by automatic routing, not by
the LLM fallback, not by explicit force-selection, and its output must not
surface as a squad "voice" in synthesis. No engine wiring seeds a planning
task in this phase; that is P5.

Every containment test below is written to be DISCRIMINATING: it shows the
guarded mechanism would have selected/surfaced the "planning" slug if the
guard were absent, by monkeypatching the guard out (RESERVED_META_SQUADS) or,
where that is impractical, by contrasting against a non-reserved slug with
the same shape.

No network, no LLMs.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from hydra_core.native_packs import NATIVE_PACKS, native_pack, native_pack_root
from hydra_core.router import RESERVED_META_SQUADS, classify_intent
from hydra_core.squad_loader import GateSpec, SquadPack, discover_squads
from hydra_core.state import HydraState

HYDRA_ROOT = Path(__file__).resolve().parents[1]


class _StubDispatcher:
    """Minimal dispatcher protocol stand-in (mirrors test_router_force_select.py)."""

    def call_mcp(self, server, tool, args, **_kw):
        return {"status": "done", "tool": tool, "result": {"ok": True}}

    def spawn_subprocess(self, cmd, env=None):
        return {"status": "done", "stdout": "", "stderr": ""}

    def emit_claude_prompt(self, prompt, agent=None):
        return {"status": "host_pickup_required", "agent": agent}

    def invoke_claude_skill(self, skill, args):
        return {"status": "host_pickup_required", "skill": skill}


def _build_runner():
    from hydra_core.supervisor import build_supervisor, _PurePythonRunner
    sup = build_supervisor(
        project_root=HYDRA_ROOT,
        dispatcher=_StubDispatcher(),
        force_pure_python=True,
    )
    assert isinstance(sup, _PurePythonRunner)
    return sup


def _run(state: HydraState, *, stop_before: str | None = None) -> HydraState:
    return _build_runner().invoke(state, stop_before=stop_before)


# --------------------------------------------------------------------------- #
# 1. The pack loads and is discovered.
# --------------------------------------------------------------------------- #

def test_planning_pack_is_discovered():
    packs = discover_squads(HYDRA_ROOT)
    assert "planning" in packs
    pack = packs["planning"]
    assert pack.entrypoint == "claude-native"
    assert pack.best_of_n == 0
    assert set(pack.accepts) >= {"C_SUITE_DECISION_PACKET", "HANDOFF"}
    assert set(pack.emits) >= {"PLAN", "HITL_REQUEST", "DECISION_RECORD"}
    assert {a.slug for a in pack.agents} == {"plan-author", "plan-critic", "plan-scribe"}


def test_planning_pack_declares_no_industries():
    pack = discover_squads(HYDRA_ROOT)["planning"]
    assert pack.industries == ()


# --------------------------------------------------------------------------- #
# 2. Landmine 1 -- no gate declares hitl_required.
# --------------------------------------------------------------------------- #

def test_no_planning_gate_declares_hitl_required():
    pack = discover_squads(HYDRA_ROOT)["planning"]
    assert pack.gates, "planning pack should declare at least one gate"
    assert not any(g.hitl_required for g in pack.gates)
    assert not any(a.hitl_trigger for a in pack.agents)


def test_hitl_required_gate_WOULD_trip_task_is_high_risk_if_present():
    """Discriminating proof for the guard above: a P2 task owned by a squad
    whose pack DOES declare hitl_required=True is high-risk regardless of its
    own acceptance criteria or priority. This is the exact mechanism the
    previous test guards `planning` against tripping."""
    from hydra_core.state import TaskState

    hot_pack = SquadPack(
        slug="planning", name="planning", description="",
        entrypoint="claude-native",
        gates=(GateSpec(rubric_id="x", hitl_required=True, when="always"),),
    )
    packs = {"planning": hot_pack}
    task = TaskState(
        description="d", owner_squad="planning", priority="P2",
        acceptance_criteria=["a clear, checkable criterion"],
    )

    def _task_is_high_risk(t):
        if t.priority in {"P0", "P1"}:
            return True
        sp = packs.get(t.owner_squad)
        return sp is not None and sp.entrypoint != "stub" and any(g.hitl_required for g in sp.gates)

    assert _task_is_high_risk(task) is True  # proves the mechanism is live
    # ...and the REAL planning pack (P2 task, real gates) does not trip it:
    real_pack = discover_squads(HYDRA_ROOT)["planning"]
    packs["planning"] = real_pack
    assert _task_is_high_risk(task) is False


# --------------------------------------------------------------------------- #
# 3. Router: deterministic layer does not select "planning".
# --------------------------------------------------------------------------- #

def test_router_denylist_blocks_industry_overlap_selection(monkeypatch):
    """Discriminating: with the denylist removed, an industry-overlap match
    WOULD select "planning"; with it present, the same overlap is refused."""
    hot_pack = SquadPack(
        slug="planning", name="planning", description="",
        entrypoint="claude-native", industries=("widgets",),
    )
    # A second, non-reserved, non-overlapping pack keeps the router's
    # absolute-last-resort default (which ignores exclusions by design, same
    # as it does for stubs) from picking "planning" merely because it was the
    # only pack in the dict -- that would be a false pass unrelated to the
    # guard under test.
    other_pack = SquadPack(
        slug="widget-squad", name="widget-squad", description="",
        entrypoint="claude-native",
    )
    packs = {"planning": hot_pack, "widget-squad": other_pack}

    monkeypatch.setattr("hydra_core.router.RESERVED_META_SQUADS", frozenset())
    decision = classify_intent("do a widgets thing", packs, industries=("widgets",))
    assert "planning" in decision.squads  # guard absent -> reachable

    monkeypatch.undo()
    decision = classify_intent("do a widgets thing", packs, industries=("widgets",))
    assert "planning" not in decision.squads  # guard present -> refused


def test_router_denylist_does_not_over_block_a_normal_squad():
    """Two-directions check: a non-reserved slug with the identical shape IS
    selected, proving the denylist targets the specific slug rather than a
    generally broken router."""
    hot_pack = SquadPack(
        slug="widget-squad", name="widget-squad", description="",
        entrypoint="claude-native", industries=("widgets",),
    )
    packs = {"widget-squad": hot_pack}
    decision = classify_intent("do a widgets thing", packs, industries=("widgets",))
    assert "widget-squad" in decision.squads


# --------------------------------------------------------------------------- #
# 4. Router: LLM fallback does not select "planning".
# --------------------------------------------------------------------------- #

def test_router_denylist_blocks_llm_fallback_selection(monkeypatch):
    packs = discover_squads(HYDRA_ROOT)
    # Goal text with no keyword fingerprints anywhere, so the deterministic
    # layer scores nothing and control reaches the LLM-fallback branch.
    goal = "zzqx flibbertigibbet nonsense goal text"

    def _fake_llm(_text, _packs):
        return ["planning"]

    monkeypatch.setattr("hydra_core.router.RESERVED_META_SQUADS", frozenset())
    decision = classify_intent(goal, packs, classify_callable=_fake_llm)
    assert decision.squads == ["planning"]  # guard absent -> LLM slug survives

    monkeypatch.undo()
    decision = classify_intent(goal, packs, classify_callable=_fake_llm)
    assert "planning" not in decision.squads  # guard present -> filtered


# --------------------------------------------------------------------------- #
# 5. Force-selection is rejected -- pre-seeded `selected_squads` / --squad.
# --------------------------------------------------------------------------- #

def test_force_select_planning_is_rejected(monkeypatch):
    state = HydraState(root_goal="Refactor the payments API code and fix the bug")
    state.selected_squads = ["planning"]
    after_intake = _run(state, stop_before="planner")
    assert "planning" not in after_intake.selected_squads
    # Falls back to the router; the misleading-but-real engineering keywords win.
    assert "engineering" in after_intake.selected_squads


def test_force_select_planning_WOULD_succeed_if_denylist_absent(monkeypatch):
    """Discriminating: patch the guard out at the site supervisor.py actually
    reads it from, and show the same force-select now succeeds."""
    import hydra_core.supervisor as supervisor_mod
    monkeypatch.setattr(supervisor_mod, "RESERVED_META_SQUADS", frozenset())
    state = HydraState(root_goal="Refactor the payments API code and fix the bug")
    state.selected_squads = ["planning"]
    after_intake = _run(state, stop_before="planner")
    assert after_intake.selected_squads == ["planning"]


def test_force_select_planning_still_rejected_via_goal_text_squad_flag():
    """The --squad token embedded in goal text (RA-10) must be rejected the
    same way as the pre-seeded selected_squads path."""
    state = HydraState(root_goal="Do the thing --squad planning")
    final = _run(state)
    assert final.phase == "surfaced"
    assert final.pending_hitl is not None
    assert "planning" in str(final.pending_hitl.get("summary", ""))


def test_force_select_engineering_via_goal_text_squad_flag_still_works():
    """Two-directions check for the goal-text path: a real, non-reserved slug
    is still force-selectable, proving the rejection is planning-specific."""
    state = HydraState(root_goal="Do the thing --squad engineering")
    final = _run(state)
    assert final.selected_squads == ["engineering"]


# --------------------------------------------------------------------------- #
# 6. Synthesis: a planning-origin envelope is not a squad "voice".
# --------------------------------------------------------------------------- #

def _synthesis_fn():
    runner = _build_runner()
    fn = dict(runner.steps).get("synthesis")
    assert fn is not None
    return fn


def test_planning_origin_envelope_excluded_from_synthesis_voices():
    state = HydraState(root_goal="anything")
    state.selected_squads = ["executive"]
    state.envelopes = [
        {"id": "e1", "origin_squad": "planning", "target_squad": "hydra",
         "decision": "plan drafted", "rationale": "r", "artifacts": [], "sealed": True},
        {"id": "e2", "origin_squad": "executive", "target_squad": "hydra",
         "decision": "exec decision", "rationale": "r", "artifacts": [], "sealed": True},
    ]
    patch = _synthesis_fn()(state) or {}
    rationale = json.dumps(patch, default=str)
    # node_synthesis's non-fleet rendering is a per-squad envelope-count line
    # ("  • <crown label> (<squad>): N envelope(s)"), not the envelope's
    # own "decision" text -- assert on that exact rendered marker so this test
    # cannot pass merely because "planning" leaked in incidentally elsewhere.
    assert "(executive): 1 envelope(s)" in rationale
    origins = {e.get("origin_squad") for e in state.envelopes}
    assert "planning" in origins  # the envelope really is there
    assert "(planning):" not in rationale  # ...but rendered no squad-voice section


def test_planning_origin_envelope_WOULD_surface_if_filter_absent(monkeypatch):
    """Discriminating: patch the filter out at the exact site node_synthesis
    reads it from and show the planning-origin envelope is now grouped."""
    import hydra_core.supervisor as supervisor_mod
    monkeypatch.setattr(supervisor_mod, "RESERVED_META_SQUADS", frozenset())
    state = HydraState(root_goal="anything")
    state.selected_squads = ["executive"]
    state.envelopes = [
        {"id": "e1", "origin_squad": "planning", "target_squad": "hydra",
         "decision": "plan drafted", "rationale": "r", "artifacts": [], "sealed": True},
    ]
    patch = _synthesis_fn()(state) or {}
    rationale = json.dumps(patch, default=str)
    assert "(planning):" in rationale  # guard absent -> planning voice surfaces


def test_materialize_attended_results_planning_origin_also_excluded():
    """Second emission point: _materialize_attended_results (hydra_core/cli.py)
    synthesizes its own DECISION_RECORD carrying origin_squad="planning" for
    an attended task. Feed its output through the same node_synthesis grouping
    used above and confirm it is excluded there too."""
    from hydra_core.cli import _materialize_attended_results

    state = HydraState(root_goal="anything")
    state.selected_squads = ["executive"]
    state.attended_results = [{
        "task_id": "t-plan-1", "owner_squad": "planning", "run_id": "run-1",
        "final_status": "complete", "summary": "plan attended-drafted",
    }]
    envelopes, artifacts = _materialize_attended_results(state)
    assert envelopes and envelopes[0]["origin_squad"] == "planning"

    state.envelopes = list(envelopes)
    state.artifacts = list(artifacts)
    patch = _synthesis_fn()(state) or {}
    rationale = json.dumps(patch, default=str)
    assert "(planning):" not in rationale


# --------------------------------------------------------------------------- #
# 6b. Synthesis: an envelope that FAILS schema validation is still redacted,
#     never passed through raw (cross-vendor judge finding, b1baf30 revise
#     round, item 3).
# --------------------------------------------------------------------------- #

def test_synthesis_redacts_envelope_that_fails_validation_never_raw(monkeypatch):
    """A legacy envelope that fails `validate_envelope` (here: an ``owner``
    outside DevTask's literal set -- the same shape as `test_
    unrepairable_envelope_is_rejected_with_trace_event`) used to fall through
    a bare ``except (ValueError, Exception)`` and get appended to synthesis
    RAW, unredacted. It must now still cross the boundary redacted, and the
    envelope must still be counted (not dropped) -- legacy synthesis must
    keep working."""
    import hydra_core.supervisor as supervisor_mod

    trace_calls: list[tuple] = []
    real_emit_trace = supervisor_mod.emit_trace
    monkeypatch.setattr(
        supervisor_mod, "emit_trace",
        lambda *a, **kw: trace_calls.append((a, kw)) or real_emit_trace(*a, **kw),
    )

    state = HydraState(root_goal="anything")
    state.selected_squads = ["engineering"]
    state.envelopes = [{
        "id": "e1", "type": "DEV_TASK", "origin_squad": "engineering",
        "target_squad": "hydra", "workflow_id": str(state.workflow_id),
        "owner": "not-a-real-owner-literal",  # fails schema enum validation
        "branch": "b", "repo": "hydra",
        "instructions": "contact ops@example.com about the fix",
    }]
    patch = _synthesis_fn()(state) or {}
    rationale = json.dumps(patch, default=str)

    # The unredacted PII never crosses the boundary in the rendered output.
    assert "ops@example.com" not in rationale
    # The envelope was still counted -- redacted-fallback, not silently
    # dropped -- so legacy synthesis keeps working.
    assert "(engineering): 1 envelope(s)" in rationale
    # The redacted-fallback path actually ran (proves it wasn't just excluded
    # some other way) and its trace event fired.
    fallback_events = [
        a for (a, kw) in trace_calls if len(a) >= 3 and a[2] == "envelope_validation_failed_redacted_fallback"
    ]
    assert len(fallback_events) == 1
    assert fallback_events[0][3]["envelope_id"] == "e1"


def test_synthesis_drops_envelope_when_redaction_itself_fails(monkeypatch):
    """If redacting the raw dict fallback ALSO raises, the envelope must be
    dropped -- never leaked raw as a last resort."""
    import hydra_core.supervisor as supervisor_mod

    def _boom(_text):
        raise RuntimeError("redaction backend down")

    monkeypatch.setattr(supervisor_mod, "redact_for_squad_boundary", _boom)

    state = HydraState(root_goal="anything")
    state.selected_squads = ["engineering"]
    state.envelopes = [{
        "id": "e1", "type": "DEV_TASK", "origin_squad": "engineering",
        "target_squad": "hydra", "workflow_id": str(state.workflow_id),
        "owner": "not-a-real-owner-literal",
        "branch": "b", "repo": "hydra",
        "instructions": "contact ops@example.com about the fix",
    }]
    patch = _synthesis_fn()(state) or {}
    rationale = json.dumps(patch, default=str)
    assert "ops@example.com" not in rationale
    assert "(engineering):" not in rationale


def test_synthesis_unrelated_bug_in_boundary_helper_is_not_swallowed(monkeypatch):
    """Cross-vendor judge finding (item 3, second half): the old
    ``except (ValueError, Exception)`` caught EVERY exception, including a
    genuine bug unrelated to schema validation. Narrowing to ``ValueError``
    must let something else propagate instead of being silently absorbed
    into the raw-envelope fallback."""
    import hydra_core.supervisor as supervisor_mod

    def _boom(*_a, **_kw):
        raise RuntimeError("unrelated bug, not a validation failure")

    # `_validate_and_redact_envelope` is a closure local to `build_supervisor`
    # that calls the module-level `validate_envelope` name -- patch that name
    # directly so the closure picks it up.
    monkeypatch.setattr(supervisor_mod, "validate_envelope", _boom)

    state = HydraState(root_goal="anything")
    state.selected_squads = ["engineering"]
    state.envelopes = [{
        "id": "e1", "type": "DEV_TASK", "origin_squad": "engineering",
        "target_squad": "hydra", "workflow_id": str(state.workflow_id),
        "owner": "not-a-real-owner-literal",
        "branch": "b", "repo": "hydra", "instructions": "x",
    }]
    with pytest.raises(RuntimeError, match="unrelated bug"):
        _synthesis_fn()(state)


# --------------------------------------------------------------------------- #
# 7. Native-pack entry resolves; output_root is not the tracked plan dir.
# --------------------------------------------------------------------------- #

def test_native_pack_entry_for_planning():
    assert "planning" in NATIVE_PACKS
    pack = native_pack("planning")
    assert pack.plugin == "hydra"
    assert pack.repo_id == "hydra"
    assert pack.lead_agent == "plan-author"
    assert pack.qualified_lead_agent == "hydra:plan-author"
    assert pack.output_root != "docs/plans"
    assert pack.output_root == ".hydra/plan"


def test_native_pack_root_for_planning_resolves():
    root = native_pack_root("planning")
    assert root.is_dir()
    assert (root / "plugins" / "hydra").is_dir()


# --------------------------------------------------------------------------- #
# 8. The three agent files exist and are listed in the plugin manifest.
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("agent_slug", ["plan-author", "plan-critic", "plan-scribe"])
def test_planning_agent_file_exists(agent_slug):
    p = HYDRA_ROOT / "plugins" / "hydra" / "agents" / f"{agent_slug}.md"
    assert p.is_file()
    text = p.read_text(encoding="utf-8")
    assert text.startswith("---")
    assert f"name: {agent_slug}" in text
    assert "permissionMode: plan" in text


def test_planning_agents_listed_in_plugin_manifest():
    manifest = json.loads(
        (HYDRA_ROOT / "plugins" / "hydra" / ".claude-plugin" / "plugin.json")
        .read_text(encoding="utf-8")
    )
    agents = set(manifest["agents"])
    for slug in ("plan-author", "plan-critic", "plan-scribe"):
        assert f"./agents/{slug}.md" in agents
