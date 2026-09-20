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
    SquadResult,
    _claude_critique,
    _drive_pp_stage_loop,
    _parse_claude_cli_result,
    _run_claude_cli,
    coerce_untrusted_cost,
    coerce_untrusted_count,
    resolve_reported_cost,
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


# ---------------------------------------------------------------------------
# Finding A (HIGH, follow-up round): `is_non_finite_float` only recognizes an
# actual `float` instance -- a vendor emitting the cost as a JSON STRING
# (`"NaN"`/`"Infinity"`) bypassed a check applied to the RAW value before
# `float()` coercion. `coerce_untrusted_cost`/`coerce_untrusted_count` (the shared
# helper every vendor cost entry now routes through) validate the COERCED
# value instead, closing the gap for every input shape.
# ---------------------------------------------------------------------------

def test_coerce_untrusted_cost_rejects_string_nan():
    value, source = coerce_untrusted_cost("NaN")
    assert value == 0.0
    assert source == "unmeasured"


def test_coerce_untrusted_cost_rejects_string_infinity():
    value, source = coerce_untrusted_cost("Infinity")
    assert value == 0.0
    assert source == "unmeasured"
    value, source = coerce_untrusted_cost("-Infinity")
    assert value == 0.0
    assert source == "unmeasured"


def test_coerce_untrusted_cost_rejects_float_nan_and_inf():
    """Control: the original (float) attack shape is still caught."""
    assert coerce_untrusted_cost(float("nan")) == (0.0, "unmeasured")
    assert coerce_untrusted_cost(float("inf")) == (0.0, "unmeasured")


def test_coerce_untrusted_cost_measured_control():
    """The control that matters most: an ordinary finite cost (as a real
    float OR a numeric string -- vendors are inconsistent about this) is
    recorded exactly as reported, with source 'measured'."""
    assert coerce_untrusted_cost(0.42) == (0.42, "measured")
    assert coerce_untrusted_cost("0.42") == (0.42, "measured")


def test_coerce_untrusted_cost_missing_and_garbage():
    assert coerce_untrusted_cost(None) == (0.0, "unmeasured")
    assert coerce_untrusted_cost("not-a-number") == (0.0, "unmeasured")


def test_coerce_untrusted_count_rejects_string_and_float_non_finite():
    assert coerce_untrusted_count("NaN") == 0
    assert coerce_untrusted_count(float("inf")) == 0
    assert coerce_untrusted_count(None) == 0


def test_coerce_untrusted_count_measured_control():
    assert coerce_untrusted_count(500) == 500
    assert coerce_untrusted_count("500") == 500


def test_coerce_untrusted_count_clamps_negative_to_zero():
    """Cross-vendor judge finding (follow-up round, HIGH): the docstring
    already promised a non-negative clamp, but the implementation never
    enforced it -- `_priced_cost` feeds this into `pricing.price_call`'s
    multiplication, so a negative token count could REDUCE a stage's
    priced cost below what the measured portion alone would total."""
    assert coerce_untrusted_count(-500) == 0
    assert coerce_untrusted_count("-500") == 0
    assert coerce_untrusted_count(-0.5) == 0


def test_coerce_untrusted_count_fractional_string_truncates_not_raises():
    """Cross-vendor judge finding (follow-up round, HIGH): a fractional
    STRING (e.g. from a preflight that only checked finiteness, not
    integer-ness) must be handled the same way at the cast site -- coerced
    (truncated), never raising."""
    assert coerce_untrusted_count("1.5") == 1
    assert coerce_untrusted_count(1.9) == 1


def test_parse_claude_cli_result_string_nan_cost_is_unmeasured_not_zero():
    """The exact bypass shape at the real parse site: a vendor stdout JSON
    document (a perfectly valid, ordinary JSON document -- the string
    `"NaN"` is a completely normal string value, not a bare non-standard
    token) carrying the cost as a STRING."""
    stdout = json.dumps({"result": "implemented the thing", "total_cost_usd": "NaN"})
    out = _parse_claude_cli_result(stdout, "", 0, "claude-opus-4-8")
    assert out["cost_usd"] == 0.0
    assert out["cost_source"] == "unmeasured"
    assert "implemented the thing" in out["text"]  # result still preserved


