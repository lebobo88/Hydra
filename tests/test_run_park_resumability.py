"""Cross-vendor judge finding (P5b flip, refuted-with-caveat): a default
`hydra run` (no --live, no --no-checkpoint) is the attended entry point --
it checkpoints (compiled LangGraph graph, thread_id=str(workflow_id)), so a
non-terminal `phase` in its printed result is RESUMABLE via `hydra step
<workflow_id>`, not abandoned. The judge's "critical" finding (an unguarded
hostless deadlock, same shape as the replay bug) was refuted: `_cmd_run`'s
default path IS this repo's mandated attended flow (run -> step -> submit),
unlike `hydra replay`, whose `replay_wf` is a throwaway id no command ever
targets.

The caveat that survived the refutation: before the plan phase shipped on by
default, `hydra run` on a typical goal usually reached a terminal phase in
one call; now a non-trivial goal commonly parks at phase="planning" awaiting
the attended planning cursor. That is a real, previously-undocumented
behaviour change for anything scripting against this command's JSON output.
This module proves the fix is genuine resumability, not merely a printed
claim of it: `hydra run` parks with an explicit `next_action`, and `hydra
step` on that exact workflow_id actually picks the parked work up.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
from pathlib import Path

import pytest

from hydra_core import cli

HYDRA_ROOT = Path(__file__).resolve().parents[1]


class _StubAttendedDispatcher:
    """Non-live stand-in for `_attended_live_dispatcher` -- `hydra step` must
    never spawn a real MCP subprocess in this hermetic test."""

    live_execution = False

    def call_mcp(self, server, tool, args, *, squad_id=None):
        return {"status": "done", "result": {"ok": True}}

    def spawn_subprocess(self, *_a, **_kw):
        return {"status": "done"}

    def emit_claude_prompt(self, prompt, agent=None):
        return {"status": "host_pickup_required", "agent": agent}

    def invoke_claude_skill(self, skill, args):
        return {"status": "host_pickup_required", "skill": skill}

    def set_squad_packs(self, packs):
        pass


@pytest.fixture()
def hermetic(tmp_path, monkeypatch):
    monkeypatch.setenv("HYDRA_CHECKPOINT_DB", str(tmp_path / "checkpoints.db"))
    monkeypatch.setenv("HYDRA_EIGHTS_SPOOL", str(tmp_path / "spool"))
    monkeypatch.setenv("HYDRA_EIGHTS_DEAD_LETTER", str(tmp_path / "dead"))
    monkeypatch.setattr(cli, "_attended_live_dispatcher",
                        lambda *_a, **_kw: _StubAttendedDispatcher())
    return tmp_path


def _run(goal: str, *, extra: list[str] | None = None) -> tuple[int, dict]:
    argv = ["--project", str(HYDRA_ROOT), "run", goal, "--repo", "hydra"]
    if extra:
        argv.extend(extra)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli.main(argv)
    out = buf.getvalue()
    start = out.index("{")
    return rc, json.loads(out[start:])


def test_default_run_parks_resumably_and_step_picks_it_up(hermetic):
    """A default `hydra run` on a goal that triages to non-trivial rigor
    parks at phase="planning" with an explicit, actionable `next_action` --
    and `hydra step` on that exact workflow_id genuinely resumes it (proving
    resumability, not merely asserting the printed claim)."""
    import os
    assert "HYDRA_PLAN_PHASE" not in os.environ, (
        "this test exercises the shipping default; it must not set the flag"
    )

    goal = (
        "resume-park regression: refactor the payments microservice for full "
        "correctness and observability across every downstream consumer"
    )
    rc, payload = _run(goal)
    assert rc == 0, payload

    # The behaviour-change caveat: parked, not silently abandoned, and the
    # next action is stated explicitly rather than left for the caller to
    # infer from `phase` alone.
    assert payload["parked"] is True, payload
    assert payload["phase"] not in ("done", "surfaced"), payload
    wf = payload["workflow_id"]
    assert payload["next_action"] == f"hydra step {wf}", payload

    # Resumability, proven POSITIVELY -- not merely "no error string
    # appeared" (a MEDIUM cross-vendor finding on an earlier draft of this
    # test: absence of two error substrings would pass for many non-
    # resuming responses too). `hydra step` on the SAME workflow_id must
    # actually open the parked planning task's attended cursor: rc == 0, a
    # real `host_action` is present, its agent is the plan-author, and the
    # cursor is opened against the EXACT workflow_id `hydra run` printed.
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        step_rc = cli.main(["--project", str(HYDRA_ROOT), "step", wf])
    step_out = buf.getvalue()
    start = step_out.index("{")
    step_payload = json.loads(step_out[start:])

    assert step_rc == 0, step_payload
    assert step_payload.get("ok") is True, step_payload
    assert step_payload.get("workflow_id") == wf, (
        f"hydra step opened a DIFFERENT workflow than the one hydra run "
        f"printed; step targeted {step_payload.get('workflow_id')!r}, "
        f"expected {wf!r}: {step_payload}"
    )
    host_action = step_payload.get("host_action")
    assert isinstance(host_action, dict) and host_action, (
        f"hydra step must open a real host_action for the parked planning "
        f"task, not merely avoid an error string: {step_payload}"
    )
    assert host_action.get("agent_type") == "hydra:plan-author", (
        f"the parked task is the seeded 'planning' task -- its host_action "
        f"must name the plan-author agent, not something else: {host_action}"
    )
    assert step_payload.get("squad_slug") == "planning", step_payload
    assert "langgraph unavailable" not in json.dumps(step_payload), step_payload
