"""Unit tests for the headless pp drive loop (no daemon, no LLM, no git).

Covers ``_drive_pp_stage_loop`` (the programmatic engineer→judge→finalize loop
the live CLI / fleet path runs because the pp daemon's ``start_run`` only
scaffolds) and its integration into ``_via_mcp``:

  - happy path drives start_stage → generate → archive → record_attempt →
    critique → record_verdict → finalize_stage(passed,winner) → finalize_run
  - a "revise" verdict triggers exactly one Reflexion retry, then surfaces
  - any exception finalizes the run as "aborted" (lock always released)
  - ``_via_mcp`` only drives when the dispatcher sets ``drive_pp_loop``; the
    driven DecisionRecord is marked ``pp_loop_terminal`` and the open-run ledger
    is drained
  - without the flag, behavior is unchanged (status="running", not judged,
    run left registered for the daemon to finish)
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest

from hydra_core.schemas import DevTask
from hydra_core.squad_loader import discover_squads
from hydra_core.squad_node import _drive_pp_stage_loop, _via_mcp
from hydra_core.state import HydraState

HYDRA_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _no_real_repo_scaffolding(monkeypatch: pytest.MonkeyPatch) -> None:
    """The _via_mcp integration tests below use _eng_pack() == the real
    engineering squad discovered against HYDRA_ROOT, and dispatch with
    run_id + status="done" -- stub the target-repo scaffolding helpers
    (.gitignore / test-runner exclude patching) so they don't write into the
    real checkout. They are exercised hermetically against tmp_path in
    tests/test_target_repo_scaffolding.py."""
    monkeypatch.setattr("hydra_core.squad_node.ensure_target_repo_ignores",
                        lambda _project_path: None)
    monkeypatch.setattr("hydra_core.squad_node.ensure_target_repo_test_excludes",
                        lambda _project_path: None)


def _happy_responses(outcome: str = "pass") -> dict[tuple[str, str], dict]:
    return {
        ("pp_harness", "start_run"): {"status": "done", "result": {"run_id": "run_T"}},
        ("pp_harness", "start_stage"): {"status": "done", "result": {"stage_id": "st_T"}},
        # F31: required_cross_vendor=False so same-vendor codex→codex doesn't
        # downgrade here (these tests exercise drive-loop mechanics, not the
        # cross-vendor enforcement gate).
        ("pp_harness", "gate_eligible_judges"): {"status": "done", "result": {
            "required_cross_vendor": False, "rubric_id": "rfc-2119-normative"}},
        ("pp_codex", "generate"): {"status": "done", "result": {
            "text": "edited foo.py\n{\"status\": \"pass\", \"reason\": \"pytest -q -> exit 0\"}",
            "model": "codex-1",
            "tokens_in": 5, "tokens_out": 7, "cost_usd": 0.02, "wall_ms": 100}},
        ("pp_harness", "archive_artifact"): {"status": "done", "result": {"path": ".harness/x"}},
        ("pp_harness", "record_attempt"): {"status": "done", "result": {"attempt_id": "att_T"}},
        ("pp_codex", "critique"): {"status": "done", "result": {"parsed": {
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

def test_happy_path_drives_full_loop(monkeypatch) -> None:
    # Smoke now runs host-side (outside the codex sandbox); stub it to a pass so
    # this wiring test stays deterministic. _run_smoke has its own unit tests.
    monkeypatch.setattr("hydra_core.squad_node._run_smoke",
                        lambda *_a, **_k: ("pass", "stub smoke pass"))
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

    # Producer fell back to codex (no host engineer in the scripted dispatcher);
    # codex judges (agy opt-out via PP_DISABLE_AGY). Same-vendor here, marked degraded — see the
    # _cross_vendor flag wired into the recorded verdict's score_json.
    assert ("pp_codex", "generate") in {(s, t) for (s, t, _a) in disp.calls}
    assert ("pp_codex", "critique") in {(s, t) for (s, t, _a) in disp.calls}
    assert ("pp_agy", "critique") not in {(s, t) for (s, t, _a) in disp.calls}


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
        # WS1-E: engineering dispatch requires an explicit, resolved target
        # repo -- these tests exercise the pp drive loop itself, not
        # repo-targeting, so point at "hydra" (this checkout) rather than
        # relying on the removed cwd fallback.
        target_repo_id="hydra",
    )


def test_via_mcp_drive_marks_judged_and_drains_run(monkeypatch) -> None:
    # Don't touch git in the harvest step.
    monkeypatch.setattr("hydra_core.squad_node.harvest_pp_run_artifacts",
                        lambda **_k: None)
    # Host-side smoke stubbed to a pass (its own unit tests cover the mechanism).
    monkeypatch.setattr("hydra_core.squad_node._run_smoke",
                        lambda *_a, **_k: ("pass", "stub smoke pass"))
    state = HydraState(root_goal="t")
    pack = _eng_pack()
    disp = _ScriptedDispatcher(_happy_responses("pass"), drive=True)

    result = _via_mcp(state, pack, _inbound(state), disp)

    assert result.status == "done"
    assert result.pp_loop_terminal is True
    # The driven run was finalized → drained from the open-run ledger.
    assert all(e.get("run_id") != "run_T" for e in state.open_pp_runs)
    # The repo contract bootstrap runs before the driven stage loop.
    assert disp.tool_seq()[:2] == ["start_run", "ensure_agents_md"]
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
    assert result.pp_loop_terminal is False
    # No drive loop ran — only the scaffold + AGENTS/CLAUDE bootstrap happened.
    assert disp.tool_seq() == ["start_run", "ensure_agents_md"]
    # The run stays registered for the daemon / abort path to finalize.
    assert any(e.get("run_id") == "run_T" for e in state.open_pp_runs)


# --------------------------------------------------------------------------- #
# Fix 1: generate-failure detection   |   Fix 3: PP-VG-5 real-smoke close
# --------------------------------------------------------------------------- #

def test_generate_failure_surfaces_reason_and_skips_judge() -> None:
    """A read-only / quota / timeout generate is surfaced with its TRUE reason,
    not masked as an empty 'revise' attempt; no critique, no Reflexion retry."""
    resp = _happy_responses("pass")
    resp[("pp_codex", "generate")] = {"status": "done", "result": {
        "text": "writing is blocked by read-only sandbox; rejected by user approval settings"}}
    disp = _ScriptedDispatcher(resp)
    out = _drive_pp_stage_loop(
        disp, run_id="run_T", project_path="/tmp/proj", request_text="do it")

    assert out["final_status"] == "surfaced"
    assert out["stage_outcome"] == "error"
    assert "read-only" in (out["error"] or "")
    seq = disp.tool_seq()
    # No Reflexion retry on an unfixable failure, and no judge on empty output.
    assert seq.count("generate") == 1
    assert "critique" not in seq
    assert "record_verdict" not in seq
    # The failed attempt is recorded honestly (status error/timeout, not "ok").
    ra = next(a for (s, t, a) in disp.calls if t == "record_attempt")
    assert ra["status"] in {"error", "timeout"}
    # The run still closes (lock released) and surfaces the reason.
    fr = next(a for (s, t, a) in disp.calls if t == "finalize_run")
    assert fr["status"] == "surfaced"
    assert "read-only" in fr["summary_md"]


def test_timeout_generate_is_detected() -> None:
    """A dispatcher-level timeout payload is surfaced, not judged as 'revise'."""
    resp = _happy_responses("pass")
    resp[("pp_codex", "generate")] = {
        "status": "failed", "timeout": True,
        "error": "pp_codex.generate timed out after 1800s"}
    disp = _ScriptedDispatcher(resp)
    out = _drive_pp_stage_loop(
        disp, run_id="run_T", project_path="/tmp/proj", request_text="do it")
    assert out["final_status"] == "surfaced"
    assert "timed out" in (out["error"] or "")
    ra = next(a for (s, t, a) in disp.calls if t == "record_attempt")
    assert ra["status"] == "timeout"


def test_pass_records_real_smoke_and_completes(monkeypatch) -> None:
    """On a passing verdict the loop runs an independent smoke, records it tied
    to candidate_index=1, and only then finalizes complete (PP-VG-5)."""
    monkeypatch.setattr("hydra_core.squad_node._run_smoke",
                        lambda *_a, **_k: ("pass", "`pytest -q` exit=0"))
    disp = _ScriptedDispatcher(_happy_responses("pass"))
    out = _drive_pp_stage_loop(
        disp, run_id="run_T", project_path="/tmp/proj", request_text="do it")

    assert out["final_status"] == "complete"
    assert out["smoke_status"] == "pass"
    # The winning attempt carries candidate_index=1 (VG-5 prerequisite).
    ra = next(a for (s, t, a) in disp.calls
              if t == "record_attempt" and a.get("status") == "ok")
    assert ra["notes"]["candidate_index"] == 1
    # An independent smoke result is recorded for that candidate slot.
    rs = next(a for (s, t, a) in disp.calls if t == "record_smoke_status")
    assert rs["candidate_index"] == 1
    assert rs["status"] == "pass"
    # smoke is recorded BEFORE finalize_stage so the gate can read it.
    seq = disp.tool_seq()
    assert seq.index("record_smoke_status") < seq.index("finalize_stage")
    fs = next(a for (s, t, a) in disp.calls if t == "finalize_stage")
    assert fs["status"] == "passed"
    assert fs["winner_attempt_id"] == "att_T"


def test_pass_verdict_but_failing_smoke_surfaces(monkeypatch) -> None:
    """A passing judge verdict with a FAILING smoke must NOT finalize complete —
    the anti-gaming gate keeps it surfaced (no forged pass)."""
    # Host-side smoke returns a real failure (non-zero exit).
    monkeypatch.setattr("hydra_core.squad_node._run_smoke",
                        lambda *_a, **_k: ("fail", "`pytest -q` exit=1 :: 1 failed"))
    resp = _happy_responses("pass")
    resp[("pp_codex", "generate")] = {"status": "done", "result": {
        "text": "edited foo.py", "model": "codex-1"}}
    disp = _ScriptedDispatcher(resp)
    out = _drive_pp_stage_loop(
        disp, run_id="run_T", project_path="/tmp/proj", request_text="do it")

    assert out["smoke_status"] == "fail"
    assert out["final_status"] == "surfaced"
    rs = next(a for (s, t, a) in disp.calls if t == "record_smoke_status")
    assert rs["status"] == "fail"
    fs = next(a for (s, t, a) in disp.calls if t == "finalize_stage")
    assert fs["status"] == "surfaced"
    assert "winner_attempt_id" not in fs


# --------------------------------------------------------------------------- #
# Host-driven Claude generation seam (Phase 2 re-vendor)
# --------------------------------------------------------------------------- #
class _HostDispatcher(_ScriptedDispatcher):
    """Scripted dispatcher that ALSO provides a host `engineer` executor."""

    def __init__(self, responses, host_result, **kw):
        super().__init__(responses, **kw)
        self.host_result = host_result
        self.host_calls: list[tuple[str, str | None]] = []

    def run_host_agent(self, agent_type, prompt, *, cwd=None, timeout_s=None):
        self.host_calls.append((agent_type, cwd))
        return self.host_result


def test_drive_generate_prefers_host_engineer_and_codex_is_cross_vendor(monkeypatch) -> None:
    monkeypatch.setattr("hydra_core.squad_node._run_smoke",
                        lambda *_a, **_k: ("pass", "stub smoke pass"))
    host = {"result": {
        "text": "edited foo.py\n{\"status\": \"pass\", \"reason\": \"ok\"}",
        "model": "claude-x", "cost_usd": 0.05, "tokens_in": 3, "tokens_out": 4}}
    # F31: override gate to required_cross_vendor=True so codex is genuinely
    # cross-vendor (claude generated, codex judged — not degraded).
    resp = _happy_responses("pass")
    resp[("pp_harness", "gate_eligible_judges")] = {"status": "done", "result": {
        "required_cross_vendor": True, "rubric_id": "rfc-2119-normative"}}
    disp = _HostDispatcher(resp, host)

    out = _drive_pp_stage_loop(
        disp, run_id="run_T", project_path="/tmp/proj", request_text="do it")

    # Host engineer produced the code → producer=claude, codex NOT used to generate.
    assert out.get("producer") == "claude"
    assert disp.host_calls and disp.host_calls[0][0] == "engineer"
    assert ("pp_codex", "generate") not in {(s, t) for (s, t, _a) in disp.calls}
    # Codex judged → genuine cross-vendor (claude generated, codex judged).
    assert ("pp_codex", "critique") in {(s, t) for (s, t, _a) in disp.calls}
    rv = next(a for (s, t, a) in disp.calls if t == "record_verdict")
    assert rv["judge_producer"] == "codex"
    assert rv["score_json"].get("_cross_vendor") is True
    # Host generate cost flows into the budget charge.
    assert out["cost_usd"] >= 0.05


def test_drive_generate_falls_back_to_codex_when_no_host(monkeypatch) -> None:
    monkeypatch.setattr("hydra_core.squad_node._run_smoke",
                        lambda *_a, **_k: ("pass", "stub smoke pass"))
    # B4: require cross-vendor. codex generated → the authoritative
    # cross-vendor mapping (`_judge_vendor_chain`) now picks agy as a
    # GENUINE cross-vendor judge (not a same-vendor codex self-judge
    # fallback, which was the only option before agy was wired in).
    resp = _happy_responses("pass")
    resp[("pp_harness", "gate_eligible_judges")] = {"status": "done", "result": {
        "required_cross_vendor": True, "rubric_id": "rfc-2119-normative"}}
    resp[("pp_agy", "critique")] = {"status": "done", "result": {"parsed": {
        "outcome": "pass", "critique_md": "c" * 90, "score": {"correctness": 9}}}}
    disp = _ScriptedDispatcher(resp)  # no run_host_agent

    _drive_pp_stage_loop(
        disp, run_id="run_T", project_path="/tmp/proj", request_text="do it")

    # No host → codex generated → genuine cross-vendor agy judge, not degraded.
    assert any(True for (s, t, _a) in disp.calls if (s, t) == ("pp_codex", "generate"))
    assert any(True for (s, t, _a) in disp.calls if (s, t) == ("pp_agy", "critique"))
    assert not any(True for (s, t, _a) in disp.calls if (s, t) == ("pp_codex", "critique"))
    rv = next(a for (s, t, a) in disp.calls if t == "record_verdict")
    assert rv["score_json"].get("_cross_vendor") is True
    assert not rv["score_json"].get("_judge_degraded")


def test_cost_accumulated_even_when_generate_fails() -> None:
    """COST_LOSS fix: a soft-block generate that wrote nothing but consumed
    tokens/cost must STILL charge the budget ledger (cost is accumulated before
    the gen_fail branch, not only on the success path)."""
    resp = _happy_responses("pass")
    resp[("pp_codex", "generate")] = {"status": "done", "result": {
        "text": "writing is blocked by read-only sandbox; rejected by user approval settings",
        "model": "codex-1", "tokens_in": 5, "tokens_out": 3, "cost_usd": 0.03}}
    disp = _ScriptedDispatcher(resp)
    out = _drive_pp_stage_loop(
        disp, run_id="run_T", project_path="/tmp/proj", request_text="x")
    assert out["final_status"] == "surfaced"   # generate failed -> surfaced
    assert out["stage_outcome"] == "error"
    assert out["cost_usd"] >= 0.03             # cost STILL charged (the fix)
    assert out["tokens_in"] >= 5


def test_drive_loop_emits_trace_events_when_workflow_id_set(monkeypatch) -> None:
    """F11-trace: with a workflow_id wired, the driven loop emits per-attempt
    judge.verdict + a terminal stage_outcome_set (it bypasses the supervisor
    judge plane, so these were previously invisible in the hydra trace)."""
    monkeypatch.setattr("hydra_core.squad_node._run_smoke",
                        lambda *_a, **_k: ("pass", "stub smoke pass"))
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr("hydra_core.telemetry.emit",
                        lambda _root, _wf, kind, payload: events.append((kind, payload)))
    disp = _ScriptedDispatcher(_happy_responses("pass"))

    _drive_pp_stage_loop(
        disp, run_id="run_T", project_path="/tmp/proj", request_text="x",
        workflow_id="wf-123")

    kinds = [k for (k, _p) in events]
    assert "judge.verdict" in kinds
    assert "stage_outcome_set" in kinds
    # The verdict event carries producer + cross_vendor for audit.
    jv = next(p for (k, p) in events if k == "judge.verdict")
    assert jv["judge_producer"] == "codex"
    assert "cross_vendor" in jv


def test_drive_loop_no_trace_without_workflow_id(monkeypatch) -> None:
    monkeypatch.setattr("hydra_core.squad_node._run_smoke",
                        lambda *_a, **_k: ("pass", "stub smoke pass"))
    events: list = []
    monkeypatch.setattr("hydra_core.telemetry.emit",
                        lambda *_a, **_k: events.append(1))
    disp = _ScriptedDispatcher(_happy_responses("pass"))
    _drive_pp_stage_loop(disp, run_id="run_T", project_path="/tmp/proj", request_text="x")
    assert events == []   # no workflow_id -> no trace writes


# --------------------------------------------------------------------------- #
# Phase 1: Claude is the generator whenever a Claude capability is attached;
# codex is reserved for cross-vendor judging (never generation) in that case.
# --------------------------------------------------------------------------- #
from hydra_core.squad_node import (  # noqa: E402
    _claude_cli_generation_enabled,
    _drive_generate,
    _judge_artifact_text,
    _parse_claude_cli_result,
    _pp_gate_type,
)


class _LiveDispatcher(_ScriptedDispatcher):
    """Scripted dispatcher that reports a real live-execution capability
    (mirrors MCPStdioDispatcher.live_execution=True)."""

    def __init__(self, responses, **kw):
        super().__init__(responses, **kw)
        self.live_execution = True


def test_claude_cli_gate_precedence(monkeypatch) -> None:
    live = _LiveDispatcher(_happy_responses())
    testd = _ScriptedDispatcher(_happy_responses())  # no live_execution

    # Disable flag beats everything (true-headless CI where codex is the only gen).
    monkeypatch.setenv("HYDRA_DISABLE_CLAUDE_ENGINEER", "1")
    monkeypatch.setenv("HYDRA_CLAUDE_ENGINEER", "1")
    assert _claude_cli_generation_enabled(live) is False
    monkeypatch.delenv("HYDRA_DISABLE_CLAUDE_ENGINEER", raising=False)

    # Explicit force-on works regardless of live/claude detection.
    assert _claude_cli_generation_enabled(testd) is True
    monkeypatch.delenv("HYDRA_CLAUDE_ENGINEER", raising=False)

    # Auto: requires BOTH a live dispatcher AND `claude` on PATH.
    monkeypatch.setattr("hydra_core.squad_node.shutil.which",
                        lambda _n: "/usr/bin/claude")
    assert _claude_cli_generation_enabled(live) is True
    assert _claude_cli_generation_enabled(testd) is False   # not a live dispatcher
    monkeypatch.setattr("hydra_core.squad_node.shutil.which", lambda _n: None)
    assert _claude_cli_generation_enabled(live) is False     # claude absent


def test_drive_generate_uses_claude_cli_when_enabled(monkeypatch) -> None:
    disp = _ScriptedDispatcher(_happy_responses())
    monkeypatch.setattr("hydra_core.squad_node._claude_cli_generation_enabled",
                        lambda _d: True)
    monkeypatch.setattr(
        "hydra_core.squad_node._run_claude_cli",
        lambda prompt, *, cwd: {
            "text": "edited foo.py", "model": "claude-opus-4-8",
            "cost_usd": 0.07, "tokens_in": 9, "tokens_out": 11, "status": "done"})

    gen, producer = _drive_generate(
        disp, prompt="p", project_path="/tmp/p", model_tier=None, sq="engineering")

    assert producer == "claude"
    inner = gen["result"]
    assert inner["cost_usd"] == 0.07 and inner["model"] == "claude-opus-4-8"
    # Generation NEVER touched codex.
    assert ("pp_codex", "generate") not in {(s, t) for (s, t, _a) in disp.calls}


def test_drive_generate_host_none_falls_through_to_claude_cli(monkeypatch) -> None:
    """A host capability that is present but returns None (host bridge not ready)
    must fall through to the Claude CLI when available — NEVER to codex."""
    host = _HostDispatcher(_happy_responses(), host_result=None)
    monkeypatch.setattr("hydra_core.squad_node._claude_cli_generation_enabled",
                        lambda _d: True)
    monkeypatch.setattr(
        "hydra_core.squad_node._run_claude_cli",
        lambda prompt, *, cwd: {"text": "edited", "model": "claude-x",
                                "cost_usd": 0.01, "status": "done"})

    gen, producer = _drive_generate(
        host, prompt="p", project_path="/tmp/p", model_tier=None, sq="engineering")

    assert producer == "claude"
    assert host.host_calls            # the host engineer was attempted first
    assert ("pp_codex", "generate") not in {(s, t) for (s, t, _a) in host.calls}


def test_drive_generate_codex_only_when_no_claude(monkeypatch) -> None:
    disp = _ScriptedDispatcher(_happy_responses())
    monkeypatch.setattr("hydra_core.squad_node._claude_cli_generation_enabled",
                        lambda _d: False)
    gen, producer = _drive_generate(
        disp, prompt="p", project_path="/tmp/p", model_tier=None, sq="engineering")
    assert producer == "codex"
    assert ("pp_codex", "generate") in {(s, t) for (s, t, _a) in disp.calls}


def test_parse_claude_cli_result_extracts_cost_and_degrades() -> None:
    j = json.dumps({"result": "did it", "total_cost_usd": 0.12,
                    "usage": {"input_tokens": 100, "output_tokens": 50},
                    "model": "claude-opus-4-8"})
    out = _parse_claude_cli_result(j, "", 0, "fallback-model")
    assert out["text"] == "did it"
    assert out["cost_usd"] == 0.12
    assert out["tokens_in"] == 100 and out["tokens_out"] == 50
    assert out["model"] == "claude-opus-4-8"
    assert out["status"] == "done"

    # Non-JSON stdout degrades to raw text (cost 0 — budget blind) without crashing.
    deg = _parse_claude_cli_result("plain text out", "", 0, "m")
    assert deg["text"] == "plain text out" and deg["cost_usd"] == 0.0

    # Non-zero exit appends the stderr tail and marks the attempt errored.
    err = _parse_claude_cli_result("", "boom", 1, "m")
    assert err["status"] == "error" and "boom" in err["text"]


# --------------------------------------------------------------------------- #
# Phase 2 (contract-safe): judge routing follows pp's gate_eligible_judges
# (cross-vendor codex vs same-vendor Claude), and the judge reads the real diff.
# --------------------------------------------------------------------------- #
def _claude_gen(monkeypatch) -> None:
    """Force Claude generation (no real subprocess) for a drive-loop test."""
    monkeypatch.setattr("hydra_core.squad_node._run_smoke",
                        lambda *_a, **_k: ("pass", "stub smoke pass"))
    monkeypatch.setattr("hydra_core.squad_node._claude_cli_generation_enabled",
                        lambda _d: True)
    monkeypatch.setattr(
        "hydra_core.squad_node._run_claude_cli",
        lambda prompt, *, cwd: {
            "text": "edited foo.py\n{\"status\": \"pass\", \"reason\": \"ok\"}",
            "model": "claude-opus-4-8", "cost_usd": 0.02, "status": "done"})


def test_judge_same_vendor_claude_when_gate_allows(monkeypatch) -> None:
    """Claude generated + pp gate says same-vendor → a Claude critique judges it
    and codex is NOT spent. (This is the common path that conserves codex.)"""
    _claude_gen(monkeypatch)
    monkeypatch.setattr(
        "hydra_core.squad_node._claude_critique",
        lambda text, rubric, cwd: {
            "parsed": {"outcome": "pass", "critique_md": "c" * 90, "score": {}},
            "model": "claude-sonnet-4-6", "cost_usd": 0.01})
    resp = _happy_responses("pass")
    resp[("pp_harness", "gate_eligible_judges")] = {"status": "done", "result": {
        "required_cross_vendor": False, "rubric_id": "rfc-2119-normative"}}
    disp = _LiveDispatcher(resp)

    out = _drive_pp_stage_loop(
        disp, run_id="run_T", project_path="/tmp/proj", request_text="do it")

    assert out["producer"] == "claude"
    rv = next(a for (s, t, a) in disp.calls if t == "record_verdict")
    assert rv["judge_producer"] == "claude"
    assert rv["score_json"].get("_cross_vendor") is False
    assert rv["score_json"].get("_judge_tier") == "same_vendor"
    assert "_judge_degraded" not in rv["score_json"]   # same-vendor by rule != degraded
    assert rv["rubric_id"] == "rfc-2119-normative"
    # codex was NOT used as a judge.
    assert ("pp_codex", "critique") not in {(s, t) for (s, t, _a) in disp.calls}


def test_judge_cross_vendor_codex_when_gate_requires(monkeypatch) -> None:
    """Claude generated + pp gate REQUIRES cross-vendor → codex judges it as a
    genuine cross-vendor judge, using the gate's rubric."""
    _claude_gen(monkeypatch)
    resp = _happy_responses("pass")
    resp[("pp_harness", "gate_eligible_judges")] = {"status": "done", "result": {
        "required_cross_vendor": True, "rubric_id": "owasp-asvs-l1"}}
    disp = _LiveDispatcher(resp)

    out = _drive_pp_stage_loop(
        disp, run_id="run_T", project_path="/tmp/proj", request_text="do it")

    assert out["producer"] == "claude"
    rv = next(a for (s, t, a) in disp.calls if t == "record_verdict")
    assert rv["judge_producer"] == "codex"
    assert rv["score_json"].get("_cross_vendor") is True
    assert rv["score_json"].get("_judge_tier") == "cross_vendor"
    assert rv["rubric_id"] == "owasp-asvs-l1"
    assert ("pp_codex", "critique") in {(s, t) for (s, t, _a) in disp.calls}
    # The driver consulted pp's gate policy before judging.
    assert ("pp_harness", "gate_eligible_judges") in {(s, t) for (s, t, _a) in disp.calls}