def test_parse_claude_cli_result_string_infinity_tokens_clamped_to_zero():
    stdout = json.dumps({
        "result": "ok", "total_cost_usd": 0.02,
        "usage": {"input_tokens": "Infinity", "output_tokens": "500"},
    })
    out = _parse_claude_cli_result(stdout, "", 0, "m")
    assert out["tokens_in"] == 0
    assert out["tokens_out"] == 500
    assert out["cost_usd"] == 0.02
    assert out["cost_source"] == "measured"


# ---------------------------------------------------------------------------
# Finding C: every vendor cost entry point in `_drive_pp_stage_loop` /
# `_drive_best_of_n_stage_loop` (generate AND critique, both the single-shot
# and best-of-N shapes) routes through the SAME `coerce_untrusted_cost`/
# `coerce_untrusted_count` helper -- not just the Claude CLI path. This proves
# it end-to-end through the real drive loop with a scripted pp_codex
# response (the "pp, Codex, or Agy" vendors the finding named), not just the
# helper in isolation.
# ---------------------------------------------------------------------------

def _codex_responses_with_poisoned_generate_cost():
    return {
        ("pp_harness", "start_stage"): {"status": "done", "result": {"stage_id": "st_T"}},
        ("pp_harness", "gate_eligible_judges"): {"status": "done", "result": {
            "required_cross_vendor": False, "rubric_id": "rfc-2119-normative"}},
        ("pp_codex", "generate"): {"status": "done", "result": {
            "text": "edited foo.py\n{\"status\": \"pass\", \"reason\": \"ok\"}",
            "model": "codex-1",
            # The exact bypass shape: a STRING, not a float.
            "tokens_in": 5, "tokens_out": 7, "cost_usd": "NaN", "wall_ms": 100}},
        ("pp_harness", "archive_artifact"): {"status": "done", "result": {"path": ".harness/x"}},
        ("pp_harness", "record_attempt"): {"status": "done", "result": {"attempt_id": "att_T"}},
        ("pp_codex", "critique"): {"status": "done", "result": {
            "parsed": {"outcome": "pass", "critique_md": "c" * 90,
                       "score": {"correctness": 9}},
            "cost_usd": 0.03}},
        ("pp_harness", "record_verdict"): {"status": "done", "result": {}},
        ("pp_harness", "finalize_stage"): {"status": "done", "result": {}},
        ("pp_harness", "finalize_run"): {"status": "done", "result": {"status": "complete"}},
    }


class _ScriptedDispatcherForCost:
    """Minimal scripted dispatcher (mirrors test_drive_pp_loop.py's
    `_ScriptedDispatcher`, duplicated here to keep this file self-contained)."""

    def __init__(self, responses):
        self.responses = responses
        self.calls = []
        self.drive_pp_loop = True

    def call_mcp(self, server, tool, args, *, squad_id=None):
        self.calls.append((server, tool, args))
        key = (server, tool)
        if key not in self.responses:
            return {"status": "failed", "error": f"unscripted call {key}"}
        return self.responses[key]


def test_drive_loop_codex_string_nan_cost_recorded_as_unmeasured(monkeypatch):
    """End-to-end through the real drive loop: a non-Claude vendor
    (pp_codex) reports its generate cost as the STRING "NaN". The stage
    still completes (real generated code is not discarded), the accumulated
    cost stays finite, and the rejection is auditable via
    `unmeasured_count` -- never silently recorded as a measured $0.00."""
    monkeypatch.setattr("hydra_core.squad_node._run_smoke",
                        lambda *_a, **_k: ("pass", "stub smoke pass"))
    disp = _ScriptedDispatcherForCost(_codex_responses_with_poisoned_generate_cost())
    out = _drive_pp_stage_loop(
        disp, run_id="run_T", project_path="/tmp/proj", request_text="do the thing")

    assert not math.isnan(out["cost_usd"])
    assert out["cost_usd"] == pytest.approx(0.03)  # only the critique's real cost
    assert out["unmeasured_count"] >= 1
    assert out["finalized"] is True
    assert out["final_status"] == "complete"


