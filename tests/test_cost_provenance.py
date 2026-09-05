"""B8 — cost provenance: an unreporting host must not be treated as free.

Covers:
  1. record_cost source semantics: measured -> spent_usd only; estimated ->
     BOTH spent_usd and estimated_usd; unmeasured -> unmeasured_stages++.
  2. The 0.8/1.0 budget tripwires fire on estimated spend, not just measured.
  3. pricing.price_call: known model prices correctly, unknown model returns
     None (never raises, never silently prices as $0 without signalling).
  4. pricing._load_override_table / get_rate: HYDRA_HOME is read at CALL TIME
     (a test that sets the env var AFTER import still sees the override), and
     a malformed pricing.json degrades to the builtin table without raising.
  5. charge_and_gate_repo still reconciles sum(repo_spend.values()) against
     spent_usd when charged with source="estimated".
  6. host_bridge._priced_cost: measured/estimated/unmeasured resolution from a
     host result dict, and the stage-level cost_source priority merge
     (estimated > measured > unmeasured).

No network, no LLM calls, hermetic per tests/conftest.py (HYDRA_HOME already
redirected to .tmp-pytest/hydra-home by the session-scoped fixture).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from hydra_core import pricing
from hydra_core.governance import (
    charge_and_gate,
    charge_and_gate_repo,
    record_cost,
    should_block_for_budget,
    should_downgrade_model,
)
from hydra_core.host_bridge import _priced_cost
from hydra_core.state import BudgetLedger, HydraState


def _state(budget: float = 10.0) -> HydraState:
    return HydraState(root_goal="cost-provenance-test", budget=BudgetLedger(budget_usd=budget))


# ---------------------------------------------------------------------------
# 1. record_cost source semantics
# ---------------------------------------------------------------------------

class TestRecordCostSource:
    def test_measured_charge_lands_only_in_spent_usd(self) -> None:
        state = _state()
        record_cost(state, 2.0, 1000, source="measured")
        assert state.budget.spent_usd == pytest.approx(2.0)
        assert state.budget.estimated_usd == pytest.approx(0.0)
        assert state.budget.unmeasured_stages == 0

    def test_estimated_charge_lands_in_both_counters(self) -> None:
        state = _state()
        record_cost(state, 2.0, 1000, source="estimated")
        assert state.budget.spent_usd == pytest.approx(2.0)
        assert state.budget.estimated_usd == pytest.approx(2.0)
        assert state.budget.unmeasured_stages == 0

    def test_unmeasured_increments_stage_counter_and_charges_nothing(self) -> None:
        state = _state()
        record_cost(state, 0.0, 0, source="unmeasured")
        assert state.budget.spent_usd == pytest.approx(0.0)
        assert state.budget.estimated_usd == pytest.approx(0.0)
        assert state.budget.unmeasured_stages == 1

    def test_default_source_is_measured_for_pre_existing_callers(self) -> None:
        # FALSIFIABILITY: revert record_cost's `source: str = "measured"`
        # default to a positional-only signature (i.e. undo the B8 fix) and
        # this call becomes a TypeError -- proving every pre-existing
        # 2-arg caller (cli.py, ingest.py, supervisor.py) still compiles
        # and behaves exactly as before.
        state = _state()
        record_cost(state, 1.0, 10)  # no `source` kwarg at all
        assert state.budget.spent_usd == pytest.approx(1.0)
        assert state.budget.estimated_usd == pytest.approx(0.0)
        assert state.budget.unmeasured_stages == 0


# ---------------------------------------------------------------------------
# 2. Tripwires fire on estimated spend
# ---------------------------------------------------------------------------

class TestTripwiresOnEstimatedSpend:
    """These tests DERIVE the charged dollar figure from a host-result dict
    via `_priced_cost` (tokens_in/tokens_out/model, no cost_usd) rather than
    handing `record_cost` a dollar amount directly. Handing in a bare dollar
    figure would make the tripwire assertion true regardless of whether B8's
    estimation plumbing exists at all -- the point is that an UNREPORTED cost
    still moves the gate, and only routing through `_priced_cost` proves that.
    """

    def test_downgrade_tripwire_fires_on_priced_estimate(self) -> None:
        # Sonnet: $3/Mtok in + $15/Mtok out -> 1M in + 1M out = $18.00.
        # Budget $20 -> percent_consumed = 0.90, past the 0.8 downgrade line
        # but under 1.0, so block must stay False.
        cursor: dict = {"stage_id": "s1", "project_path": ".", "workflow_id": None}
        result = {"tokens_in": 1_000_000, "tokens_out": 1_000_000, "model": "claude-sonnet-5"}
        cost, source = _priced_cost(cursor, result, label="generate")
        assert source == "estimated"
        assert cost == pytest.approx(18.0)

        state = _state(budget=20.0)
        record_cost(state, cost, 2_000_000, source=source)
        assert state.budget.percent_consumed == pytest.approx(0.90)
        assert should_downgrade_model(state) is True
        assert should_block_for_budget(state) is False

    def test_block_tripwire_fires_on_priced_estimate(self) -> None:
        # Same priced $18.00, but against a $18 budget -> percent_consumed
        # == 1.0 exactly, which the >= comparison in should_block_for_budget
        # must treat as blocking.
        cursor: dict = {"stage_id": "s1", "project_path": ".", "workflow_id": None}
        result = {"tokens_in": 1_000_000, "tokens_out": 1_000_000, "model": "claude-sonnet-5"}
        cost, source = _priced_cost(cursor, result, label="generate")
        assert source == "estimated"

        state = _state(budget=18.0)
        record_cost(state, cost, 2_000_000, source=source)
        assert should_block_for_budget(state) is True


# ---------------------------------------------------------------------------
# 2b. End-to-end: unreported host result -> _priced_cost -> charge_and_gate
# ---------------------------------------------------------------------------

class TestPricedEstimateReachesChargeAndGate:
    """The plumbing can be individually correct (`_priced_cost` returns a
    priced tuple; `charge_and_gate` moves counters given a number) while still
    being DISCONNECTED -- e.g. a caller that drops the returned `source` on
    the floor and always charges "measured". This test wires the two
    together exactly as `hydra_core/cli.py`'s attended charge sites do, so a
    regression that breaks that connection (not either half in isolation)
    fails here.
    """

    def test_unreported_cost_reaches_charge_and_gate_as_estimate(self) -> None:
        cursor: dict = {"stage_id": "s1", "project_path": ".", "workflow_id": None}
        result = {"tokens_in": 1_000_000, "tokens_out": 1_000_000, "model": "claude-sonnet-5"}
        # No "cost_usd" key anywhere in `result` -- this is the exact shape of
        # an unreporting host's submit_host_result payload.
        assert "cost_usd" not in result

        cost, source = _priced_cost(cursor, result, label="generate")
        toks = int(result["tokens_in"]) + int(result["tokens_out"])

        state = _state(budget=20.0)
        block, downgrade = charge_and_gate(state, cost, toks, source=source)

        assert downgrade is True
        assert block is False
        assert state.budget.estimated_usd == pytest.approx(18.0)
        assert state.budget.estimated_usd > 0.0


# ---------------------------------------------------------------------------
# 3. pricing.price_call
# ---------------------------------------------------------------------------

class TestPriceCall:
    def test_known_model_prices_correctly(self) -> None:
        # Sonnet list rates: $3/Mtok in, $15/Mtok out.
        cost = pricing.price_call("claude-sonnet-5", 1_000_000, 1_000_000)
        assert cost == pytest.approx(18.0)

    def test_known_model_with_cache_tokens(self) -> None:
        cost = pricing.price_call(
            "claude-sonnet-5", 0, 0,
            cache_write_tokens=1_000_000, cache_read_tokens=1_000_000,
        )
        assert cost == pytest.approx(3.75 + 0.30)

    def test_unknown_model_returns_none_and_does_not_raise(self) -> None:
        # FALSIFIABILITY: a naive implementation that defaults an unknown
        # rate to ModelRate(0.0, 0.0) instead of returning None would make
        # this `is None` assertion fail (it would return 0.0 instead).
        assert pricing.price_call("totally-unheard-of-model-xyz", 1000, 1000) is None
        assert pricing.get_rate("totally-unheard-of-model-xyz") is None


# ---------------------------------------------------------------------------
# 4. HYDRA_HOME call-time resolution + malformed override resilience
# ---------------------------------------------------------------------------

class TestPricingOverride:
    def test_hydra_home_is_read_at_call_time_not_import_time(self, tmp_path: Path, monkeypatch) -> None:
        # FALSIFIABILITY: if hydra_core.pricing bound HYDRA_HOME at import
        # time (the hydra_core/memory.py anti-pattern this fix explicitly
        # avoids), setting the env var here -- long after pricing was
        # imported at module load -- would have no effect, and the override
        # rate below would never be picked up: this assertion would see the
        # builtin table's price ($18.0 for 1M/1M sonnet tokens) instead.
        override = {
            "claude-sonnet-5": {
                "input_per_mtok": 999.0,
                "output_per_mtok": 999.0,
            }
        }
        (tmp_path / "pricing.json").write_text(json.dumps(override), encoding="utf-8")
        monkeypatch.setenv("HYDRA_HOME", str(tmp_path))
        cost = pricing.price_call("claude-sonnet-5", 1_000_000, 1_000_000)
        assert cost == pytest.approx(999.0 + 999.0)

    def test_malformed_override_file_falls_back_to_builtin(self, tmp_path: Path, monkeypatch) -> None:
        (tmp_path / "pricing.json").write_text("{not valid json!!", encoding="utf-8")
        monkeypatch.setenv("HYDRA_HOME", str(tmp_path))
        # FALSIFIABILITY: without the try/except around json.loads in
        # _load_override_table, this call raises json.JSONDecodeError instead
        # of returning the builtin-table price.
        cost = pricing.price_call("claude-sonnet-5", 1_000_000, 1_000_000)
        assert cost == pytest.approx(18.0)

    def test_missing_override_file_falls_back_to_builtin(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("HYDRA_HOME", str(tmp_path / "does-not-exist"))
        cost = pricing.price_call("claude-sonnet-5", 1000, 1000)
        assert cost is not None


# ---------------------------------------------------------------------------
# 5. charge_and_gate_repo reconciliation with source="estimated"
# ---------------------------------------------------------------------------

class TestChargeAndGateRepoReconciles:
    def test_reconciles_sum_repo_spend_against_spent_usd_estimated(self) -> None:
        state = _state(budget=30.0)
        state.budget.allocate_repos(["repo-a", "repo-b"])
        charge_and_gate_repo(state, "repo-a", 3.0, 100, source="estimated")
        charge_and_gate_repo(state, "repo-b", 4.0, 100, source="measured")
        charge_and_gate_repo(state, None, 1.0, 0, source="unmeasured")
        total_repo_spend = sum(state.budget.repo_spend.values())
        assert total_repo_spend == pytest.approx(state.budget.spent_usd)
        assert state.budget.spent_usd == pytest.approx(8.0)
        assert state.budget.estimated_usd == pytest.approx(3.0)
        assert state.budget.unmeasured_stages == 1


# ---------------------------------------------------------------------------
# 6. host_bridge._priced_cost resolution + stage-level source priority
# ---------------------------------------------------------------------------

class TestPricedCostResolution:
    def _cursor(self) -> dict:
        return {"stage_id": "stage-1", "project_path": ".", "workflow_id": None}

    def test_measured_when_host_reports_cost_usd(self) -> None:
        cursor = self._cursor()
        cost, source = _priced_cost(cursor, {"cost_usd": 1.23}, label="generate")
        assert source == "measured"
        assert cost == pytest.approx(1.23)
        assert cursor["cost_source"] == "measured"

    def test_estimated_when_tokens_and_model_present_but_no_cost(self) -> None:
        # FALSIFIABILITY: before this fix, host_bridge did
        # `float(result.get("cost_usd") or 0.0)` unconditionally -- a result
        # with tokens_in=1_000_000/tokens_out=1_000_000/model=claude-sonnet-5
        # and NO cost_usd key would price at $0.0, not $18.0.
        cursor = self._cursor()
        result = {"tokens_in": 1_000_000, "tokens_out": 1_000_000, "model": "claude-sonnet-5"}
        cost, source = _priced_cost(cursor, result, label="generate")
        assert source == "estimated"
        assert cost == pytest.approx(18.0)
        assert cursor["cost_source"] == "estimated"

    def test_unmeasured_when_no_cost_and_no_priceable_tokens(self) -> None:
        cursor = self._cursor()
        cost, source = _priced_cost(cursor, {}, label="generate")
        assert source == "unmeasured"
        assert cost == 0.0
        assert cursor["cost_source"] == "unmeasured"
        assert cursor["unmeasured_count"] == 1

    def test_unmeasured_when_tokens_present_but_model_unknown(self) -> None:
        cursor = self._cursor()
        result = {"tokens_in": 1000, "tokens_out": 1000, "model": "no-such-model"}
        cost, source = _priced_cost(cursor, result, label="generate")
        assert source == "unmeasured"
        assert cost == 0.0

    def test_stage_source_priority_estimated_beats_measured(self) -> None:
        # A stage where the generate call was measured but the judge call was
        # only priceable (estimated) must resolve the STAGE's cost_source to
        # "estimated" -- estimated outranks measured, per the merge policy.
        cursor = self._cursor()
        _priced_cost(cursor, {"cost_usd": 0.50}, label="generate")
        assert cursor["cost_source"] == "measured"
        _priced_cost(
            cursor,
            {"tokens_in": 500_000, "tokens_out": 500_000, "model": "claude-sonnet-5"},
            label="judge",
        )
        assert cursor["cost_source"] == "estimated"

    def test_stage_source_priority_measured_beats_unmeasured(self) -> None:
        cursor = self._cursor()
        _priced_cost(cursor, {}, label="generate")
        assert cursor["cost_source"] == "unmeasured"
        _priced_cost(cursor, {"cost_usd": 0.1}, label="judge")
        assert cursor["cost_source"] == "measured"


# ---------------------------------------------------------------------------
# 7. Mixed-provenance stage: estimated_usd must equal ONLY the estimated
#    component, never the collapsed-label stage total.
# ---------------------------------------------------------------------------

class TestMixedProvenanceEstimatedUsd:
    """A single stage can accrue a MEASURED component (e.g. generate reports
    cost_usd directly) and an ESTIMATED component (e.g. judge reports only
    tokens+model) on the SAME cursor. `_merge_cost_source` necessarily
    collapses `cursor["cost_source"]` to one winning label ("estimated" beats
    "measured") for the whole stage -- charging the STAGE TOTAL under that
    single collapsed label overstates `budget.estimated_usd` by the measured
    component, because `record_cost(source="estimated")` used to credit the
    WHOLE amount, not just the estimated portion.
    """

    def test_charging_stage_total_under_collapsed_source_overstates_estimated_usd(
        self,
    ) -> None:
        cursor: dict = {"stage_id": "s1", "project_path": ".", "workflow_id": None}

        # Measured component: generate reports cost_usd=2.50 directly.
        gen_cost, gen_source = _priced_cost(cursor, {"cost_usd": 2.50}, label="generate")
        assert gen_source == "measured"
        assert gen_cost == pytest.approx(2.50)

        # Estimated component: judge reports only tokens+model -> priced by
        # hydra_core.pricing (Sonnet: 1M in + 1M out = $18.00).
        judge_result = {
            "tokens_in": 1_000_000, "tokens_out": 1_000_000, "model": "claude-sonnet-5",
        }
        judge_cost, judge_source = _priced_cost(cursor, judge_result, label="judge")
        assert judge_source == "estimated"
        assert judge_cost == pytest.approx(18.0)

        # The stage's collapsed label is "estimated" (estimated outranks
        # measured), and the stage TOTAL is the sum of both components --
        # exactly what cli.py's charging sites compute and forward.
        assert cursor["cost_source"] == "estimated"
        stage_total = gen_cost + judge_cost
        assert stage_total == pytest.approx(20.50)

        # `_priced_cost` tracked the estimated-only component separately.
        estimated_component = float(cursor.get("estimated_cost_usd") or 0.0)
        assert estimated_component == pytest.approx(18.0)

        # Charge exactly as cli.py's _cmd_attended_submit / recover-stalled
        # sites do: pass the per-component figure through charge_and_gate's
        # `estimated_usd` kwarg rather than letting the amount be inferred
        # from the collapsed `cost_source` label.
        state = _state(budget=100.0)
        charge_and_gate(
            state, stage_total, 2_000_000,
            source=cursor["cost_source"], estimated_usd=estimated_component,
        )

        # spent_usd must still receive the FULL stage total.
        assert state.budget.spent_usd == pytest.approx(20.50)
        # estimated_usd must equal ONLY the estimated component ($18.00), NOT
        # the mixed stage total ($20.50). This is the assertion that fails
        # against the pre-fix code, which reads estimated_usd == 20.50 here.
        assert state.budget.estimated_usd == pytest.approx(18.0)

    def test_wholly_measured_stage_leaves_estimated_usd_at_zero(self) -> None:
        cursor: dict = {"stage_id": "s2", "project_path": ".", "workflow_id": None}
        gen_cost, _ = _priced_cost(cursor, {"cost_usd": 5.0}, label="generate")
        judge_cost, _ = _priced_cost(cursor, {"cost_usd": 1.0}, label="judge")
        estimated_component = float(cursor.get("estimated_cost_usd") or 0.0)
        assert estimated_component == pytest.approx(0.0)

        state = _state(budget=100.0)
        charge_and_gate(
            state, gen_cost + judge_cost, 0,
            source=cursor["cost_source"], estimated_usd=estimated_component,
        )
        assert state.budget.spent_usd == pytest.approx(6.0)
        assert state.budget.estimated_usd == pytest.approx(0.0)

    def test_wholly_estimated_stage_has_estimated_usd_equal_spent_usd(self) -> None:
        cursor: dict = {"stage_id": "s3", "project_path": ".", "workflow_id": None}
        result = {"tokens_in": 1_000_000, "tokens_out": 1_000_000, "model": "claude-sonnet-5"}
        gen_cost, _ = _priced_cost(cursor, result, label="generate")
        estimated_component = float(cursor.get("estimated_cost_usd") or 0.0)

        state = _state(budget=100.0)
        charge_and_gate(
            state, gen_cost, 2_000_000,
            source=cursor["cost_source"], estimated_usd=estimated_component,
        )
        assert state.budget.spent_usd == pytest.approx(18.0)
        assert state.budget.estimated_usd == pytest.approx(state.budget.spent_usd)