def test_pp_gate_type_mapping() -> None:
    assert _pp_gate_type("code", "code") == "code_style"      # invalid -> mapped
    assert _pp_gate_type("code", "code_style") == "code_style"  # already valid -> kept
    assert _pp_gate_type("spec", None) == "spec"
    assert _pp_gate_type("tests", "weird") == "lint_class"
    assert _pp_gate_type("unknown-kind", None) == "code_style"  # safe default


def test_judge_artifact_text_falls_back_to_summary() -> None:
    # No changed paths → just the summary (never crashes).
    assert _judge_artifact_text("/nonexistent", [], "summary text") == "summary text"
    # Non-repo path → git diff fails → falls back to the summary.
    out = _judge_artifact_text("/nonexistent", ["foo.py"], "the summary")
    assert out == "the summary"


# ─── B7: diff-truncation marker on _judge_artifact_text ────────────────────

def _judge_git(root, *args):
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=root, capture_output=True, text=True, check=False,
    )


def _judge_init_repo_with_modified_file(root, name="big.py", lines=4000):
    """A committed repo with ONE file whose working-tree edit produces a
    multi-KB unified diff (well over any small max_chars test cap)."""
    (root / name).write_text("x = 1\n", encoding="utf-8")
    _judge_git(root, "init", "-q")
    _judge_git(root, "add", "-A")
    _judge_git(root, "commit", "-q", "-m", "init")
    (root / name).write_text("x = 1\n" + ("y = 2\n" * lines), encoding="utf-8")


