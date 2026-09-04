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

# pp gate types (from pp's strict gate_type enum, see squad_node._PP_GATE_TYPES)
# that count as "security/spec" vs "contract/architecture" for the
# claude-generator tiebreak below. pp's own enum has no separate
# "architecture" value -- an architecture gate is represented as gate_type
# "design" (see squad_node._PP_GATE_TYPE_BY_KIND's "architecture" -> "design"
# mapping).
_SECURITY_SPEC_GATE_TYPES = frozenset({"security", "spec"})
_CONTRACT_ARCH_GATE_TYPES = frozenset({"contract", "design"})


def _base_judge_vendor(producer: Any) -> str:
    """Strip the LV-3 ``-same-vendor-host`` label suffix off a judge producer."""
    p = str(producer or "")
    if p.endswith(_SAME_VENDOR_HOST_SUFFIX):
        return p[: -len(_SAME_VENDOR_HOST_SUFFIX)]
    return p


def _judge_vendor_chain(generator_producer: Any, gate_type: str | None,
                        pp_preferred_producers: Sequence[Any] | None) -> list[str]:
    """Ordered cross-vendor judge producers to try, per pp's authoritative
    mapping (``.claude/agents/judge-cross-vendor.md`` "Cross-vendor mapping"):

        generator=codex  -> agy
        generator=agy    -> codex
        generator=claude -> EITHER, preferring agy for security/spec gates
                             and codex for contract/architecture gates

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
        if gate_type in _SECURITY_SPEC_GATE_TYPES:
            primary = "agy"
        elif gate_type in _CONTRACT_ARCH_GATE_TYPES:
            primary = "codex"
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