def _codex_responses_with_omitted_generate_cost():
    """Finding Y: a vendor that OMITS `cost_usd` entirely (the common
    case, not an exotic one -- plenty of vendors simply do not report a
    cost) rather than reporting a rejected one."""
    resp = _codex_responses_with_poisoned_generate_cost()
    gen_result = dict(resp[("pp_codex", "generate")]["result"])
    del gen_result["cost_usd"]
    resp[("pp_codex", "generate")] = {"status": "done", "result": gen_result}
    return resp


def test_drive_loop_omitted_generate_cost_recorded_as_unmeasured(monkeypatch):
    """End-to-end through the real drive loop: a vendor that never reports
    `cost_usd` at all (not rejected -- simply absent) must ALSO bump
    `unmeasured_count`, not just a reported-but-rejected one."""
    monkeypatch.setattr("hydra_core.squad_node._run_smoke",
                        lambda *_a, **_k: ("pass", "stub smoke pass"))
    disp = _ScriptedDispatcherForCost(_codex_responses_with_omitted_generate_cost())
    out = _drive_pp_stage_loop(
        disp, run_id="run_T", project_path="/tmp/proj", request_text="do the thing")

    assert out["unmeasured_count"] >= 1
    assert out["finalized"] is True
    assert out["final_status"] == "complete"


# ---------------------------------------------------------------------------
# Finding B: the "unmeasured" provenance must survive all the way to whatever
# CHARGES THE LEDGER -- asserting the ledger/state (`unmeasured_stages`,
# `spent_usd`), not merely the parser's/drive-loop's return value, which is
# what the earlier round's fix left disconnected.
# ---------------------------------------------------------------------------

def _squad_result_with_drive_loop(drive_loop: dict) -> SquadResult:
    return SquadResult(
        envelopes=[],
        artifacts=[{"kind": "pp_run", "ref": "run_1",
                    "raw": {"status": "done", "result": {"run_id": "run_1"}},
                    "drive_loop": drive_loop}],
        status="running",
    )


def test_unmeasured_stage_is_recorded_as_unmeasured_at_the_ledger(monkeypatch):
    """End to end: a drive-loop stage whose ONLY vendor cost report was
    rejected (never charged as a false $0.00 'measured') is charged through
    `supervisor._extract_squad_cost` -> `charge_and_gate`, and
    `state.budget.unmeasured_stages` -- the auditable signal -- increments.
    Asserts the LEDGER, not `_extract_squad_cost`'s return value alone."""
    from hydra_core.supervisor import _extract_squad_cost

    drive_loop = {"cost_usd": 0.0, "tokens_in": 0, "tokens_out": 0,
                  "unmeasured_count": 1}
    result = _squad_result_with_drive_loop(drive_loop)

    usd, tokens, source = _extract_squad_cost(result)
    assert source == "unmeasured"

    state = _state(budget=10.0, spent=0.0)
    assert state.budget.unmeasured_stages == 0
    charge_and_gate(state, usd, tokens, source=source)

    assert state.budget.spent_usd == 0.0
    assert state.budget.unmeasured_stages == 1  # the auditable signal fired


