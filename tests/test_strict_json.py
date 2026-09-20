"""Unit tests for the shared strict-JSON helper (`hydra_core.strict_json`).

Cross-vendor judge finding (b1baf30 revise round, item 1b/5): both the plan
artifact writer and the judge dispatcher route envelope/plan-to-JSON-text
through this one seam. These tests pin its two properties directly: the
walker finds a non-finite float anywhere in a nested payload (including
inside a tuple, and past ordinary recursion-limit depths), and `dumps_strict`
always refuses rather than emitting a bare NaN/Infinity token.
"""
from __future__ import annotations

import json
import sys
import time

from hydra_core.strict_json import (
    dumps_strict,
    dumps_tool_response_safe,
    find_non_finite_field,
    sanitize_non_finite,
)


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


# --------------------------------------------------------------------------- #
# item 5 LOW (this round): non-finite float DICT KEYS are examined too
# --------------------------------------------------------------------------- #

def test_find_non_finite_field_names_a_nan_dict_key():
    """A NaN dict key is rejected by `json.dumps(..., allow_nan=False)`
    exactly like a NaN value, but the walker previously only ever descended
    into VALUES, so this reported `<unknown field>` instead of naming the
    key."""
    field = find_non_finite_field({float("nan"): "x"})
    assert field is not None
    assert "<key:" in field
    assert "nan" in field.lower()


def test_dumps_strict_names_a_nan_dict_key_not_unknown_field():
    try:
        dumps_strict({float("nan"): "x"}, label="widget")
        assert False, "expected ValueError"
    except ValueError as exc:
        assert "<unknown field>" not in str(exc)
        assert "<key:" in str(exc)


# --------------------------------------------------------------------------- #
# item 4 MEDIUM (this round): dumps_tool_response_safe handles TypeError too,
# and every substitution (non-finite float OR unsupported object) is marked
# --------------------------------------------------------------------------- #

class _Unsupported:
    """A plain object `json.dumps` cannot encode without a `default=`."""

    def __repr__(self) -> str:
        return "<Unsupported obj>"


def test_dumps_tool_response_safe_handles_unsupported_object():
    payload = {"a": 1, "b": _Unsupported()}
    text = dumps_tool_response_safe(payload)
    # Must produce valid JSON (never raise).
    parsed = json.loads(text)
    assert parsed["a"] == 1
    assert isinstance(parsed["b"], str)
    assert parsed["_non_finite_fields_sanitized"], (
        "the unsupported-object substitution must be recorded, not silently stringified"
    )
    assert any("$.b" in f for f in parsed["_non_finite_fields_sanitized"])


def test_dumps_tool_response_safe_reports_every_substitution_not_just_first():
    """A payload with BOTH a non-finite float AND an unsupported object must
    report BOTH substitutions -- the previous implementation reported the
    float (via `sanitize_non_finite`) but silently re-stringified the
    unsupported object with `default=str` and no marker."""
    payload = {"nan_field": float("nan"), "obj_field": _Unsupported()}
    text = dumps_tool_response_safe(payload)
    parsed = json.loads(text)
    assert parsed["nan_field"] is None
    assert isinstance(parsed["obj_field"], str)
    fields = parsed["_non_finite_fields_sanitized"]
    assert any("nan_field" in f for f in fields)
    assert any("obj_field" in f for f in fields)


def test_dumps_tool_response_safe_still_handles_plain_non_finite():
    payload = {"budget": float("inf")}
    text = dumps_tool_response_safe(payload)
    parsed = json.loads(text)
    assert parsed["budget"] is None
    assert parsed["_non_finite_fields_sanitized"] == ["$.budget"]


def test_dumps_tool_response_safe_ordinary_payload_unaffected():
    text = dumps_tool_response_safe({"a": 1, "b": "x"})
    assert json.loads(text) == {"a": 1, "b": "x"}


