"""Part 5 — transcript_cost.summarize_transcript_cost.

Sums + prices per-turn ``message.usage`` from a JSONL subagent transcript.
Hermetic: writes only to tmp_path, no network.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from hydra_core.transcript_cost import summarize_transcript_cost


def _write_transcript(path: Path, lines: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(l) for l in lines), encoding="utf-8")


def test_sums_and_prices_known_model_turns(tmp_path: Path) -> None:
    path = tmp_path / "transcript.jsonl"
    _write_transcript(path, [
        {"message": {"model": "claude-sonnet-5",
                     "usage": {"input_tokens": 500_000, "output_tokens": 500_000}}},
        {"message": {"model": "claude-sonnet-5",
                     "usage": {"input_tokens": 500_000, "output_tokens": 500_000}}},
    ])
    summary = summarize_transcript_cost(path)
    assert summary.turns == 2
    assert summary.tokens_in == 1_000_000
    assert summary.tokens_out == 1_000_000
    # Sonnet: $3/Mtok in + $15/Mtok out => $18.0 total across both turns.
    assert summary.cost_usd == pytest.approx(18.0)
    assert summary.fully_priced is True


def test_unknown_model_tracked_as_unpriced_not_raised(tmp_path: Path) -> None:
    # FALSIFIABILITY: without the `if priced is None: unpriced_models.add(...)`
    # branch, an unknown model would either raise (price_call returning None
    # and the caller doing `summary.cost_usd += None`) or silently drop the
    # signal that the total is partial.
    path = tmp_path / "transcript.jsonl"
    _write_transcript(path, [
        {"message": {"model": "unknown-model-abc",
                     "usage": {"input_tokens": 1000, "output_tokens": 1000}}},
    ])
    summary = summarize_transcript_cost(path)
    assert summary.turns == 1
    assert summary.cost_usd == pytest.approx(0.0)
    assert "unknown-model-abc" in summary.unpriced_models
    assert summary.fully_priced is False


def test_malformed_lines_are_skipped_not_raised(tmp_path: Path) -> None:
    path = tmp_path / "transcript.jsonl"
    path.write_text(
        "not json\n"
        + json.dumps({"message": {"model": "claude-sonnet-5",
                                   "usage": {"input_tokens": 1000, "output_tokens": 1000}}})
        + "\n{}\n",
        encoding="utf-8",
    )
    summary = summarize_transcript_cost(path)
    assert summary.turns == 1


def test_missing_file_yields_zeroed_summary(tmp_path: Path) -> None:
    summary = summarize_transcript_cost(tmp_path / "does-not-exist.jsonl")
    assert summary.turns == 0
    assert summary.cost_usd == 0.0


def test_non_integer_token_values_do_not_raise(tmp_path: Path) -> None:
    # FALSIFIABILITY: the pre-fix code did
    # `int(usage.get("input_tokens") or 0)` with no type guard. A
    # syntactically valid JSONL line whose token value is a string or a
    # nested object raises TypeError/ValueError out of that bare `int(...)`
    # call, contradicting this module's own docstring claim ("a corrupt or
    # partial transcript must not crash the caller"). This line is valid
    # JSON end to end — only the *value* of a token field is malformed.
    path = tmp_path / "transcript.jsonl"
    _write_transcript(path, [
        # input_tokens is a numeric string -> should coerce to 500_000.
        {"message": {"model": "claude-sonnet-5",
                     "usage": {"input_tokens": "500000", "output_tokens": 500_000}}},
        # output_tokens is a nested object -> unparseable, contributes 0.
        {"message": {"model": "claude-sonnet-5",
                     "usage": {"input_tokens": 500_000,
                               "output_tokens": {"unexpected": "shape"}}}},
    ])
    summary = summarize_transcript_cost(path)  # must not raise
    assert summary.turns == 2
    assert summary.tokens_in == 1_000_000
    assert summary.tokens_out == 500_000
    assert summary.cost_usd == pytest.approx((3.0 * 1.0) + (15.0 * 0.5))