def test_partially_measured_stage_is_recorded_as_measured_with_real_money(monkeypatch):
    """Control: a stage where SOME vendor call was genuinely measured is
    charged as measured with the real dollar amount -- the rejected
    candidate's $0.0 contribution doesn't erase the real money that WAS
    measured elsewhere in the same stage."""
    from hydra_core.supervisor import _extract_squad_cost

    drive_loop = {"cost_usd": 0.03, "tokens_in": 10, "tokens_out": 5,
                  "unmeasured_count": 1}  # one candidate rejected, one measured
    result = _squad_result_with_drive_loop(drive_loop)

    usd, tokens, source = _extract_squad_cost(result)
    assert source == "measured"
    assert usd == pytest.approx(0.03)

    state = _state(budget=10.0, spent=0.0)
    charge_and_gate(state, usd, tokens, source=source)
    assert state.budget.spent_usd == pytest.approx(0.03)
    assert state.budget.unmeasured_stages == 0


def test_extract_squad_cost_rejects_string_non_finite_pp_harness_cost():
    """Finding C: pp_harness's OWN `start_run` response (`inner["cost_usd"]`)
    is a vendor boundary too -- a poisoned string is rejected the same way,
    never poisoning the `max()` aggregate."""
    from hydra_core.supervisor import _extract_squad_cost

    result = SquadResult(
        envelopes=[], status="running",
        artifacts=[{"kind": "pp_run", "ref": "r1",
                    "raw": {"result": {"cost_usd": "NaN", "tokens_in": 10}}}],
    )
    usd, tokens, source = _extract_squad_cost(result)
    assert usd == 0.0
    assert not math.isnan(usd)
    assert source == "unmeasured"


def test_extract_squad_cost_finite_control_is_measured():
    """Control: an ordinary finite pp_harness-reported cost is extracted
    exactly as before, source 'measured'."""
    from hydra_core.supervisor import _extract_squad_cost

    result = SquadResult(
        envelopes=[], status="running",
        artifacts=[{"kind": "pp_run", "ref": "r1",
                    "raw": {"result": {"cost_usd": 0.42, "tokens_in": 100,
                                       "tokens_out": 50}}}],
    )
    usd, tokens, source = _extract_squad_cost(result)
    assert usd == pytest.approx(0.42)
    assert tokens == 150
    assert source == "measured"


# ---------------------------------------------------------------------------
# Finding 6 (follow-up round, MEDIUM): `start_run`'s own scaffold-only
# response always reports a finite `cost_usd` (0.0 when it merely scaffolds),
# which resolves "measured" -- accurate in isolation, but OR-ing that into a
# single stage-wide flag meant a DRIVEN stage whose real cost (the drive
# loop's) was entirely rejected was still labeled "measured" overall, since
# the scaffold placeholder alone always satisfied the OR. The drive loop is
# now authoritative whenever it is present.
# ---------------------------------------------------------------------------

def test_all_rejected_drive_loop_cost_is_unmeasured_even_with_finite_inner_scaffold():
    """The exact scenario Finding 6 describes: `inner["cost_usd"] == 0.0`
    (the scaffold-only start_run placeholder, genuinely "measured" in
    isolation) alongside a drive_loop whose entire cost was rejected
    (`cost_usd=0.0`, `unmeasured_count>=1`). The STAGE must be labeled
    unmeasured -- the scaffold placeholder must not paper over a driven
    run's real, entirely-rejected cost."""
    from hydra_core.supervisor import _extract_squad_cost

    result = SquadResult(
        envelopes=[], status="running",
        artifacts=[{
            "kind": "pp_run", "ref": "r1",
            "raw": {"status": "done", "result": {"cost_usd": 0.0, "run_id": "r1"}},
            "drive_loop": {"cost_usd": 0.0, "tokens_in": 0, "tokens_out": 0,
                          "unmeasured_count": 1},
        }],
    )
    usd, tokens, source = _extract_squad_cost(result)
    assert usd == 0.0
    assert source == "unmeasured"


