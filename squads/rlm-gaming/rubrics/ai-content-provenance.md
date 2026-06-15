---
id: ai-content-provenance@1
bare_id: ai-content-provenance
kind: compliance
version: 1
title: "Generative-AI assets carry tamper-evident provenance + documented IP lineage"
owner_head: art-director
co_owner: game-security-gate
hitl_required: true
---
# AI content provenance rubric

The Artisan designs the asset brief; Garland generates + C2PA-signs; The Sentinel
refuses the ship of an unsigned one. This rubric gates any shipped gen-AI asset.

Score 0..1 per cluster:

- **c2pa_signed**: every gen-AI binary carries a C2PA manifest (model, prompt,
  base assets, editor chain). Signing is Garland's `governance-c2pa` job — verify
  the manifest exists, do not generate here. NOTE: C2PA proves *tamper-evident
  provenance* (what made this, and that it wasn't altered since) — it does NOT by
  itself prove IP ownership or licensing. IP lineage is a separate cluster below.
- **model_licensing**: the generating model's license permits commercial game
  shipping; no model trained on disallowed data for this use.
- **base_asset_lineage**: any conditioning/reference assets are owned or
  licensed; no third-party IP leaked into training/conditioning.
- **style_bible_conformity**: asset matches the art bible (cross-ref The
  Artisan's style-similarity gate).
- **human_review**: a human approved the asset for ship (HITL); fully autonomous
  asset shipping is forbidden.
- **disclosure**: gen-AI usage disclosed where the platform/region requires
  (e.g. Steam AI disclosure).
- **takedown_path**: a documented path to replace/remove an asset if an IP claim
  arises.

Outcome:
- pass: every cluster ≥ 0.8 AND C2PA manifest present AND human approved.
- revise: any cluster in [0.6, 0.8).
- fail: any cluster < 0.6 OR unsigned asset OR unclear model/base-asset license.
