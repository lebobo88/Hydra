"""``hydra_core.squad_node._parse_claude_cli_result`` is the parse boundary
for an untrusted vendor CLI's own stdout (``claude -p --output-format
json``'s ``total_cost_usd`` / ``usage.{input,output}_tokens``) -- the SAME
defect class as the CRITICAL finding that opened this whole thread: a NaN
reported cost makes every later budget COMPARISON fail open (``x <= 0`` is
always ``False`` for NaN, so the block/downgrade gates never fire), and an
Infinity cost disables the cap outright. Unlike an operator-supplied
``--budget`` flag, this is not input we can simply refuse -- the subprocess
has already run (real money may already be spent, and ``text`` carries the
real completed work), so the fix treats a non-finite reported cost exactly
like a MISSING one: ``cost_usd=0.0`` with ``cost_source="unmeasured"`` (the
same concept ``host_bridge._priced_cost`` already uses for a host that
reports no cost), never a silently-truthful "measured $0".

These tests assert the RECORDED STATE (unmeasured, and — critically — that
the budget ledger is never poisoned into a permanently-fail-open NaN), not
merely that something didn't raise.
"""
from __future__ import annotations

import json
import math

import pytest

from hydra_core.governance import charge_and_gate, should_block_for_budget
from hydra_core.squad_node import (
    _claude_critique,
    _parse_claude_cli_result,
    _run_claude_cli,
)
from hydra_core.state import BudgetLedger, HydraState


def _state(budget: float = 1.0, spent: float = 1.0) -> HydraState:
    return HydraState(
        root_goal="vendor-cost-boundary-test",
        budget=BudgetLedger(budget_usd=budget, spent_usd=spent),
    )


# ---------------------------------------------------------------------------
# _parse_claude_cli_result — the raw parse boundary.
# ---------------------------------------------------------------------------

def test_non_finite_cost_is_recorded_as_unmeasured_not_zero():
    # `json.dumps` (default allow_nan=True) writes a bare `NaN` token here,
    # exactly matching what a hostile/buggy vendor CLI's own (also
    # permissive) JSON encoder would print.
    stdout = json.dumps({"result": "implemented the thing", "total_cost_usd": float("nan")})
    out = _parse_claude_cli_result(stdout, "", 0, "claude-opus-4-8")

    assert out["cost_usd"] == 0.0
    assert out["cost_source"] == "unmeasured"
    # The real completed work is NOT discarded over an untrustworthy cost figure.
    assert "implemented the thing" in out["text"]
    assert out["status"] == "done"
    # The rejection is noted in the record, not silently absorbed.
    assert "non-finite cost_usd" in out["text"]


def test_infinity_cost_is_also_unmeasured():
    stdout = json.dumps({"result": "ok", "total_cost_usd": float("inf")})
    out = _parse_claude_cli_result(stdout, "", 0, "m")
    assert out["cost_usd"] == 0.0
    assert out["cost_source"] == "unmeasured"


def test_non_finite_tokens_do_not_destroy_the_result():
    """Before this fix, `int(nan)`/`int(inf)` on a hostile token count raised
    inside `_parse_claude_cli_result`, which (via `_run_claude_cli`'s broad
    except) discarded the ENTIRE result -- including real generated text --
    and mislabeled the attempt as an infra error. Tokens are informational
    counters here (no downstream budget COMPARISON reads them directly,
    unlike cost_usd), so a hostile value is clamped to 0, not fatal."""
    stdout = json.dumps({
        "result": "implemented the thing",
        "total_cost_usd": 0.05,
        "usage": {"input_tokens": float("nan"), "output_tokens": float("inf")},
    })
    out = _parse_claude_cli_result(stdout, "", 0, "m")
    assert out["tokens_in"] == 0
    assert out["tokens_out"] == 0
    assert out["cost_usd"] == 0.05
    assert out["cost_source"] == "measured"
    assert "implemented the thing" in out["text"]
    assert out["status"] == "done"


