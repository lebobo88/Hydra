---
description: "Multi-squad campaign template: routes executive + creative + engineering in one workflow with explicit dependency wiring."
argument-hint: "<campaign goal> [--launch-date YYYY-MM-DD] [--budget <usd>]"
model: opus
---

# /hydra:campaign

A higher-level alias for `/hydra:run` that pre-wires the canonical three-squad pattern:

```
executive (strategy + pricing + comms approval)
  ├──► creative (press kit + in-app + social + video)
  └──► engineering (pricing page + telemetry + flag rollout)
            │
            ▼
   synthesizer (one DECISION_RECORD + go-live runbook)
```

Use this when a campaign explicitly needs creative AND engineering AND executive coordination. For single-squad work, prefer `/hydra:run`.

## Example

```
/hydra:campaign Launch our new tier-3 plan on June 1 --budget 8000
```

This emits:
- `CREATIVE_BRIEF` → `creative` squad (RLM `/rlm-exec-creative-brief`)
- `PRD` → `engineering` squad (PP `/pp:team feature-team`)
- `CSuiteDecisionPacket` → `executive` squad (ES `/board-meeting --format brief`)

Synthesizer merges into a single launch runbook artifact.
