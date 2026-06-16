"""Unit tests for the headless pp drive loop (no daemon, no LLM, no git).

Covers ``_drive_pp_stage_loop`` (the programmatic engineer→judge→finalize loop
the live CLI / fleet path runs because the pp daemon's ``start_run`` only
scaffolds) and its integration into ``_via_mcp``:

  - happy path drives start_stage → generate → archive → record_attempt →
    critique → record_verdict → finalize_stage(passed,winner) → finalize_run
  - a "revise" verdict triggers exactly one Reflexion retry, then surfaces
  - any exception finalizes the run as "aborted" (lock always released)
  - ``_via_mcp`` only drives when the dispatcher sets ``drive_pp_loop``; the
    driven DecisionRecord is marked ``pp_loop_judged`` and the open-run ledger
    is drained
  - without the flag, behavior is unchanged (status="running", not judged,
    run left registered for the daemon to finish)
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from hydra_core.schemas import DevTask
from hydra_core.squad_loader import discover_squads
from hydra_core.squad_node import _drive_pp_stage_loop, _via_mcp
from hydra_core.state import HydraState

HYDRA_ROOT = Path(__file__).resolve().parents[1]


def _happy_responses(outcome: str = "pass") -> dict[tuple[str, str], dict]:
    return {
        ("pp_harness", "start_run"): {"status": "done", "result": {"run_id": "run_T"}},
        ("pp_harness", "start_stage"): {"status": "done", "result": {"stage_id": "st_T"}},
        ("pp_codex", "generate"): {"status": "done", "result": {
            "text": "edited foo.py", "model": "codex-1",
            "tokens_in": 5, "tokens_out": 7, "cost_usd": 0.02, "wall_ms": 100}},
        ("pp_harness", "archive_artifact"): {"status": "done", "result": {"path": ".harness/x"}},
        ("pp_harness", "record_attempt"): {"status": "done", "result": {"attempt_id": "att_T"}},
        ("pp_gemini", "critique"): {"status": "done", "result": {"parsed": {
            "outcome": outcome, "critique_md": "c" * 90, "score": {"correctness": 9}}}},
        ("pp_harness", "record_verdict"): {"status": "done", "result": {}},
        ("pp_harness", "finalize_stage"): {"status": "done", "result": {}},
        ("pp_harness", "finalize_run"): {"status": "done", "result": {"status": "complete"}},
    }


class _ScriptedDispatcher:
    """Returns canned responses keyed by (server, tool); records every call.

    ``raise_on`` maps (server, tool) → exception to raise. ``drive_pp_loop`` is
    set so ``_via_mcp`` engages the drive path.
    """

    def __init__(self, responses: dict[tuple[str, str], dict],
                 *, raise_on: set[tuple[str, str]] | None = None,
                 drive: bool = True) -> None:
        self.responses = responses
        self.raise_on = raise_on or set()
        self.calls: list[tuple[str, str, dict]] = []
        if drive:
            self.drive_pp_loop = True

    def call_mcp(self, server: str, tool: str, args: dict[str, Any],
                 *, squad_id: str | None = None) -> dict[str, Any]:
        self.calls.append((server, tool, args))
        if (server, tool) in self.raise_on:
            raise RuntimeError(f"boom on {server}.{tool}")
        return self.responses.get((server, tool), {"status": "done", "result": {}})

    def tool_seq(self) -> list[str]:
        return [t for (_s, t, _a) in self.calls]

    def emit_claude_prompt(self, *_a, **_k): raise NotImplementedError  # pragma: no cover
    def invoke_claude_skill(self, *_a, **_k): raise NotImplementedError  # pragma: no cover
    def spawn_subprocess(self, *_a, **_k): raise NotImplementedError  # pragma: no cover


# --------------------------------------------------------------------------- #
# _drive_pp_stage_loop
# --------------------------------------------------------------------------- #

def test_happy_path_drives_full_loop() -> None:
    disp = _ScriptedDispatcher(_happy_responses("pass"))
    out = _drive_pp_stage_loop(
        disp, run_id="run_T", project_path="/tmp/proj", request_text="do the thing")

    assert out["final_status"] == "complete"
    assert out["stage_outcome"] == "pass"
    assert out["attempt_id"] == "att_T"
    assert out["finalized"] is True

    seq = disp.tool_seq()
    # generate precedes its archive/record; critique precedes record_verdict;
    # finalize_stage precedes finalize_run.
    assert seq.index("generate") < seq.index("record_attempt")
    assert seq.index("critique") < seq.index("record_verdict")
    assert seq.index("finalize_stage") < seq.index("finalize_run")

    # finalize_stage was "passed" with the winning attempt id.
    fs = next(a for (s, t, a) in disp.calls if t == "finalize_stage")
    assert fs["status"] == "passed"
    assert fs["winner_attempt_id"] == "att_T"

    # cross-vendor: codex generated, gemini judged.
    assert ("pp_codex", "generate") in {(s, t) for (s, t, _a) in disp.calls}
    assert ("pp_gemini", "critique") in {(s, t) for (s, t, _a) in disp.calls}


def test_revise_triggers_one_reflexion_then_surfaces() -> None:
    disp = _ScriptedDispatcher(_happy_responses("revise"))
    out = _drive_pp_stage_loop(
        disp, run_id="run_T", project_path="/tmp/proj", request_text="do it")

    assert out["stage_outcome"] == "revise"
    assert out["final_status"] == "surfaced"
    # Reflexion ×1 → exactly two generate attempts, no more.
    assert disp.tool_seq().count("generate") == 2
    fs = next(a for (s, t, a) in disp.calls if t == "finalize_stage")
    assert fs["status"] == "surfaced"
    assert "winner_attempt_id" not in fs
    fr = next(a for (s, t, a) in disp.calls if t == "finalize_run")
    assert fr["status"] == "surfaced"


def test_exception_finalizes_aborted_and_releases_lock() -> None:
    disp = _ScriptedDispatcher(
        _happy_responses("pass"), raise_on={("pp_harness", "start_stage")})
    out = _drive_pp_stage_loop(
        disp, run_id="run_T", project_path="/tmp/proj", request_text="do it")

    assert out["final_status"] == "aborted"
    assert out["error"] is not None
    assert out["finalized"] is True
    # The lock-releasing finalize_run(aborted) must have fired.
    fr = next(a for (s, t, a) in disp.calls if t == "finalize_run")
    assert fr["status"] == "aborted"


def test_finalize_run_gate_downgrade_is_reflected() -> None:
    """Even on a passing stage, a finalize_run that returns a non-complete
    status (server-side gate downgrade) must surface, not falsely report done."""
    resp = _happy_responses("pass")
    resp[("pp_harness", "finalize_run")] = {"status": "done", "result": {"status": "surfaced"}}
    disp = _ScriptedDispatcher(resp)
    out = _drive_pp_stage_loop(
        disp, run_id="run_T", project_path="/tmp/proj", request_text="do it")
    assert out["final_status"] == "surfaced"


# --------------------------------------------------------------------------- #
# _via_mcp integration
# --------------------------------------------------------------------------- #

def _eng_pack():
    pack = discover_squads(HYDRA_ROOT)["engineering"]
    return pack


def _inbound(state: HydraState) -> DevTask:
    return DevTask(
        workflow_id=state.workflow_id, origin_squad="hydra",
        owner="backend", repo="hydra", branch="wf",
        instructions="add a NOTES.md file",
    )


def test_via_mcp_drive_marks_judged_and_drains_run(monkeypatch) -> None:
    # Don't touch git in the harvest step.
    monkeypatch.setattr("hydra_core.squad_node.harvest_pp_run_artifacts",
                        lambda **_k: None)
    state = HydraState(root_goal="t")
    pack = _eng_pack()
    disp = _ScriptedDispatcher(_happy_responses("pass"), drive=True)

    result = _via_mcp(state, pack, _inbound(state), disp)

    assert result.status == "done"
    assert result.pp_loop_judged is True
    # The driven run was finalized → drained from the open-run ledger.
    assert all(e.get("run_id") != "run_T" for e in state.open_pp_runs)
    # The loop actually ran (generate happened).
    assert "generate" in disp.tool_seq()


def test_via_mcp_without_drive_flag_is_unchanged(monkeypatch) -> None:
    monkeypatch.setattr("hydra_core.squad_node.harvest_pp_run_artifacts",
                        lambda **_k: None)
    state = HydraState(root_goal="t")
    pack = _eng_pack()
    # drive=False → no drive_pp_loop attribute → legacy scaffold-only behavior.
    disp = _ScriptedDispatcher(_happy_responses("pass"), drive=False)

    result = _via_mcp(state, pack, _inbound(state), disp)

    assert result.status == "running"
    assert result.pp_loop_judged is False
    # No drive loop ran — only start_run was called.
    assert disp.tool_seq() == ["start_run"]
    # The run stays registered for the daemon / abort path to finalize.
    assert any(e.get("run_id") == "run_T" for e in state.open_pp_runs)
