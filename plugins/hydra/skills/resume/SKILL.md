---
description: "Resume a paused workflow with a non-approval action: reject, modify-budget, force-dispatch, change-squads."
argument-hint: "<workflow_id> --reject | --modify-budget <usd> | --force-dispatch | --squads <a,b>"
model: sonnet
disable-model-invocation: true
---

# /hydra:resume

<authority_boundary>
This is a native Claude Code interface for an explicit human HITL decision.
Only `hydra.workflow.resume` (or the matching Hydra CLI) may validate and
persist the decision, modify a budget, alter squads, or re-enter the graph.
</authority_boundary>

**Prerequisite:** the attended route reads the operator identity from the
`hydra_control` MCP server's OWN environment, not from the operator's client
shell. Both `HYDRA_OPERATOR_ID` and `HYDRA_OPERATOR_KEY` must be set in the
`hydra_control` entry's `env` block in `~/.hydra/backends.json`, and the
`hydra_control` server must be RESTARTED after changing them — a variable
exported in the operator's own shell never reaches the already-running
server process. Symptom when either is missing: every attended resume action
(`--reject`, `--modify-budget`, `--force-dispatch`, `--squads`,
`--modify-plan`) returns `{ok: false, error: "operator_identity_required"}`.

Companion to `/hydra:approve`. Drives non-approve resume paths. The
descriptions below are the LEGACY (non-attended, `--live`) CLI behavior —
re-entering the graph/dispatch. On the attended default (gate-only, see the
Note below), every one of these actions instead applies its state patch and
STOPS without ever calling `sup.invoke`; nothing here "dispatches" or
"re-enters" on that route:

- `--reject`: mark the workflow `surfaced`, write a rejection note.
- `--modify-budget 250`: update `state.budget.budget_usd`; legacy path re-enters dispatch, attended gate-only path only patches the budget and stops.
- `--force-dispatch`: dispatch even though a gate failed (logs a `policy_override` event; operator owns the risk) on the legacy path; the attended gate-only path records the same `policy_override` event and per-action patch but never actually re-enters dispatch. At `plan_gate` this additionally stamps `plan_status="bypassed"` and appends a governance note to the plan artifact, so proceeding without an approved plan is evidence rather than a gap.
- `--squads engineering,garland`: replace `selected_squads`; legacy path re-plans, attended gate-only path only patches `selected_squads` and stops.
- `--modify-plan --critique-ref <path-or-memoryref>`: request a plan revision.
  Valid **only** at `plan_gate`. Bumps `plan_revision`, sets
  `plan_status="authoring"`, and seeds one planning task carrying the critique
  and the prior plan's id as `supersedes`. Bounded by
  `HYDRA_PLAN_MAX_REVISIONS` (default 2); at the ceiling the gate offers only
  `approve` and `abort`.
  **The critique travels as a reference, never as `--option`.** That field is
  capped at 200 characters of a restricted character class because it guards a
  string bound for a subprocess argument list — real prose would be truncated
  or refused outright. The referenced file must resolve inside the project
  root; an escaping path is refused rather than read.

Rejecting at `plan_gate` parks the workflow and deliberately does **not**
trigger a re-plan. An engine that authors a fresh plan the instant one is
rejected is a loop the operator cannot stop. The rejected plan stays on disk,
marked rejected — it is evidence of a decision, not waste.

First render the current pending HITL request from `python -m hydra_core.cli
status <workflow_id>` and obtain an explicit operator decision. Then call
`hydra.workflow.resume` with
the matching action and option (or the matching `hydra resume` CLI form). Do
not patch checkpoint state or any trace directly.

Note (RCA path K, EIGHTS-RECORD-OUTCOME-RCA-2026-09-16 §7; RESOLVE-GATE-ONLY
follow-up): the transport `hydra.workflow.resume` picks is governed by
`HYDRA_ALLOW_DETACHED`, not by whether the workflow itself is attended or
detached.

- **Detached-allowed** (automation, `HYDRA_ALLOW_DETACHED=1`): launches a
  DETACHED `hydra resume --live` subprocess and returns immediately
  (`{ok, launched: true, pid, log}`). Unchanged.
