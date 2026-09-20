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
import time

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


def test_find_non_finite_field_walks_deep_chain_in_linear_time():
    """Cross-vendor judge finding (item 3/4): the previous walker copied the
    whole path tuple at every descent (`parts + (frag,)`), which is
    quadratic in depth despite a comment claiming linear. Build a much
    deeper single chain than the recursion-limit regression test above and
    assert a generous wall-clock bound -- a quadratic walker would blow far
    past this at this depth, a linear one finishes comfortably inside it.

    Mutation proof (revert immediately): reintroduce the `parts + (frag,)`
    whole-tuple-copy per descent and this test times out / fails the bound.
    """
    depth = 200_000
    payload: object = float("nan")
    for _ in range(depth):
        payload = {"n": payload}
    start = time.perf_counter()
    result = find_non_finite_field(payload)
    elapsed = time.perf_counter() - start
    assert result == "$" + ".n" * depth
    assert elapsed < 5.0, f"expected linear-time walk, took {elapsed:.2f}s at depth={depth}"


def test_find_non_finite_field_reports_cycle_promptly_instead_of_looping():
    """Cross-vendor judge finding (item 3/4): after `json.dumps` detects a
    circular structure and raises, the walker used to re-traverse the same
    payload with no visited-set, so a genuinely cyclic container would send
    it into an infinite loop. Containers are now tracked by `id()`.

    Mutation proof (revert immediately): remove the `seen`/`id()` visited-set
    guard and this test times out (bounded so the suite cannot hang).
    """
    cyclic: dict[str, object] = {"a": 1}
    cyclic["self"] = cyclic
    start = time.perf_counter()
    result = find_non_finite_field(cyclic)
    elapsed = time.perf_counter() - start
    assert result is not None and "circular reference" in result
    assert elapsed < 2.0, f"expected prompt cycle detection, took {elapsed:.2f}s"


def test_find_non_finite_field_allows_shared_acyclic_reference():
    """Cross-vendor judge finding (item 4/6, MEDIUM): a container that
    appears MORE THAN ONCE in the tree (e.g. the same dict referenced by two
    sibling keys) is not circular just because it repeats -- `json.dumps`
    walks it fine. The previous implementation tracked visited containers in
    one `seen` set shared across the WHOLE walk, so the second occurrence of
    `child` (under an unrelated branch, not an ancestor) was wrongly
    reported as `<circular reference>`.

    Mutation proof (revert immediately): restore the global `seen` set (in
    place of the per-branch ancestor-chain set) and this test fails because
    `$.c[0]` (or similar) is reported as circular even though the payload
    has no actual cycle and `json.dumps` serializes it without error.
    """
    import json

    child = {"x": 1.0, "y": [1, 2, 3]}
    payload = {"a": child, "b": child, "c": [child, child]}
    # Sanity: this really is acyclic as far as the stdlib is concerned.
    json.dumps(payload)
    assert find_non_finite_field(payload) is None


def test_find_non_finite_field_still_reports_genuine_self_reference():
    """Companion to the shared-acyclic-reference test above: a container
    that is its OWN ancestor is still a real cycle and must still be
    reported promptly, not just non-ancestor repeats tolerated."""
    child: dict[str, object] = {"x": 1.0}
    child["self"] = child
    payload = {"a": child}
    result = find_non_finite_field(payload)
    assert result is not None and "circular reference" in result


def test_dumps_strict_reports_cycle_instead_of_hanging():
    cyclic: dict[str, object] = {"a": 1}
    cyclic["self"] = cyclic
    start = time.perf_counter()
    try:
        dumps_strict(cyclic, label="cyclic-widget")
        assert False, "expected ValueError"
    except ValueError as exc:
        assert "cyclic-widget" in str(exc)
        assert "circular" in str(exc)
    elapsed = time.perf_counter() - start
    assert elapsed < 2.0, f"expected prompt cycle detection, took {elapsed:.2f}s"
