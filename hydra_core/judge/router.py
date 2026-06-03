"""Judge tier policy.

Mirrors pair-programmer's `gate_eligible_judges` (`daemon/src/orchestrator/gates.ts`):
given an envelope and the active context, decide:
  - tier: cross_vendor | same_vendor | skip
  - rubric_ids: which rubrics to apply
  - vendor_pair: (generator_vendor, judge_vendor) preference

Three layers of policy, in order:
  1. PP-skip rule: if the envelope has a passing pp_verdict, skip Hydra judging.
  2. Base policy by envelope type.
  3. Content-aware escalation: regex over payload upgrades same_vendor → cross_vendor.

Constitution-alignment is ALWAYS included (defense in depth).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

from .schemas import JudgeTier


# Base policy by envelope type. Anything not in this map defaults to same_vendor.
_BASE_TIER_BY_TYPE: dict[str, JudgeTier] = {
    "C_SUITE_DECISION_PACKET": "cross_vendor",
    "CREATIVE_BRIEF": "same_vendor",
    "SHOT_LIST": "same_vendor",
    "ASSET_JOB": "same_vendor",
    # Engineering envelopes are skipped IF a pp_verdict is attached;
    # otherwise they fall through to same_vendor for a light check.
    "PRD": "same_vendor",
    "ARCH_RFC": "same_vendor",
    "DEV_TASK": "same_vendor",
    "HITL_REQUEST": "cross_vendor",
    "DECISION_RECORD": "same_vendor",
    "HANDOFF": "same_vendor",
}


# Regex escalation: any match upgrades same_vendor → cross_vendor.
_ESCALATION_PATTERNS = [
    re.compile(p, re.IGNORECASE) for p in [
        r"\bcapital[_ ]allocation\b",
        r"\bmerger\b",
        r"\bacquisition\b",
        r"\bcrisis\b",
        r"\bblack[_ ]swan\b",
        r"\bphi\b",
        r"\bhipaa\b",
        r"\bgdpr\b",
        r"\bconstitution\b",
        r"\bproduction\s+deploy",
        r"\b(?:auth|cred|secret|token)\b",
    ]
]


# Topic-specific rubric additions for executive envelopes.
_EXEC_TOPIC_RUBRICS = [
    (re.compile(r"\bm[& ]a\b|\bmerger\b|\bacquisition\b", re.IGNORECASE),
     "mna-due-diligence@1"),
    (re.compile(r"\bscenario\b|\bstrategy\b|\bplanning\b", re.IGNORECASE),
     "scenario-rigor@1"),
    (re.compile(r"\$|\bUSD\b|\brevenue\b|\bEBITDA\b|\bWACC\b|\bIRR\b|\bNPV\b",
                re.IGNORECASE),
     "financial-hardcoding@1"),
]


# Topic-specific rubric additions for legal-compliance (Curia) envelopes.
# citation-integrity@1 and aba-512-ethics@1 are bound unconditionally at the
# squad boundary; these three are content-conditional (mirrors the gate
# `when:` clauses in squads/legal-compliance/squad.yaml).
_LEGAL_TOPIC_RUBRICS = [
    (re.compile(r"\bgdpr\b|\bdpia\b|\bprivacy\b|\bpersonal data\b|\bdata subject\b",
                re.IGNORECASE),
     "gdpr-art-25-privacy-by-design@1"),
    (re.compile(r"\bai act\b|\bai system\b|\bhigh[- ]risk ai\b|\bgpai\b",
                re.IGNORECASE),
     "eu-ai-act-classification@1"),
    (re.compile(r"\bopen[- ]source\b|\boss\b|\bgpl\b|\bagpl\b|\blgpl\b|\blicense compatibilit",
                re.IGNORECASE),
     "open-source-license-compatibility@1"),
]


@dataclass
class JudgeRoute:
    tier: JudgeTier
    rubric_ids: list[str] = field(default_factory=list)
    # Preferred (generator_vendor, judge_vendor) — the dispatcher picks the
    # judge vendor different from the generator when tier=cross_vendor.
    preferred_judge_vendors: list[str] = field(default_factory=list)
    rationale: str = ""


def _payload_text(envelope: dict[str, Any]) -> str:
    """Flatten an envelope dict to a single search string for keyword scans."""
    parts: list[str] = []

    def walk(v: Any) -> None:
        if isinstance(v, str):
            parts.append(v)
        elif isinstance(v, dict):
            for vv in v.values():
                walk(vv)
        elif isinstance(v, (list, tuple)):
            for vv in v:
                walk(vv)

    walk(envelope)
    return " ".join(parts)


def _has_passing_pp_verdict(envelope: dict[str, Any]) -> bool:
    """True if the envelope carries a passing pair-programmer verdict in metadata.

    PP results piggy-back on engineering envelopes via the dispatcher; if we see
    pp_verdict.outcome == "pass" with a rubric_id, we trust PP and skip.
    """
    pv = envelope.get("pp_verdict") or envelope.get("constraints", {}).get("pp_verdict")
    if not isinstance(pv, dict):
        return False
    return pv.get("outcome") == "pass" and bool(pv.get("rubric_id"))


def route_judge(
    envelope: dict[str, Any],
    *,
    origin_squad: Optional[str] = None,
    profile: Optional[str] = None,
    is_post_synthesis: bool = False,
) -> JudgeRoute:
    """Decide tier + rubrics for the given envelope.

    Post-synthesis pass is forced cross_vendor with the synthesis-coherence
    rubric, regardless of envelope type.
    """
    if is_post_synthesis:
        return JudgeRoute(
            tier="cross_vendor",
            rubric_ids=["constitution-alignment@1", "synthesis-coherence@1"],
            preferred_judge_vendors=["gemini", "codex"],
            rationale="post-synthesis (final Cathedral artifact)",
        )

    if _has_passing_pp_verdict(envelope):
        return JudgeRoute(
            tier="skip",
            rubric_ids=[],
            preferred_judge_vendors=[],
            rationale="pp_verdict=pass already present (PP owned the judging)",
        )

    etype = envelope.get("type", "")
    tier: JudgeTier = _BASE_TIER_BY_TYPE.get(etype, "same_vendor")
    rubrics: list[str] = ["constitution-alignment@1"]

    # Envelope-type rubric bindings.
    if etype == "C_SUITE_DECISION_PACKET":
        rubrics.append("board-decision-quality@1")
    elif etype in ("CREATIVE_BRIEF", "SHOT_LIST"):
        rubrics.extend(["brand-consistency@1", "audience-fit@1"])

    # Squad-boundary rubrics. Healthcare + legal-compliance always escalate
    # to cross_vendor (privacy/compliance posture). Other stub squads bind
    # their rubric without tier escalation — left to content-aware logic.
    if origin_squad == "healthcare" or (envelope.get("target_squad") == "healthcare"):
        rubrics.append("phi-redaction-completeness@1")
        tier = "cross_vendor"
    if origin_squad == "legal-compliance" or (envelope.get("target_squad") == "legal-compliance"):
        # Senate (Curia Crown) boundary: the two always-on gates bind
        # unconditionally; topic-conditional legal rubrics attach below.
        rubrics.extend([
            "compliance-coverage@1",
            "citation-integrity@1",
            "aba-512-ethics@1",
        ])
        tier = "cross_vendor"
    if origin_squad == "sales-gtm" or (envelope.get("target_squad") == "sales-gtm"):
        rubrics.append("sales-gtm-rigor@1")
    if origin_squad == "research-ds" or (envelope.get("target_squad") == "research-ds"):
        rubrics.append("research-rigor@1")
    if origin_squad == "customer-support" or (envelope.get("target_squad") == "customer-support"):
        rubrics.append("support-deflection-quality@1")

    # Content-aware escalation + executive topic rubrics.
    text = _payload_text(envelope)
    if tier == "same_vendor":
        for pat in _ESCALATION_PATTERNS:
            if pat.search(text):
                tier = "cross_vendor"
                break
    if etype == "C_SUITE_DECISION_PACKET":
        for pat, rid in _EXEC_TOPIC_RUBRICS:
            if pat.search(text) and rid not in rubrics:
                rubrics.append(rid)
    if origin_squad == "legal-compliance" or (envelope.get("target_squad") == "legal-compliance"):
        for pat, rid in _LEGAL_TOPIC_RUBRICS:
            if pat.search(text) and rid not in rubrics:
                rubrics.append(rid)

    # Profile policy: "enterprise" forces cross_vendor on every gate (parity
    # with PP's profile escalation behavior).
    if profile == "enterprise" and tier == "same_vendor":
        tier = "cross_vendor"

    preferred = ["gemini", "codex"] if tier == "cross_vendor" else ["codex"]
    return JudgeRoute(
        tier=tier,
        rubric_ids=rubrics,
        preferred_judge_vendors=preferred,
        rationale=f"base={_BASE_TIER_BY_TYPE.get(etype, 'same_vendor')} type={etype}",
    )
