---
name: budget-control
description: "Per-workflow budget enforcement: tripwire at 80% (downgrade model tier), HITL at 100%. Telemetry pattern for cost-per-task tracking."
---

# Budget Control

Workflows declare `Constraints.budget_usd`. Every squad return updates `HydraState.budget.spent_usd`. The CFO gate (`hydra-cfo-gate`) tripwires at thresholds.

## Tiers

- `< 0.5` consumed: opus-tier free use.
- `0.5..0.8`: prefer sonnet for non-creative-quality tasks.
- `0.8..1.0`: downgrade to haiku where rubric allows; emit `budget_tripwire` trace event.
- `>= 1.0`: PAUSE; emit `HITL_REQUEST(reason="budget_approval")`.

## Cost Attribution

Every `record_attempt` event posted by the dispatcher includes `tokens_in`, `tokens_out`, `cost_usd`. Roll up per squad and per task to detect unit-economics anomalies (cost-per-task >> rolling-avg → defer to executive `cfo` for investigation).

## Override Semantics

`/hydra:resume <wf> --modify-budget <usd>` is the only way to lift a budget pause. It writes a `budget_override` audit row. There is no auto-renewal.