def test_drive_loop_with_real_money_is_measured_despite_finite_inner_scaffold():
    """Control: when the drive loop DOES report genuine money, the stage is
    correctly "measured" -- the fix must not flip a genuinely-measured
    driven stage to unmeasured."""
    from hydra_core.supervisor import _extract_squad_cost

    result = SquadResult(
        envelopes=[], status="running",
        artifacts=[{
            "kind": "pp_run", "ref": "r1",
            "raw": {"status": "done", "result": {"cost_usd": 0.0, "run_id": "r1"}},
            "drive_loop": {"cost_usd": 0.07, "tokens_in": 10, "tokens_out": 5,
                          "unmeasured_count": 0},
        }],
    )
    usd, tokens, source = _extract_squad_cost(result)
    assert usd == pytest.approx(0.07)
    assert source == "measured"


def test_legacy_scaffold_only_dispatch_without_drive_loop_stays_measured():
    """Control: the pre-existing legacy (non-driven) scaffold-only dispatch
    path -- no `drive_loop` key at all -- is UNCHANGED: `inner["cost_usd"]
    == 0.0` alone is the whole story for that artifact and stays
    "measured", exactly as before this fix."""
    from hydra_core.supervisor import _extract_squad_cost

    result = SquadResult(
        envelopes=[], status="running",
        artifacts=[{"kind": "pp_run", "ref": "r1",
                    "raw": {"status": "done", "result": {"cost_usd": 0.0, "run_id": "r1"}}}],
    )
    usd, tokens, source = _extract_squad_cost(result)
    assert usd == 0.0
    assert source == "measured"


# ---------------------------------------------------------------------------
# resolve_reported_cost (follow-up round, HIGH -- the fifth route, on the
# PRIMARY generation path): `_parse_claude_cli_result` already determines
# whether a Claude CLI call's cost was measured/unmeasured and substitutes a
# finite `0.0` placeholder for the unmeasured case -- but `coerce_untrusted_
# cost(0.0)` correctly returns "measured" (0.0 IS a valid finite float), so
# re-deriving the verdict from the coerced value at the accrual site
# silently overturned the parser's own judgement. `resolve_reported_cost`
# trusts an UPSTREAM `cost_source` when present, and only coerces fresh when
# no upstream verdict exists.
# ---------------------------------------------------------------------------

def test_resolve_reported_cost_trusts_upstream_unmeasured_verdict_absent():
    gi = _parse_claude_cli_result(
        json.dumps({"result": "implemented the thing"}), "", 0, "claude-opus-4-8",
    )
    assert gi["cost_source"] == "unmeasured"
    value, source = resolve_reported_cost(gi)
    assert source == "unmeasured"
    assert value == 0.0


def test_resolve_reported_cost_trusts_upstream_unmeasured_verdict_rejected():
    gi = _parse_claude_cli_result(
        json.dumps({"result": "ok", "total_cost_usd": "NaN"}), "", 0, "m",
    )
    assert gi["cost_source"] == "unmeasured"
    value, source = resolve_reported_cost(gi)
    assert source == "unmeasured"
    assert value == 0.0


def test_resolve_reported_cost_still_measured_for_a_genuine_affirmative_zero():
    """The over-correction control: a vendor that AFFIRMATIVELY reports
    `cost_usd: 0.0` must still be measured -- do not trade under-charging
    for a meaningless unmeasured signal on every genuine free call."""
    gi = _parse_claude_cli_result(
        json.dumps({"result": "ok", "total_cost_usd": 0.0}), "", 0, "m",
    )
    assert gi["cost_source"] == "measured"
    value, source = resolve_reported_cost(gi)
    assert source == "measured"
    assert value == 0.0


def test_resolve_reported_cost_falls_back_to_fresh_coercion_with_no_upstream_verdict():
    """A codex/host-driven result shape carries no `cost_source` at all --
    falls back to fresh coercion exactly as before."""
    value, source = resolve_reported_cost({"cost_usd": 0.42})
    assert (value, source) == (0.42, "measured")
    value, source = resolve_reported_cost({"cost_usd": "NaN"})
    assert (value, source) == (0.0, "unmeasured")
    value, source = resolve_reported_cost({})
    assert (value, source) == (0.0, "unmeasured")