def test_judge_artifact_text_under_cap_has_no_truncation_marker(tmp_path) -> None:
    """(a) A diff comfortably under max_chars is returned verbatim — no
    truncation marker, and it contains the real diff content."""
    _judge_init_repo_with_modified_file(tmp_path, lines=5)
    out = _judge_artifact_text(str(tmp_path), ["big.py"], "summary",
                               max_chars=40000)
    assert "TRUNCATED BY THE HARNESS" not in out
    assert "y = 2" in out
    assert "summary" in out


def test_judge_artifact_text_over_cap_marker_has_correct_n_m_k(tmp_path) -> None:
    """(b) A diff over max_chars gets the explicit truncation marker with
    correct N (shown), M (total), and K (files omitted entirely) numbers."""
    # Three modified files, each producing a large diff chunk; a small
    # max_chars only leaves room for a prefix of the first file's diff, so
    # the other two are omitted ENTIRELY (K == 2).
    for name in ("a.py", "b.py", "c.py"):
        (tmp_path / name).write_text("x = 1\n", encoding="utf-8")
    _judge_git(tmp_path, "init", "-q")
    _judge_git(tmp_path, "add", "-A")
    _judge_git(tmp_path, "commit", "-q", "-m", "init")
    for name in ("a.py", "b.py", "c.py"):
        (tmp_path / name).write_text(
            "x = 1\n" + ("y = 2\n" * 2000), encoding="utf-8")

    out = _judge_artifact_text(
        str(tmp_path), ["a.py", "b.py", "c.py"], "summary", max_chars=3000)

    assert "[!] DIFF TRUNCATED BY THE HARNESS" in out
    m = re.search(
        r"showing (\d+) of (\d+) characters; (\d+) file\(s\) omitted entirely",
        out)
    assert m, out[-400:]
    shown, total, omitted = int(m.group(1)), int(m.group(2)), int(m.group(3))
    assert 0 < shown < total  # genuinely a partial view
    assert omitted == 2  # only the first file's diff fit in the budget
    assert "UNVERIFIED" in out


