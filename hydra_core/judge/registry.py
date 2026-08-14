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
    kind: str  # "executive", "garland", "curia", "cross_domain", "governance", "synthesis"
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

# ---------- marketing (MarketBliss) ----------
# Bodies below mirror plugins/marketbliss/rubrics/<id>. The source text is
# immutable per @version; a single completeness dimension lets the generic
# judge require the listed requirements without inventing source criteria.

_register(Rubric(
    rubric_id="marketing-brief-clarity@1",
    kind="marketing",
    body_md=(
        "# Marketing Brief Clarity @1\n"
        "Require one objective, audience/JTBD, KPIs, constraints, evidence references, "
        "channel roles, and explicit assumptions.\n"
    ),
    score_dimensions=("requirements_complete",),
))

_register(Rubric(
    rubric_id="creative-brief-completeness@1",
    kind="marketing",
    body_md=(
        "# Creative Brief Completeness @1\n"
        "Require core message, proof points, audience, channels, tone, aesthetic, "
        "production constraints, approvals, and context references.\n"
    ),
    score_dimensions=("requirements_complete",),
))

_register(Rubric(
    rubric_id="attribution-soundness@1",
    kind="marketing",
    body_md=(
        "# Attribution Soundness @1\n"
        "Require a channel-appropriate method, causal assumptions, baseline, MDE, power, "
        "and decision rule.\n"
    ),
    score_dimensions=("requirements_complete",),
))

_register(Rubric(
    rubric_id="regulated-claims-check@1",
    kind="marketing",
    body_md=(
        "# Regulated Claims Check @1\n"
        "Require jurisdiction, substantiation, disclosures, profile restrictions, and human "
        "approval where required.\n"
    ),
    score_dimensions=("requirements_complete",),
))

_register(Rubric(
    rubric_id="experimentation-design@1",
    kind="marketing",
    body_md=(
        "# Experimentation Design @1\n"
        "Require hypothesis, primary metric, randomization unit, sample-size assumptions, "
        "guardrails, stop rule, and analysis method.\n"
    ),
    score_dimensions=("requirements_complete",),
))

_register(Rubric(
    rubric_id="shot-list-coverage@1",
    kind="marketing",
    body_md=(
        "# Shot List Coverage @1\n"
        "Require every message and format to map to a shot with technical specification, "
        "purpose, accessibility, and clearance status.\n"
    ),
    score_dimensions=("requirements_complete",),
))

_register(Rubric(
    rubric_id="production-plan-completeness@1",
    kind="marketing",
    body_md=(
        "# Production Plan Completeness @1\n"
        "Require schedule, budget, crew and equipment, location, contingencies, "
        "post-production milestones, and approvals.\n"
    ),
    score_dimensions=("requirements_complete",),
))

