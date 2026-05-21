"""Regression tests for the preemptive envelope_ceiling and mcp_failure_ceiling
governance gates.

These ceilings exist because a single Claude Code sub-agent supervisor turn
shares one context window across intake/planning/dispatch/judging/synthesis.
Empirically a 9-envelope Phase-3 dispatch consumed 91 tool uses and died with
zero commits — see plan
`C:\\Users\\robob\\.claude\\plans\\what-happened-in-this-stateful-curry.md`.

No network, no LLMs.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from hydra_core.governance import enforce_governance
from hydra_core.squad_loader import discover_squads
from hydra_core.state import HydraState


HYDRA_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def packs():
    return discover_squads(HYDRA_ROOT)


def _state() -> HydraState:
    # Tight ceilings so the test is fast + readable.
    return HydraState(
        root_goal="test",
        envelope_ceiling=5,
        mcp_failure_ceiling=2,
    )


def test_envelope_ceiling_not_tripped_under_cap(packs):
    s = _state()
    s.envelopes.extend({"i": i} for i in range(3))
    verdict = enforce_governance(s, packs)
    assert verdict.surfaced is False
    assert "envelope_ceiling" not in verdict.reason


def test_envelope_ceiling_surfaces_at_cap(packs):
    s = _state()
    s.envelopes.extend({"i": i} for i in range(5))
    assert s.is_over_envelope_ceiling() is True
    verdict = enforce_governance(s, packs)
    assert verdict.surfaced is True
    assert verdict.reason.startswith("envelope_ceiling")
    assert "envelopes=5" in verdict.reason
    assert "ceiling=5" in verdict.reason


def test_envelope_ceiling_surfaces_over_cap(packs):
    s = _state()
    s.envelopes.extend({"i": i} for i in range(12))
    verdict = enforce_governance(s, packs)
    assert verdict.surfaced is True
    assert verdict.reason.startswith("envelope_ceiling")


def test_mcp_failure_ceiling_not_tripped_under_threshold(packs):
    s = _state()
    s.error_counters["mcp_failure:executive-suite"] = 1
    verdict = enforce_governance(s, packs)
    assert verdict.surfaced is False


def test_mcp_failure_ceiling_surfaces_at_threshold(packs):
    s = _state()
    s.error_counters["mcp_failure:executive-suite"] = 2
    tripped, server = s.any_mcp_over_ceiling()
    assert tripped is True
    assert server == "executive-suite"
    verdict = enforce_governance(s, packs)
    assert verdict.surfaced is True
    assert verdict.reason.startswith("mcp_disconnect:executive-suite")
    assert "failures=2" in verdict.reason
    assert "ceiling=2" in verdict.reason


def test_mcp_failure_ceiling_identifies_correct_server(packs):
    s = _state()
    s.error_counters["mcp_failure:eights"] = 5
    verdict = enforce_governance(s, packs)
    assert verdict.surfaced is True
    assert "mcp_disconnect:eights" in verdict.reason


def test_envelope_ceiling_precedes_budget_check(packs):
    """Envelope ceiling must surface before the budget gate so the operator
    sees the structural problem rather than a derived dollar overshoot."""
    s = _state()
    s.envelopes.extend({"i": i} for i in range(5))
    s.budget.spent_usd = 9_999.0
    verdict = enforce_governance(s, packs)
    assert verdict.reason.startswith("envelope_ceiling")