def test_judge_artifact_text_truncated_never_exceeds_max_chars(tmp_path) -> None:
    """(c) The returned string never exceeds max_chars INCLUDING the marker,
    across several cap sizes small enough to force truncation."""
    _judge_init_repo_with_modified_file(tmp_path, lines=4000)
    for cap in (500, 1500, 3000, 10000):
        out = _judge_artifact_text(str(tmp_path), ["big.py"], "summary",
                                   max_chars=cap)
        assert len(out) <= cap, (cap, len(out))


# ─── D1: the marker itself must never be silently truncated ────────────────

def test_judge_artifact_text_marker_boundary_caps_stay_intact(tmp_path) -> None:
    """A marker cut mid-string is exactly the invisible-truncation failure
    B7 exists to eliminate. Across caps at and around the marker's own
    length (both the full N/M/K marker and the fixed-length minimal
    fallback), the result must ALWAYS (len(out) <= cap) and must contain a
    COMPLETE, parseable notice whenever the cap is large enough to hold one
    — never a mangled fragment of either marker. Only below the minimal
    marker's own length is a notice legitimately impossible; that is
    asserted explicitly rather than silently ignored."""
    from hydra_core.squad_node import (
        _DIFF_TRUNCATION_MARKER_MINIMAL,
        _DIFF_TRUNCATION_MARKER_TEMPLATE,
    )
    _judge_init_repo_with_modified_file(tmp_path, lines=4000)

    minimal_len = len(_DIFF_TRUNCATION_MARKER_MINIMAL)
    # Worst-case full-marker length for a single-file diff (K in {0,1}).
    full_len = len(_DIFF_TRUNCATION_MARKER_TEMPLATE.format(
        shown=99999, total=99999, omitted=1))

    caps = sorted({
        0, 1, 50,
        minimal_len - 1, minimal_len, minimal_len + 1,
        full_len - 1, full_len, full_len + 1,
    })
    # A genuinely INTACT full marker must parse back its own N/M/K numbers.
    full_marker_re = re.compile(
        r"\[!\] DIFF TRUNCATED BY THE HARNESS - showing (\d+) of (\d+) "
        r"characters; (\d+) file\(s\) omitted entirely")

    for cap in caps:
        if cap < 0:
            continue
        out = _judge_artifact_text(str(tmp_path), ["big.py"], "summary",
                                   max_chars=cap)
        assert len(out) <= cap, (cap, len(out))

        has_full = bool(full_marker_re.search(out))
        has_minimal = _DIFF_TRUNCATION_MARKER_MINIMAL in out
        # A "mangled fragment" would be a prefix of either marker that is
        # NOT the complete marker string — assert that never happens by
        # checking neither marker's literal opening tag appears without
        # the marker being intact.
        fragment_of_full = ("DIFF TRUNCATED BY THE HARNESS" in out
                            and not has_full)
        fragment_of_minimal = ("[!] TRUNCATED BY THE HARNESS" in out
                               and not has_minimal and not has_full)
        assert not fragment_of_full, (cap, out[-250:])
        assert not fragment_of_minimal, (cap, out[-250:])

        if cap >= full_len:
            assert has_full, (cap, out[-250:])
        elif cap >= minimal_len:
            assert has_minimal, (cap, out[-250:])
        else:
            # Documented degenerate case: the cap is smaller than even the
            # fixed-length minimal notice, so NO visible truncation warning
            # can fit without breaking the max_chars contract. Truncation
            # is genuinely invisible here — the caller's cap wins.
            assert not has_full and not has_minimal


