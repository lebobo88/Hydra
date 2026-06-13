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
- Allow-listed ids (`hydra_core/repo_registry.py`): `hydra`, `pair-programmer`, `agentsmith`, `theeights`, `xenia`, `executivesuite`, `senate`, `marketbliss`, `rlm-creative`. Raw paths are rejected — the allow-list is the injection guard. Each id is resolved by a real `git rev-parse` check before dispatch.
- Unknown ids surface an immediate HITL (`reason=high_risk`, `gate_node=intake`, options=["abort"]).
- Fleet is engineering-only; `selected_squads` is locked to `["engineering"]` for fleet runs (only `entrypoint="mcp"` packs are fleet-eligible).
- Cancellation propagates: if any repo's run surfaces, in-flight runs for other repos are cancelled (not-yet-started runs return `cancelled`; runs already past their entry-check finish naturally — threads are not force-killed).

### Per-repo budget scoping

The workflow's global `--budget` is **equal-split across the fleet repos**
by `HydraState.allocate_repos` (exact to the micro-dollar, 1e-6 USD). Each
repo charges against its own `repo_budgets[id]` / `repo_spend[id]` ledger via
`charge_and_gate_repo`, so one repo overrunning its slice does not consume
another repo's allocation — budgets are isolated, not shared.

### Deterministic result merge

`dispatch_fleet` collects worker results via `as_completed` (so cancellation
fires on the first surfaced result) but stores each result by its **input
index**, so the merged result list is always in submission order regardless of
which repo finishes first. The synthesizer then merges the per-repo
`SquadResult`s into a single `DECISION_RECORD` deterministically.

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
