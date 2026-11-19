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


def validate_plan_heads(
    plan: Plan,
    *,
    packs: Mapping[str, SquadPack] | None = None,
    discover: Callable[[], Mapping[str, SquadPack]] | None = None,
    resolver: Callable[..., HeadResolution] | None = None,
) -> list[dict[str, str]]:
    """Return one ``{"field", "msg"}`` error per head fault in ``plan``."""
    try:
        return _validate(plan, packs, discover, resolver)
    except Exception as exc:  # fail closed; the contract is "never raises"
        return [{
            "field": "steps",
            "msg": ("plan heads could not be checked: head validation failed "
                    f"({_exc_text(exc)})"),
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
            for head in dict.fromkeys(step.heads):
                errors.append(_err(i, (
                    f"step '{step.step_id}' head '{head}' could not be checked: "
                    f"squad pack discovery failed ({discovery_error})")))
        return errors
    assert packs is not None

    resolve = resolver if resolver is not None else head_registry.resolve_head
    for i, step in stepped:
        sid, squad = step.step_id, step.target_squad
        distinct = list(dict.fromkeys(step.heads))
        if squad == "planning":
            for head in distinct:
                errors.append(_err(i, (
                    f"step '{sid}' names head '{head}' but squad 'planning' "
                    "cannot run head legs")))
            continue
        pack = packs.get(squad)
        entrypoint = getattr(pack, "entrypoint", None) if pack is not None else None
        if pack is None or entrypoint != _NATIVE:
            for head in distinct:
                errors.append(_err(i, (
                    f"step '{sid}' names head '{head}' but squad '{squad}' "
                    f"(entrypoint '{entrypoint or 'missing'}') cannot run head legs")))
            continue
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
    return errors