# ─── D2: quoted/non-ASCII diff headers must not corrupt file attribution ──

def test_judge_artifact_text_quoted_header_path_splits_and_counts_correctly(
    tmp_path,
) -> None:
    """git quotes+octal-escapes a diff header path once it contains a
    non-ASCII byte (core.quotepath defaults to true), e.g.
    ``diff --git "a/caf\\303\\251.py" "b/caf\\303\\251.py"``. Before the D2
    fix that header didn't match the plain ``a/... b/...`` regex, so the
    quoted file's diff silently merged into the PRECEDING file's chunk —
    corrupting the K (files-omitted) count the truncation marker reports.
    """
    _judge_git(tmp_path, "init", "-q")
    plain = tmp_path / "a_plain.py"
    quoted = tmp_path / "café.py"
    plain.write_text("x = 1\n", encoding="utf-8")
    quoted.write_text("x = 1\n", encoding="utf-8")
    _judge_git(tmp_path, "add", "-A")
    _judge_git(tmp_path, "commit", "-q", "-m", "init")
    plain.write_text("x = 1\n" + ("y = 2\n" * 1500), encoding="utf-8")
    quoted.write_text("x = 1\n" + ("z = 3\n" * 1500), encoding="utf-8")

    # Confirm this environment's git actually quotes the non-ASCII path
    # (otherwise the test would trivially pass for the wrong reason).
    raw = subprocess.run(
        ["git", "-C", str(tmp_path), "diff", "--", "a_plain.py", "café.py"],
        capture_output=True, text=True,
    ).stdout
    assert '"a/caf' in raw or '"b/caf' in raw, raw[:200]

    from hydra_core.squad_node import _split_diff_into_file_chunks
    chunks = _split_diff_into_file_chunks(raw)
    paths = [p for p, _chunk in chunks]
    assert "café.py" in paths  # correctly split into its OWN chunk
    assert len(chunks) == 2  # NOT merged into the preceding file's chunk

    # And end-to-end: a small cap should correctly report exactly ONE file
    # omitted entirely (whichever diff chunk didn't fit), not zero (which
    # is what the pre-fix merge bug would silently produce).
    out = _judge_artifact_text(
        str(tmp_path), ["a_plain.py", "café.py"], "summary", max_chars=1500)
    m = re.search(r"(\d+) file\(s\) omitted entirely", out)
    assert m, out[-300:]
    assert int(m.group(1)) == 1


def test_judge_defaults_cross_vendor_codex_when_gate_unavailable(monkeypatch) -> None:
    """Claude generated but the gate tool returns nothing (no daemon) → default
    to cross-vendor codex (conservative); gate_eligible_judges is still called.

    F31: gate returns empty → required_cross_vendor defaults to True; Claude
    generates + codex judges → cross_vendor=True → NOT degraded → pass accepted.
    """
    _claude_gen(monkeypatch)
    # Explicitly remove gate_eligible_judges from responses so it returns {}
    # (simulates "no daemon / tool missing"), falling back to required_cross=True.
    resp = _happy_responses("pass")
    del resp[("pp_harness", "gate_eligible_judges")]
    disp = _LiveDispatcher(resp)  # no gate_eligible_judges response → default True
    out = _drive_pp_stage_loop(
        disp, run_id="run_T", project_path="/tmp/proj", request_text="do it")
    assert out["producer"] == "claude"
    assert ("pp_harness", "gate_eligible_judges") in {(s, t) for (s, t, _a) in disp.calls}
    rv = next(a for (s, t, a) in disp.calls if t == "record_verdict")
    assert rv["judge_producer"] == "codex"
    assert rv["score_json"].get("_cross_vendor") is True
    assert rv["score_json"].get("_judge_tier") == "cross_vendor"


def test_judge_codex_same_vendor_not_degraded_when_gate_allows(monkeypatch) -> None:
    """Codex generated (true-headless) + pp gate says same-vendor → codex judges
    same-vendor, and that is NOT flagged degraded (the gate sanctioned it)."""
    resp = _happy_responses("pass")
    resp[("pp_harness", "gate_eligible_judges")] = {"status": "done", "result": {
        "required_cross_vendor": False, "rubric_id": "rfc-2119-normative"}}
    disp = _ScriptedDispatcher(resp)  # no live_execution -> codex generates
    out = _drive_pp_stage_loop(
        disp, run_id="run_T", project_path="/tmp/proj", request_text="do it")
    assert out["producer"] == "codex"
    rv = next(a for (s, t, a) in disp.calls if t == "record_verdict")
    assert rv["judge_producer"] == "codex"
    assert rv["score_json"].get("_cross_vendor") is False
    assert rv["score_json"].get("_judge_tier") == "same_vendor"
    assert "_judge_degraded" not in rv["score_json"]   # gate-sanctioned same-vendor


# --------------------------------------------------------------------------- #
# Phase 2b: honour pp's finalize readiness + finalize_run downgrade signal.
# --------------------------------------------------------------------------- #
def test_readiness_can_pass_false_surfaces(monkeypatch) -> None:
    """A passing verdict + passing smoke must STILL surface if pp's readiness
    preflight says the stage is not ready (e.g. a server-side gate violation)."""
    monkeypatch.setattr("hydra_core.squad_node._run_smoke",
                        lambda *_a, **_k: ("pass", "ok"))
    resp = _happy_responses("pass")
    resp[("pp_harness", "get_stage_finalize_readiness")] = {"status": "done", "result": {
        "can_pass": False, "next_action": "surface_stage",
        "blockers": [{"kind": "verdict", "reason": "latest verdict fail"}]}}
    disp = _ScriptedDispatcher(resp)
    out = _drive_pp_stage_loop(
        disp, run_id="run_T", project_path="/tmp/proj", request_text="x")
    assert out["final_status"] == "surfaced"
    fs = next(a for (s, t, a) in disp.calls if t == "finalize_stage")
    assert fs["status"] == "surfaced"        # readiness downgraded it
    assert "winner_attempt_id" not in fs
    assert "readiness" in (out.get("error") or "")


def test_readiness_absent_does_not_block(monkeypatch) -> None:
    """When the readiness tool is unavailable (no daemon / scripted default {}),
    the smoke-based decision stands — a passing stage still completes."""
    monkeypatch.setattr("hydra_core.squad_node._run_smoke",
                        lambda *_a, **_k: ("pass", "ok"))
    disp = _ScriptedDispatcher(_happy_responses("pass"))  # no readiness response
    out = _drive_pp_stage_loop(
        disp, run_id="run_T", project_path="/tmp/proj", request_text="x")
    assert out["final_status"] == "complete"
    # The preflight was still attempted (read-only).
    assert ("pp_harness", "get_stage_finalize_readiness") in {
        (s, t) for (s, t, _a) in disp.calls}


def test_finalize_run_downgraded_flag_is_honored(monkeypatch) -> None:
    """finalize_run's PP-VG-7 shape {effective_status, downgraded} must be read:
    a downgraded='complete' must surface, not be laundered into complete."""
    monkeypatch.setattr("hydra_core.squad_node._run_smoke",
                        lambda *_a, **_k: ("pass", "ok"))
    resp = _happy_responses("pass")
    resp[("pp_harness", "finalize_run")] = {"status": "done", "result": {
        "effective_status": "surfaced", "requested_status": "complete",
        "downgraded": True, "surfaced_stage_count": 1}}
    disp = _ScriptedDispatcher(resp)
    out = _drive_pp_stage_loop(
        disp, run_id="run_T", project_path="/tmp/proj", request_text="x")
    assert out["final_status"] == "surfaced"


