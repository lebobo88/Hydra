---
description: "Attended (host-bridged) execution: drive the Hydra supervisor lifecycle IN-CONTEXT so you follow along, with engineering generation + judging surfacing as visible Agent subagents."
argument-hint: "<goal text> [--squad slug,slug] [--budget 50]"
model: opus
disable-model-invocation: false
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

`/hydra:drive` is always attended — and so is every interactive `/hydra:run`,
which follows this same runbook. Detached execution (`hydra.workflow.launch`)
is automation-only (cron / external callers / the cross-repo fleet), gated by
`HYDRA_ALLOW_DETACHED=1`.

## Steps

1. **Plan (non-detaching).** Call `hydra.workflow.plan {goal, squad?, budget?}`.
   It routes + decomposes and returns `{workflow_id, selected_squads, tasks
   (TaskState[]), requires_human_approval, pending_hitl, budget}` WITHOUT
   dispatching. Keep the `workflow_id` — it threads every later call.
2. **Approval gate.** If `pending_hitl` is set (`requires_human_approval`),
   render it and STOP. The operator resolves via `/hydra:approve <workflow_id>`
   (which calls `hydra.workflow.resume`) before you step into engineering.

   With the plan phase on at non-`trivial` rigor, this gate deliberately
   stands down for `high_risk` and the decision moves to `plan_gate`, where
   the operator approves a plan they can actually read instead of a squad list
   they cannot. `budget_exhausted` still stops here — budget is not a risk
   signal a plan can inform. With the flag off, this gate behaves exactly as
   it always has.

2a. **The plan leg** (plan phase on, rigor not `trivial`). The first task the
   cursor returns is a `planning` task and the barrier holds everything else.
   Drive it like any other attended task, and return the authored `PLAN` in
   `submit_host_result`'s `emitted_envelopes` — **not** through
   `hydra.workflow.submit_envelopes`, which detaches and would race the resume
   lock. The engine validates the plan (a cyclic or dangling dependency is
   refused at construction), writes `docs/plans/plan-<slug>.html`, judges it,
   and parks at `plan_gate`. Render the plan and STOP: path, step table with
   dependencies, judge verdict and vendor, budget estimate, open questions, and
   whether the artifact is committed — no node ever runs `git commit`, so say
   so rather than letting the operator assume. Steps become tasks only on
   approval.
