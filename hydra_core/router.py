"""Intent router.

Two layers, mirroring `Enterprise Master AI Orchestration System Architecture.md`
§ "Graph-driven routing" + "LLM-assisted intent routing":

1. **Deterministic edges** — pure-function checks (envelope type, phase, industries,
   keyword-trip).
2. **LLM-assisted fallback** — when the deterministic layer returns no high-confidence
   squad, fall back to an LLM classifier with the squad descriptions as context.

The LLM call is pluggable (`classify_callable`) so this module has no runtime
dependency on any specific provider — Claude Code, Codex, Gemini, or a local
classifier can all satisfy the protocol.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .squad_loader import SquadPack


# Keyword fingerprints per domain. Hand-tuned, not learned. Add as you scaffold.
_KEYWORDS: dict[str, tuple[str, ...]] = {
    "engineering": (
        "code", "bug", "refactor", "deploy", "ci/cd", "pull request", "pr ",
        "feature", "api", "endpoint", "schema", "migration", "test", "lint",
        "openapi", "kubernetes", "docker", "github", "gitlab",
    ),
    "executive": (
        "strategy", "roadmap", "budget", "p&l", "okr", "board", "investor",
        "m&a", "acquisition", "merger", "capex", "opex", "wacc", "irr", "npv",
        "risk appetite", "earnings", "shareholder", "crisis", "succession",
    ),
    "garland": (
        "campaign", "brand", "logo", "video", "shot", "script", "copy",
        "press kit", "social", "thumbnail", "voiceover", "music", "image",
        "render", "scene", "storyboard", "cinematic", "youtube", "tiktok",
        "calliope", "erato", "polyhymnia", "terpsichore", "euterpe", "clio",
        "urania", "helios", "garland", "muse", "muses", "creative crew",
    ),
    "legal-compliance": (
        "gdpr", "ccpa", "hipaa", "contract", "nda", "msa", "privacy",
        "license", "trademark", "patent", "regulatory", "lawsuit", "dmca",
        "eu ai act", "sox", "data subject", "litigation",
    ),
    "healthcare": (
        "patient", "diagnosis", "clinical", "ehr", "icd-10", "snomed", "fhir",
        "drug interaction", "perioperative", "phi", "hl7", "differential",
    ),
    "sales-gtm": (
        "lead", "pipeline", "prospect", "deal", "quote", "cpq", "pricing",
        "renewal", "churn", "icp", "battlecard", "competitive intel",
    ),
    "research-ds": (
        "experiment", "hypothesis", "paper", "arxiv", "p-value", "ablation",
        "preregister", "literature review", "factorial", "regression",
    ),
    "customer-support": (
        "ticket", "support", "complaint", "outage", "downtime", "sla",
        "p1 incident", "escalation", "knowledge base", "kb", "tier 1",
        "refund", "csat", "churn", "help desk", "angry customer",
    ),
    "marketing-research": (
        "market research", "competitor", "competitive intel", "tam", "sam", "som",
        "persona", "audience", "segment", "icp", "serp", "keyword", "topic cluster",
        "trends", "industry analysis",
    ),
    "marketing-strategy": (
        "campaign strategy", "go-to-market", "gtm", "positioning", "channel mix",
        "marketing plan", "campaign brief", "okr", "marketing okr",
        "marketing budget", "demand generation", "demand gen", "abm",
        "funnel strategy",
    ),
    "marketing-creative": (
        "copywriting", "creative", "brand voice", "tagline", "headline", "ad copy",
        "landing page copy", "marketing copy", "brand narrative", "aesthetic",
        "tone of voice", "messaging framework", "creative brief",
    ),
    "marketing-ops": (
        "media buying", "bid", "ppc", "paid media", "budget allocation",
        "media plan", "mmm", "mta", "attribution", "lifecycle marketing",
        "email marketing", "crm", "marketing automation", "a/b test",
        "conversion rate optimization", "cro",
    ),
    "marketing-production": (
        "shoot", "shot list", "production plan", "shoot day", "location scout",
        "talent release", "model release", "music license", "stock footage",
        "ip clearance", "post-production", "cinematographer", "dp",
        "director of photography", "storyboard", "scheduling", "production budget",
        "production schedule", "permit", "crew", "gaffer", "grip",
    ),
}


@dataclass(frozen=True)
class ToolScope:
    """Intent-based tool scope emitted alongside routing decisions."""
    relevant_tools: tuple[str, ...] = ()
    relevant_categories: tuple[str, ...] = ()
    intent_keywords: tuple[str, ...] = ()

    @property
    def tool_count(self) -> int:
        return len(self.relevant_tools)


@dataclass(frozen=True)
class RoutingDecision:
    squads: list[str]
    confidence: float                # 0..1, max across selected squads
    rationale: str
    used_fallback: bool = False
    tool_scope: ToolScope = field(default_factory=ToolScope)


ClassifyCallable = Callable[[str, dict[str, SquadPack]], list[str]]


def classify_intent(
    text: str,
    packs: dict[str, SquadPack],
    *,
    industries: tuple[str, ...] = (),
    classify_callable: Optional[ClassifyCallable] = None,
    min_confidence: float = 0.25,
) -> RoutingDecision:
    text_l = text.lower()
    scores: dict[str, float] = {}

    # Deterministic keyword pass
    for slug, kws in _KEYWORDS.items():
        if slug not in packs:
            continue
        hits = sum(1 for k in kws if re.search(rf"\b{re.escape(k)}\b", text_l))
        if hits:
            scores[slug] = min(1.0, 0.2 + 0.15 * hits)

    # Industry-tag boost
    for slug, pack in packs.items():
        overlap = set(industries) & set(pack.industries)
        if overlap:
            scores[slug] = max(scores.get(slug, 0.0), 0.4 + 0.2 * len(overlap))

    if scores:
        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        top_score = ranked[0][1]
        seen: set[str] = set()
        chosen = [s for s, sc in ranked
                  if sc >= max(min_confidence, top_score * 0.6)
                  and not (s in seen or seen.add(s))]
        if chosen and top_score >= min_confidence:
            return RoutingDecision(
                squads=chosen,
                confidence=top_score,
                rationale=f"keyword+industry match: {dict(ranked[:3])}",
            )

    # LLM fallback
    if classify_callable is not None:
        try:
            squads = classify_callable(text, packs) or []
            squads = [s for s in squads if s in packs]
            if squads:
                return RoutingDecision(
                    squads=squads,
                    confidence=0.6,
                    rationale="llm-fallback classifier",
                    used_fallback=True,
                )
        except Exception as e:
            return RoutingDecision(
                squads=["executive"] if "executive" in packs else list(packs)[:1],
                confidence=0.1,
                rationale=f"llm-fallback failed ({e!r}); default to executive triage",
                used_fallback=True,
            )

    # Last resort: send to executive for human-triage
    default = "executive" if "executive" in packs else next(iter(packs), "")
    return RoutingDecision(
        squads=[default] if default else [],
        confidence=0.1,
        rationale="no signal; default to executive for triage",
    )


# ---------- intent-based tool gating ----------

_TOOL_INTENT_KEYWORDS: dict[str, tuple[str, ...]] = {
    "rubric": ("rubric", "judge", "verdict", "score", "grade", "critique"),
    "run": ("run", "start", "execute", "dispatch", "launch"),
    "budget": ("budget", "cost", "spend", "price", "expense", "financial"),
    "profile": ("profile", "config", "setting", "preference"),
    "taxonomy": ("taxonomy", "section", "mapping", "category"),
    "artifact": ("artifact", "archive", "file", "output", "deliverable"),
    "evolution": ("evolve", "propose", "approve", "reject", "commit", "drift"),
    "memory": ("memory", "episodic", "semantic", "recall", "remember"),
    "governance": ("governance", "policy", "constitution", "gate", "hitl"),
    "audit": ("audit", "trace", "log", "decision", "record"),
    "squad": ("squad", "team", "roster", "agent"),
    "test": ("test", "check", "validate", "verify", "smoke"),
    "design": ("design", "template", "wireframe", "ux", "ui"),
    "docs": ("doc", "readme", "changelog", "release", "runbook"),
}


def compute_tool_scope(
    text: str,
    selected_squads: list[str],
    packs: dict[str, SquadPack],
    *,
    toolshed: Any = None,
    top_k: int = 20,
) -> ToolScope:
    """Compute intent-based tool scope from the goal text.

    Scores each tool category against the goal using keyword overlap,
    then returns the top-k most relevant tool names. This is the
    "Tool Attention" pattern from arXiv:2604.21816.
    """
    text_l = text.lower()
    text_terms = set(re.split(r"[\s_\-.,;:()]+", text_l))
    text_terms.discard("")

    # Score intent categories
    category_scores: dict[str, float] = {}
    matched_keywords: list[str] = []
    for category, keywords in _TOOL_INTENT_KEYWORDS.items():
        hits = sum(1 for k in keywords if k in text_l)
        if hits:
            category_scores[category] = hits / len(keywords)
            matched_keywords.extend(k for k in keywords if k in text_l)

    # Collect tools from selected squads' packs
    squad_tools: list[str] = []
    for slug in selected_squads:
        pack = packs.get(slug)
        if pack:
            for t in pack.tools:
                squad_tools.append(f"{t.mcp_server or slug}.{t.name}")

    # If toolshed is available, search it with the top intent keywords
    toolshed_results: list[str] = []
    if toolshed and matched_keywords:
        query = " ".join(matched_keywords[:5])
        try:
            results = toolshed.search(query, limit=top_k)
            toolshed_results = [f"{r.server}.{r.name}" for r in results]
        except Exception:
            pass

    # Merge: squad tools first (always relevant), then toolshed results
    seen: set[str] = set()
    relevant: list[str] = []
    for t in squad_tools + toolshed_results:
        if t not in seen:
            seen.add(t)
            relevant.append(t)
            if len(relevant) >= top_k:
                break

    ranked_categories = sorted(category_scores, key=lambda c: -category_scores[c])

    return ToolScope(
        relevant_tools=tuple(relevant),
        relevant_categories=tuple(ranked_categories[:5]),
        intent_keywords=tuple(matched_keywords[:10]),
    )
