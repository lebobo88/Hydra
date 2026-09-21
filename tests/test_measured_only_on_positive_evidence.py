"""The "measured only on positive evidence" rule (cross-vendor judge
finding, follow-up round, HIGH -- the rule fix, replacing four rounds of
site-by-site patching of Findings B, 6, X, and Y).

The rule: a cost is "measured" ONLY when a vendor actually reported a
usable finite value. Everything else -- absent, rejected, floored,
unpriceable -- defaults to "unmeasured". Before this fix, a missing cost, a
rejected cost, a floored (broken-rate) cost, and a genuine free call all
converged on the SAME state: $0.00 + "measured". This is the same
non-convergence class the JSON-serialization work in this thread hit
earlier, closed only once patching individual call sites stopped and the
RULE was fixed instead.

Where the rule lives (one seam per evidence type, not per call site):
  - REPORTED cost evidence: `squad_node.coerce_untrusted_cost` already
    correctly resolves None/unparseable/non-finite to "unmeasured" -- the
    bug was call sites re-deriving "was this reported at all" ON TOP of
    that answer (`and raw is not None`) instead of trusting it. Fixed by
    deleting the redundant condition at all four `unmeasured_count`
    accrual sites in `squad_node.py`.
  - ESTIMATED (priced) cost evidence: `pricing.price_call` is the ONE seam
    every caller shares. It now distinguishes "priced as genuinely zero"
    (a validated, non-negative, finite rate against real token counts,
    including zero) from "could not price" (an unknown model, a
    negative/non-finite rate -- validated once via `_rate_is_priceable`,
    checked both at `_load_override_table` load time and again inside
    `price_call` itself) by returning `None` for the latter. Every
    downstream caller's existing `if priced is not None: ... "estimated"`
    check becomes correct FOR FREE once this one function's contract is
    honest.

These tests assert the LEDGER (`state.budget.unmeasured_stages`,
`state.budget.spent_usd`) and the recorded `cost_source`, not intermediate
return values.
"""
from __future__ import annotations

import pytest

from hydra_core.governance import charge_and_gate
from hydra_core.host_bridge import _priced_cost
from hydra_core.pricing import ModelRate, _rate_is_priceable, price_call
from hydra_core.state import BudgetLedger, HydraState


def _state(budget: float = 10.0) -> HydraState:
    return HydraState(
        root_goal="positive-evidence-rule-test",
        budget=BudgetLedger(budget_usd=budget),
    )


def _cursor() -> dict:
    return {"stage_id": "stage-1", "project_path": ".", "workflow_id": None}


# ---------------------------------------------------------------------------
# pricing.price_call: "priced as genuinely zero" vs "could not price".
# ---------------------------------------------------------------------------

def test_negative_rate_is_not_priceable():
    assert _rate_is_priceable(ModelRate(-1.0, 5.0)) is False
    assert _rate_is_priceable(ModelRate(float("nan"), 5.0)) is False
    assert _rate_is_priceable(ModelRate(float("inf"), 5.0)) is False


def test_ordinary_positive_rate_is_priceable():
    assert _rate_is_priceable(ModelRate(3.0, 15.0, 3.75, 0.30)) is True


def test_genuine_zero_tokens_against_valid_rate_prices_as_real_zero():
    """A real, trustworthy rate applied to zero tokens is a genuinely
    PRICED $0.00 -- distinct from "could not price"."""
    assert price_call("claude-sonnet-5", 0, 0) == 0.0


def test_broken_override_rate_cannot_be_priced(monkeypatch):
    """Finding X, at the source: a hostile/broken `pricing.json` override
    (a negative rate component) must never enter the resolvable rate
    table, so `price_call` sees it as unpriceable, not as a valid rate to
    multiply and floor."""
    import hydra_core.pricing as pricing_module

    monkeypatch.setattr(
        pricing_module, "_load_override_table",
        lambda: {"hostile-model": ModelRate(-5.0, 10.0)},
    )
    assert price_call("hostile-model", 1000, 1000) is None