def test_readiness_auto_resolvable_action_does_not_surface(monkeypatch) -> None:
    """A can_pass=False whose next_action is an auto-resolvable missing row
    (finalize_stage runs it) must NOT pre-surface — defer to finalize_stage."""
    monkeypatch.setattr("hydra_core.squad_node._run_smoke",
                        lambda *_a, **_k: ("pass", "ok"))
    resp = _happy_responses("pass")
    resp[("pp_harness", "get_stage_finalize_readiness")] = {"status": "done", "result": {
        "can_pass": False, "next_action": "run_artifact_validate", "blockers": []}}
    disp = _ScriptedDispatcher(resp)
    out = _drive_pp_stage_loop(
        disp, run_id="run_T", project_path="/tmp/proj", request_text="x")
    # Deferred, not surfaced: finalize_stage still attempts passed.
    assert out["final_status"] == "complete"
    fs = next(a for (s, t, a) in disp.calls if t == "finalize_stage")
    assert fs["status"] == "passed"


# --------------------------------------------------------------------------- #
# Phase 2c: opt-in best-of-N (HYDRA_BEST_OF_N). Default OFF; single-candidate
# behavior is unchanged when the flag is absent (covered by every test above).
# --------------------------------------------------------------------------- #
from hydra_core.squad_node import _best_of_n, _rank_key  # noqa: E402


def _best_of_responses(n_candidates: int = 2, merge_status: str = "merged") -> dict:
    resp = _happy_responses("pass")
    cands = [{"candidate_index": i, "judge_position": i,
              "attempt_slot_id": f"slot{i}", "worktree_path": f"/tmp/c{i}",
              "worktree_mode": "copy"} for i in range(1, n_candidates + 1)]
    resp[("pp_harness", "start_best_of_stage")] = {"status": "done", "result": {
        "stage_id": "st_BO", "candidates": cands, "shuffle_seed": 1}}
    resp[("pp_harness", "borda_count")] = {"status": "done", "result": {
        "winner": "att_T", "scores": []}}
    resp[("pp_harness", "archive_winner_and_losers")] = {"status": "done", "result": {
        "merge_status": merge_status, "winner_diff_path": "code/winner.diff",
        "losers_archived": n_candidates - 1}}
    resp[("pp_harness", "teardown_candidates")] = {"status": "done", "result": {
        "teardown_status": "ok"}}
    return resp


def test_best_of_n_off_by_default_uses_single_candidate(monkeypatch) -> None:
    """No HYDRA_BEST_OF_N → the single-candidate path runs (no best-of tools)."""
    monkeypatch.setattr("hydra_core.squad_node._run_smoke",
                        lambda *_a, **_k: ("pass", "ok"))
    disp = _ScriptedDispatcher(_best_of_responses())  # responses present but unused
    out = _drive_pp_stage_loop(
        disp, run_id="run_T", project_path="/tmp/proj", request_text="x")
    assert out["final_status"] == "complete"
    assert out.get("best_of_n") is None
    assert ("pp_harness", "start_best_of_stage") not in {(s, t) for (s, t, _a) in disp.calls}
    assert ("pp_harness", "start_stage") in {(s, t) for (s, t, _a) in disp.calls}


def test_best_of_n_drives_candidates_and_merges_winner(monkeypatch) -> None:
    monkeypatch.setenv("HYDRA_BEST_OF_N", "2")
    monkeypatch.setattr("hydra_core.squad_node._run_smoke",
                        lambda *_a, **_k: ("pass", "ok"))
    disp = _ScriptedDispatcher(_best_of_responses(n_candidates=2, merge_status="merged"))
    out = _drive_pp_stage_loop(
        disp, run_id="run_T", project_path="/tmp/proj", request_text="do it")

    assert out["final_status"] == "complete"
    assert out.get("best_of_n") == 2
    seq = {(s, t) for (s, t, _a) in disp.calls}
    assert ("pp_harness", "start_best_of_stage") in seq
    assert ("pp_harness", "archive_winner_and_losers") in seq
    assert ("pp_harness", "teardown_candidates") in seq
    # Two candidates generated + judged.
    assert disp.tool_seq().count("generate") == 2
    assert disp.tool_seq().count("record_smoke_status") == 2
    fs = next(a for (s, t, a) in disp.calls if t == "finalize_stage")
    assert fs["status"] == "passed"
    assert fs["winner_attempt_id"] == "att_T"


def test_best_of_n_merge_conflict_surfaces(monkeypatch) -> None:
    monkeypatch.setenv("HYDRA_BEST_OF_N", "2")
    monkeypatch.setattr("hydra_core.squad_node._run_smoke",
                        lambda *_a, **_k: ("pass", "ok"))
    disp = _ScriptedDispatcher(_best_of_responses(merge_status="conflict"))
    out = _drive_pp_stage_loop(
        disp, run_id="run_T", project_path="/tmp/proj", request_text="x")
    assert out["final_status"] == "surfaced"
    # Finding 5: finalize_stage is now called BEFORE archive_winner_and_losers,
    # so it reflects the preliminary winner verdict ("passed"), not the merge result.
    # The run is surfaced via finalize_run(surfaced) after the merge fails.
    fs = next(a for (s, t, a) in disp.calls if t == "finalize_stage")
    assert fs["status"] == "passed"   # preliminary verdict was pass; merge failed after
    assert "conflict" in (out.get("error") or ""), (
        f"merge failure error must mention conflict: {out.get('error')}")


def test_best_of_n_falls_back_to_single_when_cannot_open(monkeypatch) -> None:
    """If start_best_of_stage can't open (e.g. no cross-vendor judge), fall back
    to the single-candidate path rather than aborting the run."""
    monkeypatch.setenv("HYDRA_BEST_OF_N", "3")
    monkeypatch.setattr("hydra_core.squad_node._run_smoke",
                        lambda *_a, **_k: ("pass", "ok"))
    resp = _happy_responses("pass")
    resp[("pp_harness", "start_best_of_stage")] = {
        "status": "failed", "error": "no non-Claude judge vendor reachable"}
    disp = _ScriptedDispatcher(resp)
    out = _drive_pp_stage_loop(
        disp, run_id="run_T", project_path="/tmp/proj", request_text="x")
    assert out["final_status"] == "complete"
    seq = {(s, t) for (s, t, _a) in disp.calls}
    assert ("pp_harness", "start_stage") in seq          # single-candidate fallback ran
    assert disp.tool_seq().count("generate") == 1


def test_best_of_n_env_parsing() -> None:
    import os as _os
    for val, exp in [("2", 2), ("8", 8), ("1", None), ("9", None),
                     ("", None), ("x", None), ("0", None)]:
        _os.environ["HYDRA_BEST_OF_N"] = val
        try:
            assert _best_of_n() == exp, val
        finally:
            _os.environ.pop("HYDRA_BEST_OF_N", None)
    assert _best_of_n() is None  # unset


def test_rank_key_orders_pass_over_revise_over_fail() -> None:
    p = _rank_key("pass", {"correctness": 0.9}, "pass")
    r = _rank_key("revise", {"correctness": 0.9}, "pass")
    f = _rank_key("fail", {"correctness": 0.9}, "skipped")
    assert p > r > f
    # Booleans in score (e.g. _cross_vendor) are ignored in the mean.
    assert _rank_key("pass", {"_cross_vendor": True, "x": 1.0}, "pass") > 0


def test_best_of_n_copy_merge_passes(monkeypatch) -> None:
    """merge_status='copy' (copy-mode merge-back on a non-git project) is a valid
    successful apply — must complete, not surface."""
    monkeypatch.setenv("HYDRA_BEST_OF_N", "2")
    monkeypatch.setattr("hydra_core.squad_node._run_smoke",
                        lambda *_a, **_k: ("pass", "ok"))
    disp = _ScriptedDispatcher(_best_of_responses(merge_status="copy"))
    out = _drive_pp_stage_loop(
        disp, run_id="run_T", project_path="/tmp/proj", request_text="x")
    assert out["final_status"] == "complete"
    fs = next(a for (s, t, a) in disp.calls if t == "finalize_stage")
    assert fs["status"] == "passed"


