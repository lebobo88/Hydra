"""Versioned rubric registry.

Mirrors pair-programmer's `daemon/src/rubrics/registry.ts` pattern: each rubric
has a frozen body bound to an immutable `@<version>` ID so that past verdicts
can be re-validated against the exact same criteria during replay.

Rule: NEVER edit the body of an existing `@N` rubric. To change a rubric, add
`@N+1` and update consumers to opt in. This invariant is enforced by code review
and the AGENTS.md hard rule.

Phase-1 contents: skeleton bodies sufficient to validate the routing path.
Phase-2 fleshes them out with full numeric dimensions and detailed criteria.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Rubric:
    rubric_id: str
    kind: str  # "executive", "garland", "cross_domain", "governance", "synthesis"
    body_md: str
    score_dimensions: tuple[str, ...]


_REGISTRY: dict[str, Rubric] = {}


def _register(r: Rubric) -> None:
    if r.rubric_id in _REGISTRY:
        raise RuntimeError(f"Duplicate rubric registration: {r.rubric_id}")
    _REGISTRY[r.rubric_id] = r


# ---------- cross-domain (always applied) ----------

_register(Rubric(
    rubric_id="constitution-alignment@1",
    kind="governance",
    body_md=(
        "# Constitution Alignment Rubric (v1)\n"
        "Score the artifact on whether it honors `CONSTITUTION.md`:\n"
        "- **refusal_respect** (0-5): does it avoid actions the constitution refuses?\n"
        "- **values_consistency** (0-5): is it consistent with the stated values?\n"
        "- **harm_minimization** (0-5): are harms surfaced and minimized?\n"
        "Pass requires every dimension ≥3 AND no refusal violation.\n"
    ),
    score_dimensions=("refusal_respect", "values_consistency", "harm_minimization"),
))

# ---------- executive ----------

_register(Rubric(
    rubric_id="board-decision-quality@1",
    kind="executive",
    body_md=(
        "# Board Decision Quality (v1)\n"
        "Grounded in the executive-protocol skill.\n"
        "- **objective_clarity** (0-5)\n"
        "- **option_coverage** (0-5)\n"
        "- **risk_treatment** (0-5)\n"
        "- **financial_rigor** (0-5)\n"
        "- **dissent_surfaced** (0-5)\n"
    ),
    score_dimensions=(
        "objective_clarity", "option_coverage", "risk_treatment",
        "financial_rigor", "dissent_surfaced",
    ),
))

_register(Rubric(
    rubric_id="mna-due-diligence@1",
    kind="executive",
    body_md=(
        "# M&A Due Diligence (v1)\n"
        "Grounded in the mna-playbook skill.\n"
        "- **thesis_strength** (0-5)\n"
        "- **valuation_method_diversity** (0-5)\n"
        "- **integration_realism** (0-5)\n"
        "- **regulatory_coverage** (0-5)\n"
    ),
    score_dimensions=(
        "thesis_strength", "valuation_method_diversity",
        "integration_realism", "regulatory_coverage",
    ),
))

_register(Rubric(
    rubric_id="scenario-rigor@1",
    kind="executive",
    body_md=(
        "# Scenario Rigor (v1)\n"
        "Grounded in the scenario-planning skill.\n"
        "- **axis_independence** (0-5)\n"
        "- **case_coverage** (0-5)\n"
        "- **sensitivity_analysis** (0-5)\n"
        "- **kill_criteria_present** (0-5)\n"
    ),
    score_dimensions=(
        "axis_independence", "case_coverage",
        "sensitivity_analysis", "kill_criteria_present",
    ),
))

_register(Rubric(
    rubric_id="financial-hardcoding@1",
    kind="executive",
    body_md=(
        "# Financial Hardcoding Directive (v1)\n"
        "Per the financial-frameworks skill: figures MUST be derived, not made up.\n"
        "- **derivation_transparency** (0-5)\n"
        "- **sensitivity_disclosed** (0-5)\n"
        "- **no_fabricated_constants** (0-5)\n"
    ),
    score_dimensions=(
        "derivation_transparency", "sensitivity_disclosed", "no_fabricated_constants",
    ),
))

# ---------- garland ----------

_register(Rubric(
    rubric_id="brand-consistency@1",
    kind="garland",
    body_md=(
        "# Brand Consistency (v1)\n"
        "- **voice_match** (0-5)\n"
        "- **visual_system_fidelity** (0-5)\n"
        "- **claim_substantiation** (0-5)\n"
    ),
    score_dimensions=("voice_match", "visual_system_fidelity", "claim_substantiation"),
))

_register(Rubric(
    rubric_id="audience-fit@1",
    kind="garland",
    body_md=(
        "# Audience Fit (v1)\n"
        "- **persona_resonance** (0-5)\n"
        "- **channel_appropriateness** (0-5)\n"
        "- **call_to_action_clarity** (0-5)\n"
    ),
    score_dimensions=(
        "persona_resonance", "channel_appropriateness", "call_to_action_clarity",
    ),
))

# ---------- compliance / healthcare ----------

_register(Rubric(
    rubric_id="phi-redaction-completeness@1",
    kind="governance",
    body_md=(
        "# PHI Redaction Completeness (v1)\n"
        "Run against any envelope that crosses the healthcare boundary.\n"
        "- **identifier_coverage** (0-5): name/DOB/MRN/SSN all masked?\n"
        "- **quasi_identifier_coverage** (0-5): zip/age/rare-condition combos?\n"
        "- **free_text_scan** (0-5): unstructured notes scanned?\n"
        "Any dimension <4 → outcome=fail (HIPAA-equivalent stance).\n"
    ),
    score_dimensions=(
        "identifier_coverage", "quasi_identifier_coverage", "free_text_scan",
    ),
))

_register(Rubric(
    rubric_id="compliance-coverage@1",
    kind="governance",
    body_md=(
        "# Legal Compliance Coverage (v1)\n"
        "Placeholder until the legal-compliance squad is non-stub.\n"
        "- **jurisdiction_mapping** (0-5)\n"
        "- **citation_quality** (0-5)\n"
        "- **risk_classification** (0-5)\n"
    ),
    score_dimensions=(
        "jurisdiction_mapping", "citation_quality", "risk_classification",
    ),
))

# ---------- stub-squad placeholders ----------
# Wired ahead of those squads being implemented so the judge plane is one less
# moving part to land when the stubs become real. Each rubric is intentionally
# minimal — flesh out the dimensions when the corresponding squad is built.

_register(Rubric(
    rubric_id="sales-gtm-rigor@1",
    kind="cross_domain",
    body_md=(
        "# Sales / GTM Rigor (v1)\n"
        "Placeholder until the sales-gtm squad is non-stub.\n"
        "- **icp_fit** (0-5): clear ideal customer profile?\n"
        "- **pricing_justification** (0-5): price grounded in value, not gut?\n"
        "- **funnel_metrics_present** (0-5): conversion / CAC / LTV called out?\n"
        "- **competitive_positioning** (0-5)\n"
    ),
    score_dimensions=(
        "icp_fit", "pricing_justification",
        "funnel_metrics_present", "competitive_positioning",
    ),
))

_register(Rubric(
    rubric_id="research-rigor@1",
    kind="cross_domain",
    body_md=(
        "# Research / Data Science Rigor (v1)\n"
        "Placeholder until the research-ds squad is non-stub.\n"
        "- **hypothesis_clarity** (0-5)\n"
        "- **method_appropriateness** (0-5)\n"
        "- **stats_validity** (0-5): power, multiple-comparisons, confounds\n"
        "- **reproducibility_path** (0-5): code/data/seed available?\n"
        "- **uncertainty_disclosed** (0-5): confidence intervals, not point estimates\n"
    ),
    score_dimensions=(
        "hypothesis_clarity", "method_appropriateness",
        "stats_validity", "reproducibility_path", "uncertainty_disclosed",
    ),
))

_register(Rubric(
    rubric_id="support-deflection-quality@1",
    kind="cross_domain",
    body_md=(
        "# Customer-Support Deflection Quality (v1)\n"
        "Placeholder until the customer-support squad is non-stub.\n"
        "- **intent_correctly_identified** (0-5)\n"
        "- **resolution_completeness** (0-5)\n"
        "- **escalation_path_clear** (0-5): does it know when to hand off?\n"
        "- **tone_appropriate** (0-5)\n"
    ),
    score_dimensions=(
        "intent_correctly_identified", "resolution_completeness",
        "escalation_path_clear", "tone_appropriate",
    ),
))

# ---------- synthesis ----------

_register(Rubric(
    rubric_id="synthesis-coherence@1",
    kind="synthesis",
    body_md=(
        "# Synthesis Coherence (v1)\n"
        "Applied to the post-synthesis Cathedral output.\n"
        "- **squad_representation** (0-5): every selected squad's voice present?\n"
        "- **dissent_preserved** (0-5): Kan-cell dissent surfaced, not flattened?\n"
        "- **single_voice** (0-5): reads as one document, not a stapled report?\n"
        "- **constraint_respect** (0-5): budget/deadline/risk-tolerance honored?\n"
    ),
    score_dimensions=(
        "squad_representation", "dissent_preserved",
        "single_voice", "constraint_respect",
    ),
))


def get_rubric(rubric_id: str) -> Rubric:
    if rubric_id not in _REGISTRY:
        raise KeyError(f"Unknown rubric: {rubric_id}. Known: {sorted(_REGISTRY)}")
    return _REGISTRY[rubric_id]


def list_rubrics(kind: str | None = None) -> list[Rubric]:
    rs = list(_REGISTRY.values())
    if kind:
        rs = [r for r in rs if r.kind == kind]
    return sorted(rs, key=lambda r: r.rubric_id)
