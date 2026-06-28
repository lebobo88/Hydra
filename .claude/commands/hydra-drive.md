---
description: "Attended (host-bridged) execution: drive the Hydra supervisor lifecycle IN-CONTEXT so you follow along, with engineering generation + judging surfacing as visible Agent subagents."
argument-hint: "<goal text> [--squad slug,slug] [--budget 50]"
model: opus
---

# /hydra:drive

Drive a goal through Hydra **attended** — the host session itself plays the
supervisor and follows along, instead of detaching a headless `hydra run --live`
subprocess you cannot watch. The **deterministic Python engine stays
authoritative** (HydraState budget, checkpoint, the pp attempt/verdict ledger,
judge routing, finalize gates); you only fulfil the generate + judge steps as
**visible `Agent` subagents**.

This is the same governance as the detached path — you are NOT hand-emulating the
supervisor and NOT hand-writing engine source. You call the real engine MCP
tools; the engine records every attempt/verdict and charges budget.

## When this runs

`/hydra:drive` is always attended. `/hydra:run` is attended by default when
`HYDRA_HOST_DRIVEN=1` (the session default, set in `.claude/settings.json`) and
detached when it's unset/`0`.

## Steps

1. **Plan (non-detaching).** Call `hydra.workflow.plan {goal, squad?, budget?}`.
   It routes + decomposes and returns `{workflow_id, selected_squads, tasks
   (TaskState[]), requires_human_approval, pending_hitl, budget}` WITHOUT
   dispatching. Keep the `workflow_id` — it threads every later call.
2. **Approval gate.** If `pending_hitl` is set (`requires_human_approval`),
   render it and STOP. The operator resolves via `/hydra:approve <workflow_id>`
   (which calls `hydra.workflow.resume`) before you step into engineering.
3. **Drive engineering, one stage at a time.** Loop:
   a. Call `hydra.workflow.step {workflow_id}`. It scaffolds a pp run for the next
      engineering task and returns `{status:"awaiting_host", host_action, run_id}`
      — or `{status:"no_pending_engineering_task"}` when engineering is done
      (exit the loop).
   b. The `host_action.agent_type` is `engineer` (first) or
      `judge-cross-vendor`/`judge-same-vendor` (after the attempt). **Spawn that
      visible `Agent` subagent** with the provided `prompt`/`artifact_text` and
      `cwd` (an isolated `.harness/worktrees/` worktree — write-safe under the
      `hydra-block-direct-write` hook; the engine merges it back on a passing
      finalize).
   c. Call `hydra.workflow.submit_host_result {workflow_id, run_id, call_key,
      result}` with the subagent's output:
      - engineer → `{text, cost_usd, tokens_in, tokens_out, model}`
      - judge → `{outcome:pass|revise|fail, critique_md, judge_producer,
        judge_model_id, score_json, cost_usd}`
   d. The response is either the next `host_action` (the judge, then the next
      stage) or a terminal `{status:"complete"|"surfaced"}` carrying the real
      `final_status`, smoke result, `merge`, and budget charge. On terminal, go
      back to (a) for the next stage.
4. **Skill-squad work** (rlm-gaming, garland, etc.): run the squad Skill in-host
   as usual; submit any emitted engineering envelopes via
   `hydra.workflow.submit_envelopes` (those still drive the headless engineering
   loop) OR drive them attended via step/submit. Garland-bound envelopes are
   `deferred_to_host`.
5. **Synthesis.** When engineering is done and skills are resolved, read the
   workflow `trace.jsonl` + task results and present a conversational summary +
   artifact/commit paths.

## Hard rules (unchanged)

- You drive the REAL engine MCP tools — never hand-emulate the ledger, never
  hand-write engine source. The engineer subagent writes; the engine records.
- Budget tripwires stay live: `submit_host_result` charges accrued cost on the
  checkpointed HydraState at each stage finalize.
- A pp `finalize_run` downgrade (or a failed worktree merge-back) is surfaced,
  never laundered into "complete".

## Examples

```
/hydra:drive Add idempotency-key support to the payments API
/hydra:drive --squad engineering Fix the off-by-one in the pagination cursor
/hydra:drive --budget 40 Refactor the retry helper to exponential backoff
```