def test_ordinary_known_model_prices_exactly_as_before():
    """Control: the normal (non-broken) pricing path is unaffected."""
    assert price_call("claude-sonnet-5", 1_000_000, 1_000_000) == pytest.approx(18.0)


# ---------------------------------------------------------------------------
# host_bridge._priced_cost + the ledger (charge_and_gate).
# ---------------------------------------------------------------------------

def test_genuine_free_call_is_measured_on_the_ledger():
    """A vendor that AFFIRMATIVELY reports `cost_usd: 0.0` is positive
    evidence of a real free call -- measured, not unmeasured."""
    cursor = _cursor()
    cost, source = _priced_cost(cursor, {"cost_usd": 0.0}, label="generate")
    assert source == "measured"

    state = _state()
    charge_and_gate(state, cost, 0, source=source)
    assert state.budget.spent_usd == 0.0
    assert state.budget.unmeasured_stages == 0


def test_absent_cost_is_unmeasured_on_the_ledger():
    """Finding Y: a vendor that OMITS `cost_usd` entirely (the common
    case, not an exotic one) must be unmeasured on the ledger, not a
    confident measured $0.00."""
    cursor = _cursor()
    cost, source = _priced_cost(cursor, {}, label="generate")
    assert source == "unmeasured"

    state = _state()
    charge_and_gate(state, cost, 0, source=source)
    assert state.budget.spent_usd == 0.0
    assert state.budget.unmeasured_stages == 1


def test_rejected_cost_is_unmeasured_on_the_ledger():
    cursor = _cursor()
    cost, source = _priced_cost(cursor, {"cost_usd": "NaN"}, label="generate")
    assert source == "unmeasured"

    state = _state()
    charge_and_gate(state, cost, 0, source=source)
    assert state.budget.unmeasured_stages == 1


def test_broken_override_is_unmeasured_not_confidently_estimated(monkeypatch):
    """Finding X, end to end through `_priced_cost`: a broken rate that
    `price_call` correctly refuses to price must resolve to unmeasured on
    the ledger, never a confident $0.00 'estimated' charge."""
    import hydra_core.pricing as pricing_module

    monkeypatch.setattr(
        pricing_module, "_load_override_table",
        lambda: {"hostile-model": ModelRate(-5.0, 10.0)},
    )
    cursor = _cursor()
    cost, source = _priced_cost(
        cursor,
        {"tokens_in": 1000, "tokens_out": 1000, "model": "hostile-model"},
        label="generate",
    )
    assert source == "unmeasured"
    assert cost == 0.0

    state = _state()
    charge_and_gate(state, cost, 0, source=source)
    assert state.budget.spent_usd == 0.0
    assert state.budget.unmeasured_stages == 1


def test_ordinary_finite_cost_is_measured_and_charged_exactly_as_before():
    """The control that matters most: every normal attended
    generate/judge call is charged and labeled exactly as before."""
    cursor = _cursor()
    cost, source = _priced_cost(cursor, {"cost_usd": 1.23}, label="generate")
    assert source == "measured"

    state = _state()
    charge_and_gate(state, cost, 0, source=source)
    assert state.budget.spent_usd == pytest.approx(1.23)
    assert state.budget.unmeasured_stages == 0


def test_ordinary_estimated_cost_is_measured_via_pricing_and_charged(monkeypatch):
    """Control: an ordinary priced estimate (no cost_usd, real tokens+model)
    is charged and labeled "estimated" exactly as before -- the ledger
    still sees real money, not unmeasured."""
    cursor = _cursor()
    cost, source = _priced_cost(
        cursor,
        {"tokens_in": 1_000_000, "tokens_out": 1_000_000, "model": "claude-sonnet-5"},
        label="generate",
    )
    assert source == "estimated"
    assert cost == pytest.approx(18.0)

    state = _state()
    charge_and_gate(state, cost, 0, source=source)
    assert state.budget.spent_usd == pytest.approx(18.0)
    assert state.budget.unmeasured_stages == 0
