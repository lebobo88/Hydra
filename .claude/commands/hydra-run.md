---
description: "Run a goal through Hydra's full supervisor lifecycle. Routes to 1+ squads, executes, synthesizes, postchecks."
argument-hint: "<goal text> [--squad slug,slug] [--budget 50] [--risk low|medium|high] [--repo repo_id] [--repos id,id,...]"
model: opus
---

# /hydra:run

Drive the user goal through Hydra's supervisor lifecycle:

`intake → planning → approval(?) → dispatch → executing → synthesis → postcheck`

## Hybrid execution model (READ FIRST)

Hydra runs **hybrid**: the deterministic Python engine drives engineering
dispatch + execution; you (the LLM) are the conversational front and the **only**
executor of claude-skill squads (rlm-gaming, garland) — because skills cannot run
headlessly. **You never hand-write engine source.** Engineering code is produced
by the pair-programmer harness, in Python, through the pp stage loop. The
`hydra-block-direct-write` hook enforces this (blocks your direct Write/Edit to
engine source when `HYDRA_ENFORCE_ROUTING=1`).

### Two execution modes (the `HYDRA_HOST_DRIVEN` toggle)

- **Attended (default, `HYDRA_HOST_DRIVEN=1`)** — drive the lifecycle IN-CONTEXT
  so the operator follows along: `hydra.workflow.plan` → loop
  `hydra.workflow.step` (spawn the visible `engineer`/judge `Agent`) →
  `hydra.workflow.submit_host_result`. The engine stays authoritative (ledger,
  budget, judge routing, finalize gates); the engineer writes into an isolated
  `.harness/worktrees/` worktree merged back on pass. **This is exactly
  `/hydra:drive`** — see that command for the full runbook.
- **Detached (`HYDRA_HOST_DRIVEN` unset/`0`)** — hand the goal to
  `hydra.workflow.launch` (detached `hydra run --live`); the engine runs the pp
  stage loop headlessly in a background subprocess (no follow-along).

Both modes use the SAME engine + governance; they differ only in WHERE the stage
loop runs (your context vs a detached subprocess). When attended, follow the
`/hydra:drive` steps below instead of step 2's `launch`.

The deterministic engine is reachable via the `hydra_control` MCP tools (or the
CLI they wrap):

- `hydra.workflow.launch` — start a workflow (`hydra run --live`); engineering
  dispatches through the full pp stage cycle (`start_stage → generate →
  archive_artifact → record_attempt → record_verdict → finalize_stage →
  finalize_run`) with cross-vendor judges. **This is how engineering executes.**
- `hydra.workflow.submit_envelopes` — inject host-completed skill envelopes
  (DEV_TASK/PRD/ARCH_RFC) back into a running workflow so the engine forwards
  them to engineering and runs the pp loop. **This is the rlm-gaming → engineering
  seam** (see Skill-squad path below).
- `hydra.workflow.resume` — resolve a pending HITL gate.

## Steps

1. Parse `$ARGUMENTS` into `{goal, squad?, budget?, risk?, repo?, repos?}`. `--repo <id>` / `--repos <id,id>` resolve through the allow-list in `hydra_core.repo_registry` (raw paths rejected); `--budget <usd>` caps spend.
2. **Engineering / mcp-squad work** (or "uncertain — let the router decide"): call `hydra.workflow.launch` with `{goal, squad?, budget?}`. It returns a `workflow_id` immediately (fire-and-attach). The Python engine routes, dispatches, and drives the pp stage loop — **do not** invoke `Agent({subagent_type:"engineer"})` or write code yourself.
3. **Skill-squad work** (rlm-gaming game design, garland creative): run the squad's **Skill in-host** (only you can). Capture its `DECISION_RECORD` + `emitted_envelopes`. For each emitted engineering envelope (DEV_TASK/PRD), call `hydra.workflow.submit_envelopes({workflow_id, envelopes})` so the engine dispatches engineering deterministically. Garland-bound envelopes (CREATIVE_BRIEF/SHOT_LIST/ASSET_JOB) are returned as `deferred_to_host` — run the garland Skill in-host for those.
4. **HITL**: when a launch/ingest surfaces a pending gate, STOP and render it — operator resumes with `/hydra:approve` or `/hydra:resume` (which call `hydra.workflow.resume`).
5. **Synthesis**: on completion, read the workflow `trace.jsonl` + the final `DECISION_RECORD` and present a conversational summary + artifact paths.

## Cross-repo fleet

Use `--repos <id,id,...>` (or the synonym `--fleet`) to launch a **parallel engineering fleet** across multiple allow-listed sibling repos. Each named repo gets its own pair-programmer run dispatched concurrently; results are aggregated into one `DECISION_RECORD`.

Rules:
- `--repos` requires >=2 distinct allow-listed ids for fleet mode. Exactly 1 id behaves like `--repo`.
- `--repos` and `--repo` are mutually exclusive; using both surfaces an HITL with `reason=high_risk`.
- Ids are comma-separated; duplicates are silently deduplicated (first-occurrence order).
- Unknown ids surface an immediate HITL (`reason=high_risk`, `gate_node=intake`, options=["abort"]).
- Fleet is engineering-only; `selected_squads` is locked to `["engineering"]`.
- Cancellation propagates: if any repo's run surfaces, the fleet cancels remaining in-flight runs.

Example:
```
/hydra:run "Fix the fail-open bug --repos agentsmith,theeights,xenia"
```

## Examples

```
/hydra:run Audit our customer-data retention policy for GDPR compliance
/hydra:run Launch Q3 campaign for billing-microservice (press kit + pricing-page update)
/hydra:run --squad engineering Add idempotency-key support to the payments API
/hydra:run --budget 200 --risk low Evaluate acquiring CompetitorX for $80M
/hydra:run --repo agentsmith --squad engineering Fix AS-GV-2 governance validation bug
/hydra:run "Fix the fail-open bug --repos agentsmith,theeights,xenia"
/hydra:run "Upgrade dependencies --fleet pair-programmer,agentsmith"
```
