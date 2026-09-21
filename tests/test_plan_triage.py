"""P3 plan-rigor triage.

`hydra_core.plan_triage.triage_plan` is RECORDING-ONLY today: node_planner
computes plan_rigor/plan_rigor_source but seeds no planning task, sets no
plan_status, and does not alter requires_human_approval or the frozen HITL
reason precedence. Those change in a later phase.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from hydra_core.plan_triage import triage_plan
from hydra_core.squad_loader import GateSpec, SquadPack

HYDRA_ROOT = Path(__file__).resolve().parents[1]


def _pack(slug: str, *, hitl: bool = False, stub: bool = False) -> SquadPack:
    return SquadPack(
        slug=slug,
        name=slug,
        description=slug,
        entrypoint="stub" if stub else "claude-skill",
        gates=(GateSpec(hitl_required=True),) if hitl else (),
    )


# --------------------------------------------------------------------------- #
# Purity / determinism
# --------------------------------------------------------------------------- #

def test_module_imports_no_dispatcher_or_host_bridge():
    """Import-closure check -- not merely 'plan_triage.py has no banned import
    line'. A transitive import (e.g. via `.judge.router`, which itself pulls
    in `judge/__init__.py`'s eager dispatcher + MCP critique client re-exports)
    would defeat a source-text/AST check while still breaking replay
    determinism and `HYDRA_TEST_NO_DAEMONS=1` hermeticity (see module
    docstring).

    This runs the import in a FRESH subprocess rather than mutating
    `sys.modules` in this (shared pytest) process. Deleting every
    `hydra_core*` entry from `sys.modules` in-process and re-importing
    previously caused 13 order-dependent failures elsewhere in the suite
    (test_target_repo_scaffolding.py, test_venom_registry_fail_closed.py,
    test_worktree_relocation.py): pytest had already collected modules
    holding references to the OLD hydra_core objects, and after the
    deletion, later string-based monkeypatches bound to NEW module objects
    while earlier-imported callables still pointed at the old ones -- the
    module graph split. A subprocess has its own independent sys.modules,
    so the parent process's module graph is never touched.
    """
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, json; before = set(sys.modules); "
            "import hydra_core.plan_triage; "
            "print(json.dumps(sorted(set(sys.modules) - before)))",
        ],
        cwd=str(HYDRA_ROOT),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert child.returncode == 0, (
        f"subprocess import of hydra_core.plan_triage failed: {child.stderr}"
    )
    added = json.loads(child.stdout.strip().splitlines()[-1])

    forbidden = [
        m for m in added
        if "dispatcher" in m or "mcp_client" in m or "host_bridge" in m
    ]
    assert not forbidden, (
        f"hydra_core.plan_triage's import closure pulled in {forbidden!r} "
        "-- these make plan_rigor recomputation non-deterministic on replay"
    )


def test_triage_plan_is_deterministic():
    kwargs = dict(
        goal="Refactor the widget renderer for clarity.",
        selected_squads=["engineering"],
        task_priorities=["P2"],
        budget_usd=5.0,
        risk_tolerance="medium",
        packs={"engineering": _pack("engineering")},
        repo_count=1,
    )
    first = triage_plan(**kwargs)
    second = triage_plan(**kwargs)
    assert first == second


# --------------------------------------------------------------------------- #
# Baselines
# --------------------------------------------------------------------------- #

_TRIVIAL_KW = dict(
    goal="Fix a typo in the README.",
    selected_squads=["research-ds"],
    task_priorities=["P2"],
    budget_usd=1.0,
    risk_tolerance="medium",
    packs={"research-ds": _pack("research-ds")},
    repo_count=1,
)


def test_trivial_baseline():
    rigor, reason = triage_plan(**_TRIVIAL_KW)
    assert rigor == "trivial"
    assert "one selected squad" in reason


# --------------------------------------------------------------------------- #
# Major triggers -- each fires independently against the trivial baseline
# --------------------------------------------------------------------------- #

def test_major_three_or_more_squads():
    rigor, reason = triage_plan(**{
        **_TRIVIAL_KW,
        "selected_squads": ["research-ds", "sales-gtm", "healthcare"],
        "packs": {s: _pack(s, stub=True) for s in
                  ("research-ds", "sales-gtm", "healthcare")},
    })
    assert rigor == "major"
    assert "squads" in reason


def test_major_p0_task():
    rigor, reason = triage_plan(**{**_TRIVIAL_KW, "task_priorities": ["P2", "P0"]})
    assert rigor == "major"
    assert "P0" in reason


def test_major_fleet_repo_count():
    rigor, reason = triage_plan(**{**_TRIVIAL_KW, "repo_count": 2})
    assert rigor == "major"
    assert "repos" in reason


def test_major_budget_ceiling():
    rigor, reason = triage_plan(**{**_TRIVIAL_KW, "budget_usd": 100.0})
    assert rigor == "major"
    assert "budget_usd" in reason


def test_major_hitl_gate_and_budget_floor():
    rigor, reason = triage_plan(**{
        **_TRIVIAL_KW,
        "budget_usd": 30.0,
        "packs": {"research-ds": _pack("research-ds", hitl=True)},
    })
    assert rigor == "major"
    assert "hitl_required" in reason

    # The joint condition: the SAME hitl gate below the budget floor must NOT
    # escalate to major on its own (proves the "AND budget_usd >= 25" half is
    # load-bearing, not decorative).
    rigor2, _ = triage_plan(**{
        **_TRIVIAL_KW,
        "budget_usd": 5.0,
        "packs": {"research-ds": _pack("research-ds", hitl=True)},
    })
    assert rigor2 != "major"


def test_major_escalation_keyword():
    rigor, reason = triage_plan(**{
        **_TRIVIAL_KW,
        "goal": "Prepare the merger due-diligence summary.",
    })
    assert rigor == "major"
    assert "escalation" in reason


def test_major_high_risk_tolerance():
    rigor, reason = triage_plan(**{**_TRIVIAL_KW, "risk_tolerance": "high"})
    assert rigor == "major"
    assert "risk_tolerance" in reason


# --------------------------------------------------------------------------- #
# Trivial conditions -- violating exactly one alone drops to standard
# (escalation keyword is the one exception: it is ALSO a major trigger, so
# violating "no escalation keyword" correctly produces major, not standard --
# see test_major_escalation_keyword above and the note in plan_triage.py).
# --------------------------------------------------------------------------- #

def test_trivial_violation_more_than_one_squad_drops_to_standard():
    rigor, _ = triage_plan(**{
        **_TRIVIAL_KW,
        "selected_squads": ["research-ds", "sales-gtm"],
        "packs": {s: _pack(s, stub=True) for s in ("research-ds", "sales-gtm")},
    })
    assert rigor == "standard"


def test_trivial_violation_p1_task_drops_to_standard():
    rigor, _ = triage_plan(**{**_TRIVIAL_KW, "task_priorities": ["P1"]})
    assert rigor == "standard"


def test_trivial_violation_budget_at_floor_drops_to_standard():
    rigor, _ = triage_plan(**{**_TRIVIAL_KW, "budget_usd": 10.0})
    assert rigor == "standard"


def test_trivial_violation_hitl_gate_below_floor_drops_to_standard():
    rigor, _ = triage_plan(**{
        **_TRIVIAL_KW,
        "packs": {"research-ds": _pack("research-ds", hitl=True)},
    })
    assert rigor == "standard"


def test_trivial_violation_long_goal_drops_to_standard():
    rigor, _ = triage_plan(**{**_TRIVIAL_KW, "goal": "x" * 200})
    assert rigor == "standard"


def test_standard_fallback_has_no_major_trigger_and_no_full_trivial_match():
    # Two squads: not >=3 (no major trigger), not ==1 (fails trivial).
    rigor, reason = triage_plan(**{
        **_TRIVIAL_KW,
        "selected_squads": ["research-ds", "sales-gtm"],
        "packs": {s: _pack(s, stub=True) for s in ("research-ds", "sales-gtm")},
    })
    assert rigor == "standard"
    assert "no major trigger" in reason


# --------------------------------------------------------------------------- #
# node_planner wiring: recording only, operator override, downgrade event.
#
# We call the bare `planner` step off a pure-python supervisor runner and
# inspect its returned patch dict directly -- the same technique
# tests/test_repo_targeting.py::test_node_postcheck_preserves_surfaced_phase
# uses for node_postcheck. node_planner's return value IS the LangGraph patch
# (tasks/hitl_history use append reducers, so what it returns is the
# NET-NEW delta, not the full merged state) -- exactly what we want to pin.
# --------------------------------------------------------------------------- #

from unittest.mock import MagicMock

from hydra_core.squad_node import Dispatcher
from hydra_core.state import HydraState  # noqa: E402
from hydra_core.supervisor import build_supervisor  # noqa: E402


def _planner_fn():
    stub_dispatcher = MagicMock(spec=Dispatcher)
    stub_dispatcher._tool_tracker = None
    runner = build_supervisor(
        project_root=HYDRA_ROOT,
        dispatcher=stub_dispatcher,
        force_pure_python=True,
    )
    fn = dict(runner.steps).get("planner")
    assert fn is not None, "planner step not found in runner"
    return fn


def test_node_planner_records_rigor_without_side_effects():
    """The regression guard for the whole run: triage recording must not seed
    a task, set plan_status, or touch requires_human_approval/pending_hitl."""
    state = HydraState(
        root_goal="Fix a typo in the README.",
        selected_squads=["research-ds"],
    )
    state.budget.budget_usd = 1.0
    patch = _planner_fn()(state)

    assert patch["plan_rigor"] == "trivial"
    assert patch["plan_rigor_source"] == "triage"
    # No planning task seeded: exactly one synthesised task (research-ds' own
    # default work task), never a second "planning" task.
    assert len(patch["tasks"]) == 1
    assert patch["tasks"][0].owner_squad == "research-ds"
    assert "plan_status" not in patch
    assert patch["requires_human_approval"] is False
    assert "pending_hitl" not in patch
    assert "hitl_history" not in patch


def test_node_planner_operator_override_wins_and_records_downgrade():
    """A goal that triages major (3 squads), overridden down to trivial,
    must win with plan_rigor_source=operator_flag AND log the downgrade."""
    state = HydraState(
        root_goal="Coordinate three squads on a small task.",
        selected_squads=["research-ds", "sales-gtm", "healthcare"],
    )
    state.budget.budget_usd = 1.0
    state.plan_rigor_override = "trivial"
    patch = _planner_fn()(state)

    assert patch["plan_rigor"] == "trivial"
    assert patch["plan_rigor_source"] == "operator_flag"
    downgrades = [
        e for e in patch.get("hitl_history", [])
        if e.get("event") == "plan_rigor_override_downgrade"
    ]
    assert len(downgrades) == 1
    assert downgrades[0]["computed_rigor"] == "major"
    assert downgrades[0]["override_rigor"] == "trivial"


def test_node_planner_operator_override_no_downgrade_event_when_upgrading():
    """Overriding UP (or to the same value) is not a downgrade -- no event."""
    state = HydraState(
        root_goal="Fix a typo in the README.",
        selected_squads=["research-ds"],
    )
    state.budget.budget_usd = 1.0
    state.plan_rigor_override = "major"
    patch = _planner_fn()(state)

    assert patch["plan_rigor"] == "major"
    assert patch["plan_rigor_source"] == "operator_flag"
    assert "hitl_history" not in patch


def test_surfaced_missing_engineering_target_never_computes_triage():
    """A planner that surfaces on WS1-E (no target repo) must return before
    triage runs at all -- the patch carries no plan_rigor key."""
    state = HydraState(
        root_goal="Implement a feature with no target repo.",
        selected_squads=["engineering"],
    )
    patch = _planner_fn()(state)

    assert patch["phase"] == "surfaced"
    assert patch["pending_hitl"]["reason"] == "missing_engineering_target"
    assert "plan_rigor" not in patch
    assert "plan_rigor_source" not in patch