def _claude_gen_responses(*, gen_result: dict, critique_cost_usd: float | None = 0.03) -> dict:
    """Same-vendor critique routed to codex (`required_cross_vendor=True`)
    so this exercises ONLY the generate accrual site's fix, not the
    critique's -- `_claude_critique` (same-vendor) also calls
    `_run_claude_cli` and would otherwise double up the effect under test.

    ``critique_cost_usd=None`` omits the critique's own cost field (so a
    test asserting the STAGE-LEVEL ledger label reflects the generate call
    ALONE, rather than being correctly overridden to "measured" by an
    unrelated genuinely-measured critique cost -- see Finding 6's
    "a stage with ANY real money stays measured" rule, which is correct
    and must not be confused with this fix)."""
    critique_result = {
        "parsed": {"outcome": "pass", "critique_md": "c" * 90,
                   "score": {"correctness": 9}},
    }
    if critique_cost_usd is not None:
        critique_result["cost_usd"] = critique_cost_usd
    return {
        ("pp_harness", "start_stage"): {"status": "done", "result": {"stage_id": "st_T"}},
        ("pp_harness", "gate_eligible_judges"): {"status": "done", "result": {
            "required_cross_vendor": True, "rubric_id": "rfc-2119-normative"}},
        ("pp_harness", "archive_artifact"): {"status": "done", "result": {"path": ".harness/x"}},
        ("pp_harness", "record_attempt"): {"status": "done", "result": {"attempt_id": "att_T"}},
        ("pp_codex", "critique"): {"status": "done", "result": critique_result},
        ("pp_harness", "record_verdict"): {"status": "done", "result": {}},
        ("pp_harness", "finalize_stage"): {"status": "done", "result": {}},
        ("pp_harness", "finalize_run"): {"status": "done", "result": {"status": "complete"}},
    }


def _charge_drive_loop_and_get_state(out: dict) -> HydraState:
    from hydra_core.supervisor import _extract_squad_cost

    result = SquadResult(
        envelopes=[], status="running",
        artifacts=[{"kind": "pp_run", "ref": "r1",
                    "raw": {"status": "done", "result": {"run_id": "r1"}},
                    "drive_loop": out}],
    )
    usd, tokens, source = _extract_squad_cost(result)
    state = HydraState(root_goal="x", budget=BudgetLedger(budget_usd=10.0, spent_usd=0.0))
    charge_and_gate(state, usd, tokens, source=source)
    return state


def test_claude_cli_absent_cost_recorded_unmeasured_end_to_end(monkeypatch):
    """END TO END through the real drive loop: a Claude CLI generate call
    with an ABSENT cost is recorded unmeasured all the way to the LEDGER,
    not merely in the parser's return value. The critique's own cost is
    ALSO omitted here so the stage-level label reflects this fix, rather
    than being correctly overridden to "measured" by an unrelated real
    critique cost (Finding 6's "any real money in the stage stays
    measured" rule -- a different, already-covered case)."""
    monkeypatch.setattr("hydra_core.squad_node._claude_cli_generation_enabled",
                        lambda _d: True)
    monkeypatch.setattr(
        "hydra_core.squad_node._run_claude_cli",
        lambda prompt, *, cwd: _parse_claude_cli_result(
            json.dumps({"result": "edited foo.py\n{\"status\": \"pass\", \"reason\": \"ok\"}"}),
            "", 0, "claude-opus-4-8",
        ),
    )
    monkeypatch.setattr("hydra_core.squad_node._run_smoke",
                        lambda *_a, **_k: ("pass", "stub smoke pass"))
    disp = _ScriptedDispatcherForCost(
        _claude_gen_responses(gen_result={}, critique_cost_usd=None))
    out = _drive_pp_stage_loop(
        disp, run_id="run_T", project_path="/tmp/proj", request_text="do the thing")

    state = _charge_drive_loop_and_get_state(out)
    assert state.budget.unmeasured_stages == 1
    assert state.budget.spent_usd == 0.0


