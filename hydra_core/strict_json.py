"""Shared strict-JSON serialization helper.

Cross-vendor judge finding (b1baf30 revise round, item 1/4): more than one
site turns an envelope/plan into JSON text for a downstream consumer —
``plan_artifact.render_plan_json`` (AgentSmith's future ``checkPlan``
validator) and ``judge.dispatcher._envelope_to_text`` (the cross-vendor
critique prompt). Both need the same guarantee: the emitted text is strict
RFC 8259 JSON, never the bare ``NaN``/``Infinity``/``-Infinity`` tokens
Python's ``json`` module accepts by default but which downstream JSON
parsers reject. Two independent ``allow_nan=False`` call sites would be two
independent places to regress (and, before this module existed, one of them
-- the judge dispatcher -- had regressed: it used the module default
``allow_nan=True``). This is the one seam both route through.
"""
from __future__ import annotations

import json
from typing import Any


def find_non_finite_field(obj: Any, path: str = "$") -> str | None:
    """Iterative depth-first search for the first non-finite float in ``obj``.

    Returns a dotted/bracketed path string (e.g. ``"$.steps[2].estimated_
    budget_usd"``) naming the offending field, or ``None`` if every float in
    ``obj`` is finite. Used only to build a clear error message after
    ``json.dumps(..., allow_nan=False)`` has already raised ``ValueError`` --
    the caller needs to say WHICH field broke strict JSON, not just that
    something did.

    Iterative (an explicit stack, not recursion) so a deeply nested payload
    cannot hit Python's recursion limit, and traverses tuples as well as
    lists (a tuple is JSON-serializable and `json.dumps` treats it exactly
    like a list). Path segments are accumulated as a tuple of pre-rendered
    fragments and joined once at the end, so building the path costs O(depth)
    per node rather than reallocating a growing string at every level.
    """
    stack: list[tuple[Any, tuple[str, ...]]] = [(obj, (path,))]
    while stack:
        current, parts = stack.pop()
        if isinstance(current, float) and (
            current != current or current in (float("inf"), float("-inf"))
        ):
            return "".join(parts)
        if isinstance(current, dict):
            for key, value in current.items():
                stack.append((value, parts + (f".{key}",)))
        elif isinstance(current, (list, tuple)):
            for i, value in enumerate(current):
                stack.append((value, parts + (f"[{i}]",)))
    return None


def dumps_strict(payload: Any, *, label: str = "payload", **kwargs: Any) -> str:
    """``json.dumps`` that refuses to emit non-finite floats.

    Forces ``allow_nan=False`` regardless of what the caller passes, so a
    ``NaN``/``Infinity``/``-Infinity`` anywhere in ``payload`` raises
    ``ValueError`` naming the offending field (via ``find_non_finite_field``)
    instead of silently writing the bare token -- valid Python-``json``
    output, invalid RFC 8259 JSON that a strict downstream parser (or
    AgentSmith's ``checkPlan``) refuses.

    ``label`` identifies the payload in the raised error (e.g. an envelope
    id) so the message is actionable without the caller re-deriving it.
    """
    kwargs["allow_nan"] = False
    try:
        return json.dumps(payload, **kwargs)
    except ValueError as exc:
        field = find_non_finite_field(payload)
        raise ValueError(
            f"{label} contains a non-finite value at {field or '<unknown field>'}; "
            "refusing to write invalid JSON"
        ) from exc