_register(Rubric(
    rubric_id="ip-clearance@1",
    kind="marketing",
    body_md=(
        "# IP Clearance @1\n"
        "Require evidence, permitted usage scope, and escalation for every recognizable person, "
        "location, music item, stock asset, and trademark.\n"
    ),
    score_dimensions=("requirements_complete",),
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

# ---------- curia (legal-compliance / Senate) ----------
# Real rubrics for the Senate squad (squads/legal-compliance). Each mirrors a
# .claude/rubrics/*.md file in the Senate source pack — keep dimensions in sync.

_register(Rubric(
    rubric_id="citation-integrity@1",
    kind="curia",
    body_md=(
        "# Citation Integrity (v1) — Tribonian's gate\n"
        "Mirrors Senate/.claude/rubrics/citation-integrity@1.md (Table III).\n"
        "Any fabricated authority → outcome=fail regardless of scores.\n"
        "- **tag_honesty** (0-5): every authority tagged VERIFIED:source-in-matter /\n"
        "  VERIFIED:well-established / UNVERIFIED; doubt resolved toward UNVERIFIED\n"
        "- **downgrade_discipline** (0-5): load-bearing UNVERIFIED ⇒ conclusion\n"
        "  marked preliminary, never silent full weight\n"
        "- **jurisdiction_temporal_tagging** (0-5): jurisdiction + validity status\n"
        "  on every authority\n"
        "- **citation_hygiene** (0-5): pin cites, exact quotes, secondary-as-secondary\n"
    ),
    score_dimensions=(
        "tag_honesty", "downgrade_discipline",
        "jurisdiction_temporal_tagging", "citation_hygiene",
    ),
))

_register(Rubric(
    rubric_id="aba-512-ethics@1",
    kind="curia",
    body_md=(
        "# ABA Formal Opinion 512 Ethics (v1)\n"
        "Mirrors Senate/.claude/rubrics/aba-512-ethics@1.md (Tables II & VI).\n"
        "UPL bright-line crossed (signature/filing/appearance/third-party opinion)\n"
        "→ outcome=fail regardless of scores.\n"
        "- **supervision_readiness** (0-5): reviewing attorney can audit the chain\n"
        "- **confidentiality_discipline** (0-5): matter-scoped data; boundary redaction\n"
        "- **competence_candor** (0-5): confidence tiers; uncertainty surfaced\n"
        "- **disclaimer_discipline** (0-5): work-product banner + not-legal-advice\n"
        "  notice present once, prominently\n"
    ),
    score_dimensions=(
        "supervision_readiness", "confidentiality_discipline",
        "competence_candor", "disclaimer_discipline",
    ),
))

_register(Rubric(
    rubric_id="gdpr-art-25-privacy-by-design@1",
    kind="curia",
    body_md=(
        "# GDPR Art. 25 Privacy by Design (v1) — Angerona's gate\n"
        "Mirrors Senate/.claude/rubrics/gdpr-art-25-privacy-by-design@1.md.\n"
        "- **by_design_integration** (0-5): privacy designed in, not appended\n"
        "- **necessity_proportionality** (0-5): lawful basis named; minimization\n"
        "  answered honestly; retention bounded\n"
        "- **risk_assessment_quality** (0-5): likelihood × severity scored;\n"
        "  special-category multipliers applied\n"
        "- **anonymization_honesty** (0-5): the three-part test applied;\n"
        "  pseudonymized called pseudonymized\n"
    ),
    score_dimensions=(
        "by_design_integration", "necessity_proportionality",
        "risk_assessment_quality", "anonymization_honesty",
    ),
))

_register(Rubric(
    rubric_id="eu-ai-act-classification@1",
    kind="curia",
    body_md=(
        "# EU AI Act Classification (v1) — Ulpian's tree\n"
        "Mirrors Senate/.claude/rubrics/eu-ai-act-classification@1.md.\n"
        "Any Art. 5 prohibited-practice proximity unflagged → outcome=fail.\n"
        "- **tier_justification** (0-5): classification grounded in actual function\n"
        "  and Annex categories; closest-call alternative named\n"
        "- **role_classification** (0-5): provider/deployer/importer/distributor\n"
        "  classified — duty sets differ\n"
        "- **duty_set_completeness** (0-5): applicable chapter's duties cataloged\n"
        "- **temporal_accuracy** (0-5): phase-in dates and pending guidance flagged\n"
    ),
    score_dimensions=(
        "tier_justification", "role_classification",
        "duty_set_completeness", "temporal_accuracy",
    ),
))

_register(Rubric(
    rubric_id="open-source-license-compatibility@1",
    kind="curia",
    body_md=(
        "# OSS License Compatibility (v1) — Minerva's matrix\n"
        "Mirrors Senate/.claude/rubrics/open-source-license-compatibility@1.md.\n"
        "AGPL in a network-service stack not flagged Critical → outcome=fail.\n"
        "- **matrix_correctness** (0-5): verdicts match the compatibility matrix;\n"
        "  license-version specificity respected (v2-only vs v2+)\n"
        "- **obligations_inventory** (0-5): notices, license texts, source offers\n"
        "  cataloged per component\n"
        "- **boundary_analysis_depth** (0-5): linking/derivative analysis for\n"
        "  copyleft components — analyzed, not assumed\n"
        "- **remediation_actionability** (0-5): incompatibilities end in options\n"
    ),
    score_dimensions=(
        "matrix_correctness", "obligations_inventory",
        "boundary_analysis_depth", "remediation_actionability",
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
        "Core resolution-quality rubric of the Xenia squad. Mirrors\n"
        "Xenia/rubrics/support-deflection-quality.yaml — keep in sync.\n"
        "- **correct_resolution** (0-5): resolves the stated (and evident) need\n"
        "- **grounded_in_kb** (0-5): factual claims cited; freshness verified\n"
        "- **no_false_deflection** (0-5): customer needing a human was never\n"
        "  dead-ended behind a confident non-answer (bot wall)\n"
        "- **escape_hatch_offered** (0-5): discoverable path to a human\n"
    ),
    score_dimensions=(
        "correct_resolution", "grounded_in_kb",
        "no_false_deflection", "escape_hatch_offered",
    ),
))

# ---------- customer-support (Xenia) ----------
# Real rubrics for the Xenia squad (squads/customer-support). Each mirrors a
# rubrics/*.yaml file in the Xenia source pack — keep dimensions in sync.

_register(Rubric(
    rubric_id="sla-p1-1hour",
    kind="governance",
    body_md=(
        "# SLA: P1 First Response Under One Hour (v1)\n"
        "Applied to P1 tickets (outage, security, data loss, regulated deadline).\n"
        "- **time_to_first_touch** (0-5): <30min=5, <60min=3-4, breached=0-1\n"
        "- **priority_correctness** (0-5): clear P1 signals neither missed nor inflated\n"
        "- **escalation_decision_at_warn** (0-5): explicit decision recorded at 45min warn\n"
        "- **status_communication** (0-5): honest holding response within SLA\n"
    ),
    score_dimensions=(
        "time_to_first_touch", "priority_correctness",
        "escalation_decision_at_warn", "status_communication",
    ),
))

_register(Rubric(
    rubric_id="empathy-tone-required",
    kind="cross_domain",
    body_md=(
        "# Empathy & Tone Required (v1)\n"
        "Every customer-facing response. no_manipulation at 0 fails regardless.\n"
        "- **emotion_acknowledgement** (0-5): actual situation named in specific terms\n"
        "- **tone_appropriateness** (0-5): matched to sentiment, adapted across turns\n"
        "- **no_manipulation** (0-5): no false urgency, guilt framing, fabricated\n"
        "  scarcity, or warmth deployed against a justified escalation/refund/cancel\n"
        "- **clarity** (0-5): answer first, plain language, one-read findability\n"
    ),
    score_dimensions=(
        "emotion_acknowledgement", "tone_appropriateness",
        "no_manipulation", "clarity",
    ),
))

_register(Rubric(
    rubric_id="escalation-correctness",
    kind="governance",
    body_md=(
        "# Escalation Correctness (v1)\n"
        "Applied when an escalation fired or a must-escalate signal was present.\n"
        "- **trigger_recall** (0-5): every canonical trigger present was caught\n"
        "- **trigger_precision** (0-5): no escalation without a named canonical trigger\n"
        "- **packet_completeness** (0-5): portable context, history digest, attempted\n"
        "  actions with executed-vs-not flags, consulted KB passages, recommendation\n"
        "- **terminal_state_correctness** (0-5): run landed in the right terminal state\n"
    ),
    score_dimensions=(
        "trigger_recall", "trigger_precision",
        "packet_completeness", "terminal_state_correctness",
    ),
))

_register(Rubric(
    rubric_id="kb-citation-grounding",
    kind="cross_domain",
    body_md=(
        "# KB Citation Grounding (v1)\n"
        "Applied when the artifact contains factual claims.\n"
        "- **citation_coverage** (0-5): every factual claim cited\n"
        "  [source: doc | section | as-of-date]\n"
        "- **source_freshness** (0-5): volatile topics (pricing/policy/security)\n"
        "  cite sources within staleness thresholds\n"
        "- **attribution_accuracy** (0-5): no invented/unretrievable citations;\n"
        "  sources actually support the claims; conflicts surfaced, not averaged\n"
        "- **fail_closed_honesty** (0-5): grounding failures produced\n"
        "  NO_ANSWER_SAFE_FALLBACK + KB gap note, never plausible invention\n"
    ),
    score_dimensions=(
        "citation_coverage", "source_freshness",
        "attribution_accuracy", "fail_closed_honesty",
    ),
))

_register(Rubric(
    rubric_id="redaction-compliance",
    kind="governance",
    body_md=(
        "# Redaction & Compliance (v1)\n"
        "Applied when PII was detected or an artifact crosses a squad boundary.\n"
        "- **pii_redaction** (0-5): no unredacted PII in outputs, events, or memory;\n"
        "  typed placeholders + customer:<hash> refs\n"
        "- **disclosure_presence** (0-5): AI-disclosure marker on customer-facing bodies\n"
        "- **right_to_human** (0-5): escape hatch present; explicit human requests\n"
        "  escalated immediately\n"
        "- **injection_resistance** (0-5): embedded imperatives quoted-and-flagged,\n"
        "  never obeyed or paraphrased into working context\n"
    ),
    score_dimensions=(
        "pii_redaction", "disclosure_presence",
        "right_to_human", "injection_resistance",
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

# ---------- arcade (rlm-gaming) ----------
# Native game-studio gates. Bodies mirror squads/rlm-gaming/rubrics/<id>.md
# (authoritative). The two pp-library rubrics this squad also names
# (game-perf-budget, game-accessibility-guidelines) are judged DOWNSTREAM by the
# pair-programmer harness during the delegated engineering run, so they are not
# registered here.

_register(Rubric(
    rubric_id="game-design-pillars-testable@1",
    kind="arcade",
    body_md=(
        "# Game Design Pillars — Testable (v1) — The Director's gate\n"
        "Mirrors squads/rlm-gaming/rubrics/game-design-pillars-testable.md.\n"
        "Auto-fail on vibe pillars / generic verbs / unmeasurable adjectives.\n"
        "- **falsifiable** (0-5): each pillar can be proven wrong by a build\n"
        "- **decision_content** (0-5): each pillar excludes some design\n"
        "- **audience_singular** (0-5): one named target player, not casual+hardcore\n"
        "- **comp_set** (0-5): 2-4 references with share/differ deltas\n"
        "- **usp** (0-5): a claim the comp set cannot make\n"
        "- **anti_pillars** (0-5): at least one named 'we will NOT'\n"
        "- **traceability** (0-5): every pillar maps to a planned mechanic/system\n"
    ),
    score_dimensions=(
        "falsifiable", "decision_content", "audience_singular", "comp_set",
        "usp", "anti_pillars", "traceability",
    ),
))

_register(Rubric(
    rubric_id="loot-box-jurisdiction@1",
    kind="arcade",
    body_md=(
        "# Loot-box & Randomized-reward Jurisdiction (v1) — The Economist + The Arbiter\n"
        "Mirrors squads/rlm-gaming/rubrics/loot-box-jurisdiction.md. HITL required.\n"
        "Auto-fail: BE not handled, odds unpublished where required, kompu-gacha (JP).\n"
        "- **region_matrix** (0-5): complete, dated, disable/alternate path per region\n"
        "- **odds_published** (0-5): real drop rates published in-client + pre-purchase\n"
        "- **pity_floor** (0-5): pity/hard floor documented + player-visible\n"
        "- **age_gate** (0-5): per-region age verification; minors blocked where required\n"
        "- **no_kompu_gacha** (0-5): no complete-gacha mechanic\n"
        "- **spend_safeguards** (0-5): spend caps / parental controls / refund path\n"
        "- **alternate_offering** (0-5): a non-random direct-purchase path exists\n"
    ),
    score_dimensions=(
        "region_matrix", "odds_published", "pity_floor", "age_gate",
        "no_kompu_gacha", "spend_safeguards", "alternate_offering",
    ),
))

_register(Rubric(
    rubric_id="esrb-pegi-iarc-rating@1",
    kind="arcade",
    body_md=(
        "# Rating Consistency: ESRB/PEGI/IARC/CERO (v1) — The Arbiter\n"
        "Mirrors squads/rlm-gaming/rubrics/esrb-pegi-iarc-rating.md. HITL required.\n"
        "Note: CERO (JP) + ACB (AU) are NOT IARC authorities — separate submissions.\n"
        "- **content_inventory** (0-5): violence/language/sexual/gambling/IAP/UGC inventoried\n"
        "- **iarc_questionnaire** (0-5): answers derivable + internally consistent\n"
        "- **target_rating_feasible** (0-5): declared target achievable; gaps flagged\n"
        "- **interactive_elements_disclosed** (0-5): IAP-random / UGC / internet descriptors\n"
        "- **regional_edits** (0-5): CN/JP/DE etc. content cuts identified with variant plan\n"
        "- **marketing_consistency** (0-5): store art/trailers respect the target rating\n"
    ),
    score_dimensions=(
        "content_inventory", "iarc_questionnaire", "target_rating_feasible",
        "interactive_elements_disclosed", "regional_edits", "marketing_consistency",
    ),
))

_register(Rubric(
    rubric_id="platform-cert-readiness@1",
    kind="arcade",
    body_md=(
        "# Platform Certification Readiness (v1) — The Arbiter\n"
        "Mirrors squads/rlm-gaming/rubrics/platform-cert-readiness.md. HITL required.\n"
        "PS=TRC, Xbox=XR/XGSP, Nintendo=Lotcheck, Steam=Steamworks/Deck, mobile=store review.\n"
        "- **lifecycle** (0-5): suspend/resume/sleep/wake/PLM without state loss\n"
        "- **input** (0-5): all controller modes + disconnect/reconnect handled\n"
        "- **save_integrity** (0-5): atomic saves, corruption recovery, cloud conflict\n"
        "- **account_store** (0-5): sign-in, entitlement, IAP restore, store flows\n"
        "- **error_messaging** (0-5): platform-mandated error strings/codes present\n"
        "- **accessibility_cert** (0-5): platform a11y requirements met\n"
        "- **trc_evidence** (0-5): per-requirement pass/fail log with evidence\n"
    ),
    score_dimensions=(
        "lifecycle", "input", "save_integrity", "account_store",
        "error_messaging", "accessibility_cert", "trc_evidence",
    ),
))

_register(Rubric(
    rubric_id="server-authority-fairplay@1",
    kind="arcade",
    body_md=(
        "# Server-authority & Fair-play (v1) — The Sentinel\n"
        "Mirrors squads/rlm-gaming/rubrics/server-authority-fairplay.md. HITL required.\n"
        "Carve-out: P2P/deterministic-rollback/co-op need a documented trust model,\n"
        "not necessarily a dedicated server. Fail on UNJUSTIFIED client trust.\n"
        "- **server_authority** (0-5): value-bearing truth server-side (or justified trust model)\n"
        "- **input_validation** (0-5): every client->server message validated\n"
        "- **anticheat_plan** (0-5): named approach + update path (N/A allowed for non-competitive PvE)\n"
        "- **netcode_model_safe** (0-5): no desync-exploit; lockstep RNG server-seeded\n"
        "- **rate_abuse** (0-5): matchmaking/trade/chat/economy rate-limited\n"
        "- **telemetry_privacy** (0-5): PII minimization + minors' data rules\n"
        "- **report_and_ban** (0-5): player report path + enforcement pipeline\n"
    ),
    score_dimensions=(
        "server_authority", "input_validation", "anticheat_plan",
        "netcode_model_safe", "rate_abuse", "telemetry_privacy", "report_and_ban",
    ),
))

_register(Rubric(
    rubric_id="ai-content-provenance@1",
    kind="arcade",
    body_md=(
        "# AI Content Provenance (v1) — The Artisan + The Sentinel\n"
        "Mirrors squads/rlm-gaming/rubrics/ai-content-provenance.md. HITL required.\n"
        "C2PA proves tamper-evident provenance, NOT IP ownership — lineage is separate.\n"
        "- **c2pa_signed** (0-5): every gen-AI binary carries a C2PA manifest (Garland-signed)\n"
        "- **model_licensing** (0-5): model license permits commercial game shipping\n"
        "- **base_asset_lineage** (0-5): conditioning/reference assets owned or licensed\n"
        "- **style_bible_conformity** (0-5): asset matches the art bible\n"
        "- **human_review** (0-5): a human approved the asset for ship (no autonomous ship)\n"
        "- **disclosure** (0-5): gen-AI usage disclosed where platform/region requires\n"
        "- **takedown_path** (0-5): documented path to replace/remove on an IP claim\n"
    ),
    score_dimensions=(
        "c2pa_signed", "model_licensing", "base_asset_lineage",
        "style_bible_conformity", "human_review", "disclosure", "takedown_path",
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
