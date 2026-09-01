"""E2-22 — an attended approve pass defers engineering instead of running a
skeleton dispatch and scoring it with fake verdicts.

Live signature (workflow 8e002d23, 2026-09-01): `hydra approve <wf>` re-enters
node_dispatch with the CLI's inert `_NullDispatcher`. Native squads were
correctly parked (`dispatch.deferred_to_host`), but the engineering pack is
`entrypoint: mcp`, so it fell through to `_via_mcp`. The stub response carried
a `run_id`, so the graph recorded a placeholder engineering DECISION_RECORD,
the NoOp judge scored it (`score_json={"_skeleton": true}`), a Reflexion retry
produced a second identical skeleton verdict, and the pass ran on toward
synthesis — all before the host had driven a single attended stage.

The contract asserted here: on a non-live dispatcher every squad defers, the
judge plane records nothing, and the workflow does not reach synthesis. The
live path is unchanged and is covered by test_dispatch_defer_nonmcp.py.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from hydra_core.state import HydraState
from hydra_core.telemetry import trace_path

HYDRA_ROOT = Path(__file__).resolve().parents[1]


class _AttendedNullDispatcher:
    """Mirrors `hydra_core.cli._NullDispatcher` — the dispatcher an attended
    (non-``--live``) approve pass builds. Note the absence of both
    ``live_execution`` and ``allow_offline_mcp_dispatch``."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.skill_calls: list[str] = []
        self.prompt_calls: list[str] = []

    def call_mcp(self, server, tool, args, **_kw):
        self.calls.append((server, tool))
        return {"status": "stub", "tool": tool, "args": args, "run_id": "stub-run"}

    def spawn_subprocess(self, cmd, env=None):
        return {"status": "stub", "stdout": "", "cmd": cmd}

    def emit_claude_prompt(self, prompt, agent=None):
        self.prompt_calls.append(agent or "")
        return {"status": "stub", "summary": prompt[:80], "agent": agent}

    def invoke_claude_skill(self, skill, args):
        self.skill_calls.append(skill)
        return {"status": "stub", "summary": f"would invoke /{skill}"}

    def run_host_agent(self, agent_type, prompt, *, cwd=None, timeout_s=None):
        return None

    def set_squad_packs(self, packs):
        pass


def _run(squads: list[str], dispatcher) -> HydraState:
    from hydra_core.supervisor import build_supervisor

    runner = build_supervisor(
        project_root=HYDRA_ROOT,
        dispatcher=dispatcher,
        force_pure_python=True,
    )
    state = HydraState(
        root_goal="ship the widget",
        selected_squads=list(squads),
        # WS1-E: engineering dispatch needs a resolved target. Supplied so the
        # planner does not surface a repo HITL before dispatch is reached.
        target_repo_id="hydra",
    )
    return runner.invoke(state)


def _trace_kinds(workflow_id) -> list[str]:
    path = trace_path(HYDRA_ROOT, workflow_id)
    if not path.exists():
        return []
    kinds: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            kinds.append(json.loads(line).get("kind", ""))
        except json.JSONDecodeError:
            continue
    return kinds


@pytest.fixture(autouse=True)
def _no_git_harvest(monkeypatch):
    monkeypatch.setattr("hydra_core.squad_node.harvest_pp_run_artifacts",
                        lambda **_k: None)


def test_attended_pass_defers_all_three_squads_and_judges_nothing():
    disp = _AttendedNullDispatcher()
    final = _run(["executive", "garland", "engineering"], disp)

    by_squad = {t.owner_squad: t.status for t in final.tasks}
    for slug in ("executive", "garland", "engineering"):
        assert by_squad.get(slug) == "deferred_to_host", (
            f"{slug} was not deferred; tasks={by_squad}"
        )

    kinds = _trace_kinds(final.workflow_id)
    assert kinds.count("dispatch.deferred_to_host") == 3, (
        f"expected three deferrals; trace kinds={kinds}"
    )
    # No fabricated verdicts, no fabricated Reflexion retry.
    assert "judge.verdict" not in kinds
    assert "judge.reflexion" not in kinds
    assert final.verdicts == []
    # No placeholder engineering DECISION_RECORD.
    assert [e for e in final.envelopes
            if e.get("origin_squad") == "engineering"] == []
    # pp was never touched — the host drives the real stage loop afterwards.
    assert ("pp_harness", "start_run") not in disp.calls
    # And the pass held at "executing" rather than running on to synthesis.
    assert final.phase == "executing"
    assert final.phase not in ("synthesis", "judge_synthesis", "done")


def test_attended_engineering_only_workflow_defers():
    disp = _AttendedNullDispatcher()
    final = _run(["engineering"], disp)

    eng = [t for t in final.tasks if t.owner_squad == "engineering"]
    assert eng, f"no engineering task planned; tasks={[t.owner_squad for t in final.tasks]}"
    assert all(t.status == "deferred_to_host" for t in eng)
    assert ("pp_harness", "start_run") not in disp.calls
    assert final.verdicts == []
    assert final.phase == "executing"


def test_offline_opt_in_still_runs_the_in_graph_mcp_path():
    """The offline skeleton path survives, but only behind the explicit flag —
    this is what the scripted unit-test dispatchers set."""
    disp = _AttendedNullDispatcher()
    disp.allow_offline_mcp_dispatch = True
    final = _run(["engineering"], disp)

    assert ("pp_harness", "start_run") in disp.calls
    eng = [t for t in final.tasks if t.owner_squad == "engineering"]
    assert eng and all(t.status != "deferred_to_host" for t in eng)
