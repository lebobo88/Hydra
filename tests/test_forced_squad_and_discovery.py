"""Regression tests for F-12 (forced --squad honored over the auto-router)
and F-11 (built-in squad fallback when --project points elsewhere).

Before these fixes: node_intake unconditionally overwrote any preset
selected_squads with the router's keyword pick, and discover_squads()
searched only <project>/squads + ~/.hydra/squads (despite the docstring
promising a built-in tier).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from hydra_core import cli
from hydra_core.squad_loader import BUILTIN_SQUAD_DIR, discover_squads
from hydra_core.state import HydraState


HYDRA_ROOT = Path(__file__).resolve().parents[1]

# A goal whose keywords route to marketing squads (garland / sales-gtm),
# NOT engineering — so a surviving "engineering" selection proves the
# forced path, not a lucky router coincidence.
MARKETING_GOAL = "outline a Q3 marketing campaign and sales launch for Helios"


class _StubDispatcher:
    def call_mcp(self, server, tool, args, **_kw):
        return {"status": "done", "tool": tool, "result": {"ok": True}}

    def spawn_subprocess(self, cmd, env=None):
        return {"status": "done", "stdout": "", "stderr": ""}

    def emit_claude_prompt(self, prompt, agent=None):
        return {"status": "host_pickup_required", "agent": agent}

    def invoke_claude_skill(self, skill, args):
        return {"status": "host_pickup_required", "skill": skill}


class _ScriptedCritiqueClient:
    def critique(self, *, vendor, artifact_text, rubric_md):
        return {
            "outcome": "pass",
            "critique_md": "solid analysis " * 10,
            "score_json": {"clarity": 5, "rigor": 4},
        }


def _build_runner():
    from hydra_core.supervisor import build_supervisor
    return build_supervisor(
        project_root=HYDRA_ROOT,
        dispatcher=_StubDispatcher(),
        critique_client=_ScriptedCritiqueClient(),
        force_pure_python=True,
    )


def _invoke(sup, state):
    from hydra_core.supervisor import _PurePythonRunner
    if isinstance(sup, _PurePythonRunner):
        return sup.invoke(state)
    out = sup.invoke(
        state,
        config={"configurable": {"thread_id": str(state.workflow_id)}},
    )
    if isinstance(out, dict):
        return HydraState.model_validate(out)
    return out


# --- F-12: forced squads survive intake and are used -------------------------

def test_forced_squad_survives_intake_and_is_used():
    runner = _build_runner()
    state = HydraState(
        root_goal=MARKETING_GOAL,
        selected_squads=["engineering"],
        squads_forced=True,
    )
    final = _invoke(runner, state)
    assert final.selected_squads == ["engineering"], (
        f"forced squad was overwritten: {final.selected_squads}"
    )
    # Used downstream, not just recorded: the planner creates work owned by
    # the selected squad(s).
    owners = {t.owner_squad for t in final.tasks}
    assert "engineering" in owners, f"no task owned by forced squad: {owners}"
    assert "intake: forced" in (final.last_event or "") or final.last_event


def test_unforced_goal_still_auto_routes():
    runner = _build_runner()
    state = HydraState(root_goal=MARKETING_GOAL)
    final = _invoke(runner, state)
    assert final.selected_squads, "router selected nothing"
    assert "engineering" not in final.selected_squads, (
        "marketing goal unexpectedly routed to engineering — router bypassed?"
    )


def test_invalid_forced_squad_errors_clearly():
    runner = _build_runner()
    state = HydraState(
        root_goal=MARKETING_GOAL,
        selected_squads=["no-such-squad"],
        squads_forced=True,
    )
    with pytest.raises(ValueError, match="no-such-squad"):
        _invoke(runner, state)


def test_cli_run_forced_squad_overrides_router(capsys):
    """End-to-end: `hydra run <marketing goal> --squad engineering` must
    keep engineering — pre-fix this routed to garland/sales-gtm."""
    rc = cli.main([
        "--project", str(HYDRA_ROOT),
        "run", MARKETING_GOAL,
        "--squad", "engineering",
    ])
    out = capsys.readouterr().out
    payload = None
    lines = out.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("{"):
            try:
                payload = json.loads("\n".join(lines[i:]))
                break
            except json.JSONDecodeError:
                continue
    assert payload is not None, f"no JSON found in: {out[-500:]}"
    assert rc == 0
    assert payload["selected_squads"] == ["engineering"]
    # Any valid lifecycle phase is fine — this test pins squad forcing,
    # not where the non-live workflow halts.
    assert payload["phase"]


# --- F-11: built-in squad fallback -------------------------------------------

def test_discover_squads_falls_back_to_builtins(tmp_path):
    """A project dir with no squads/ must still see the built-in registry."""
    packs = discover_squads(tmp_path)
    assert packs, "no built-in squads discovered for an empty project root"
    assert "engineering" in packs


def test_project_squad_shadows_builtin(tmp_path):
    """project > user > built-in: a same-slug project pack wins."""
    sq = tmp_path / "squads" / "engineering"
    sq.mkdir(parents=True)
    (sq / "squad.yaml").write_text(
        "name: engineering\ndescription: project override\n",
        encoding="utf-8",
    )
    packs = discover_squads(tmp_path)
    assert "engineering" in packs
    assert packs["engineering"].description == "project override"
    # Built-ins not shadowed still come through.
    builtin_slugs = {
        p.name for p in BUILTIN_SQUAD_DIR.iterdir()
        if p.is_dir() and (p / "squad.yaml").exists()
    }
    assert builtin_slugs - {"engineering"} <= set(packs)