def test_dumps_tool_response_safe_keeps_valid_scalars_intact_once_triggered():
    """Once ANY field triggers fallback sanitization, every OTHER already
    valid, natively JSON-representable scalar (finite float, int, bool,
    str, None) must round-trip unchanged -- not get needlessly stringified
    as if it were an unsupported object."""
    payload = {
        "bad": float("nan"),
        "cost": 1.5,
        "count": 3,
        "ok": True,
        "name": "engineer",
        "note": None,
    }
    text = dumps_tool_response_safe(payload)
    parsed = json.loads(text)
    assert parsed["bad"] is None
    assert parsed["cost"] == 1.5 and isinstance(parsed["cost"], float)
    assert parsed["count"] == 3 and isinstance(parsed["count"], int)
    assert parsed["ok"] is True
    assert parsed["name"] == "engineer"
    assert parsed["note"] is None
    fields = parsed["_non_finite_fields_sanitized"]
    assert any("bad" in f for f in fields)
    assert not any("cost" in f for f in fields)


def test_sanitize_non_finite_names_a_nan_dict_key():
    result, fields = sanitize_non_finite({float("nan"): "x"})
    assert "null" in result
    assert any("<key:" in f for f in fields)


# ---------------------------------------------------------------------------
# Cross-vendor judge finding (this round, item 2 MEDIUM): key collisions,
# marker overwrite, and cyclic transport input.
# ---------------------------------------------------------------------------

def test_sanitize_non_finite_key_collision_keeps_both_values_distinct():
    """A genuine `"null"` key and a non-finite-float key that would
    naively map to the same replacement key must both survive, distinctly."""
    payload = {"null": "real", float("nan"): "replacement"}
    result, fields = sanitize_non_finite(payload)
    assert result["null"] == "real", "the caller's genuine 'null' value must not be lost"
    assert "real" in result.values()
    assert "replacement" in result.values()
    # Exactly two entries -- no value silently dropped by a key collision.
    assert len(result) == 2
    assert any("<key:" in f for f in fields)


def test_sanitize_non_finite_key_collision_none_vs_nan_survives_json_roundtrip():
    """Cross-vendor judge finding (this round, item 1 MEDIUM): uniqueness
    must be checked against the JSON MEMBER NAME a key serializes to, not
    the Python key object. `None` -> `"null"`, so a dict with BOTH a
    genuine `None` key and a NaN key (which also sanitizes toward `"null"`)
    must keep both values distinct through an actual `json.dumps`/
    `json.loads` round trip -- not just as a Python dict in memory."""
    payload = {None: "original", float("nan"): "replacement"}
    result, fields = sanitize_non_finite(payload)
    assert len(result) == 2, "both values must survive sanitization itself"
    text = json.dumps(result)
    parsed = json.loads(text)
    assert len(parsed) == 2, (
        "sanitized keys must not collide once serialized to real JSON "
        "member names -- a naive fix can pass in-memory but still emit "
        "two members named \"null\""
    )
    assert "original" in parsed.values()
    assert "replacement" in parsed.values()
    assert any("<key:" in f for f in fields)


def test_sanitize_non_finite_key_collision_true_vs_string_true():
    """`True` serializes to the JSON member name `"true"`; a dict with both
    a genuine `True` key and a literal `"true"` string key must keep both
    values distinct through a real JSON round trip."""
    payload = {True: "bool-key", "true": "string-key"}
    result, fields = sanitize_non_finite(payload)
    assert len(result) == 2
    parsed = json.loads(json.dumps(result))
    assert len(parsed) == 2, "True and \"true\" must not collide once serialized"
    assert "bool-key" in parsed.values()
    assert "string-key" in parsed.values()


def test_sanitize_non_finite_key_collision_int_vs_string_digit():
    """The int key `3` serializes to the JSON member name `"3"`; a dict
    with both `3` and the literal string `"3"` as keys must keep both
    values distinct through a real JSON round trip."""
    payload = {3: "int-key", "3": "string-key"}
    result, fields = sanitize_non_finite(payload)
    assert len(result) == 2
    parsed = json.loads(json.dumps(result))
    assert len(parsed) == 2, "3 and \"3\" must not collide once serialized"
    assert "int-key" in parsed.values()
    assert "string-key" in parsed.values()


