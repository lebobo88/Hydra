"""RA-5 regression test: stub squads must NOT be stranded as deferred_to_host
when dispatched with a live dispatcher (live_execution=True).

Fix B in node_dispatch defers non-mcp squads to the host when the dispatcher
has live_execution=True — this is correct for agent-impersonation and
claude-skill (which need a real host executor). It was WRONG for stub squads:
a stub has a real in-graph path (_stub) that produces a canned [STUB]
DecisionRecord with status='surfaced'. Deferring it to the host loses that
signal and strands the task as deferred_to_host indefinitely.

RA-5 excludes entrypoint='stub' from the live-defer pre-filter so the
in-graph _stub path executes and the DecisionRecord is produced.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from hydra_core.state import HydraState

HYDRA_ROOT = Path(__file__).resolve().parents[1]


class _LiveDispatcherWithStubSupport:
    """Minimal live dispatcher (live_execution=True) that can answer MCP calls
    for engineering tasks. The stub squad needs no MCP calls — _stub() produces
    its result in-process — but the engineering mcp leg is also wired in."""

    live_execution = True

    def __init__(self):
        self.calls: list[tuple[str, str]] = []
        self.skill_calls: list[str] = []
        self.deferred_to_host: list[str] = []

    def call_mcp(self, server: str, tool: str, args: Any,
                 *, squad_id: str | None = None) -> dict[str, Any]:
        self.calls.append((server, tool))
        return {"status": "done", "result": {"run_id": "stub_test_run"}}

    def spawn_subprocess(self, *_a, **_kw) -> dict[str, Any]:
        return {"status": "done", "returncode": 0}

    def emit_claude_prompt(self, prompt: str, agent: str | None = None) -> dict[str, Any]:
        return {"status": "host_pickup_required"}

    def invoke_claude_skill(self, skill: str, args: Any) -> dict[str, Any]:
        self.skill_calls.append(skill)
        return {"status": "host_pickup_required"}

    def set_squad_packs(self, packs: dict) -> None:
        pass


class _StubCritique:
    def critique(self, *, vendor, artifact_text, rubric_md):
        return {"outcome": "pass", "critique_md": "fine " * 30,
                "score_json": {"correctness": 9}}


@pytest.fixture(autouse=True)
def _no_git_harvest(monkeypatch):
    monkeypatch.setattr("hydra_core.squad_node.harvest_pp_run_artifacts",
                        lambda **_k: None)


def _invoke_supervisor(runner, state: HydraState) -> HydraState:
    from hydra_core.supervisor import _PurePythonRunner
    if isinstance(runner, _PurePythonRunner):
        return runner.invoke(state)
    out = runner.invoke(
        state, config={"configurable": {"thread_id": str(state.workflow_id)}}
    )
    return HydraState.model_validate(out) if isinstance(out, dict) else out


def test_stub_squad_surfaces_in_graph_with_live_dispatcher() -> None:
    """RA-5: with a live dispatcher, a stub squad must produce a [STUB]
    DecisionRecord (status='surfaced') — NOT be deferred to the host."""
    from hydra_core.supervisor import build_supervisor

    disp = _LiveDispatcherWithStubSupport()
    runner = build_supervisor(
        project_root=HYDRA_ROOT,
        dispatcher=disp,
        critique_client=_StubCritique(),
        force_pure_python=True,
    )

    # Force-select a stub squad (healthcare is stub in this worktree).
    state = HydraState(
        root_goal="stub surface test",
        selected_squads=["healthcare"],
    )
    final = _invoke_supervisor(runner, state)

    by_squad = {t.owner_squad: t for t in final.tasks}
    assert "healthcare" in by_squad, (
        f"healthcare task missing; tasks={list(by_squad)}"
    )
    healthcare_task = by_squad["healthcare"]
    # RA-5 fix: stub must surface in-graph, NOT strand as deferred_to_host.
    assert healthcare_task.status != "deferred_to_host", (
        "RA-5 regression: stub squad 'healthcare' was deferred_to_host with a "
        "live dispatcher — it should have run the in-graph _stub path (status='surfaced')"
    )
    assert healthcare_task.status == "surfaced", (
        f"Expected status='surfaced' for stub squad, got {healthcare_task.status!r}"
    )

    # No skill/prompt calls should have been made (stub is in-process).
    assert disp.skill_calls == [], "stub squad must not invoke any skill"

    # The synthesized envelopes should include a [STUB] DecisionRecord.
    stub_decisions = [
        e for e in final.envelopes
        if isinstance(e, dict) and "[STUB]" in e.get("decision", "")
    ]
    assert stub_decisions, (
        "Expected a [STUB] DecisionRecord in the final envelopes; "
        f"envelopes={[e.get('decision', e.get('type', '?'))[:60] for e in final.envelopes if isinstance(e, dict)]}"
    )
