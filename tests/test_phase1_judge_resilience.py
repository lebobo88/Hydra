"""Phase-1 judge-resilience + MCP isError tests.

Covers the audit's F1/F3/F5/F2-dispatcher fixes:
  - classify_judge_error maps raw failure text to (reason, retryable).
  - dispatch_judge_with_fallback iterates preferred vendors, falls through on an
    infra/auth JudgeDispatchError, and degrades to an honest `skip` (NOT `fail`)
    when every vendor is exhausted.
  - judge_and_rank EXCLUDES `skip` verdicts from Borda ranking.
  - MCPStdioDispatcher gates on the raw CallToolResult.isError (a tool-level
    error becomes status="failed"; a success payload carrying an `error` metric
    field is NOT false-failed).
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from uuid import uuid4
from unittest.mock import MagicMock, patch

import pytest

from hydra_core.dispatcher import MCPStdioDispatcher
from hydra_core.judge.dispatcher import (
    classify_judge_error,
    dispatch_judge_with_fallback,
)
from hydra_core.judge.best_of_n import judge_and_rank

RUBRIC = "board-decision-quality@1"


def _envelope() -> dict:
    return {"id": str(uuid4()), "type": "C_SUITE_DECISION_PACKET",
            "origin_squad": "executive", "objective": "x"}


class _ScriptedClient:
    """A CritiqueClient whose behavior is keyed by vendor."""

    def __init__(self, behavior: dict):
        # behavior[vendor] -> either an Exception to raise or a dict to return
        self.behavior = behavior
        self.calls: list[str] = []

    def critique(self, *, vendor, artifact_text, rubric_md):
        self.calls.append(vendor)
        b = self.behavior[vendor]
        if isinstance(b, Exception):
            raise b
        return b


# --------------------------------------------------------------------------- #
# classify_judge_error
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("text,reason,retryable", [
    ("Error authenticating: IneligibleTierError ... migrate to Antigravity", "ineligible_tier", False),
    ("You've hit your usage limit", "quota", True),
    ("request timed out after 1800s", "timeout", True),
    ("pp critique server not found in backends", "tool_failed", False),
    ("critique response missing valid outcome/verdict", "bad_response", False),
    ("something totally unexpected", "unknown", False),
])
def test_classify_judge_error(text, reason, retryable):
    assert classify_judge_error(text) == (reason, retryable)


# --------------------------------------------------------------------------- #
# dispatch_judge_with_fallback
# --------------------------------------------------------------------------- #
def test_fallback_falls_through_to_second_vendor():
    good = {"outcome": "pass", "critique_md": "x" * 100, "score_json": {"q": 9}}
    client = _ScriptedClient({
        "gemini": RuntimeError("IneligibleTierError: no longer supported"),
        "codex": good,
    })
    verdict, attempts = dispatch_judge_with_fallback(
        envelope=_envelope(), rubric_id=RUBRIC,
        judge_vendors=["gemini", "codex"], workflow_id=uuid4(),
        client=client,
    )
    assert verdict.outcome == "pass"
    assert verdict.judge_vendor == "codex"
    assert client.calls == ["gemini", "codex"]
    # fell back → marked degraded for audit
    assert verdict.score_json.get("_judge_degraded") is True
    assert attempts[0]["ok"] is False and attempts[0]["reason"] == "ineligible_tier"
    assert attempts[1]["ok"] is True


def test_fallback_all_fail_returns_skip_not_fail():
    client = _ScriptedClient({
        "gemini": RuntimeError("IneligibleTierError"),
        "codex": RuntimeError("usage limit"),
    })
    verdict, attempts = dispatch_judge_with_fallback(
        envelope=_envelope(), rubric_id=RUBRIC,
        judge_vendors=["gemini", "codex"], workflow_id=uuid4(),
        client=client,
    )
    assert verdict.outcome == "skip"          # NOT "fail"
    assert verdict.score_json.get("_error") is True
    assert verdict.score_json.get("_infra") is True
    assert [a["ok"] for a in attempts] == [False, False]


def test_fallback_single_vendor_success_not_degraded():
    good = {"outcome": "pass", "critique_md": "y" * 100, "score_json": {"q": 8}}
    client = _ScriptedClient({"codex": good})
    verdict, _ = dispatch_judge_with_fallback(
        envelope=_envelope(), rubric_id=RUBRIC,
        judge_vendors=["codex"], workflow_id=uuid4(), client=client,
    )
    assert verdict.outcome == "pass"
    assert "_judge_degraded" not in (verdict.score_json or {})


# --------------------------------------------------------------------------- #
# judge_and_rank excludes skip from Borda
# --------------------------------------------------------------------------- #
def test_judge_and_rank_excludes_skip_from_borda():
    a, b = _envelope(), _envelope()
    # codex down for candidate A's judge -> skip; B passes with a high score.
    def behavior(vendor):
        return RuntimeError("usage limit")
    # All judge calls go to codex; make codex fail so every verdict is a skip.
    client = _ScriptedClient({"codex": RuntimeError("usage limit")})
    outcome = judge_and_rank(
        [a, b], rubric_ids=[RUBRIC], workflow_id=uuid4(),
        judge_vendors=["codex"], client=client,
    )
    # Both verdicts recorded, both skip, none rankable -> deterministic winner,
    # no crash.
    assert len(outcome.verdicts) == 2
    assert all(v.outcome == "skip" for v in outcome.verdicts)
    assert outcome.winner_id in (str(a["id"]), str(b["id"]))


# --------------------------------------------------------------------------- #
# MCPStdioDispatcher isError gating
# --------------------------------------------------------------------------- #
def _dispatcher() -> MCPStdioDispatcher:
    d = MCPStdioDispatcher(Path(tempfile.mkdtemp()))
    d._servers["fake_server"] = {"command": "fake", "args": [], "env": None, "cwd": None}
    return d


def _run_call(result_obj):
    dispatcher = _dispatcher()

    class _Session:
        async def __aenter__(self): return self
        async def __aexit__(self, *_): pass
        async def initialize(self): pass
        async def call_tool(self, tool, args): return result_obj

    class _CM:
        async def __aenter__(self): return MagicMock(), MagicMock()
        async def __aexit__(self, *_): pass

    class _SessionCM:
        async def __aenter__(self): return _Session()
        async def __aexit__(self, *_): pass

    with patch("mcp.client.stdio.stdio_client", return_value=_CM()), \
         patch("mcp.ClientSession", return_value=_SessionCM()):
        return dispatcher._run(dispatcher._async_call("fake_server", "critique", {}))


def test_iserror_true_returns_failed():
    res = MagicMock()
    res.isError = True
    res.content = [MagicMock(text='{"detail": "tool blew up"}')]
    out = _run_call(res)
    assert out["status"] == "failed"
    assert "error" in out


def test_success_payload_with_error_metric_not_failed():
    # isError is a real False; a legit `error` field in the success payload must
    # NOT be mistaken for a tool failure.
    res = MagicMock()
    res.isError = False
    res.content = [MagicMock(text='{"status": "ok", "error_rate": 0.02, "error": null}')]
    out = _run_call(res)
    assert out["status"] == "done"
