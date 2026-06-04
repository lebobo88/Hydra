"""Regression tests: operator force-select (`hydra run --squad ...`) must win
over the intent router.

Bug (2026-06-03, workflow bc3032b4): `node_intake` unconditionally overwrote
`state.selected_squads` with `classify_intent()` output, so a docs-edit goal
that *mentioned* "legal-compliance"/"senate" misrouted to the legal squad even
though the CLI pre-seeded `selected_squads=["engineering"]`.

Contract after the fix:
  * Pre-seeded valid slugs skip classification entirely (confidence=1.0,
    rationale="operator force-select: ...").
  * Unknown slugs are dropped (trace event `force_select_unknown_squads`);
    if nothing valid survives, the router classifies as before.
  * No seed → router behavior unchanged.

No network, no LLMs.
"""
from __future__ import annotations

from pathlib import Path

from hydra_core.state import HydraState


HYDRA_ROOT = Path(__file__).resolve().parents[1]

# Goal text deliberately salted with legal-compliance fingerprints
# ("compliance", "senate") — the router alone would misroute this.
MISLEADING_GOAL = (
    "Update the legal-compliance and senate rows of the squads table "
    "in the AGENTS.md docs to match the registry"
)


class _StubDispatcher:
    """Minimal dispatcher protocol stand-in. Returns a fake result envelope."""
    def call_mcp(self, server, tool, args, **_kw):
        return {"status": "done", "tool": tool, "result": {"ok": True}}

    def spawn_subprocess(self, cmd, env=None):
        return {"status": "done", "stdout": "", "stderr": ""}

    def emit_claude_prompt(self, prompt, agent=None):
        return {"status": "host_pickup_required", "agent": agent}

    def invoke_claude_skill(self, skill, args):
        return {"status": "host_pickup_required", "skill": skill}


def _run(state: HydraState, *, stop_before: str | None = None) -> HydraState:
    from hydra_core.supervisor import build_supervisor, _PurePythonRunner
    sup = build_supervisor(
        project_root=HYDRA_ROOT,
        dispatcher=_StubDispatcher(),
        force_pure_python=True,
    )
    assert isinstance(sup, _PurePythonRunner)
    return sup.invoke(state, stop_before=stop_before)


def test_force_select_overrides_router():
    state = HydraState(root_goal=MISLEADING_GOAL)
    state.selected_squads = ["engineering"]
    final = _run(state)
    assert final.selected_squads == ["engineering"]
    # Every planned task must belong to the forced squad.
    assert {t.owner_squad for t in final.tasks} <= {"engineering"}


def test_force_select_rationale_recorded_at_intake():
    state = HydraState(root_goal=MISLEADING_GOAL)
    state.selected_squads = ["engineering"]
    after_intake = _run(state, stop_before="planner")
    assert after_intake.selected_squads == ["engineering"]
    assert "force-select" in (after_intake.last_event or "")


def test_force_select_drops_unknown_and_keeps_valid():
    state = HydraState(root_goal=MISLEADING_GOAL)
    state.selected_squads = ["no-such-squad", "engineering"]
    final = _run(state)
    assert final.selected_squads == ["engineering"]


def test_force_select_all_unknown_falls_back_to_router():
    state = HydraState(root_goal="Refactor the payments API code and fix the bug")
    state.selected_squads = ["no-such-squad"]
    final = _run(state)
    # Router takes over: engineering keywords win, unknown slug is gone.
    assert "no-such-squad" not in final.selected_squads
    assert "engineering" in final.selected_squads


def test_no_seed_routes_as_before():
    state = HydraState(root_goal=MISLEADING_GOAL)
    final = _run(state)
    assert "legal-compliance" in final.selected_squads