def test_dumps_tool_response_safe_marker_does_not_overwrite_caller_field():
    """A caller-authored `_non_finite_fields_sanitized` field must survive
    untouched; the sanitizer's own marker lands under a different name."""
    payload = {
        "budget": float("nan"),
        "_non_finite_fields_sanitized": "caller-owned-value",
    }
    text = dumps_tool_response_safe(payload)
    parsed = json.loads(text)
    assert parsed["_non_finite_fields_sanitized"] == "caller-owned-value", (
        "the sanitizer must never clobber the caller's own field of the same name"
    )
    # The sanitizer's own marker still names the sanitized field, just under
    # a non-colliding key.
    marker_keys = [k for k in parsed if k != "_non_finite_fields_sanitized" and "sanitized" in k]
    assert marker_keys, "expected a fallback marker key naming the sanitized field"
    assert any("$.budget" in f for f in parsed[marker_keys[0]])


def test_dumps_tool_response_safe_cyclic_payload_returns_marker_not_raise():
    """`dumps_tool_response_safe` promises to NEVER raise on the transport
    path -- a cyclic payload (which trips `dumps_strict`'s ValueError, then
    used to come back out of `sanitize_non_finite` unchanged and blow up the
    fallback `json.dumps` call) must instead produce valid JSON with an
    explicit circular-reference marker."""
    cyclic: dict[str, object] = {"a": 1}
    cyclic["self"] = cyclic
    text = dumps_tool_response_safe(cyclic, label="cyclic-tool-response")
    parsed = json.loads(text)  # must not raise
    assert parsed["a"] == 1
    assert parsed["self"] == "<circular reference>"


def test_sanitize_non_finite_cyclic_dict_returns_marker_directly():
    cyclic: dict[str, object] = {"a": 1}
    cyclic["self"] = cyclic
    result, fields = sanitize_non_finite(cyclic)
    assert result["self"] == "<circular reference>"
    assert any("circular reference" in f for f in fields)
    json.dumps(result)  # must not raise


def test_dumps_tool_response_safe_deep_nesting_returns_marker_not_raise():
    """Cross-vendor judge finding (this round, MEDIUM):
    `dumps_tool_response_safe` promises to NEVER raise, but its initial
    `dumps_strict` attempt can hit `RecursionError` on a payload nested far
    past any reasonable depth (CPython's json encoder recurses per
    container level), and the fallback `sanitize_non_finite` walker is
    itself recursive so it could raise the same error while trying to
    recover. Both must be handled: the deep payload must still come back as
    valid JSON, with an explicit depth-exceeded marker instead of a crash."""
    # CPython's C-accelerated json encoder tolerates far deeper nesting than
    # `sys.getrecursionlimit()` before it actually raises `RecursionError`
    # (it trips its own C-stack-depth check, not the Python frame counter) --
    # go deep enough to reliably reproduce that failure mode.
    depth = 20000
    payload: object = {"leaf": 1}
    for _ in range(depth):
        payload = {"n": payload}
    text = dumps_tool_response_safe({"root": payload}, label="deep-tool-response")
    parsed = json.loads(text)  # must not raise
    marker_keys = [k for k in parsed if "sanitized" in k]
    assert marker_keys, "expected a marker key recording the depth substitution"
    assert any("max depth" in f for f in parsed[marker_keys[0]])


def test_dumps_tool_response_safe_ordinary_nesting_unaffected():
    """Control for the depth guard above: ordinary, non-adversarial nesting
    (well under the depth bound) must serialize normally, unchanged and
    without any sanitization marker."""
    payload = {"a": {"b": {"c": [1, 2, {"d": "leaf", "e": 3.5}]}}}
    text = dumps_tool_response_safe(payload, label="ordinary-tool-response")
    parsed = json.loads(text)
    assert parsed == payload
    assert not any("sanitized" in k for k in parsed)
