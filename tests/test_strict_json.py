"""Unit tests for the shared strict-JSON helper (`hydra_core.strict_json`).

Cross-vendor judge finding (b1baf30 revise round, item 1b/5): both the plan
artifact writer and the judge dispatcher route envelope/plan-to-JSON-text
through this one seam. These tests pin its two properties directly: the
walker finds a non-finite float anywhere in a nested payload (including
inside a tuple, and past ordinary recursion-limit depths), and `dumps_strict`
always refuses rather than emitting a bare NaN/Infinity token.
"""
from __future__ import annotations

import sys

from hydra_core.strict_json import dumps_strict, find_non_finite_field


def test_find_non_finite_field_in_dict():
    assert find_non_finite_field({"a": {"b": float("nan")}}) == "$.a.b"


def test_find_non_finite_field_in_list():
    assert find_non_finite_field([1, 2, float("inf")]) == "$[2]"


def test_find_non_finite_field_in_tuple():
    """Cross-vendor judge finding (item 5): a tuple is JSON-serializable
    (`json.dumps` treats it exactly like a list) but the pre-fix recursive
    walker only handled `list`, so a non-finite value reachable only through
    a tuple was never named."""
    assert find_non_finite_field({"steps": (1, {"budget": float("-inf")})}) == "$.steps[1].budget"


def test_find_non_finite_field_returns_none_for_all_finite():
    assert find_non_finite_field({"a": [1, 2.5, "x"], "b": (True, None)}) is None


def test_find_non_finite_field_handles_deep_nesting_without_recursion_error():
    """Cross-vendor judge finding (item 5): the walker must be iterative (an
    explicit stack), not recursive, so a payload deeper than Python's default
    recursion limit does not crash with `RecursionError` before it can even
    report which field is bad."""
    depth = sys.getrecursionlimit() + 500
    payload: object = float("nan")
    for _ in range(depth):
        payload = {"n": payload}
    assert find_non_finite_field(payload) == "$" + ".n" * depth


def test_dumps_strict_refuses_non_finite_and_names_field():
    try:
        dumps_strict({"budget": float("nan")}, label="widget")
        assert False, "expected ValueError"
    except ValueError as exc:
        assert "widget" in str(exc)
        assert "$.budget" in str(exc)


def test_dumps_strict_forces_allow_nan_false_even_if_caller_passes_true():
    """Mutation proof (revert immediately): a caller passing `allow_nan=True`
    must NOT be able to defeat the guarantee -- `dumps_strict` always forces
    `allow_nan=False`."""
    try:
        dumps_strict({"budget": float("inf")}, allow_nan=True)
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_dumps_strict_writes_ordinary_finite_payload_unchanged():
    text = dumps_strict({"b": 1.5, "a": 2}, sort_keys=True)
    assert text == '{"a": 2, "b": 1.5}'
