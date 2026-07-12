"""Deferred-hardening: same-or-higher-tier rule + vendor_pairs policy parsing
(task #7) and the quota-failure tag (task #8)."""
from __future__ import annotations

from hydra_core.tiers import tier_rank, is_same_or_higher_tier
from hydra_core.judge.policy import JudgePolicy, load_policy
from hydra_core.squad_node import _generate_failure_reason


# --- tier ordering ---------------------------------------------------------- #
def test_tier_rank_ordering():
    assert tier_rank("haiku") < tier_rank("sonnet") < tier_rank("opus") < tier_rank("fable")
    assert tier_rank("deep") == tier_rank("fable")
    assert tier_rank(None) is None
    assert tier_rank("nonsense") is None


def test_same_or_higher_tier():
    assert is_same_or_higher_tier("opus", "sonnet") is True      # higher judge ok
    assert is_same_or_higher_tier("opus", "opus") is True        # same ok
    assert is_same_or_higher_tier("haiku", "opus") is False      # weaker judge NOT ok
    # unknown/None → permissive (can't prove a violation)
    assert is_same_or_higher_tier(None, "opus") is True
    assert is_same_or_higher_tier("opus", None) is True


# --- vendor_pairs policy ---------------------------------------------------- #
def test_vendor_pair_allowed_empty_is_permissive():
    p = JudgePolicy()
    assert p.vendor_pair_allowed("claude", "codex") is True


def test_vendor_pair_allowed_enforced_when_configured():
    p = JudgePolicy(vendor_pairs=frozenset({("claude", "codex")}))
    assert p.vendor_pair_allowed("claude", "codex") is True
    assert p.vendor_pair_allowed("codex", "agy") is False


def test_cross_vendor_distinct():
    assert JudgePolicy.cross_vendor_distinct("claude", "codex") is True
    assert JudgePolicy.cross_vendor_distinct("codex", "codex") is False


def test_same_vendor_tier_ok_delegates():
    assert JudgePolicy.same_vendor_tier_ok("opus", "sonnet") is True
    assert JudgePolicy.same_vendor_tier_ok("haiku", "opus") is False


def test_load_policy_parses_vendor_pairs():
    # The packaged policy.yaml declares (claude,codex) + (codex,codex) post-retire.
    p = load_policy()
    assert ("claude", "codex") in p.vendor_pairs
    assert p.vendor_pair_allowed("claude", "codex") is True


# --- quota tag -------------------------------------------------------------- #
def test_generate_failure_tags_quota_distinctly():
    reason = _generate_failure_reason(
        {"status": "done", "result": {}},
        "Error: You've hit your usage limit. Try again later.",
        wrote_changes=False,
    )
    assert reason is not None
    assert "quota exhausted" in reason.lower()


def test_generate_failure_non_quota_block_not_tagged_quota():
    reason = _generate_failure_reason(
        {"status": "done", "result": {}},
        "writing is blocked by read-only sandbox",
        wrote_changes=False,
    )
    assert reason is not None
    assert "quota" not in reason.lower()
    assert "blocked" in reason.lower()


# --- generator-vendor provenance resolver ----------------------------------- #
def test_resolve_generator_vendor():
    from hydra_core.supervisor import _resolve_generator_vendor
    # Host-produced squad envelopes default to claude (NOT the squad slug).
    assert _resolve_generator_vendor({"origin_squad": "executive"}) == "claude"
    # An explicit generator_vendor (e.g. engineering drive loop) wins.
    assert _resolve_generator_vendor({"generator_vendor": "codex"}) == "codex"
    assert _resolve_generator_vendor({"generator_vendor": "  Claude "}) == "claude"
