"""Rubric id resolution + body lookup, shared by both engineering stage loops.

Finding E2-25. Both the attended driver (``hydra_core.host_bridge``) and the
headless driver (``hydra_core.squad_node``) used to hand a judge the *unversioned*
id ``rfc-2119-normative`` and then look its body up in a registry that only
serves versioned ids. The lookup missed, the judge silently received a generic
one-line rubric, and the ledger still recorded ``rubric_id=rfc-2119-normative``.
Two defects, both fixed here:

1. **Version resolution.** Rubric registries (Hydra's
   ``hydra_core.judge.registry`` and pair-programmer's
   ``daemon/src/rubrics/registry.ts``) key every rubric by an immutable
   ``<base>@<N>`` id. :func:`resolve_rubric_id` turns a base id into the highest
   registered ``@N``, leaves an already-versioned id alone, and reports an
   unresolved base to the caller instead of pretending it resolved.

2. **Gate-typed defaults.** A *code* gate must not default to the *spec* rubric.
   :func:`default_rubric_id` mirrors pair-programmer's
   ``gates.ts::pickDefaultRubric`` for the gate types pp covers, and fills pp's
   gap for the code-shaped gate types (pp returns ``null`` there) with Hydra's
   local ``code-change-quality`` rubric.

When a body genuinely cannot be resolved, :func:`rubric_body` returns the generic
fallback text *and says so*, so the caller can flag the verdict rather than
ledger an id whose body was never used.
"""
from __future__ import annotations

import logging
import re
import threading
from typing import Any, Callable, Protocol

_log = logging.getLogger(__name__)

#: The rubric text used when no registry can serve a body. Kept verbatim from
#: the pre-E2-25 ``squad_node._rubric_md`` fallback so existing traces compare.
GENERIC_FALLBACK_BODY = (
    "Evaluate the change for correctness, adherence to the stated request, "
    "and absence of regressions. Output outcome (pass/revise/fail), a "
    "critique, and per-dimension scores."
)

#: Hydra-local code-quality rubric (see ``judge/registry.py``). pp has no
#: rubric of kind "code"; this fills that hole for code-shaped gates.
CODE_RUBRIC_BASE = "code-change-quality"

#: Gate type -> default rubric BASE id (unversioned; resolved at call time).
#:
#: The spec/design/contract/security rows mirror pair-programmer
#: ``daemon/src/orchestrator/gates.ts::pickDefaultRubric``. The code-shaped rows
#: (code_style / lint_class / docs_polish) are where pp returns ``null`` — Hydra
#: fills them with the local code rubric rather than inheriting the spec rubric.
DEFAULT_RUBRIC_BY_GATE_TYPE: dict[str, str] = {
    "spec": "rfc-2119-normative",
    "design": "c4-system-context",
    "contract": "openapi-3.1-stability",
    "security": "owasp-asvs-l1",
    "code_style": CODE_RUBRIC_BASE,
    "lint_class": CODE_RUBRIC_BASE,
    "docs_polish": CODE_RUBRIC_BASE,
}

_VERSIONED_RE = re.compile(r"@\d+$")


class Dispatcher(Protocol):  # pragma: no cover - structural typing only
    def call_mcp(self, server: str, tool: str, args: dict[str, Any],
                 squad_id: str | None = ...) -> Any: ...


def default_rubric_id(gate_type: str | None) -> str:
    """Default rubric BASE id for a pp gate type. Unknown gate types are treated
    as code-shaped, matching ``_pp_gate_type``'s own ``code_style`` default."""
    return DEFAULT_RUBRIC_BY_GATE_TYPE.get(gate_type or "", CODE_RUBRIC_BASE)


def is_versioned(rubric_id: str) -> bool:
    return bool(_VERSIONED_RE.search(rubric_id or ""))


# --------------------------------------------------------------------------- #
# Version index (base -> highest @N), cached per process                       #
# --------------------------------------------------------------------------- #

_cache_lock = threading.Lock()
_pp_versions: dict[str, int] | None = None


def reset_rubric_cache() -> None:
    """Drop the cached pp rubric list (tests, and a pp daemon restart)."""
    global _pp_versions
    with _cache_lock:
        _pp_versions = None


def _split(rubric_id: str) -> tuple[str, int] | None:
    base, _, ver = (rubric_id or "").rpartition("@")
    if not base or not ver.isdigit():
        return None
    return base, int(ver)


def _merge(index: dict[str, int], rubric_id: str) -> None:
    parts = _split(rubric_id)
    if parts is None:
        return
    base, ver = parts
    if ver > index.get(base, -1):
        index[base] = ver


def _local_versions() -> dict[str, int]:
    """Highest ``@N`` per base id in Hydra's own rubric registry."""
    index: dict[str, int] = {}
    try:
        from .registry import list_rubrics
        for r in list_rubrics():
            _merge(index, r.rubric_id)
    except Exception:  # noqa: BLE001 — a registry import failure is not fatal
        _log.warning("hydra rubric registry unreadable during id resolution",
                     exc_info=True)
    return index


