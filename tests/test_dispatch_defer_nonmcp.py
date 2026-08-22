"""Fix B — the live engine defers non-mcp squads to the host instead of running
headless best-of-N placeholders.

Regression for the campaign deadlock signature: executive (agent-impersonation)
and garland (claude-skill) were auto-dispatched through best-of-N in the
deterministic engine, which has no headless generator for them. The live
dispatcher only returns host_pickup_required, so every candidate was a
placeholder → judge.bon_all_pending + misleading RBAC/cost noise. The honest
behaviour (already used by ingest.py) is to defer non-mcp squads to the host and
still dispatch the engineering (mcp) leg.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from hydra_core.state import HydraState

HYDRA_ROOT = Path(__file__).resolve().parents[1]


def _happy_responses() -> dict[tuple[str, str], dict]:
    return {
        ("pp_harness", "start_run"): {"status": "done", "result": {"run_id": "run_B"}},
        ("pp_harness", "start_stage"): {"status": "done", "result": {"stage_id": "st_B"}},
        ("pp_codex", "generate"): {"status": "done", "result": {
            "text": "edited foo.py", "model": "codex-1",
            "tokens_in": 5, "tokens_out": 7, "cost_usd": 0.01, "wall_ms": 50}},
        ("pp_harness", "archive_artifact"): {"status": "done", "result": {"path": ".harness/x"}},
        ("pp_harness", "record_attempt"): {"status": "done", "result": {"attempt_id": "att_B"}},
        ("pp_agy", "critique"): {"status": "done", "result": {"parsed": {
            "outcome": "pass", "critique_md": "c" * 90, "score": {"correctness": 9}}}},
        ("pp_harness", "record_verdict"): {"status": "done", "result": {}},
        ("pp_harness", "finalize_stage"): {"status": "done", "result": {}},
        ("pp_harness", "finalize_run"): {"status": "done", "result": {"status": "complete"}},
    }


class _LiveScriptedDispatcher:
    """A *live* dispatcher (live_execution=True) — this is what trips Fix B."""
    live_execution = True

    def __init__(self, responses):
        self.responses = responses
        self.calls: list[tuple[str, str]] = []
        self.skill_calls: list[str] = []
        self.prompt_calls: list[str] = []

    def call_mcp(self, server, tool, args, *, squad_id=None):
        self.calls.append((server, tool))
        return self.responses.get((server, tool), {"status": "done", "result": {}})

    def spawn_subprocess(self, *_a, **_k):
        return {"status": "done", "returncode": 0}

    def emit_claude_prompt(self, prompt, agent=None):
        self.prompt_calls.append(agent or "")
        return {"status": "host_pickup_required"}

    def invoke_claude_skill(self, skill, args):
        self.skill_calls.append(skill)
        return {"status": "host_pickup_required"}

    def set_squad_packs(self, packs):
        pass


class _StubCritique:
    def critique(self, *, vendor, artifact_text, rubric_md):
        return {"outcome": "pass", "critique_md": "solid " * 30,
                "score_json": {"correctness": 9}}


@pytest.fixture(autouse=True)
def _no_git_harvest(monkeypatch):
    monkeypatch.setattr("hydra_core.squad_node.harvest_pp_run_artifacts",
                        lambda **_k: None)


def _invoke(sup, state):
    from hydra_core.supervisor import _PurePythonRunner
    if isinstance(sup, _PurePythonRunner):
        return sup.invoke(state)
    out = sup.invoke(state, config={"configurable": {"thread_id": str(state.workflow_id)}})
    return HydraState.model_validate(out) if isinstance(out, dict) else out


def test_live_engine_defers_nonmcp_and_dispatches_engineering():
    from hydra_core.supervisor import build_supervisor

    disp = _LiveScriptedDispatcher(_happy_responses())
    runner = build_supervisor(
        project_root=HYDRA_ROOT,
        dispatcher=disp,
        critique_client=_StubCritique(),
        force_pure_python=True,
    )
    # Force-select a claude-skill squad + the mcp engineering squad.
    # WS1-E: engineering dispatch requires an explicit, resolved target repo
    # -- this test's concern is nonmcp-defer-vs-engineering-dispatch, not
    # repo-targeting, so point at "hydra" (this checkout).
    state = HydraState(
        root_goal="build the feature",
        selected_squads=["garland", "engineering"],
        target_repo_id="hydra",
    )
    final = _invoke(runner, state)

    by_squad = {t.owner_squad: t for t in final.tasks}
    assert "garland" in by_squad, f"garland task missing; tasks={list(by_squad)}"
    # Non-mcp squad was deferred to the host, NOT executed headlessly.
    assert by_squad["garland"].status == "deferred_to_host"
    # The skill/impersonation stubs were never invoked by the engine.
    assert disp.skill_calls == []
    assert disp.prompt_calls == []
    # Engineering (mcp) WAS dispatched — the leg the engine can actually run.
    assert ("pp_harness", "start_run") in disp.calls


def test_stub_dispatcher_defers_native_pack_without_legacy_shim_call():
    """Native packs remain attended even under a non-live test dispatcher.

    Their Claude Code plugin agents require a host cursor, so Hydra must not
    fall back to the retired in-graph skill invocation merely because a test
    dispatcher lacks ``live_execution``.
    """
    from hydra_core.supervisor import build_supervisor

    class _StubDispatcher:
        def __init__(self):
            self.skill_calls: list[str] = []

        def call_mcp(self, server, tool, args, **_kw):
            return {"status": "done", "tool": tool, "result": {"ok": True}}
        def spawn_subprocess(self, *_a, **_k):
            return {"status": "done"}
        def emit_claude_prompt(self, prompt, agent=None):
            return {"status": "host_pickup_required", "agent": agent}
        def invoke_claude_skill(self, skill, args):
            self.skill_calls.append(skill)
            return {"status": "host_pickup_required", "skill": skill}

    disp = _StubDispatcher()
    runner = build_supervisor(
        project_root=HYDRA_ROOT,
        dispatcher=disp,
        critique_client=_StubCritique(),
        force_pure_python=True,
    )
    state = HydraState(root_goal="build the feature",
                       selected_squads=["garland"])
    final = _invoke(runner, state)
    by_squad = {t.owner_squad: t for t in final.tasks}
    assert by_squad.get("garland") is not None
    assert disp.skill_calls == []
    assert by_squad["garland"].status == "deferred_to_host"
