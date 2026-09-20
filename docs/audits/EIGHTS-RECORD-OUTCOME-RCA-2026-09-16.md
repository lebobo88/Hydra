# RCA — TheEights record outcomes, the spool, replay, and the path to finishing the plan phase

**Status:** revision 6 — **passed cross-vendor review.** Findings confirmed by agy
`gemini-3.8-flash-medium` reviews B and C (pass) and A (revise, all points folded in);
the path forward (§6–§8) confirmed correct, complete and correctly ordered by
integrative review D (pass, "no required dependency is missing, and none is
superfluous"). Remediation is not implemented.

## Review history

| Rev | Reviewer | Outcome | What it changed |
|---|---|---|---|
| 1 | codex `gpt-5.6-terra` | revise | Reason-string classification unsafe (timeouts are `failed` too); gateway drops `isError`. |
| 2 | codex | revise | `exception:*` is not provably unsent; memory scope overstated. |
| 3 | codex | revise → pass | Idempotency needs a client-sent stable key; 26% of HITL ids empty. |
| 4 | — | withdrawn | Mapping to the goal showed rev 3's first remediation step (quarantine every `stub`) was wrong, despite passing review. |
| 5 | agy `gemini-3.8-flash-medium` | A **revise**, B **pass**, C **pass** | Replay telemetry was ≥ 99.7% test runs; the attended lifecycle does not drain the spool; path S and K constraints; `.partial` files. |
| 6 | agy `gemini-3.8-flash-medium` | D **pass** | Every rev-5 finding folded in; all figures re-measured. Review D confirmed §6–§8 and added three refinements marked **[D]**. |

Rev 4's own agy review hit the gateway's 1,800 s tool cap with no verdict; rev 5 was
therefore split into three focused reviews run directly through the agy CLI in
read-only plan mode. Before and after every judge run, the operator's pending spool,
dead-letter queue, TheEights state database and all three repositories were
verified byte-identical to a recorded baseline.

## Method

**Evidence bundle.** Every measurement comes from one read-only script whose exact
text and verbatim output are in
`C:\Users\robob\AppData\Local\Temp\claude\C--AiAppDeployments-Hydra\8c7d2187-dff9-46a6-8e50-2a885c8e7d29\scratchpad\rca-evidence.md`
(sections M0–M6). It reads spool JSON, project traces, `backends.json`, and
TheEights state through a read-only SQLite URI with `PRAGMA query_only=ON`. It
never writes, replays, drains or sends. Building and reviewing the bundle caught
four flaws in the author's own measurements, all corrected here: M3's test/real
classification, M5's substring matching, counting `.json.partial` files as live
spool entries, and an `id(d)` fallback key in M2.

**Labels:** **MEASURED** — observed on this machine · **REPRODUCED** — shown in an
isolated harness with every state path redirected to a temp directory · **READ** —
established from source · **INFERRED** — follows from the above, not directly
observed · **DISPROVED** — tested and found false. **[A]**, **[B]**, **[C]** mark
facts independently confirmed by the corresponding rev-5 agy review.

**Scope.** Observed: Hydra `feat/planning-phase`; TheEights `fable-audit-2`;
operator spool `C:\Users\robob\.hydra\eights-pending` and `...\eights-pending-dead`;
TheEights state `C:\Users\robob\.eights\state.db`; project traces
`C:\AiAppDeployments\Hydra\.hydra\*\trace.jsonl`.

**Goal.** Finish the planning phase — X3 (TheEights), X2 (AgentSmith), X1
(pair-programmer), then flip the `HYDRA_PLAN_PHASE` default — and determine what
in the TheEights integration blocks that, if anything.

---

## 1. Summary

1. **Primary root cause of `stub` pollution: non-live control verbs emit real
   governance through a stub dispatcher.** `hydra.workflow.plan` and non-live
   `hydra run` build a supervisor on `_NullDispatcher` because they never dispatch —
   but that supervisor also performs the workflow's constitution attestation and
   raises planner HITL requests, and routes both through the same stub.
2. **A `stub` entry's legitimacy depends on its workflow, not its reason.** 65 are
   attended workflows' only constitution attestations and must be delivered; every
   `stub` HITL request is stale, and almost all spooled HITL requests lack an
   expiry, so replaying them would file never-expiring tickets.
3. **The attended lifecycle does not drain the spool.** Replay runs on a daemon
   thread inside short-lived CLI subprocesses and dies with them: 73 of 77 pending
   entries have never been attempted, and all 77 are past the age at which any
   completed replay would have removed them.
4. **"Check the return" is a no-op**: every failure mode returns the same `None`.
5. **Replay is not idempotent** on TheEights, and idempotency needs a stable key
   sent by the client; 26% of spooled HITL requests have an empty id, traced to
   three raw-dict producers.
6. **The gateway reports every backend tool error as success.**
7. **Envelope recording has never produced a linked memory.**
8. **`hydra.workflow.resume` is refused in attended sessions**, and the approve
   runbook states the opposite. The CLI route works today.
9. **Three spool payloads are silently lost** as orphaned `.json.partial` files.
10. **The enum gap X3 fixes is latent**, and X3 should proceed.

---

## 2. Findings

### F1 · Every failure mode collapses to `None` — READ, REPRODUCED

`EightsAttestor._call` (`C:\AiAppDeployments\Hydra\hydra_core\eights\attestation.py:261`):

| Outcome | Spooled as | Returns |
|---|---|---|
| attestor disabled / no dispatcher | `eights_disabled_or_no_dispatcher` | `None` |
| `_dispatch_call` raised | `exception:<Type>` | `None` |
| advisory constitution rejection | not spooled | `None` |
| non-success result — daemon rejection, **timeout**, post-connect transport error, `stub` | `_failure_reason(result)` | `None` |
| success | — | result dict |

`_failure_reason` (`:228`) computes a discriminator and passes it only to the spool;
`envelope_record` (`:502`) returns `_call`'s value unchanged. REPRODUCED:
`_NullDispatcher`, a dispatcher without `call_mcp`, and a rejecting dispatcher each
yield `None` and are spooled as `stub`, `exception:AttributeError`, `failed:...`.

The cockpit audit (`...\mcp_servers\hydra_control\server.py:247-250`) is the one
caller with any signal: a `pending_count()` delta tells *spooled vs not*, not *why*,
and is racy against concurrent writers. **"Check the return" is a no-op at the direct
`envelope_record` call sites.**

### F2 · `failed` does not mean rejected — READ

`MCPStdioDispatcher.call_mcp` maps raw MCP `isError` to `status: "failed"`
(`...\hydra_core\dispatcher.py:749`, `:876`), so on the direct path a daemon
rejection is correctly a failure. The same status is used for a timeout
(`:852-863`, `"timeout": True`) and for exceptions after connect. A timeout is an
**unknown** outcome — the daemon may have committed. A reason string cannot decide
permanence.

### F3 · The gateway reports tool errors as success — READ

`...\mcp_servers\hydra_gateway\server.py` `_extract_result` (`:662-677`) always
returns `{"status": "done", ...}` and never reads `isError`. Any backend tool error
proxied through the gateway arrives as `done`. The supervisor's attestor dispatches
directly (F2) and is unaffected; host sessions, subagents and judges reading
`status` through the gateway are affected. The consumer set is not enumerated.

### F4 · RC6 — non-live control verbs emit real governance through a stub — READ, MEASURED [A]

- `_cmd_plan` (`...\hydra_core\cli.py:757-765`), the entry point of every attended
  workflow, sets `dispatcher = _NullDispatcher()` with the comment *"Planning never
  dispatches, so a NullDispatcher is correct and cheap"*, and builds the supervisor
  with it. **[A]**
- **Non-live `hydra run`** (`_cmd_run_locked`, `cli.py:~626`) does the same in its
  `else` branch. **[A]**
- `build_supervisor` creates the TheEights attestor from that dispatcher:
  `eights = EightsAttestor(dispatcher=dispatcher)` (`...\hydra_core\supervisor.py:296`). **[A]**
- Governance therefore goes out through the stub: the **only** production
  constitution-attestation call, in `node_intake` (`supervisor.py:1061`) **[A]**, and
  planner HITL requests (`supervisor.py:1185-1207` missing engineering target).
- `_NullDispatcher` (`cli.py:98-110`) is the **sole** producer of a `stub` status
  across Hydra's core, its MCP servers, and TheEights' daemon; its four methods
  perform no I/O. Every other `"stub"` in those trees refers to a stub *squad pack*
  or a stub *graph driver*. **[A]**

The design assumption was sound about dispatch and wrong about governance.

**MEASURED origin of the 293 `stub` spool entries** (bundle M1; `*.json` only), by
whether the workflow has attended cursors under `...\Hydra\.hydra\<wf>\attended\`:

| Attended workflow? | Tool | Detail | Count |
|---|---|---|---|
| yes | `constitution.attest` | — | **65** |
| no | `constitution.attest` | — | 116 |
| no | `hydra.envelope.record` | — | 75 |
| no | `governance.hitl.request` | planner / `missing_engineering_target` | 21 |
| no | `governance.hitl.request` | approval / `high_risk` | 12 |
| no | `governance.hitl.request` | dispatch / `over_budget` | 2 |
| no | `governance.hitl.request` | judge_per_squad / `reflexion_override` | 2 |

The 191 non-HITL "no" rows are unattributable: the record stores no process
identity, and workflows without attended cursors include plan calls halted at a
gate, surfaced workflows, other projects, and test or ad-hoc runs.

### F5 · `stub` legitimacy is per-workflow; stale HITLs lack expiry — MEASURED [A]

- **The 65 attended-workflow `stub` attestations are legitimate.** Attended workflows
  attest only in `node_intake`; later `step` and `submit_host_result` calls do not
  re-attest **[A]**. Replay through a live dispatcher is currently their only route to
  TheEights. **Quarantining `stub` wholesale — revision 3's step 1 — would discard
  them. Withdrawn.** **[A]**
- **All 37 `stub` HITL requests belong to workflows that never reached an attended
  stage.** The operator saw those gates in-band at the time; the TheEights ticket
  was only the shared-ledger copy.
- **159 of 163 spooled HITL requests carry no `expires_at`** (4 carry a past one)
  (bundle M2). Their arguments were persisted before the E2-17 expiry fix; current
  `hitl_request` always sets one (`attestation.py:~718`). Because TheEights'
  `hitlRequest` plain-inserts a pending row (F10), replaying them would create
  **never-expiring pending tickets** — the zombie-ticket failure E2-17 cleaned up. **[A]**

### F6 · `exception:AttributeError` is an unknown outcome — READ

The reason comes from the broad `except` around the whole `_dispatch_call` →
`call_mcp` (`attestation.py:266-275`). An `AttributeError` raised after a request went
out is recorded identically to a dispatcher with no `call_mcp`. These entries (488
dead-letter, 2 pending) cannot be proven unsent.

### F7 · Some never-attempted payloads are legitimate — READ

`_guarded_call` spools `eights_guard_breaker_open` and `eights_guard_inflight`
without calling `_call` — never attempted, but from live runs. They must stay
replayable. On guard timeout the worker keeps the call and spools only if it finally
fails, so the guard does not double-send (**DISPROVED**: guard-induced duplicate
write). "Never sent" and "must not replay" are independent properties.

### F8 · Replay is unfiltered; a stub replay burns attempts — READ, REPRODUCED

- `PendingSpool.replay` (`...\hydra_core\eights\pending_spool.py:236-262`) and
  `EightsAttestor.replay_pending._send` (`attestation.py:380`) retry every entry
  through the **current** dispatcher; nothing reads `reason`. **[B]**
- A failed replay increments `attempts` (`pending_spool.py:267`, `:272`).
- **REPRODUCED with default settings:** a legitimate `daemon_unavailable` payload
  replayed through `_NullDispatcher` — as `node_intake` does on the two F4 paths —
  was **dead-lettered after six replays without ever being sent**.
- Stub-dispatcher replay entry points are exactly two: `node_intake`
  (`supervisor.py:432`) reached from `_cmd_plan` and from non-live `_cmd_run_locked`.
  `cli.py:1240` and `:1831` replay only under `--live`; `eights-drain` always uses a
  live dispatcher. **[A]**
- **Withdrawn as production evidence.** Earlier revisions cited 14,014
  `supervisor.eights_replay` events (8,296 sent) as "replay runs at scale". Bundle M3
  splits the 13,840 trace directories containing them into **12,992 with no
  `workflow_start`**, **711 with a named-test goal**, and **137 other workflows**;
  sends split **7,737 / 536 / 23**. Tests redirect the spool to
  `.tmp-pytest\hydra-home\` (`tests\conftest.py:76-86`) but traces are built from
  the project root with no environment override (`hydra_core\telemetry.py:19-22`),
  so they land in the project's `.hydra\`. **At least 99.7% of that telemetry
  describes test runs, not the operator's spool.** **[B]**
- **Production incidence of the burn is UNKNOWN**, and likely low: the stub-dispatcher
  replays are exactly the asynchronous ones F14 shows rarely complete.
- `daemon_unavailable` dead-letter counts are therefore not reliable evidence of a
  real outage.

### F9 · The recommended bulk replay is hazardous — READ

`hydra doctor` and the SessionStart banner print `run hydra eights-drain
--replay-dead-letter` above a threshold. That path (`cli.py:~4786`) calls
`requeue_dead_letters` (`pending_spool.py:296`), which moves **all** dead letters back
with attempts reset, disables the age check, and replays through a real
`MCPStdioDispatcher`. Run today it would send, undifferentiated: stale HITL requests
with no expiry (F5), unattributable `stub` records (F4), and 488 unknown-outcome
entries into a non-idempotent daemon (F6, F10).

### F10 · Replay is not idempotent; the fix needs a client key — READ, MEASURED [C]

- `GovernanceState.hitlRequest`
  (`C:\AiAppDeployments\TheEights\daemon\src\engines\governance-state.ts:222-231`)
  mints `hitl_${nanoid()}` per call and plain-inserts. **Every replay files a new
  ticket.** **[C]**
- `HydraEngine.record` (`...\daemon\src\engines\hydra.ts:42-77`) calls `memory.add`
  first, then plain-inserts on `hydra_envelopes.envelope_id TEXT PRIMARY KEY`
  (`...\daemon\src\stores\sqlite.ts:320-332`); a duplicate raises a UNIQUE-constraint
  error rather than deduplicating. The attestor docstring "the daemon dedupes by
  envelope id" (`attestation.py:503-504`) is **false**. **[C]**
- No index or migration changes either: `hitl_queue` has only status and run
  indexes, `hydra_envelopes` only workflow, type and target indexes
  (`sqlite.ts:333-335`, `:370-371`). **[C]**
- `HitlRequestArgs` has no key (`...\daemon\src\mcp\governance.ts:47-52`); Hydra
  sends none. Daemon support alone is not sufficient. **[C]**
- `hitl_id` (the HITL envelope id) is replay-stable: the spool persists arguments
  verbatim and replay re-sends them. **MEASURED (bundle M2):** of 163 spooled HITL
  requests, **43 (26%) have an empty `hitl_id`**; the 120 non-empty ids are all
  distinct; `(workflow_id, gate_node)` maps to more than one request once, so it is
  not a safe key. **[C]**
- **Empty-id producers**, built as raw dicts with no `id`: `missing_engineering_target`
  (21) at `supervisor.py:1185-1207`; `envelope_ceiling` (16) at `:1877-1897`;
  `over_budget` (6) at `:1910-1923`. `attestation.py:711`
  (`str(hitl_envelope.get("id", ""))`) turns each into `""`. **[C]**

### F11 · Envelope recording has never produced a linked memory — MEASURED

1,989 `hydra_envelopes` rows, **0** with a `memory_id` (bundle M5). `extractSummary`
(`hydra.ts`) probes only `objective`, `summary`, `description`, `goal`; Hydra's
attestor sends only `id, type, workflow_id, origin_squad, target_squad, parent_id`
(`attestation.py:502-515`), so `memory.add` is never called — 1,880 stored payloads
have exactly that key set. Of 14,704 memories, 27 Hydra-related ones were written by
other routes (`pp-bridge`, `execsuite-bridge`, direct `hydra-supervisor` writes).
**By exact token, none references a recorded envelope id**, and none carries a
`hydra-envelope://` URI. Substring matching would report 1,007 false links, all on
one hand-named envelope id `x`; 119 of the 1,989 ids are such hand-named test or
probe ids sitting in the production database. A memory paraphrasing an envelope
without its id cannot be excluded.

### F12 · `hydra.workflow.resume` is refused in attended sessions — READ, MEASURED [C]

- `_launch_resume` (`...\mcp_servers\hydra_control\server.py:358-364`) returns
  `_detached_refusal("resume")` unless `HYDRA_ALLOW_DETACHED == "1"`, and the
  `workflow_resume` handler (`:778-811`) routes to it unconditionally, with **no
  attended or in-process branch**. **[C]**
- The `hydra_control` backend is started with only `HYDRA_OPERATOR_KEY`,
  `HYDRA_ROOT`, `PYTHONPATH` (`C:\Users\robob\.hydra\backends.json:154-167`; bundle
  M6). **[C]**
- `...\plugins\hydra\skills\approve\SKILL.md:21-33` tells the operator to call
  `hydra.workflow.resume` and asserts that *"attended workflows are unaffected:
  approval continues in-process."* **That statement is false.** **[C]**
- P5c attached `--modify-plan` and `critique_ref` to exactly this verb.
- **A working route exists today:** the CLI — `python -m hydra_core.cli approve
  <workflow_id>` or `... resume <workflow_id> --action approve` — runs in-process
  on `_NullDispatcher` (`cli.py:1244`), bypasses `_launch_resume`, and does not
  replay (resume skips `node_intake`; `cli.py:1240` replays only under `--live`). **[C]**
- **Not exercised live on purpose**: a successful MCP resume launches `hydra resume
  --live`, whose entry replays the spool.
- Pre-existing; affects every gate, not only `plan_gate`.

### F13 · Hypotheses tested and disproved

| Hypothesis | Result |
|---|---|
| A daemon rejection counts as success | **DISPROVED on the direct path** (F2); **true through the gateway** (F3). |
| The enum gap is causing live data loss | **DISPROVED**: 0 of the spool/dead-letter records carry the five types. |
| The guard's timeout-abandon double-sends | **DISPROVED** (F7). |
| Approving a plan through non-live resume fabricates completed work | **DISPROVED, REPRODUCED:** on `_NullDispatcher`, `node_dispatch` marks an approved engineering plan step `deferred_to_host`, emits no envelopes, spools nothing. |
| The stranded types feed the dead-letter backlog | **DISPROVED.** |
| Duplicate memories accumulate from replays | **Not realised**: no envelope memory is written (F11); latent until that changes. |
| Replay runs against the operator's spool at scale | **DISPROVED** (F8, F14). |

### F14 · RC8 — the attended lifecycle does not drain the spool — READ, MEASURED [B]

- `replay_pending_async` starts `threading.Thread(..., daemon=True)`
  (`attestation.py:469-475`); a daemon thread dies with its process. **[B]**
- The attended verbs `plan`, `step` and `submit_host_result` each run `python -m
  hydra_core.cli` as a **short-lived synchronous subprocess** through `_run_cli_json`
  (`server.py:582-608`), called from `_run_plan`, `_run_step` and
  `_run_submit_host_result` (`:673-725`). **[B]**
- `PendingSpool.replay` sweeps expired entries **before** the replay cap
  (`pending_spool.py:247-260`), 24 h default, and dead-lettering does not count
  against the cap; every production lifecycle caller uses that default. So one
  completed default replay dead-letters every entry over 24 h, even with a cap of 1. **[B]**
- **MEASURED (bundle M4):** all 77 pending entries are over 24 h old (youngest 59.5 h,
  median 91.5 h, oldest 288.2 h) and **73 of 77 have `attempts == 0`**. No default
  replay has completed against the real spool since they aged out, despite `plan`,
  `step` and `submit` calls in this session. **[B]** Consistent with M3: only **23**
  replay sends in all recorded history came from non-test workflows — plausibly
  long-lived processes such as a detached `hydra run --live`.
- Consequence: legitimate governance, including the 65 attended attestations, is not
  delivered by the lifecycle. It waits until something long-lived or synchronous
  replays it.
- **And that wait is not safe either [B]:** the next *completed* default replay —
  for instance any long-lived detached run — would **dead-letter all 65 legitimate
  attestations without delivering them**, because they are over the 24 h limit.
  Dead-lettered entries are retained, not deleted, so this is recoverable through
  triage (T), but it moves them into the queue F9 warns against bulk-replaying.
- Dead-letter file modification times reflect spool time (moves preserve mtime), so
  they cannot date when dead-lettering happened.

### F15 · Three spool payloads are silently lost — MEASURED [A]

The pending directory holds **77 `.json` files and 3 `.json.partial` files**
(bundle M0). `PendingSpool.spool` writes `<id>.json.partial` then `os.replace`s it
to `<id>.json` (`pending_spool.py:147-163`). `replay` iterates only `*.json`, so a
partial is **never replayed and never dead-lettered**. **[A]**

Read directly (MEASURED): all three are **complete, valid JSON** — the write
finished and only the rename did not happen.

| File | Tool | Reason | Spooled | Attended workflow? |
|---|---|---|---|---|
| `2e4b2581…` | `constitution.attest` | `stub` | 2026-09-04 11:10 | yes |
| `505a0b5a…` | `constitution.attest` | `stub` | 2026-09-04 16:01 | yes |
| `3dcac060…` | `hydra.envelope.record` | `stub` | 2026-09-01 21:22 | no |

So two more attended workflows have lost their only attestation (F5), and all three
are recoverable by completing the rename. Cause — INFERRED, not established: either
the process died between write and rename, or `os.replace` raised (on Windows,
commonly a transient lock such as antivirus scanning) and the error was swallowed by
the "spool write must never crash dispatch" handler in `_maybe_spool`.

---

## 3. The plan's original claims, re-examined

| Plan claim | Finding |
|---|---|
| Hydra types "cannot reach TheEights" | True of the closed 11-member enum. |
| It is **live** | Not observed (F13). Reachable: the supervisor records whatever a squad produced (`produced.model_dump`, `supervisor.py:2235/2551/2664`). |
| Rejection discarded at `server.py:~1281` | True in code; that verb returns `{"ok": true}` regardless. Its declared caller, AgentSmith `HydraBridge.envelopeRecord`, is defined and never called. |
| Fix: "check the return" | No-op (F1). |

---

## 4. What is not known

1. Whether TheEights already holds records of `stub` origin from some past completed
   replay; the telemetry that could say is test data (F8).
2. The originating process of any unattributable `stub` or `exception:*` entry.
3. Whether duplicate HITL tickets already exist in TheEights.
4. The full set of gateway consumers that key on `status`.
5. Whether each `exception:*` entry failed before or after the send.
6. Which process performed the dead-lettering that did occur (1,298 entries).
7. How often, in production, a stub-dispatcher replay completes and burns an attempt.

---

## 5. Root causes

| # | Root cause | Findings |
|---|---|---|
| **RC6** | **Non-live control verbs emit real governance through a stub dispatcher, and the attestor and spool treat a stub result as a genuine attempt.** Primary cause of `stub` pollution. | F4, F5, F8 |
| **RC8** | **Spool replay is fire-and-forget on daemon threads inside short-lived subprocesses, so the attended lifecycle never drains the spool.** | F14 |
| **RC7** | **The operator resume verb has no attended route, and the runbook says it does.** | F12 |
| RC1 | Outcome information is flattened to strings and `None` before the component that must act on it. | F1, F2, F6 |
| RC2 | Writes to TheEights are not idempotent while the client assumes they are, and no client key exists. | F10 |
| RC3 | The spool and replay have no notion of legitimacy, staleness, or orphaned partial writes; operator tooling trusts them. | F5, F7, F9, F15 |
| RC4 | The gateway discards the MCP error bit. | F3 |
| RC5 | The attestor's envelope and TheEights' indexing contract disagree. | F11 |

---

## 6. Implications for the planning-phase goal

| Goal item | Status | Why |
|---|---|---|
| **X3** (enum) | **Proceed now.** | Correct and additive. Commit message must call the gap latent and must not claim a PLAN gets a memory row in production (F11); nothing in Hydra records a PLAN to TheEights. |
| **X2**, **X1** | **Proceed, independent.** | None of F1–F15 touches them. |
| **Plan-gate HITL delivery** | **Sound.** | Built as a `HITLRequest` (non-empty id) by `node_plan_judge` (`supervisor.py:3703-3848`) and sent through the live dispatcher of `_cmd_attended_submit` (`cli.py:3180`, `:3413-3417`). On resume, `_prune_spooled_hitl_requests` and `_resolve_eights_hitl_for_workflow` close it. **[C]** |
| **Plan-gate operator actions** | **Broken through the documented verb (F12).** | Approve / reject / `--modify-plan` / force-dispatch via `hydra.workflow.resume` are refused in attended sessions. **The CLI route works now** and is functionally safe (F13). |
| **Approval stand-down (P5a)** | Helps. | Removes the planner's `approval/high_risk` HITL — a current `stub` producer — at non-trivial rigor. **Flipping the flag therefore reduces, rather than increases, `stub` emissions** (`supervisor.py:1344-1347`). **[D]** |
| **Planner `missing_engineering_target` HITL** | Pre-existing defect on the plan path. | Raw dict with empty id (F10), emitted through the stub (F4). |
| **Plan memory lineage (plan §13b)** | **Never implemented.** | If built: deliver through a live path, pass an idempotency key, only after I. |
| **TheEights idempotency and replay (F8–F11, F14)** | **Not blockers for the goal.** | The plan-gate HITL is delivered synchronously and never spooled on the attended path. **[C]** |
| **Flag flip** | **Gate on K**, plus the planned holistic cross-vendor pass. | Flipping makes `plan_gate` the primary operator decision point; without K the documented verb for that decision fails. **[C]** |

---

## 7. Candidate paths

| # | Path | Assessment |
|---|---|---|
| B | "Check the return" | No-op. **Decline.** |
| C | Change `_call`'s public return contract | Wide hot-path blast radius, no caller needs it. **Decline.** |
| E′ (rev 3) | Quarantine all `stub` entries | **Withdrawn**: discards the 65 attended attestations (F5). |
| **S** | **Disarm stub replay, and drop the bulk-replay advice.** Specified precisely [A]: (1) `_NullDispatcher` declares an **explicit dry-run marker**; (2) **both** `replay_pending` and `replay_pending_async` return early when the dispatcher carries it — gating only the former still spawns a thread per call; (3) do **not** key on `live_execution`, because test doubles such as `_UpDispatcher` in `tests\test_eights_replay_queue.py:333-340` lack it and would silently stop replaying; (4) covers both F4 paths, `_cmd_plan` and non-live `_cmd_run_locked`; (5) remove the doctor/banner advice to run an unfiltered bulk replay. | Small and safe. Removes F8's mechanism and F9's trigger. |
| **K** | **Give the operator resume an attended route.** Either an in-process, non-detached resume for attended sessions — as `plan`, `step` and `submit_host_result` already are — or correct the approve and resume runbooks to the working CLI form, removing the false "attended workflows are unaffected" statement. **Constraint [C]:** K must **not** be implemented by spawning `hydra resume --live`, which replays the spool at `cli.py:1240`; a resume that proceeds into dispatch can live long enough for that unfiltered drain to *complete*. K resumes without replay, or S lands first. **Concrete safe implementation [D]:** route `hydra.workflow.resume` through the existing non-detaching transport `_run_cli_json(["resume", <wf>, "--action", <action>, ...])` (`server.py:582-608`) **without** `--live` — a short-lived in-process resume on `_NullDispatcher` that takes the resume lock, verifies the operator capability token, prunes the spooled request, resolves the gate in TheEights, and does not replay. | Goal-critical: before the flag flip. |
| **R** | **Deliver control-verb governance live at emission.** The control verbs keep a stub *dispatcher* for dispatch but send attestation and HITL through a live attestor, following `_reconcile_attestor` (`cli.py:~958`). **Tradeoff [A]:** no failure risk — `_guarded_call` is fail-soft and `node_intake` treats a `None` receipt as degraded-open — but a hung daemon can add up to **~10 s** to `hydra plan` (two independently guarded calls at the 5 s `_EIGHTS_GUARD_TIMEOUT_DEFAULT`, `attestation.py:48-49`, before breakers trip), plus cold-start cost for an `MCPStdioDispatcher`. **Mitigation [D]:** `ceiling_tick` is unnecessary on the plan path, which never loops; bound the plan-path guard timeout to 1–2 s, or make intake attestation non-blocking while still durable. | Removes the primary cause of new `stub` entries. |
| **T** | **Triage the backlog by legitimacy and staleness, not reason.** Attended-workflow attestations → deliver after I; HITL requests for never-progressed workflows or with no `expires_at` → never replay as pending (record as historical / expired); guard-skip reasons → replay; `exception:*` and unattributable entries → operator ruling; `.json.partial` files → inspect and either restore or record as lost. Include entries already dead-lettered (F14). | Replaces blanket quarantine; recovers what F14 and F15 would lose. |
| **I** | **End-to-end idempotency.** TheEights: key on `HitlRequestArgs`, `hitlRequest` returns the existing row; `record` checks for an existing `envelope_id`; `memory.add` keyed by envelope id. Hydra: `hitl_request` sends a stable non-empty key; the three raw-dict producers (`supervisor.py:1185`, `:1877`, `:1910`) build real `HITLRequest`s; no keyless send or spool. | Prerequisite for replaying anything of unknown outcome, and for T, L and J. |
| **L** | **Make lifecycle replay actually complete** — a bounded synchronous drain in the long-lived `hydra_control` server, or a scheduled one, rather than a daemon thread in each short-lived subprocess. | Fixes RC8. **Must follow S, R, T and I** (§8). |
| D′ | Typed outcome from the dispatcher (`rejected` / `unknown` / `not_sent` / `not_live`) plus a persisted pre-send marker; spool only `unknown` and legitimate `not_sent`. | Long-term fix for RC1; makes future `exception:*` classifiable. |
| J | Fix the envelope contract so indexing works. | Strictly after I, or it activates duplicate-memory amplification. |
| H | Gateway preserves the MCP error bit. | Own run with a consumer audit. |
| P | Recover orphaned `.json.partial` writes, and make a startup sweep detect them. | Closes F15. |

---

## 8. Proposed path forward

**Operational guidance, effective now:**
- Do **not** run `hydra eights-drain --replay-dead-letter`.
- Do **not** quarantine or delete spool entries.
- To approve, reject or revise at an attended gate, use the **CLI**:
  `python -m hydra_core.cli approve <workflow_id>` (non-live). Do **not** add
  `--live`.
- Be aware that a long-lived or detached `hydra run --live` would dead-letter the 65
  legitimate attestations on its first completed replay. They remain recoverable
  from dead-letter through T.

**For the goal:**
1. **X3** — finalize and merge now, with the corrected commit message.
2. **X2** and **X1** — proceed; independent.
3. **S**, then **K** — S first, so K cannot re-arm the drain.
4. **Flag flip** — its own commit, after K, with the holistic cross-vendor pass.

**For the TheEights integration** (separate governed runs, not part of the planning
feature), in order:
1. **S** — disarm stub replay; remove the bulk-replay advice. *(Shared with the goal
   sequence above.)*
2. **R** — deliver control-verb governance live at emission.
3. **I** — end-to-end idempotency, including the three raw-dict HITL producers.
4. **T** and **P** — triage the backlog and recover partial writes.
5. **L** — make lifecycle replay complete.
6. **D′** — typed outcomes.
7. **J** — indexing.
8. **H** — gateway error bit.

**Why this order.** L before S, R, T and I would be harmful in *both* directions
**[B]**: a working drain would deliver stale zombie tickets and dry-run records, and
its expiry sweep would dead-letter every legitimate attestation it had not yet
delivered. Today's broken drain is accidentally protective. I precedes T, L and J
because each of them replays or writes something whose outcome may be unknown. K
follows S because the obvious implementation of K would otherwise re-arm the drain.

## 9. Addendum — forensic check of TheEights records (read-only, 2026-09-16)

Measured against `~/.eights/state.db` through a `mode=ro` connection with
`PRAGMA query_only=ON`. Nothing was written, drained or replayed.

**Scale.** `hitl_queue` 1,038 rows: 1,030 `pending`, 8 `approved`. 1,019 of the
pending rows carry no `expires_at`. By request day the pending rows are 825 on
2026-07-06, 170 on 2026-09-01, 25 on 2026-07-11, and 10 elsewhere.

**Finding G1 — a bulk drain produced most of the zombie backlog.** On 2026-07-06,
between 06:38Z and 06:40Z, TheEights accepted 774 HITL requests and 1,499 envelope
records in three minutes. That is 20 minutes after merge 168a474 (RA-7, "eights
spool drain"). The pending backlog is mostly the output of one drain, not of
ongoing production. This is direct evidence for F9 and for putting T before L.

**Finding G2 — five exact duplicate tickets, all from that burst.** Five runs hold
pairs of HITL rows with byte-identical payloads, requested 0–10 ms apart
(`cb5d069a`, `eb4dd7d5`, `7b8876fc`, `9f3d5808`, `b2839d00`; all 2026-07-06
06:38:57–06:39:00Z). A spacing of milliseconds means concurrent double sends, not
a retry after a timeout. Path I therefore needs a key that the server enforces.
Client-side "send once" discipline is not enough. This corroborates F10.

**Finding G3 — the unit test suite wrote to the live daemon until E2-26.**
`tests/test_cli.py:317` (`test_run_workflow_id_passthrough`) uses the fixture
workflow id `c2c2c2c2-c2c2-4c2c-8c2c-c2c2c2c2c2c2`. The live ledger holds, under
that id: 117 envelope records, 71 HITL requests (65 `lock_release_pending`
postcheck gates and 6 `missing_engineering_target` planner gates, all pending),
and 70 constitution attestations. By day: 64 HITL on 07-06, 1 on 07-11, 6 on
09-01. A further 104 attestations use trace id `no-workflow`, on the same days.
The last fixture write is 2026-09-01 21:45:36Z. Commit 457d56f (E2-26, the
hermetic Hydra home with `HYDRA_TEST_NO_DAEMONS=1`) landed at 21:47:58Z the same
day. Nothing has leaked since, so this is **historical and closed**. The records
remain, and T must classify them as test pollution. They are not live gates.

**Classification for T.**

| Class | Identification | Count |
|---|---|---|
| Test pollution | `run_id`/`workflow_id` = the `c2c2c2c2-…` fixture; attest trace `no-workflow` on 07-06/07-11/09-01 | 71 HITL, 117 envelopes, 70 + 104 attestations |
| Concurrent duplicates | identical payload, same run, ≤10 ms apart | 5 extra HITL rows |
| Hand-named envelope ids | non-UUID `envelope_id` (for example `env-a11y-r9-*`, `cms-admin-portal-r8`) | 119 envelopes; operator-authored campaign records, **legitimate** |
| Drain-era zombies | pending, no `expires_at`, requested 2026-07-06 | about 750 after removing the rows above |

AgentSmith attestations repeated under its constitution-hash trace id (77 and 4)
are boot re-attestations. They are expected and are not duplicates.

**Effect on the path.** None of this changes the order in §8. It sharpens two
paths. T gains a deterministic first pass (the fixture id and the drain-burst
window). I must be enforced on the server.

## 10. Addendum — X1c (`generate_image`) deferred, 2026-09-18

X1c is **not merged**, by decision. The work is preserved on pair-programmer branch
`attended/run_8jZ0GaC9Cb2m` at commit 76dfe1d and can be resumed at any time.

**Why defer.** The plan makes imagery explicitly degrade-open: a plan renders
without it and says so in its provenance, and an image "must never be able to
block a plan gate". X1c is therefore not on the path to the `HYDRA_PLAN_PHASE`
flip. Three cross-vendor passes scored `security_hygiene` 1, 1 and 1, each time
on a *new* surface, because harvesting files a vendor CLI wrote into a shared
directory is a hostile-filesystem problem, not a feature problem.

**What the work does deliver** (worth keeping when it resumes): the codex path
harvests only the session directory that `codex exec --json` reports, never by
newest mtime — the race the plan warned about; downscaling uses the existing
`pngjs` dependency; and it returns file paths, never base64, which §7a requires.

**What the last pass still found open** (all in `daemon/src/mcp/codex-server.ts`):
1. Containment is **lexical, not physical**. A session directory replaced by a
   symlink passes `dirname(resolve(...))`; `readdirSync` follows it. A file can be
   swapped from regular file to symlink between `lstatSync` and `readFileSync`.
   `wx` protects only the leaf, so a symlinked `output_dir` parent escapes while
   `isInside` still reports a lexical child. Needs `O_NOFOLLOW`-equivalent opens
   (open the directory handle once and read relative to it) rather than path
   re-resolution.
2. `readPngDimensionsFast` validates 24 bytes only — no IHDR length/CRC, no
   IDAT/IEND. A partially written 24-byte file passes `fitsAsIs`, is copied
   verbatim, and is reported `ok`, so the advertised per-file malformed-PNG
   failure does not hold. Polling returns as soon as any `.png` name appears,
   which makes this a realistic flush race.
3. The caps do not bound the pre-call snapshot, which walks every session
   directory under the images root.

**Also settled by X1c's probe, and worth recording** — `agy` *can* generate an
image, but writes it to a single global scratch directory
(`~/.gemini/antigravity-cli/scratch/`) under a model-chosen name reported only in
prose, with no per-turn handle in stdout. A deterministic per-turn harvest is
therefore impossible without an mtime scan that races concurrent agy sessions.
This corrects §7b, which assumed agy's named tool would be the cleaner of the
two: it is the *less* harvestable of the two.

---

## §11 — The attended path can still report a false generate-failure (2026-09-20)

Recorded because it cost a stage teardown and is a recurrence of a defect this
ledger already believed closed.

**What happened.** Strict-JSON stage A (workflow `b5f951d1`, run
`run_FZ4a7AXavRQn`) completed: the engineer committed `3a51ee6` and `c84eda7`,
the full suite passed (2 779 passed, 5 skipped, 1 known pre-existing
marketing-symlink failure), and the cross-vendor judge (codex `gpt-5.6-terra`)
returned `pass` with zero findings. `hydra.workflow.submit_host_result` then
returned:

```
"status": "surfaced", "stage_outcome": "error",
"changed_paths": [], "smoke_status": "skipped",
"merge": {"merged": false, "error": "discarded_non_complete"},
"error": "codex generate returned no output (no code written)"
```

**Why it is false.** No codex generator runs on the attended path — the host
supplies the result. The engine nonetheless reached a generator-failure
classification, concluded `changed_paths: []`, and discarded its own merge. The
commits were intact on `attended/run_FZ4a7AXavRQn` the whole time and survived
worktree teardown.

**Relationship to the 2026-06-23 fix.** That fix stopped the engine trusting
`_GEN_FAIL_MARKERS` found in a *result summary*, and added diff-aware
classification plus a host-side smoke. This is the adjacent hole: the attended
path can still arrive at an empty `changed_paths` and label it a generation
failure, on a run where no generator was ever invoked. The marker-scanning fix
does not cover it, which is why it reappeared.

**Resolution taken.** Salvage, not re-run, per the recorded lesson. Verified the
commits survived teardown, confirmed `feat/planning-phase` was a strict ancestor
of `c84eda7`, and fast-forwarded. No rebuild, no `git add -u`. Post-merge full
suite on the main checkout: **2 783 passed, 2 skipped, 0 failed** — the symlink
failure is worktree-only, as expected. Constitution `4060cb542fcc…` unchanged.

**Open.** The misclassification itself is unfixed. Two things want doing, and
neither is in the strict-JSON scope: the attended branch should not consult a
generator-failure path at all, and an empty `changed_paths` on a run whose branch
carries commits ahead of its base should be treated as a detection fault rather
than a generation failure. Until then, **a surfaced attended stage is not
evidence the work is absent** — check the run branch before re-running anything.

**Second-order cost.** A falsely surfaced run stays in the pp surfaced list and
is offered for retry (`/pp:retry run_FZ4a7AXavRQn`). Retrying a run whose work is
already merged would redo landed work. The surfaced entry should be dismissed with
`ack_run`, not retried.

### §11a — Root cause confirmed (2026-09-20, second occurrence)

Stage B1 (workflow `aa4ffdf1`, run `run_zjFm-RuvKXeX`) reproduced §11 and
identified the mechanism.

That run's `submit_host_result` returned the same
`"codex generate returned no output (no code written)"` with the same
`discarded_non_complete` merge, but `changed_paths` was **not** empty:

```
"changed_paths": ["hydra_core/auth/capability.py"]
```

That is exactly — and only — the file carrying a stale git stat-cache `M` flag
after a mutation proof was reverted. Its content was provably byte-identical to
`HEAD` (`git diff --exit-code` clean; `git hash-object` and
`git rev-parse HEAD:<path>` both `68905b71…`). Nine files were committed in that
stage; eight of them, being cleanly committed, did not appear.

**Therefore `changed_paths` is computed from the DIRTY WORKING TREE, not from the
commit diff against the stage base.** The two observations fit exactly:

| Run | Worktree state | `changed_paths` | Engine verdict |
|---|---|---|---|
| `run_FZ4a7AXavRQn` | fully clean | `[]` | "no code written" |
| `run_zjFm-RuvKXeX` | one stale stat flag | that one file | "no code written" |

**The consequence is the defect.** An engineer that does the right thing — commits
its work and leaves a clean tree — is indistinguishable from one that did nothing.
The check rewards an uncommitted tree and punishes a committed one, which is
backwards: a committed stage is the *stronger* outcome. It also means the signal
is non-deterministic, since it depends on stat-cache noise rather than on content.

**Fix direction** (not taken here; outside the strict-JSON scope): compare the run
branch against the stage base — `git diff --name-only <base>..<branch_head>` — and
treat a non-empty commit range as work performed regardless of working-tree
cleanliness. Reserve the generator-failure classification for the detached path,
where a generator is actually invoked; on the attended path the host supplies the
result and that classification has no basis at all.

**Until fixed:** both `run_FZ4a7AXavRQn` and `run_zjFm-RuvKXeX` are falsely
surfaced with their work merged. Dismiss with `ack_run`; do not `/pp:retry`
either, which would redo landed work.
