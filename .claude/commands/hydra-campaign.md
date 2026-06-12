---
description: "Multi-squad campaign template: routes executive + creative + engineering in one workflow with explicit dependency wiring."
argument-hint: "<campaign goal> [--launch-date YYYY-MM-DD] [--budget <usd>] [--repos id,id,...]"
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

## Cross-repo fleet

Use `--repos <id,id,...>` (or the synonym `--fleet`) to launch a **parallel engineering fleet** across multiple allow-listed sibling repos. Each named repo receives its own pair-programmer run dispatched concurrently; all results are aggregated into one `DECISION_RECORD`.

Rules:
- `--repos` requires >=2 distinct allow-listed ids for fleet mode. Exactly 1 id behaves like `--repo`.
- `--repos` and `--repo` are mutually exclusive; using both surfaces an HITL.
- Ids are comma-separated; duplicates are silently deduplicated (first-occurrence order).
- Unknown ids surface an immediate HITL (`reason=high_risk`, `gate_node=intake`, options=["abort"]).
- Fleet is engineering-only; `selected_squads` is locked to `["engineering"]` for fleet runs.
- Cancellation propagates: if any repo's run surfaces, in-flight runs for other repos are cancelled.

Example:
```
/hydra:campaign "Fix the fail-open bug --repos agentsmith,theeights,xenia"
```

## Examples

```
/hydra:campaign Launch our new tier-3 plan on June 1 --budget 8000
```

This emits:
- `CREATIVE_BRIEF` → `creative` squad (RLM `/rlm-exec-creative-brief`)
- `PRD` → `engineering` squad (PP `/pp:team feature-team`)
- `CSuiteDecisionPacket` → `executive` squad (ES `/board-meeting --format brief`)

Synthesizer merges into a single launch runbook artifact.
