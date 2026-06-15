---
id: platform-cert-readiness@1
bare_id: platform-cert-readiness
kind: compliance
version: 1
title: "Build meets platform technical certification requirements"
owner_head: compliance-cert
hitl_required: true
---
# Platform certification-readiness rubric

The Arbiter gates cert submission on this. Maps the build to each target
platform's technical certification requirements (TRC/XR/Lotcheck/Steamworks).

Per-platform checklist coverage (artifact MUST address the target set):

| Platform | Cert program | Always-checked essentials |
|---|---|---|
| PlayStation | TRC | trophies, suspend/resume, controller disconnect, save-data integrity, account/sign-in, error messages |
| Xbox | XR / XGSP | achievements, MSA sign-in, suspend/resume/PLM, controller, store flows |
| Nintendo Switch | Lotcheck | sleep/wake, controller modes (handheld/docked/detached), user mgmt, save integrity |
| Steam | Steamworks (no cert, but) | Deck verified checklist, cloud saves, achievements, input, EULA |
| iOS / Android | App Store / Play review | privacy nutrition labels, IAP, age rating, background behavior, permissions |

Score 0..1 per cluster:

- **lifecycle**: suspend/resume/sleep/wake/PLM handled without state loss.
- **input**: all controller modes + disconnect/reconnect handled gracefully.
- **save_integrity**: atomic saves, corruption recovery, cloud-save conflict
  resolution (cross-ref `save-data-atomicity` missability check).
- **account_store**: sign-in, entitlement, IAP restore, store flows compliant.
- **error_messaging**: platform-mandated error strings/codes present.
- **accessibility_cert**: platform a11y requirements met (cross-ref
  `game-accessibility-guidelines`).
- **trc_evidence**: a per-requirement pass/fail log with evidence, not a claim.

Outcome:
- pass: every cluster ≥ 0.8 AND a per-requirement evidence log exists AND The
  Arbiter approved.
- revise: any cluster in [0.6, 0.8) OR evidence log incomplete.
- fail: any cluster < 0.6 OR save-integrity unproven OR lifecycle untested.