def test_finite_cost_recorded_exactly_as_before():
    """Control: the normal path for every judge/generate call. This is the
    one that matters most -- it must be byte-for-byte unaffected."""
    stdout = json.dumps({
        "result": "did it", "total_cost_usd": 0.12,
        "usage": {"input_tokens": 100, "output_tokens": 50},
        "model": "claude-opus-4-8",
    })
    out = _parse_claude_cli_result(stdout, "", 0, "fallback-model")
    assert out["text"] == "did it"
    assert out["cost_usd"] == 0.12
    assert out["cost_source"] == "measured"
    assert out["tokens_in"] == 100 and out["tokens_out"] == 50
    assert out["model"] == "claude-opus-4-8"
    assert out["status"] == "done"


def test_missing_cost_field_still_degrades_to_unmeasured_unchanged():
    """Pre-existing behaviour (a JSON result with no cost field at all) is
    preserved exactly -- this fix only changes the NON-FINITE case, not the
    missing-field case."""
    deg = _parse_claude_cli_result("plain text out", "", 0, "m")
    assert deg["text"] == "plain text out"
    assert deg["cost_usd"] == 0.0
    assert deg["cost_source"] == "unmeasured"


def test_run_claude_cli_exception_fallback_carries_unmeasured_source(monkeypatch):
    monkeypatch.setattr(
        "hydra_core.squad_node.run_text",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    out = _run_claude_cli("prompt", cwd="/tmp/x")
    assert out["cost_usd"] == 0.0
    assert out["cost_source"] == "unmeasured"


# ---------------------------------------------------------------------------
# _claude_critique — the judge re-wrap propagates cost_source rather than
# dropping it (never silently defaults to a false "measured").
# ---------------------------------------------------------------------------

def test_claude_critique_propagates_unmeasured_cost_source(monkeypatch):
    monkeypatch.setattr(
        "hydra_core.squad_node._run_claude_cli",
        lambda *a, **kw: {
            "text": '{"outcome":"pass","critique_md":"ok","score":{}}',
            "model": "claude-sonnet-4-6", "cost_usd": 0.0,
            "cost_source": "unmeasured", "tokens_in": 0, "tokens_out": 0,
            "status": "done",
        },
    )
    out = _claude_critique("change", "rubric", "/tmp/x")
    assert out["cost_usd"] == 0.0
    assert out["cost_source"] == "unmeasured"


def test_claude_critique_propagates_measured_cost_source(monkeypatch):
    monkeypatch.setattr(
        "hydra_core.squad_node._run_claude_cli",
        lambda *a, **kw: {
            "text": '{"outcome":"pass","critique_md":"ok","score":{}}',
            "model": "claude-sonnet-4-6", "cost_usd": 0.03,
            "cost_source": "measured", "tokens_in": 10, "tokens_out": 5,
            "status": "done",
        },
    )
    out = _claude_critique("change", "rubric", "/tmp/x")
    assert out["cost_usd"] == 0.03
    assert out["cost_source"] == "measured"


# ---------------------------------------------------------------------------
# End-to-end: the guarded (unmeasured) value never poisons the budget
# ledger, so the block gate still fires correctly for the REST of the
# workflow -- the concrete consequence this whole fix exists to prevent.
# ---------------------------------------------------------------------------

def test_guarded_unmeasured_cost_does_not_poison_the_budget_gate():
    stdout = json.dumps({"result": "ok", "total_cost_usd": float("nan")})
    parsed = _parse_claude_cli_result(stdout, "", 0, "m")

    state = _state(budget=1.0, spent=1.0)  # already fully spent
    block, downgrade = charge_and_gate(
        state, parsed["cost_usd"], 0, source=parsed["cost_source"],
    )

    assert not math.isnan(state.budget.spent_usd)
    assert state.budget.spent_usd == 1.0  # charging 0.0 leaves it exactly unchanged
    assert block is True  # the exhausted-budget gate still fires correctly
    assert should_block_for_budget(state) is True
    assert state.budget.unmeasured_stages == 1