def test_best_of_n_teardown_partial_surfaced_but_not_fatal(monkeypatch) -> None:
    """A non-ok teardown (orphaned worktrees) is SURFACED in the result, but does
    not discard a successfully-merged winner."""
    monkeypatch.setenv("HYDRA_BEST_OF_N", "2")
    monkeypatch.setattr("hydra_core.squad_node._run_smoke",
                        lambda *_a, **_k: ("pass", "ok"))
    resp = _best_of_responses(merge_status="merged")
    resp[("pp_harness", "teardown_candidates")] = {"status": "done", "result": {
        "teardown_status": "partial", "not_torn_down": [2]}}
    disp = _ScriptedDispatcher(resp)
    out = _drive_pp_stage_loop(
        disp, run_id="run_T", project_path="/tmp/proj", request_text="x")
    assert out["final_status"] == "complete"        # merged winner still lands
    assert out.get("teardown_status") == "partial"  # ... but the gap is visible
    assert out.get("teardown_not_torn_down") == [2]


def test_rank_key_verdict_dominates_high_score_fail() -> None:
    """A fail with a high rubric score must NEVER outrank a pass with a low one
    (rubrics may be 0-1 or 0-10; verdict dominates absolutely)."""
    assert _rank_key("pass", {"x": 0.5}, "skipped") > _rank_key("fail", {"x": 9.0}, "pass")
    assert _rank_key("revise", {"x": 0.0}, "skipped") > _rank_key("fail", {"x": 10.0}, "pass")


def test_best_of_n_unknown_borda_winner_falls_back_to_ranked(monkeypatch) -> None:
    """If Borda returns an id that matches NO candidate (or multiple), the winner
    falls back to the top-RANKED candidate rather than guessing — the run still
    finalizes against a real winner attempt id."""
    monkeypatch.setenv("HYDRA_BEST_OF_N", "2")
    monkeypatch.setattr("hydra_core.squad_node._run_smoke",
                        lambda *_a, **_k: ("pass", "ok"))
    resp = _best_of_responses(merge_status="merged")
    resp[("pp_harness", "borda_count")] = {"status": "done", "result": {
        "winner": "NONEXISTENT_ID", "scores": []}}
    disp = _ScriptedDispatcher(resp)
    out = _drive_pp_stage_loop(
        disp, run_id="run_T", project_path="/tmp/proj", request_text="x")
    assert out["final_status"] == "complete"
    fs = next(a for (s, t, a) in disp.calls if t == "finalize_stage")
    assert fs["status"] == "passed"
    assert fs["winner_attempt_id"] == "att_T"   # ranked[0]'s real attempt id


# --------------------------------------------------------------------------- #
# Phase 7b: hydra_context_block threading into generator prompts (_via_mcp)   #
# --------------------------------------------------------------------------- #

def test_via_mcp_threads_hydra_context_block_to_generate(monkeypatch) -> None:
    """When start_run returns hydra_context_block, _via_mcp must prepend it to
    the request_text so the generator (pp_codex.generate) receives it inside the
    prompt built by _build_engineer_prompt."""
    monkeypatch.setattr("hydra_core.squad_node.harvest_pp_run_artifacts",
                        lambda **_k: None)
    monkeypatch.setattr("hydra_core.squad_node._run_smoke",
                        lambda *_a, **_k: ("pass", "stub smoke pass"))
    state = HydraState(root_goal="t")
    pack = _eng_pack()
    ctx_block = "## Hydra context\nworkflow_id: wf-ctx-x"
    resp = _happy_responses("pass")
    resp[("pp_harness", "start_run")] = {"status": "done", "result": {
        "run_id": "run_T", "hydra_context_block": ctx_block}}
    disp = _ScriptedDispatcher(resp, drive=True)

    _via_mcp(state, pack, _inbound(state), disp)

    # Locate the generate call and verify its prompt argument contains the block.
    gen_args_list = [a for (s, t, a) in disp.calls if t == "generate"]
    assert gen_args_list, "expected at least one pp_codex.generate call"
    gen_prompt = gen_args_list[0].get("prompt", "")
    assert ctx_block in gen_prompt, (
        f"hydra_context_block must appear in generate prompt; got: {gen_prompt[:200]!r}"
    )


def test_via_mcp_without_hydra_context_block_prompt_unchanged(monkeypatch) -> None:
    """When start_run does NOT return hydra_context_block, the generate prompt
    must not contain any spurious Hydra context header."""
    monkeypatch.setattr("hydra_core.squad_node.harvest_pp_run_artifacts",
                        lambda **_k: None)
    monkeypatch.setattr("hydra_core.squad_node._run_smoke",
                        lambda *_a, **_k: ("pass", "stub smoke pass"))
    state = HydraState(root_goal="t")
    pack = _eng_pack()
    # Default _happy_responses has no hydra_context_block in start_run.
    disp = _ScriptedDispatcher(_happy_responses("pass"), drive=True)

    _via_mcp(state, pack, _inbound(state), disp)

    gen_args_list = [a for (s, t, a) in disp.calls if t == "generate"]
    assert gen_args_list, "expected at least one pp_codex.generate call"
    gen_prompt = gen_args_list[0].get("prompt", "")
    assert "## Hydra context" not in gen_prompt, (
        "generate prompt must not contain a Hydra context header when block is absent"
    )


# --------------------------------------------------------------------------- #
# B9: real gate_type threading (headless single-candidate + best-of + _via_mcp)
# --------------------------------------------------------------------------- #
#
# Before this fix every headless call site hardcoded
# gate_type_used = _pp_gate_type("code", "code_style"), so the security/spec/
# contract/design tiebreak branches in _judge_vendor_chain were unreachable
# dead code. These tests drive the real functions (not just the pure
# _judge_vendor_chain helper) and assert the real gate_type argument reaches
# gate_eligible_judges / _judge_vendor_chain.

def test_gate_type_for_envelope_maps_hydra_envelope_types() -> None:
    from hydra_core.squad_node import _gate_type_for_envelope

    assert _gate_type_for_envelope("PRD") == "spec"
    assert _gate_type_for_envelope("ARCH_RFC") == "design"
    assert _gate_type_for_envelope("DEV_TASK") == "code_style"
    assert _gate_type_for_envelope("HANDOFF") == "code_style"
    # Unrecognized/missing type (e.g. the planner's C_SUITE_DECISION_PACKET
    # dispatch envelope) falls through to the documented code_style DEFAULT.
    assert _gate_type_for_envelope("C_SUITE_DECISION_PACKET") == "code_style"
    assert _gate_type_for_envelope(None) == "code_style"


def test_drive_pp_stage_loop_single_candidate_threads_gate_type(monkeypatch) -> None:
    """squad_node call site (single-candidate path): the caller-supplied
    gate_type must be the SAME value sent to gate_eligible_judges."""
    monkeypatch.setattr("hydra_core.squad_node._run_smoke",
                        lambda *_a, **_k: ("pass", "stub smoke pass"))
    disp = _ScriptedDispatcher(_happy_responses("pass"))
    out = _drive_pp_stage_loop(
        disp, run_id="run_T", project_path="/tmp/proj", request_text="do the thing",
        gate_type="security")
    assert out["final_status"] == "complete"
    gate_call = next(a for (s, t, a) in disp.calls if t == "gate_eligible_judges")
    assert gate_call["gate_type"] == "security"


def test_drive_pp_stage_loop_default_gate_type_is_code_style() -> None:
    """No gate_type supplied -- falls through to the documented DEFAULT,
    unchanged from before this fix."""
    disp = _ScriptedDispatcher(_happy_responses("pass"))
    _drive_pp_stage_loop(
        disp, run_id="run_T", project_path="/tmp/proj", request_text="do it")
    gate_call = next(a for (s, t, a) in disp.calls if t == "gate_eligible_judges")
    assert gate_call["gate_type"] == "code_style"