def _pp_rows(payload: Any) -> list[dict[str, Any]]:
    """Unwrap ``list_rubrics`` out of pp's ``{status, result}`` MCP envelope.

    pp returns a bare array; the gateway may wrap it under ``result`` and/or
    ``rubrics``. Anything else yields no rows (and therefore no resolution)."""
    seen: set[int] = set()
    node = payload
    for _ in range(4):
        if isinstance(node, list):
            return [r for r in node if isinstance(r, dict)]
        if not isinstance(node, dict) or id(node) in seen:
            return []
        seen.add(id(node))
        for key in ("result", "rubrics", "data", "content"):
            if key in node:
                node = node[key]
                break
        else:
            return []
    return []


def _fetch_pp_versions(dispatcher: Any, squad_id: str) -> dict[str, int]:
    """Highest ``@N`` per base id served by pp's rubric registry. Cached on
    success only, so a transient pp outage does not pin an empty index."""
    global _pp_versions
    with _cache_lock:
        if _pp_versions is not None:
            return _pp_versions
    if dispatcher is None or not hasattr(dispatcher, "call_mcp"):
        return {}
    try:
        payload = dispatcher.call_mcp("pp_harness", "list_rubrics", {},
                                      squad_id=squad_id)
    except Exception:  # noqa: BLE001 — never block a stage on rubric listing
        _log.warning("pp_harness.list_rubrics failed; resolving rubric ids from "
                     "the local registry only", exc_info=True)
        return {}
    index: dict[str, int] = {}
    for row in _pp_rows(payload):
        rid = row.get("id") or row.get("rubric_id")
        if isinstance(rid, str):
            _merge(index, rid)
    if not index:
        # An empty/again-unparseable list is not a durable fact about pp.
        return {}
    with _cache_lock:
        _pp_versions = index
    return index


def resolve_rubric_id(
    dispatcher: Any,
    base_or_versioned: str,
    *,
    squad_id: str = "engineering",
    on_unresolved: Callable[[str], None] | None = None,
) -> str:
    """Return a versioned ``<base>@<N>`` rubric id.

    * An id that already carries ``@N`` passes through untouched — a caller that
      pinned a version keeps it (replay determinism).
    * A bare base id resolves to the highest ``@N`` registered for it, looking in
      Hydra's local registry first and then in pp's registry via
      ``pp_harness.list_rubrics`` (cached per process).
    * A base no registry knows is returned unchanged and reported through
      ``on_unresolved`` (``rubric_unresolved``), so the caller can trace it
      rather than silently ledger an id it could not resolve.
    """
    rid = (base_or_versioned or "").strip()
    if not rid:
        rid = CODE_RUBRIC_BASE
    if is_versioned(rid):
        return rid
    versions = dict(_fetch_pp_versions(dispatcher, squad_id))
    for base, ver in _local_versions().items():  # local registry wins on ties
        if ver >= versions.get(base, -1):
            versions[base] = ver
    ver = versions.get(rid)
    if ver is None:
        _log.warning("rubric_unresolved: no versioned rubric registered for "
                     "base id %r; requesting it unversioned", rid)
        if on_unresolved is not None:
            try:
                on_unresolved(rid)
            except Exception:  # noqa: BLE001 — a trace hook must never break the loop
                pass
        return rid
    return f"{rid}@{ver}"


def rubric_body(
    rubric_id: str,
    dispatcher: Any = None,
    *,
    squad_id: str = "engineering",
) -> tuple[str, bool]:
    """Return ``(body_md, fallback)`` for ``rubric_id``.

    Looks in Hydra's local registry, then pp's (``pp_harness.get_rubric``), and
    only then degrades to :data:`GENERIC_FALLBACK_BODY` with ``fallback=True``.
    ``fallback=True`` means the judge did NOT see the named rubric, and callers
    must say so rather than record the id as if it had been applied.
    """
    if not rubric_id:
        return GENERIC_FALLBACK_BODY, True
    try:
        from .registry import get_rubric
        return get_rubric(rubric_id).body_md, False
    except Exception:  # noqa: BLE001 — a local miss is expected for pp-owned ids
        pass
    if dispatcher is not None and hasattr(dispatcher, "call_mcp"):
        try:
            payload = dispatcher.call_mcp(
                "pp_harness", "get_rubric", {"id": rubric_id}, squad_id=squad_id)
            body = _pp_body(payload)
            if body:
                return body, False
        except Exception:  # noqa: BLE001 — never block the loop on a registry RPC
            _log.warning("pp_harness.get_rubric(%r) failed", rubric_id,
                         exc_info=True)
    return GENERIC_FALLBACK_BODY, True


def _pp_body(payload: Any) -> str:
    """Pull the markdown body out of pp's ``get_rubric`` envelope."""
    node = payload
    for _ in range(4):
        if not isinstance(node, dict):
            return ""
        for key in ("markdown", "body_md", "body"):
            val = node.get(key)
            if isinstance(val, str) and val.strip():
                return val
        nxt = node.get("result", node.get("rubric"))
        if nxt is None or nxt is node:
            return ""
        node = nxt
    return ""
