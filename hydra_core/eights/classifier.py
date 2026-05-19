"""Rules-first eights classifier.

Given an envelope or a raw payload dict, return a list of Cells the memory
write should be tagged with. Multi-label by design — a `DecisionRecord` that
also surfaces a substantive dissent properly lives in both Li (Focus) and
Kan (Risk); a campaign win lives in Li and Dui.

Rules layer (deterministic, this module). LLM fallback is wired as a hook
point — `classify_with_fallback()` accepts an optional callable for the LLM
arm. Per the constitution, any LLM-proposed cell choice that touches the
procedural spine must pass through `enforce_constitution` before commit.
"""
from __future__ import annotations

import re
from typing import Any, Callable, Optional

from . import ALL_CELLS, Cell, validate_cells


# --- envelope-type defaults --------------------------------------------------
# Envelope-type alone narrows the cell set. Origin-squad and content rules
# layered on top can add to the list. We never reduce by type alone — that
# would make tagging brittle to envelope refactors.

_TYPE_DEFAULTS: dict[str, tuple[Cell, ...]] = {
    "C_SUITE_DECISION_PACKET": ("li", "qian"),   # focus + vision
    "PRD":                     ("li", "qian"),   # focus + vision
    "ARCH_RFC":                ("li", "gen"),    # focus + constraints
    "DEV_TASK":                ("li",),
    "CREATIVE_BRIEF":          ("li", "xun"),    # focus + influence (brand)
    "SHOT_LIST":               ("li",),
    "ASSET_JOB":               ("li",),
    "HITL_REQUEST":            ("zhen",),         # triggers
    "DECISION_RECORD":         ("li",),          # focus; dissents add kan, wins add dui
    "HANDOFF":                 ("li",),
}


# --- origin-squad nudges -----------------------------------------------------

_SQUAD_NUDGES: dict[str, tuple[Cell, ...]] = {
    "executive":         ("qian",),
    "engineering":       ("gen",),
    "creative":          ("xun",),
    "garland":           ("xun",),
    "legal-compliance":  ("gen",),
    "healthcare":        ("gen", "kan"),
    "sales-gtm":         ("kun",),
    "research-ds":       ("kun",),
    "customer-support":  ("zhen", "kun"),
    "marketing-strategy":   ("xun", "qian"),
    "marketing-ops":        ("li",),
    "marketing-creative":   ("xun",),
    "marketing-production": ("li",),
    "marketing-research":   ("kun",),
}


# --- content-keyword rules ---------------------------------------------------
# Coarse regex over a stringified payload. False positives are cheap; missing
# a real Risk or Delight is expensive. Lean toward over-tagging.

_KEYWORD_RULES: tuple[tuple[Cell, re.Pattern[str]], ...] = (
    ("kan",  re.compile(r"\b(risk|threat|failure|incident|breach|dissent|post[- ]mortem|"
                        r"vulnerab|exfiltrat|prompt[- ]inject|constitution_breach)\b", re.IGNORECASE)),
    ("dui",  re.compile(r"\b(win|wins|delight|loved it|gratitud|celebrate|"
                        r"shipped successfully|nailed it|exceeded|outperformed)\b", re.IGNORECASE)),
    ("gen",  re.compile(r"\b(compliance|regulat|gdpr|hipaa|pci|sox|contract(ual)?|"
                        r"covenant|legal hold|immutable|sealed)\b", re.IGNORECASE)),
    ("zhen", re.compile(r"\b(alert|paged|trigger|event fired|webhook|signal received|"
                        r"on[- ]call|incident response)\b", re.IGNORECASE)),
    ("xun",  re.compile(r"\b(brand|reputation|community|relationship|press|"
                        r"earned media|tone of voice|customer love)\b", re.IGNORECASE)),
    ("qian", re.compile(r"\b(mission|vision|covenant|calling|long[- ]horizon|"
                        r"north star|strategy intent)\b", re.IGNORECASE)),
    ("kun",  re.compile(r"\b(market|customer base|persona|segment|industry|"
                        r"landscape|competitor|territory)\b", re.IGNORECASE)),
    ("li",   re.compile(r"\b(in[- ]flight|active|sprint|this week|current|"
                        r"focus|in progress|wip)\b", re.IGNORECASE)),
)


def _stringify(payload: Any) -> str:
    if payload is None:
        return ""
    if isinstance(payload, str):
        return payload
    if isinstance(payload, dict):
        return " ".join(_stringify(v) for v in payload.values())
    if isinstance(payload, (list, tuple)):
        return " ".join(_stringify(v) for v in payload)
    if hasattr(payload, "model_dump"):
        try:
            return _stringify(payload.model_dump(mode="json"))
        except Exception:
            return str(payload)
    return str(payload)


def classify(
    *,
    envelope_type: Optional[str] = None,
    origin_squad: Optional[str] = None,
    payload: Any = None,
) -> list[Cell]:
    """Deterministic cell classification. Returns at least one cell — falls
    back to Li (Focus) if nothing else matches, since every active memory
    write is at least *in flight*.
    """
    cells: list[Cell] = []

    if envelope_type and envelope_type in _TYPE_DEFAULTS:
        cells.extend(_TYPE_DEFAULTS[envelope_type])

    if origin_squad and origin_squad in _SQUAD_NUDGES:
        cells.extend(_SQUAD_NUDGES[origin_squad])

    text = _stringify(payload)
    if text:
        for cell, pat in _KEYWORD_RULES:
            if pat.search(text):
                cells.append(cell)

    # Normalize: validate, dedupe in order, ensure at least Li.
    cells = validate_cells(cells)
    if not cells:
        cells = ["li"]
    return cells


def classify_envelope(envelope: Any) -> list[Cell]:
    """Convenience for HydraEnvelope-shaped objects. Reads .type and .origin_squad."""
    return classify(
        envelope_type=getattr(envelope, "type", None),
        origin_squad=getattr(envelope, "origin_squad", None),
        payload=envelope,
    )


# --- LLM fallback (gated) ----------------------------------------------------

LLMClassifier = Callable[[str], list[str]]


def classify_with_fallback(
    *,
    envelope_type: Optional[str] = None,
    origin_squad: Optional[str] = None,
    payload: Any = None,
    llm: Optional[LLMClassifier] = None,
    confidence_floor: int = 2,
) -> list[Cell]:
    """Run the rules classifier. If it produces fewer than `confidence_floor`
    distinct cells AND an `llm` callable was supplied, ask the LLM and union
    its proposed cells (after validation).

    The LLM's output is *added* to the rule-based cells — never used to remove
    a cell the rules already chose. The constitution gate is the caller's
    responsibility before any procedural-memory commit (see hydra_core.procedural).
    """
    rule_cells = classify(envelope_type=envelope_type, origin_squad=origin_squad, payload=payload)
    if len(rule_cells) >= confidence_floor or llm is None:
        return rule_cells

    try:
        proposed = llm(_stringify(payload))
    except Exception:
        return rule_cells
    if not proposed:
        return rule_cells

    merged = list(rule_cells)
    for c in validate_cells(list(proposed)):
        if c not in merged:
            merged.append(c)
    return merged


# Re-export for `from hydra_core.eights.classifier import ALL_CELLS`.
__all__ = ["ALL_CELLS", "classify", "classify_envelope", "classify_with_fallback"]