def _best_of_responses_b9(n_candidates: int = 1) -> dict:
    cands = [
        {"candidate_index": i, "attempt_slot_id": f"slot{i}",
         "worktree_path": f"/tmp/b9c{i}", "worktree_mode": "copy"}
        for i in range(1, n_candidates + 1)
    ]
    return {
        ("pp_harness", "start_best_of_stage"): {"status": "done", "result": {
            "stage_id": "st_b9", "candidates": cands}},
        ("pp_harness", "gate_eligible_judges"): {"status": "done", "result": {
            "required_cross_vendor": False, "rubric_id": "rfc-2119-normative"}},
        ("pp_codex", "generate"): {"status": "done", "result": {
            "text": "edited foo.py", "model": "codex-1",
            "tokens_in": 5, "tokens_out": 7, "cost_usd": 0.02}},
        ("pp_harness", "archive_artifact"): {"status": "done", "result": {"path": "x"}},
        ("pp_harness", "record_attempt"): {"status": "done", "result": {"attempt_id": "att_b9"}},
        ("pp_codex", "critique"): {"status": "done", "result": {"parsed": {
            "outcome": "pass", "critique_md": "ok", "score": {}}}},
        ("pp_harness", "record_verdict"): {"status": "done", "result": {}},
        ("pp_harness", "record_smoke_status"): {"status": "done", "result": {}},
        ("pp_harness", "get_stage_finalize_readiness"): {"status": "done", "result": {}},
        ("pp_harness", "finalize_stage"): {"status": "done", "result": {}},
        ("pp_harness", "finalize_run"): {"status": "done", "result": {
            "effective_status": "complete", "status": "complete"}},
        ("pp_harness", "borda_count"): {"status": "done", "result": {"winner": "att_b9"}},
        ("pp_harness", "archive_winner_and_losers"): {"status": "done", "result": {
            "merge_status": "merged", "winner_diff_path": "w.diff"}},
        ("pp_harness", "teardown_candidates"): {"status": "done", "result": {
            "teardown_status": "ok"}},
    }


class _B9BoDispatcher:
    def __init__(self, responses):
        self.responses = responses
        self.calls: list[tuple] = []

    def call_mcp(self, server, tool, args, *, squad_id=None):
        self.calls.append((server, tool, dict(args)))
        return self.responses.get((server, tool), {"status": "done", "result": {}})

    def emit_claude_prompt(self, *a, **k): raise NotImplementedError  # pragma: no cover
    def invoke_claude_skill(self, *a, **k): raise NotImplementedError  # pragma: no cover
    def spawn_subprocess(self, *a, **k): raise NotImplementedError  # pragma: no cover


def test_drive_best_of_loop_threads_gate_type(monkeypatch) -> None:
    """squad_node call site (best-of path): the caller-supplied gate_type
    must reach BOTH start_best_of_stage and gate_eligible_judges/
    _judge_vendor_chain -- not the old hardcoded code_style literal."""
    from hydra_core.squad_node import _drive_best_of_loop

    monkeypatch.setattr("hydra_core.squad_node._run_smoke",
                        lambda *_a, **_k: ("pass", "stub smoke pass"))
    disp = _B9BoDispatcher(_best_of_responses_b9(n_candidates=1))
    out = _drive_best_of_loop(
        disp, run_id="run_b9", project_path="/tmp/proj", request_text="do it",
        n=1, gate_type="contract")
    assert out is not None
    open_call = next(a for (s, t, a) in disp.calls if t == "start_best_of_stage")
    assert open_call["gate_type"] == "contract"
    gate_call = next(a for (s, t, a) in disp.calls if t == "gate_eligible_judges")
    assert gate_call["gate_type"] == "contract"


def test_via_mcp_threads_real_gate_type_from_dev_task(monkeypatch) -> None:
    """_via_mcp call site: a DEV_TASK-typed inbound envelope must resolve to
    gate_type='code_style' and that exact value must reach
    _drive_pp_stage_loop (proving the end-to-end envelope -> kind ->
    gate_type derivation, not just the pure mapping function)."""
    captured: dict = {}

    def _fake_loop(_dispatcher, **kwargs):
        captured.update(kwargs)
        return {
            "final_status": "complete", "stage_outcome": "pass",
            "verdict_outcome": "pass", "attempt_id": "att_x", "critique": "",
            "error": None, "finalized": True, "wrote_changes": False,
            "smoke_status": "pass", "smoke_reason": "", "harvest_sha": None,
            "harvest_error": None, "changed_paths": [],
        }

    monkeypatch.setattr("hydra_core.squad_node._drive_pp_stage_loop", _fake_loop)
    monkeypatch.setattr("hydra_core.squad_node.harvest_pp_run_artifacts",
                        lambda **_k: None)
    state = HydraState(root_goal="t")
    pack = _eng_pack()
    disp = _ScriptedDispatcher(_happy_responses("pass"), drive=True)

    _via_mcp(state, pack, _inbound(state), disp)

    assert captured.get("gate_type") == "code_style"


def test_via_mcp_threads_real_gate_type_from_prd_envelope(monkeypatch) -> None:
    """A PRD-typed inbound envelope (squad.yaml accepts PRD/ARCH_RFC/DEV_TASK/
    HANDOFF) must resolve to gate_type='spec' and that exact value must reach
    _drive_pp_stage_loop."""
    from uuid import uuid4

    captured: dict = {}

    def _fake_loop(_dispatcher, **kwargs):
        captured.update(kwargs)
        return {
            "final_status": "complete", "stage_outcome": "pass",
            "verdict_outcome": "pass", "attempt_id": "att_x", "critique": "",
            "error": None, "finalized": True, "wrote_changes": False,
            "smoke_status": "pass", "smoke_reason": "", "harvest_sha": None,
            "harvest_error": None, "changed_paths": [],
        }

    monkeypatch.setattr("hydra_core.squad_node._drive_pp_stage_loop", _fake_loop)
    monkeypatch.setattr("hydra_core.squad_node.harvest_pp_run_artifacts",
                        lambda **_k: None)
    state = HydraState(root_goal="t")
    pack = _eng_pack()
    disp = _ScriptedDispatcher(_happy_responses("pass"), drive=True)

    class _FakePRDEnvelope:
        """Minimal stand-in carrying only what _via_mcp reads via getattr --
        the real PRD schema has no target_repo_id field, so a genuine PRD
        instance can never resolve a dispatch target; this fake proves the
        gate_type derivation without fighting that unrelated constraint."""
        type = "PRD"

        def __init__(self, workflow_id):
            self.id = uuid4()
            self.workflow_id = workflow_id
            self.origin_squad = "hydra"
            self.model_tier = None
            self.target_repo_id = "hydra"
            self.target_repo_subpath = None
            self.instructions = "write the PRD"
            self.summary = None
            self.objective = None
            self.pp_team = None
            self.pp_profile = None

        def model_dump(self, mode="json"):
            return {"type": self.type}

    inbound = _FakePRDEnvelope(state.workflow_id)
    _via_mcp(state, pack, inbound, disp)

    assert captured.get("gate_type") == "spec"


def test_via_mcp_threads_real_gate_type_from_arch_rfc_envelope(monkeypatch) -> None:
    """An ARCH_RFC-typed inbound envelope must resolve to gate_type='design'
    and that exact value must reach _drive_pp_stage_loop."""
    from uuid import uuid4

    captured: dict = {}

    def _fake_loop(_dispatcher, **kwargs):
        captured.update(kwargs)
        return {
            "final_status": "complete", "stage_outcome": "pass",
            "verdict_outcome": "pass", "attempt_id": "att_x", "critique": "",
            "error": None, "finalized": True, "wrote_changes": False,
            "smoke_status": "pass", "smoke_reason": "", "harvest_sha": None,
            "harvest_error": None, "changed_paths": [],
        }

    monkeypatch.setattr("hydra_core.squad_node._drive_pp_stage_loop", _fake_loop)
    monkeypatch.setattr("hydra_core.squad_node.harvest_pp_run_artifacts",
                        lambda **_k: None)
    state = HydraState(root_goal="t")
    pack = _eng_pack()
    disp = _ScriptedDispatcher(_happy_responses("pass"), drive=True)

    class _FakeArchRFCEnvelope:
        type = "ARCH_RFC"

        def __init__(self, workflow_id):
            self.id = uuid4()
            self.workflow_id = workflow_id
            self.origin_squad = "hydra"
            self.model_tier = None
            self.target_repo_id = "hydra"
            self.target_repo_subpath = None
            self.instructions = "review the architecture"
            self.summary = None
            self.objective = None
            self.pp_team = None
            self.pp_profile = None

        def model_dump(self, mode="json"):
            return {"type": self.type}

    inbound = _FakeArchRFCEnvelope(state.workflow_id)
    _via_mcp(state, pack, inbound, disp)

    assert captured.get("gate_type") == "design"