3. **Drive engineering, one stage at a time.** Loop:
   a. Call `hydra.workflow.step {workflow_id}`. It scaffolds a pp run for the next
      engineering task and returns `{status:"awaiting_host", host_action, run_id}`
      — or `{status:"ready_to_finalize"}` when every task is attended-done
      (exit the loop and go to step 5).
   b. The `host_action.agent_type` is `engineer` (first) or
      `judge-cross-vendor`/`judge-same-vendor` (after the attempt). **Spawn that
      visible `Agent` subagent** with the provided `prompt`/`artifact_text` and
      `cwd` (an isolated worktree under `resolve_worktree_root()` — default
      `<AIAPP_BASE>/.hydra-worktrees/<repo_id>/`, overridable via
      `HYDRA_WORKTREE_ROOT`, outside the target repo — write-safe under the
      `hydra-block-direct-write` hook, which resolves the same root; the
      engine merges it back on a passing finalize).
   c. Call `hydra.workflow.submit_host_result {workflow_id, run_id, call_key,
      result}` with the subagent's output:
      - engineer → `{text, cost_usd, tokens_in, tokens_out, model}`
      - judge → `{outcome:pass|revise|fail, critique_md, judge_producer,
        judge_model_id, score_json, cost_usd}`. For the same-vendor judge,
        `judge_producer` MUST be `"claude-same-vendor-host"` (never `"claude"`
        — pp vendor pinning rejects generator-identical producer+model, and
        the rejection currently surfaces only as an error payload).
   d. The response is either the next `host_action` (the judge, then the next
      stage), an `await_smoke` poll (see below), or a terminal
      `{status:"complete"|"surfaced"}` carrying the real `final_status`, smoke
      result, `merge`, and budget charge. On terminal, go back to (a) for the
      next stage.
   e. **`await_smoke` (Hydra#70)** — a PASSING judge verdict is recorded
      immediately, then the repo smoke runs as a **detached background job**
      (`hydra_core.smoke_job`) instead of blocking inside the
      `submit_host_result` call (a smoke that ran past the MCP call's own
      timeout used to orphan its process tree with no verdict ever recorded).
      `submit_host_result` returns promptly with `{status:"awaiting_host",
      state:"await_smoke", host_action:{action:"poll_smoke", instructions}}`
      — there is **no agent to spawn**. Poll by either:
      - calling `hydra.workflow.step {workflow_id}` again (it detects the
        in-flight job for the current task and polls instead of opening a new
        stage — it never mints a second pp run/worktree while one is
        pending), or
      - re-issuing `submit_host_result` with the SAME judge `call_key` (a
        harmless poll — it never re-records the verdict or restarts the job).
      Keep polling (a short delay between calls) until the response is
      terminal. A lost job (process vanished, or it ran past its own
      `HYDRA_SMOKE_TIMEOUT_S` deadline) is classified as an infra failure —
      its whole process tree is killed — and the stage finalizes
      non-complete; it never wedges in `await_smoke` forever. A
      just-launched job may report still-launching (non-terminal, same as
      "still pending") for up to the startup bound
      (`HYDRA_SMOKE_LAUNCH_GRACE_S`, default 60s) before a total absence of
      worker evidence is finalized lost — this is normal for a worker that
      is simply slow to start, not itself a failure.
4. **Non-engineering squads** (claude-skill / agent-impersonation packs:
   executive, garland, rlm-gaming, marketing-*, …) are ALSO driven by the same
   step/submit loop: when the next pending task belongs to such a pack, `step`
   returns a lightweight **squad cursor** host_action instead of an engineering
   one — `{call_key: "squad-<task_id>-0", agent_type: <pack lead agent>,
   cwd: <pack checkout>, prompt: <task text>}`, cursor state
   `await_squad_agent`, `run_id` = the task id (no pp run, no worktree — these
   squads produce documents, not engine code). Spawn that visible pack-lead
   `Agent`, then `submit_host_result {workflow_id, run_id: <task_id>, call_key,
   result: {text, cost_usd, tokens_in, tokens_out}}` — the engine records the
   artifact, charges budget exactly once (`already_charged` on duplicate
   submits), and marks the task attended-done (RA-12a: a later resume will NOT
   re-dispatch it). When the step result carries `leg` (`"head"` or `"lead"`),
   the result may also add an optional `dissents: [{statement, rationale?}]`,
   built from the bullets of the agent's `## Dissents` section (the prompt
   explains the format). That field is the only channel: dissents left in the
   text or in an emitted envelope are not recorded. A head-less squad result is
   unchanged. If the pack emits engineering envelopes (DEV_TASK/PRD),
   submit them via `hydra.workflow.submit_envelopes` as before.

   **Parallel head legs (W2.4, Hydra#92).** A squad step response carries
   `host_actions` only when two or more head legs are served: spawn one
   visible `Agent` per entry, in the declared order, and submit each entry with
   its own `run_id` and `host_action.call_key`. Exactly one served leg gives the
   single-leg response with no `host_actions`.
   <!-- test_three_heads_one_step_returns_three_actions_in_declared_order -->
   The top-level `host_action` is the first entry, so a host that reads only it
   still converges one leg at a time.
   <!-- test_three_head_serial_characterization_matches_capture -->
   Submits may arrive in any order and still converge.
   <!-- test_six_submit_orders_converge -->
   <!-- test_two_process_submits_converge -->
   A step always returns every open leg. A step opens every unrecorded head that is not yet open unless the budget gate blocks it; it opens nothing new when every unrecorded head is already open or budget-blocked.
   <!-- test_restep_with_all_legs_open_is_idempotent -->
   <!-- test_restep_after_one_submit_returns_remaining_open_legs_no_lead -->
   <!-- test_budget_block_mixed_order_reserves_open_head -->
   `budget_blocked_heads` names the heads that were not opened because the
   budget is exhausted; open legs are still re-served in the same response.
   <!-- test_budget_block_mixed_order_reserves_open_head -->
   With nothing open, the step returns `budget_blocked`.
   <!-- test_budget_exhausted_with_nothing_open_returns_budget_blocked -->
   If a step fails partway it returns the error. The next step re-serves the
   legs that are open, and opens each remaining head only if that head now
   resolves, its dispatch seam succeeds and the budget gate does not block it.
   <!-- test_seam_failure_on_second_leg_then_restep -->
   <!-- test_head_unresolved_on_second_leg_then_restep -->
   The lead leg appears only after every head has a result.
   <!-- test_lead_only_after_all_heads_submitted -->
   `submit_host_result` may return `status: "resume_in_progress"` (another
   submit holds the resume lock); retry the same submit. A duplicate submit is
   reported `already_charged` and is not charged again.
   <!-- test_submit_refused_while_resume_lock_held -->
   <!-- test_two_process_submits_converge -->
   <!-- test_duplicate_submit_not_recharged -->
   Engineering stays single-stream: an engineering step never returns `host_actions`.
   <!-- test_baseline_responses_match_capture -->
5. **Synthesis — call the engine, do not improvise it.** When every task is
   attended-done, `step` returns `{status:"ready_to_finalize"}` (the legacy
   `no_pending_task` field is still set for older hosts). Call
   `hydra.workflow.finalize {workflow_id}`: it materialises every attended
   result into squad `DECISION_RECORD` envelopes + artifact rows and resumes the
   graph so `synthesis → judge_synthesis → postcheck` run over the real outputs,
   persisting the engine `DECISION_RECORD` to episodic memory (RA-8) and driving
   the phase terminal. Then present THAT record — its decision line, rationale,
   preserved dissents and `artifact_refs` — plus the commit/worktree paths from
   the stage results. `{status:"tasks_pending", pending:[...]}` means a task is
   still open: go back to (3)/(4). A second finalize is a no-op
   (`{status:"already_finalized", decision_record_id}`). Never hand the operator
   a hand-written summary in place of the engine record.

## Resume after a timeout (G6)

`step`/`submit` are sync CLI subprocesses with ceilings (defaults: plan 180s,
step 900s, submit 1800s; env `HYDRA_PLAN/STEP/SUBMIT_TIMEOUT_S`). On overrun
you get a structured `{error: "<label>_timeout", remediation, ...}`:

- **submit timeout** — simply re-issue the SAME `submit_host_result` (same
  `call_key`, same result payload). The cursor + `call_key` idempotency and the
  `verdict_recorded_for` / `already_charged` markers guarantee every pp ledger
  write and budget charge happens exactly once across retries.
- **step timeout** — the killed subprocess can leave stale state, listed in the
  error's `stale_state` field: the workflow's `resume.lock` (delete it), an
  orphan pp run (`finalize_run` it `aborted` to release the project lock), and
  an orphan `attended-*` worktree under `resolve_worktree_root()`
  (`git worktree remove`, or let the Hydra-side janitor sweep it once the
  cursor reaches a terminal state — it never removes a non-terminal or
  cursor-less worktree, and never deletes a branch). Clean those, then
  re-issue `step`. First-stage steps on large repos pay a
  full-suite smoke **baseline** (cached per HEAD sha; a baseline that exceeds
  `HYDRA_BASELINE_TIMEOUT_S` writes a degraded `<sha>.timeout.json` marker so
  subsequent stages skip the re-run instead of re-paying it).

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