- **Attended (the normal interactive session, gate not set)**: runs
  `hydra resume --gate-only` as a real, synchronous CHILD PROCESS spawned by
  `hydra_control` (`_run_cli_json`, `subprocess.run`), on `_NullDispatcher`,
  and waits for it in-band. This resolves the gate (lock, operator-capability mint+verify, spool
  prune, per-action state patch clearing `pending_hitl`) and returns
  WITHOUT EVER re-entering the compiled graph — no `sup.invoke`, no
  `node_dispatch`, no squad of any kind runs, not even a stub result. The
  result JSON says so explicitly (`graph_reentered: false`, a `note` naming
  `hydra.workflow.step`); the host's existing step/submit loop continues the
  workflow from its own cursor afterward, exactly as if it had never
  paused. This JSON body is ADDITIVE relative to the pre-gate-only shape —
  `gate_only` and (when applicable) `eights_resolution` are new fields, not
  a byte-for-byte-identical document.

  The operator-identity check covers EVERY action that can mutate
  checkpoint state or the spool, including `--reject` (not only
  `approve`/`force-dispatch`/`modify-budget`/`change-squads`/`modify-plan`).
  Identity is verified TWICE, at two different points: a pure, state-free
  precheck (operator id known + a signing key present) runs BEFORE the
  resume lock is even acquired and before the checkpoint is opened, so an
  unauthenticated call creates nothing on disk at all (no `.hydra/<workflow>/`
  lock directory, no checkpoint database); the real mint+verify (bound to
  the actual pending gate) runs again immediately after the checkpoint
  loads, before any state mutation. If either check fails — unknown
  operator (no `HYDRA_OPERATOR_ID`), no signing key (no
  `HYDRA_OPERATOR_KEY`), or a capability that fails verification — the
  resume REFUSES with `{ok: false, error: "operator_identity_required"}`
  before touching any state — it does not silently proceed with a degraded
  token. Once the gate clears locally, TheEights' matching pending ticket is
  resolved NOW with one narrow live call (list + resolve, never a replay or
  spool drain), bounded by its OWN inner deadline
  (`HYDRA_GATE_ONLY_EIGHTS_TIMEOUT_S`, default `8` seconds — see **Timeout
  and retry** below); the result reports `eights_resolution: "resolved"` on
  success or `eights_resolution: "unavailable"` (with a reason, e.g.
  `"deadline"` or `"eights_unreachable"`) when TheEights cannot be reached
  in time — never a hardcoded "deferred", and never a spooled retry on this
  route. A retry after an interrupted or timed-out gate-only resume
  (killed, or the inner deadline fired, between its checkpoint patch and
  TheEights resolution) reconciles the already-resolved gate against
  TheEights' ledger AND any stale spooled HITL request, rather than leaving
  either orphaned.

  `recover-stalled-stage` is the ONE resume action this route REFUSES
  outright, before any subprocess runs (`{ok: false, error:
  "recovery_is_live_operation"}`): it is a LIVE operation — a real
  `MCPStdioDispatcher` that can replay a pp verdict, run
  smoke/finalization, and merge code — not a gate resolution. Only the
  DETACHED route (`hydra resume --live --action recover-stalled-stage`,
  requires `HYDRA_ALLOW_DETACHED=1`) may run it.

  `--gate-only` and `--live` are mutually exclusive on the `hydra resume`
  CLI — never pass both. `--live` builds a real `MCPStdioDispatcher` and
  starts a background eights spool drain, exactly the live side effects
  the gate-only route promises never to trigger; the CLI refuses the
  combination before doing any work (argparse-level and, defensively, a
  matching runtime check).

  **Timeout and retry:** the MCP transport (`_run_cli_json` in
  `mcp_servers/hydra_control/server.py`) runs `hydra resume --gate-only` as
  a real, synchronous CHILD PROCESS (`subprocess.run([sys.executable, "-m",
  "hydra_core.cli", ...])`) and waits for it in-band — this is a real
  subprocess every time, never an in-process call. The work the child
  itself does once running (mint+verify, a checkpoint patch, a spool prune,
  and one narrow live TheEights list+resolve call — no dispatch, no live
  squad/engineering work, no replay) is what "no subprocess" refers to
  elsewhere in this doc; the CHILD ITSELF is always a real subprocess.

  Two DISTINCT, nested timeouts govern this call:
  - The WHOLE child is bounded by an outer timeout
    (`HYDRA_RESUME_TIMEOUT_S`, default `45` seconds). Everything the child
    does besides the TheEights call is sub-second, so this outer bound
    exists mainly to catch a genuinely stalled child process (Python/module
    cold start, an unrelated hang) — not to bound the TheEights call
    itself.
  - The live TheEights list+resolve call, INSIDE that child, has its own,
    separate, tighter inner deadline (`HYDRA_GATE_ONLY_EIGHTS_TIMEOUT_S`,
    default `8` seconds), enforced independently of the outer bound and of
    whatever timeout the MCP transport to TheEights itself might apply.
    When this inner deadline fires, the local gate is ALREADY cleared (the
    checkpoint patch happens before this call), so the child still returns
    normally with `eights_resolution: "unavailable"` (reason: `"deadline"`)
    rather than hanging or being killed — it is TheEights' ledger entry
    that is left unresolved, not the workflow's own state.

  If the OUTER (45s) timeout fires instead, that does signal a genuinely
  stalled child process (not merely a slow TheEights daemon, which the
  inner deadline already absorbs on its own).

  Either way, retry the identical `hydra.workflow.resume` call (same
  `workflow_id`/`action`/`option`); the route is idempotent. A retry lands
  on the no-pending-gate path (the checkpoint already shows the gate
  cleared) and reconciles TheEights' ledger for that same gate with the
  same bounded call — reporting `eights_resolution: "resolved"` if a
  pending ticket was still there and got resolved, or `"none_pending"` if
  TheEights already shows no matching ticket (already resolved earlier, or
  nothing was ever pending) — never treating "no ticket" as an error. It
  also reconciles a stale spooled HITL request left by a prior call that
  was interrupted between its checkpoint patch and its spool prune.

  To inspect state directly rather than trusting the transport's own
  report — for example after repeatedly exceeding the outer window — use
  `hydra status <workflow_id>` / `/hydra:status`; it reads the checkpoint
  directly and does not depend on this resume transport at all. Do **not**
  use `hydra.workflow.step` for this: it opens the next attended
  engineering stage and MUTATES the workflow rather than merely reporting
  on it.