def test_claude_cli_rejected_cost_recorded_unmeasured_end_to_end(monkeypatch):
    monkeypatch.setattr("hydra_core.squad_node._claude_cli_generation_enabled",
                        lambda _d: True)
    monkeypatch.setattr(
        "hydra_core.squad_node._run_claude_cli",
        lambda prompt, *, cwd: _parse_claude_cli_result(
            json.dumps({
                "result": "edited foo.py\n{\"status\": \"pass\", \"reason\": \"ok\"}",
                "total_cost_usd": "NaN",
            }),
            "", 0, "claude-opus-4-8",
        ),
    )
    monkeypatch.setattr("hydra_core.squad_node._run_smoke",
                        lambda *_a, **_k: ("pass", "stub smoke pass"))
    disp = _ScriptedDispatcherForCost(
        _claude_gen_responses(gen_result={}, critique_cost_usd=None))
    out = _drive_pp_stage_loop(
        disp, run_id="run_T", project_path="/tmp/proj", request_text="do the thing")

    state = _charge_drive_loop_and_get_state(out)
    assert state.budget.unmeasured_stages == 1
    assert state.budget.spent_usd == 0.0


def test_claude_cli_genuine_affirmative_zero_still_measured_end_to_end(monkeypatch):
    """The over-correction control, end to end: a vendor AFFIRMATIVELY
    reporting `cost_usd: 0.0` must still be measured on the ledger."""
    monkeypatch.setattr("hydra_core.squad_node._claude_cli_generation_enabled",
                        lambda _d: True)
    monkeypatch.setattr(
        "hydra_core.squad_node._run_claude_cli",
        lambda prompt, *, cwd: _parse_claude_cli_result(
            json.dumps({
                "result": "edited foo.py\n{\"status\": \"pass\", \"reason\": \"ok\"}",
                "total_cost_usd": 0.0,
            }),
            "", 0, "claude-opus-4-8",
        ),
    )
    monkeypatch.setattr("hydra_core.squad_node._run_smoke",
                        lambda *_a, **_k: ("pass", "stub smoke pass"))
    disp = _ScriptedDispatcherForCost(_claude_gen_responses(gen_result={}))
    out = _drive_pp_stage_loop(
        disp, run_id="run_T", project_path="/tmp/proj", request_text="do the thing")

    state = _charge_drive_loop_and_get_state(out)
    assert state.budget.unmeasured_stages == 0


def test_claude_cli_ordinary_finite_cost_measured_and_charged_end_to_end(monkeypatch):
    """Control: an ordinary finite Claude CLI cost is measured and charged
    exactly as before."""
    monkeypatch.setattr("hydra_core.squad_node._claude_cli_generation_enabled",
                        lambda _d: True)
    monkeypatch.setattr(
        "hydra_core.squad_node._run_claude_cli",
        lambda prompt, *, cwd: _parse_claude_cli_result(
            json.dumps({
                "result": "edited foo.py\n{\"status\": \"pass\", \"reason\": \"ok\"}",
                "total_cost_usd": 0.11,
            }),
            "", 0, "claude-opus-4-8",
        ),
    )
    monkeypatch.setattr("hydra_core.squad_node._run_smoke",
                        lambda *_a, **_k: ("pass", "stub smoke pass"))
    disp = _ScriptedDispatcherForCost(_claude_gen_responses(gen_result={}))
    out = _drive_pp_stage_loop(
        disp, run_id="run_T", project_path="/tmp/proj", request_text="do the thing")

    state = _charge_drive_loop_and_get_state(out)
    assert state.budget.unmeasured_stages == 0
    assert state.budget.spent_usd == pytest.approx(0.11 + 0.03)  # generate + critique
