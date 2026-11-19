"""Hydra#91: validate ``PlanStep.heads`` against the squad registry at ingest.

``validate_plan_heads`` is pure and NEVER raises (``BaseException`` that is not
an ``Exception`` -- ``KeyboardInterrupt`` / ``SystemExit`` -- propagates). It
returns the same ``{"field", "msg"}`` shape ``ingest``'s PLAN branch already
uses for its ``_plan_validation_errors``; an empty list means "accept".

Fail closed: a fault in discovery or in the resolver is reported as an error
for the heads it prevented checking, never silently accepted.
"""
from __future__ import annotations

from typing import Any, Callable, Mapping

from . import head_registry
from .head_registry import HeadResolution
from .schemas import Plan
from .squad_loader import SquadPack, discover_squads
from .state import _check_head_slug

_NATIVE = "claude-native"


def _exc_text(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {str(exc)[:200]}"


def _default_discover() -> Mapping[str, SquadPack]:
    # Same source head_registry.resolve_head itself falls back to.
    return discover_squads(head_registry._repo_root())


def _err(i: int, msg: str) -> dict[str, str]:
    return {"field": f"steps[{i}].heads", "msg": msg}


def _attributed(i: int, step: Any, reason: str) -> list[dict[str, str]]:
    """One error per distinct head of ``step``, all with ``reason``."""
    sid = getattr(step, "step_id", "?")
    return [
        _err(i, f"step '{sid}' head '{head}' could not be checked: {reason}")
        for head in dict.fromkeys(step.heads)
    ]


def validate_plan_heads(
    plan: Plan,
    *,
    packs: Mapping[str, SquadPack] | None = None,
    discover: Callable[[], Mapping[str, SquadPack]] | None = None,
    resolver: Callable[..., HeadResolution] | None = None,
) -> list[dict[str, str]]:
    """Return one ``{"field", "msg"}`` error per head fault in ``plan``.

    Attribution: a failure while checking one step is caught per step and
    reported for that step's distinct heads (``steps[<i>].heads``); the other
    steps are still checked. The last-resort outer handler also attributes
    one error per (step index, distinct head). The ONLY unattributed case is
    when ``plan.steps`` itself cannot be enumerated: then a single
    ``{"field": "steps", ...}`` error is returned.
    """
    try:
        return _validate(plan, packs, discover, resolver)
    except Exception as exc:  # fail closed; the contract is "never raises"
        reason = f"head validation failed ({_exc_text(exc)})"
        try:
            steps = list(enumerate(plan.steps))
        except Exception:
            return [{
                "field": "steps",
                "msg": f"plan heads could not be checked: {reason}",
            }]
        errors: list[dict[str, str]] = []
        for i, step in steps:
            try:
                if list(step.heads):
                    errors.extend(_attributed(i, step, reason))
            except Exception:
                errors.append(_err(i, (
                    f"step '{getattr(step, 'step_id', '?')}' heads could not be "
                    f"checked: {reason}")))
        return errors or [{
            "field": "steps",
            "msg": f"plan heads could not be checked: {reason}",
        }]


def _validate(
    plan: Plan,
    packs: Mapping[str, SquadPack] | None,
    discover: Callable[[], Mapping[str, SquadPack]] | None,
    resolver: Callable[..., HeadResolution] | None,
) -> list[dict[str, str]]:
    stepped = [(i, s) for i, s in enumerate(plan.steps) if list(s.heads)]
    if not stepped:
        return []  # head-less plan: no discovery, no resolver

    errors: list[dict[str, str]] = []
    discovery_error: str | None = None
    if packs is None:
        try:
            packs = (discover or _default_discover)()
        except Exception as exc:
            discovery_error = _exc_text(exc)
    if discovery_error is None and not isinstance(packs, Mapping):
        discovery_error = (
            f"TypeError: discovery returned {type(packs).__name__}, not a mapping")
    if discovery_error is not None:
        for i, step in stepped:
            errors.extend(_attributed(
                i, step, f"squad pack discovery failed ({discovery_error})"))
        return errors
    assert packs is not None

    resolve = resolver if resolver is not None else head_registry.resolve_head
    for i, step in stepped:
        try:
            _check_step(i, step, packs, resolve, errors)
        except Exception as exc:  # keep this step's earlier errors, go on
            errors.extend(_attributed(
                i, step, f"head validation failed ({_exc_text(exc)})"))
    return errors


def _check_step(
    i: int,
    step: Any,
    packs: Mapping[str, SquadPack],
    resolve: Callable[..., HeadResolution],
    errors: list[dict[str, str]],
) -> None:
    sid, squad = step.step_id, step.target_squad
    distinct = list(dict.fromkeys(step.heads))
    if squad == "planning":
        for head in distinct:
            errors.append(_err(i, (
                f"step '{sid}' names head '{head}' but squad 'planning' "
                "cannot run head legs")))
        return
    pack = packs.get(squad)
    entrypoint = getattr(pack, "entrypoint", None) if pack is not None else None
    if pack is None or entrypoint != _NATIVE:
        for head in distinct:
            errors.append(_err(i, (
                f"step '{sid}' names head '{head}' but squad '{squad}' "
                f"(entrypoint '{entrypoint or 'missing'}') cannot run head legs")))
        return
    seen: set[str] = set()
    reported_dupes: set[str] = set()
    for head in step.heads:
        if head in seen:
            if head not in reported_dupes:
                reported_dupes.add(head)
                errors.append(_err(i, f"step '{sid}' repeats head '{head}'"))
            continue
        seen.add(head)
        try:
            _check_head_slug(head)
        except ValueError as exc:
            errors.append(_err(i, (
                f"step '{sid}' head '{head}' is not a valid head slug: {exc}")))
            continue
        try:
            resolution: Any = resolve(squad, head, packs=packs)
            rule = resolution.rule
            error_text = getattr(resolution, "error", None)
        except Exception as exc:
            errors.append(_err(i, (
                f"step '{sid}' head '{head}' could not be checked: "
                f"head resolver failed ({_exc_text(exc)})")))
            continue
        if rule == "unresolved":
            errors.append(_err(i, (
                f"step '{sid}' head '{head}' does not resolve for squad "
                f"'{squad}': {error_text}")))
