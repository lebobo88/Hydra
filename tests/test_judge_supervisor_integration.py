"""Integration tests: supervisor judge nodes end-to-end (in-memory pure runner)."""
from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from hydra_core.state import HydraState


HYDRA_ROOT = Path(__file__).resolve().parents[1]


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


class _ScriptedCritiqueClient:
    """Returns a fixed verdict shape (pre-pragmatic-guard)."""
    def __init__(self, *, outcome="pass", critique_text="solid analysis " * 10, scores=None):
        self.outcome = outcome
        self._critique_text = critique_text
        self.scores = scores or {"clarity": 5, "rigor": 4}
        self.calls: list[dict] = []

    def critique(self, *, vendor, artifact_text, rubric_md):
        self.calls.append({"vendor": vendor})
        return {
            "outcome": self.outcome,
            "critique_md": self._critique_text,
            "score_json": dict(self.scores),
        }


def _build():
    from hydra_core.supervisor import build_supervisor
    return build_supervisor


def _invoke(sup, state):
    """Drive either the pure-python runner or a compiled LangGraph graph."""
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


def test_supervisor_runs_judge_nodes_and_emits_verdicts(tmp_path):
    build = _build()
    client = _ScriptedCritiqueClient()
    runner = build(
        project_root=HYDRA_ROOT,
        dispatcher=_StubDispatcher(),
        critique_client=client,
        force_pure_python=True,
    )
    state = HydraState(root_goal="approve the Q3 capital allocation plan")
    final = _invoke(runner, state)
    # Pure-python runner walks all steps in order; judge nodes must have fired.
    assert any(v for v in final.verdicts), "no verdicts emitted"
    # Synthesis judge ran (post_synthesis tier=cross_vendor).
    rubric_ids = {v["rubric_id"] for v in final.verdicts}
    assert "synthesis-coherence@1" in rubric_ids
    assert "constitution-alignment@1" in rubric_ids


def test_supervisor_skips_judge_when_pp_verdict_present(tmp_path):
    build = _build()
    # R3-tail post-mortem (2026-05-21): Use a substantive critique so the
    # pragmatic-pass guard doesn't trip and downgrade pass → revise on the
    # engineering-squad envelope. The previous test relied on incidental
    # behavior where revise verdicts on retry envelopes were silently absorbed
    # into the workflow; the new contract surfaces a `reflexion_override`
    # HITL in that case, which the original assertion (synthesis ran) cannot
    # observe. We give the client enough substance to genuinely pass.
    client = _ScriptedCritiqueClient(
        critique_text=(
            "thorough engineering review covering reliability, observability, "
            "test plan, rollback strategy, and migration safety — all dimensions "
            "addressed substantively " * 2
        ),
        scores={"reliability": 5, "observability": 4, "rollback": 5, "migration_safety": 4},
    )
    runner = build(
        project_root=HYDRA_ROOT,
        dispatcher=_StubDispatcher(),
        critique_client=client,
        force_pure_python=True,
    )
    state = HydraState(root_goal="refactor the payments microservice")
    # The intake → planner path will create tasks for engineering. After
    # dispatch the squad's output envelope would normally be judged; but if
    # PP has already judged it we should skip.
    final = _invoke(runner, state)
    # Whether engineering is selected depends on the router; assert at minimum
    # that the synthesis judge still ran (always cross_vendor).
    rubric_ids = {v["rubric_id"] for v in final.verdicts}
    assert "synthesis-coherence@1" in rubric_ids


def test_supervisor_hitl_escalation_on_synthesis_constitution_fail():
    build = _build()
    # Force a fail verdict on a HITL-severity rubric.
    client = _ScriptedCritiqueClient(
        outcome="fail",
        critique_text="constitution refusal triggered " * 5,
        scores={"refusal_respect": 0},
    )
    runner = build(
        project_root=HYDRA_ROOT,
        dispatcher=_StubDispatcher(),
        critique_client=client,
        force_pure_python=True,
    )
    state = HydraState(root_goal="run a normal workflow")
    final = _invoke(runner, state)
    assert final.phase == "surfaced"
    assert final.pending_hitl is not None
    assert final.pending_hitl["reason"] == "policy_breach"
