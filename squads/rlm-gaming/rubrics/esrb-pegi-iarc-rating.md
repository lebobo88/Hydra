---
id: esrb-pegi-iarc-rating@1
bare_id: esrb-pegi-iarc-rating
kind: compliance
version: 1
title: "Content rating is consistent and submission-ready across boards"
owner_head: compliance-cert
hitl_required: true
---
# ESRB / PEGI / IARC / CERO rating-consistency rubric

The Arbiter gates store submission on this. The artifact is a **content
disclosure + target-rating map** consistent across rating boards.

Score 0..1 per cluster:

- **content_inventory**: violence, language, sexual content, gambling/sim-
  gambling, controlled substances, in-game purchases, user interaction (chat /
  UGC) all inventoried with severity and frequency.
- **iarc_questionnaire**: the IARC questionnaire answers are derivable from the
  inventory and internally consistent. One IARC submission yields the
  participating boards' ratings — **ESRB** (N. America), **PEGI** (Europe),
  **USK** (Germany), **ClassInd** (Brazil), **GRAC** (Korea), and the generic
  IARC rating (e.g. for the MS/Nintendo/Google/Steam storefronts that use IARC).
  **CERO (Japan) is NOT an IARC authority** — it requires a separate CERO
  submission; **ACB (Australia)** is a separate national process for many
  releases. Track CERO/ACB on their own rows.
- **target_rating_feasible**: declared target rating (e.g. ESRB T / PEGI 12) is
  achievable given the content inventory; gaps flagged with cut/edit options.
- **interactive_elements_disclosed**: "In-Game Purchases (Incl. Random Items)",
  "Users Interact", "Unrestricted Internet" descriptors applied where true.
- **regional_edits**: any content requiring a regional cut (CN/JP/DE gore, etc.)
  is identified with the variant plan.
- **marketing_consistency**: store art / trailers respect the target rating
  (no M content in a T trailer).

Outcome:
- pass: every cluster ≥ 0.8 AND The Arbiter approved the rating map.
- revise: any cluster in [0.6, 0.8) OR target rating needs an edit list.
- fail: any cluster < 0.6 OR undisclosed random-purchase / UGC descriptor.
