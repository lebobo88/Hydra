---
description: "Run a goal through Hydra's full supervisor lifecycle. Routes to 1+ squads, executes, synthesizes, postchecks."
argument-hint: "<goal text> [--squad slug,slug] [--budget 50] [--risk low|medium|high] [--repo repo_id]"
model: opus
---

# /hydra:run

Drive the user goal through `hydra_core.supervisor.build_supervisor`. Lifecycle:

`intake → planning → approval(?) → dispatch → executing → synthesis → postcheck`

## Steps

1. Parse `$ARGUMENTS` into `{goal, squad?, budget?, risk?, repo?}`. The optional `--repo <repo_id>` argument sets `HydraState.target_repo_id`; the engineering squad resolves it via the allow-list in `hydra_core.repo_registry` before invoking pair-programmer. Raw paths are rejected — only allow-listed ids are accepted.
2. Adopt the `hydra-supervisor` agent persona (`.claude/agents/hydra-supervisor.md`).
3. Run the supervisor graph via the host-bound dispatcher (which proxies to `pp_harness`, `hydra_memory`, `executive_suite` filesystem MCP, `rlm_creative` filesystem MCP).
4. If an HITL request fires, STOP and surface the request — operator resumes with `/hydra:approve` or `/hydra:resume`.
5. On completion, print the final `DECISION_RECORD` summary + paths to the trace and archived artifacts.

## Examples

```
/hydra:run Audit our customer-data retention policy for GDPR compliance
/hydra:run Launch Q3 campaign for billing-microservice (press kit + pricing-page update)
/hydra:run --squad engineering Add idempotency-key support to the payments API
/hydra:run --budget 200 --risk low Evaluate acquiring CompetitorX for $80M
/hydra:run --repo agentsmith --squad engineering Fix AS-GV-2 governance validation bug
```
