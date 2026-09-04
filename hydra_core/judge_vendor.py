"""Shared cross-vendor judge-selection helpers.

Extracted from ``host_bridge.py`` (B4) so the headless drive loop
(``squad_node._drive_pp_stage_loop``) and the attended host-bridge state
machine (``host_bridge._drive_attended_stage`` et al.) select the judge
producer through ONE authoritative mapping instead of two copies drifting
apart. ``host_bridge.py`` already imports from ``squad_node.py``, so
``squad_node.py`` importing back from ``host_bridge.py`` would be circular;
this standalone module has no dependency on either and both import from here.
"""

from __future__ import annotations

from typing import Any, Sequence

# LV-3 same-vendor-host label suffix: a same-vendor judge (allowed only when
# cross-vendor was NOT required) is tagged with this suffix so downstream
# consumers can distinguish it from a genuine cross-vendor result.
_SAME_VENDOR_HOST_SUFFIX = "-same-vendor-host"

# B9 PART 2 — OPERATOR DECISION, INTENTIONAL DIVERGENCE FROM pp.
#
# pp's own authoritative mapping (`.claude/agents/judge-cross-vendor.md`
# "Cross-vendor mapping") prefers agy for security/spec gates and codex for
# contract/architecture gates. The Hydra operator has deliberately chosen the
# OPPOSITE tiebreak for a claude generator here -- this is NOT accidental
# drift from pp's spec, it is a considered override:
#
#     gate_type   pp's own preference   Hydra's claude-generator tiebreak
#     ---------   -------------------   ----------------------------------
#     security    agy                   codex
#     spec        agy                   codex
#     contract    codex                 codex   (unchanged)
#     design      codex                 agy     (Hydra's architecture gate;
#                                                 pp has no separate
#                                                 "architecture" enum value --
#                                                 see squad_node
#                                                 ._PP_GATE_TYPE_BY_KIND's
#                                                 "architecture" -> "design")
#     other       n/a                   pp's own preferred_producers order,
#                 (code_style, docs_polish, lint_class, unknown/None)
#
# See `_judge_vendor_chain`'s docstring below for the full mapping table.
_CODEX_PREFERRED_GATE_TYPES = frozenset({"security", "spec", "contract"})
_AGY_PREFERRED_GATE_TYPES = frozenset({"design"})


def _base_judge_vendor(producer: Any) -> str:
    """Strip the LV-3 ``-same-vendor-host`` label suffix off a judge producer."""
    p = str(producer or "")
    if p.endswith(_SAME_VENDOR_HOST_SUFFIX):
        return p[: -len(_SAME_VENDOR_HOST_SUFFIX)]
    return p


def _judge_vendor_chain(generator_producer: Any, gate_type: str | None,
                        pp_preferred_producers: Sequence[Any] | None) -> list[str]:
    """Ordered cross-vendor judge producers to try.

    ``generator=codex``/``generator=agy`` follow pp's own authoritative
    mapping (``.claude/agents/judge-cross-vendor.md`` "Cross-vendor mapping"):
    the OTHER vendor always judges. For ``generator=claude`` the tiebreak
    between agy and codex is gate-type-driven -- and here Hydra INTENTIONALLY
    DIVERGES from pp's own mapping by explicit operator decision (B9 PART 2),
    not accidental drift:

        generator=codex  -> agy
        generator=agy    -> codex
        generator=claude -> gate_type security/spec/contract -> codex
                             gate_type design                 -> agy
                             anything else (code_style, docs_polish,
                             lint_class, unknown/None) -> pp's own
                             preferred_producers order, then the defensive
                             other-lane append

    pp's OWN preference (for reference, NOT followed here for claude
    generators) is agy for security/spec and codex for contract/architecture
    -- exactly inverted. See the module-level comment above
    ``_CODEX_PREFERRED_GATE_TYPES`` / ``_AGY_PREFERRED_GATE_TYPES`` for the
    full table and rationale.

    B2: pp's ``gate_eligible_judges`` returns ``preferred_producers`` in pool
    order (``[codex, agy, claude]`` filtered to other vendors), so naively
    taking its first entry always picks codex and agy would never be selected
    for a claude generator on a security/spec gate. This picks the mapping's
    vendor FIRST, then appends the rest of pp's own preferred_producers (for
    availability fallback / engine-side failover) with the primary and any
    duplicates removed, so the return value is always usable as a fallback
    chain even when the caller doesn't need to fail over.
    """
    generator_vendor = _base_judge_vendor(generator_producer)
    pp_pref = [str(p) for p in (pp_preferred_producers or []) if p]
    if generator_vendor == "codex":
        primary = "agy"
    elif generator_vendor == "agy":
        primary = "codex"
    else:
        # claude (or any other same-side generator): EITHER vendor is valid;
        # break the tie on the gate's shape, defaulting to pp's own ordering
        # when the gate type carries no security/spec/contract/design signal.
        # B9 PART 2: this INVERTS pp's own mapping by operator decision --
        # see the module-level comment and this function's docstring.
        if gate_type in _CODEX_PREFERRED_GATE_TYPES:
            primary = "codex"
        elif gate_type in _AGY_PREFERRED_GATE_TYPES:
            primary = "agy"
        else:
            # pp's own first cross-vendor pick -- but a malformed/legacy
            # response could (in principle) still list the generator's own
            # vendor, so this must be guarded exactly like the loop below;
            # skip same-vendor entries when picking pp's first choice.
            _pp_pref_other = [p for p in pp_pref if p != generator_vendor]
            primary = _pp_pref_other[0] if _pp_pref_other else "codex"
    chain = [primary]
    # A malformed/legacy gate_eligible_judges response could list the
    # generator's own vendor in preferred_producers (pp's real response never
    # does -- gates.ts:499 filters it via sameVendorAs -- but this function
    # must not trust that). Guard here exactly as the defensive loop below
    # does, so a same-vendor lane can never land in the failover chain.
    for p in pp_pref:
        if p != generator_vendor and p not in chain:
            chain.append(p)
    # Defensive: always offer the other cross-vendor lane as a failover target
    # even if pp's preferred_producers omitted it (e.g. the fake/legacy
    # gate_eligible_judges response used by most tests carries no
    # allowed_judges at all).
    for p in ("codex", "agy"):
        if p != generator_vendor and p not in chain:
            chain.append(p)
    return chain
