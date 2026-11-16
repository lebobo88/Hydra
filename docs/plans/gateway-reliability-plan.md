# Gateway, daemon and TheEights reliability plan

Status: DRAFT v2, awaiting operator approval. Nothing in this plan has been implemented.
Date: 2026-11-15. Scope: Hydra, TheEights, pair-programmer, AgentSmith.

Evidence, all under `docs/audits/gateway-reliability-2026-11-15/`:
five investigation reports, one research report, and `plan-review.md`, an adversarial review of draft v1.
Draft v2 incorporates that review; section 7 lists what changed and why.

Finding ids: `GW-n`, `DISP-n` (inv-gateway); `C-nn` (inv-cli-parity, written there as F-nn);
`E-n` (inv-eights, written there as Fn); `PP-n`, `AS-n` (inv-pp-smith); `RT` (inv-runtime); `R-n` (plan-review issue n).

Confidence words used here: **reproduced** (an experiment showed it), **verified** (read end to end in code or
logs), **inferred** (consistent with evidence, not shown).

## 1. What is wrong

### 1.1 The gateway "goes down"

The gateway process does not crash. The host stops using the connection after a request is cancelled. The host
logs hold 17 drops between 2026-10-03 and 2026-11-15, all with the same signature (RT, recounted in R-4).

- In flight at the drop: `pp_codex.critique` in 14, `hydra.workflow.submit_host_result` in 3.
- Trigger: 15 followed the host's 1800 s tool timeout; 2 followed a cancel with no timeout (an interrupt).

Mechanism, in order:

1. **A call hangs for 30 minutes. The cause of the hang is not established.** A retry of the same critique after
   reconnect finished in 18 s, so it is a hang and not slow work. PP-1 (below) is a reproduced defect on this
   path, but pair-programmer's own logs show no codex child in flight in 6 of 7 sampled 1800 s hangs (R-1). The
   stalled hop could be before the spawn, after the child exits, or a response the Python client never reads.
   One of the three `submit` drops was traced to the same critique hang reached through the engine path (R-5a).
2. **No inner layer times out before the host.** Host 1800 s; gateway long cap 3000 s; `hydra_control` submit
   1800 s; engine critique 1800 s (GW-4, RT). Where an inner 1800 s timer happened to answer first, the
   connection survived: two such cases are in the logs (R-5c). That is direct evidence that ordering the
   timeouts prevents the timeout-triggered drops.
3. **The gateway mishandles the cancel.** The SDK answers the cancelled request itself. The gateway handler
   catches `asyncio.CancelledError` and returns normally (`mcp_servers/hydra_gateway/server.py:478`, `:863`,
   `:903`); the SDK's second `respond()` hits an assertion, `server.run` ends, and all backends are closed
   (GW-1, reproduced; the fix was reproduced against a test host, not against Claude Code itself).
4. **The process lingers.** It stays alive but deaf until the host next writes to it (GW-7). Interactive Claude
   Code does not reconnect a stdio server by itself, so the operator has to.

One cancel also kills every other call in flight on the connection; on two dates a second live critique was
lost this way (R-4).

Other defects on the same path:

- GW-2 (reproduced): a timeout or error on one call tears down the backend's shared session. Sibling calls get
  no error and hang to their own cap; when a stale call's cap fires it tears down the new healthy session.
- GW-3 (verified): `finalize`, `repo.resolve`, `cockpit.audit` and `envelope.record` get the 120 s short cap,
  though `hydra_control` allows `finalize` 600 s. Expiry kills `hydra_control` and its CLI child.
- C-01 (reproduced): `resolve_repo_path` runs `git` without closing stdin (`hydra_core/repo_registry.py:315`)
  and blocks on the inherited MCP pipe until the gateway's 120 s cap tears the backend down. With
  `stdin=DEVNULL` it takes 0.09 s.
- C-02 (verified): `step` runs the whole `pytest tests/` baseline synchronously (600 s cap) before saving the
  cursor. 38 of 70 baseline cache entries are timeout markers.
- PP-1 (reproduced): pair-programmer's sub-CLI timeout kills only the `cmd.exe` shim; the real process keeps the
  pipes and the call never settles. `cli-runner.ts:287-303` records this as a known limitation.
- PP-2 (verified): pair-programmer has no cancellation handling, and its retry default doubles the worst case.
- GW-5, GW-6: no backoff for a dead backend; the gateway writes no log file at all.

### 1.2 TheEights is "unreachable"

TheEights answers `initialize` about 0.7 s after spawn and is healthy when served. It is unreachable because of
a race (E-1, RT). Every client spawns its own copy and sends its first call about 70 ms after boot. The audit
readiness gate opens at about 820 ms (p99 3.0 s). The daemon answers `not_ready` with a `retry_after_ms` hint,
and no Hydra client reads the hint (verified by grep). Over 20 active days: 4,147 boots, 4,516 calls refused,
368 served. Hydra reports the refusal as `eights_unreachable`; this is Hydra#123. 628 writes are spooled in
`~/.hydra/eights-pending` for the same reason.

Other TheEights defects (inv-eights): stale `dist` build that rejects `PLAN` and `JUDGE_VERDICT` envelopes
(E-12); embeddings dead since 2026-09-19 because Ollama lacks the model, so search is substring match (E-4);
1,141 pending HITL tickets with no expiry and a 955 KB `hitl.list` reply (E-9); about 75 junk audit events per
completed boot from an unregistered `eights.system` actor (E-3); every process that passes the gate starts
every watcher (E-5); `verifyChain` blocking the connection, 5,418 "connection is busy" errors (E-7); no exit on
stdin EOF (E-6); an empty home cannot boot (E-8); a 30 s synchronous drift scan and missing indexes (E-11);
879 MB `state.db` plus more than 700 MB of unrotated jsonl with no compaction (E-10).

### 1.3 The shared cause

pair-programmer, TheEights and AgentSmith are not long-lived daemons. Every stdio client gets a private
process, and each of those spawns private copies of its dependencies (E-5, PP-3, AS-2). Four gateways were
live, one per Claude session. 292 distinct pair-programmer pids were logged in one day. User-scope hooks start
six pair-programmer processes per prompt, and one hook boots a TheEights copy per prompt only to be refused.
The session-start lines "[pp] daemon ok" and "[smith] online" do not contact a running daemon.

AgentSmith specifically: it does not exit when its client disconnects (AS-1, reproduced); its Hydra tail scan
takes 10 to 12.6 s on a 5 s interval over 44,926 workflow directories (AS-3, measured); its machine-global
lease can kill another instance's TheEights child (AS-2, code reading only).

### 1.4 Not the cause

Ruled out by test or measurement: gateway startup (lazy backends, static catalog, about 2 s); catalog size
(272 tools, 92 to 98 KB); gateway stdout hygiene; orphan cleanup; sleep and resume; memory.
Not observed, but not provable from the logs: SQLite `SQLITE_BUSY` contention (tool errors are logged without
messages). Duplicate registration: none for pair-programmer, TheEights or AgentSmith; `blender` is registered
twice.

## 2. Correction to the brief: what CLI parity does and does not buy

The attended verbs already run the CLI: `hydra_control` executes `python -m hydra_core.cli` as a child for
plan, step, submit, finalize and resume (`mcp_servers/hydra_control/server.py:729`). So:

- Driving `hydra ...` from the Bash tool removes the gateway hop and its cancel fault from the critical path.
- It does not remove the slow work. The Bash tool defaults to 120 s and caps at 600 s, so any verb that can run
  longer must become start-then-poll.
- Each call costs 3.2 to 3.6 s today because LangGraph is imported eagerly (C-04).
- The two judge agents have MCP tools and no Bash, so the cross-vendor judge keeps an MCP route until the
  operator-capability work (approved W3.4) lands (C-11).

## 3. Design

Unchanged: Hydra routes, governs and synthesizes. The engine stays authoritative for ledger, budget, judges,
HITL and checkpoint. Squad boundaries, RBAC, envelopes and the constitution are untouched. TheEights,
pair-programmer and AgentSmith keep their purposes, their tool contracts, and their ability to run without
Hydra (AGENTS.md, Security). The work fits the approved unified-orchestration plan (epic Hydra#78).

1. **Fix the faults where they are.** Cancellation, timeout ordering, stdin inheritance, readiness and process
   exit are local defects with local fixes. They account for every reported symptom.
2. **Timeouts are strictly ordered.** Sub-CLI < backend < gateway or dispatcher < host, each shorter than the
   one above by a fixed margin, from one shared table, asserted by a test.
3. **Readiness is a bounded wait.** A server answers the handshake at once, waits for a pending gate for less
   than the smallest client timeout, then returns a retryable status. A failed gate is reported as failed at
   once.
4. **Long work is a job, using what exists.** A job is the existing CLI child started detached, as the smoke
   job already is. State is a file under the workflow directory written atomically like the cursor; the key is
   the existing `call_key`; mutual exclusion is the existing resume lock. A job is one verb, never a stage
   loop, so it does not bypass the detached-mode gate (W5.1).
5. **The CLI is the critical path; MCP is optional.** Verbs get `--json` and exit codes that track `ok`. The
   gateway stays for discovery and for agents without Bash.
6. **A shared broker only if measurement asks for it.** After the fixes, sharing daemons adds a cold-start
   saving of 0.7 to 1.7 s per daemon and one writer per database. It also adds a lifecycle, identity and
   stale-code design that does not exist yet (R-15 to R-18). It is gated on one week of measurement.

## 4. Phases

Every fix ships with a test that fails when the fix is removed. The gateway has no test today that starts the
process or exercises a call; Phase 1 adds that harness, using fake backends through `HYDRA_BACKENDS`.

### Phase 0: record

- File GitHub issues on the owning repos, grouped by root cause, each with evidence and fix plan. Check the
  existing issues first; GW-1 and GW-3 belong on Hydra#117, E-1 on Hydra#123.
- Link this plan from epic Hydra#78.

### Phase 1: stop the drops (ordered; items depend on each other as noted)

| # | Repo | Change | Finding |
|---|---|---|---|
| 1.1 | Hydra | One timeout table shared by the gateway and `MCPStdioDispatcher`, strictly ordered below the host limit with a 60 s margin per layer. Add `finalize`, `repo.resolve`, `cockpit.audit`, `envelope.record`. Values are set from 1.5's measurements, because the logs show successful critiques of up to 1527 s. A constants change; expected to prevent the 15 timeout-triggered drops by itself | GW-3, GW-4, DISP-1, R-10 |
| 1.2 | Hydra | Gateway and dispatcher log files with rotation: request sent, response received, timeout, teardown, per backend call. `gateway.health` reports last failure and reason. Needed to verify everything below | GW-6, R-11 |
| 1.3 | Hydra | Re-raise `CancelledError` in the three gateway call paths. On a host cancel, recycle that backend session when no sibling is in flight, otherwise when the siblings finish, because the Python client forwards no cancel and the abandoned request would stay on a live session. Exit the process when `server.run` ends. Pin the `mcp` version. Tests cover cancel during the backend wait, during a meta-tool, and just after the backend answers, plus one run against real Claude Code with a short per-server timeout | GW-1, GW-7, R-3, R-5d |
| 1.4 | Hydra | `stdin=DEVNULL` as the default in `hydra_core.proc.run_text`, and on the `git` call in `resolve_repo_path` | C-01 |
| 1.5 | pair-programmer, Hydra | Diagnose the hang before fixing it. pair-programmer logs one line at request receipt, CLI spawn (pid), CLI exit and response write. Reproduce one hang and name the stalled hop. Record the `timeout_ms` values callers pass today | R-1, R-6 |
| 1.6 | Hydra | Preserve the CLI's structured stdout on non-zero exit; honour `isError` | C-07, GW-8 |
| 1.7 | Hydra | `step` saves the cursor first and runs the baseline through the existing smoke-job machinery | C-02 |
| 1.8 | Hydra | Pin the project root for every CLI verb, so a call from a session worktree cannot open a duplicate stage | C-08 |
| 1.9 | TheEights | Open the gate first (run the tail verify before seeding, prompt re-scan and the unused `kuzu` import). Register `eights.system` correctly. Background work only in a process that holds a leader lease. These must land before 1.10, or every boot would complete bootstrap and write junk events and start watchers | E-2, E-3, E-5, R-7 |
| 1.10 | TheEights | Wait up to 2.5 s for a pending gate, then `not_ready` with the hint; `failed` at once when verification failed; `eights.health` stays ungated. Rebuild `dist` from a named commit and report the build id in `eights.health` | E-1, E-12, R-8 |
| 1.11 | Hydra, pair-programmer | Hydra maps `not_ready` to its own status, not `eights_unreachable`. pair-programmer's sticky `unavailable` state expires. Drain the 628 spooled writes with `hydra eights-drain` once 1.10 is live | E-1, PP-3 |
| 1.12 | pair-programmer | Make the deadline real, informed by 1.5: resolve `codex` to the vendored `codex.exe` and spawn it directly; own deadline that runs `taskkill /PID /T /F` while the direct child is still alive, then closes its stdio and settles the call as `timeout`. Same wrapper for the `tdd-gate` commands. Honour the request's abort signal and stop retrying after a cancel. No Job Object (Node has no API for it) and no sweep after the child is dead (the reason the earlier sweep was withdrawn) | PP-1, PP-2, R-6 |
| 1.13 | Hydra | Gateway session guard: a stale call may not tear down a newer session; in-flight siblings fail at once with `backend_reset` at teardown. Short cool-down for a dead backend | GW-2, GW-5, R-9 |
| 1.14 | AgentSmith, TheEights | Exit on stdin EOF; `unref` background timers; AgentSmith releases its lease on exit | AS-1, E-6 |
| 1.15 | Hydra | Regenerate the gateway schema cache (missing 6 TheEights tools and 1 AgentSmith tool); staleness check per approved W0.3 | RT |

Exit criteria:
- After a host timeout or interrupt, the next call on the same connection succeeds (integration test), and no
  `Connection closed` tool failure occurs over one week of normal use.
- The stalled hop in a 30-minute critique hang is identified from logs, and no call outlives its deadline.
- `not_ready` refusals fall from about 92% of TheEights calls to under 1%; the spool is empty.

### Phase 2: TheEights and daemon health and cost

| # | Repo | Change | Finding |
|---|---|---|---|
| 2.1 | operator | `ollama pull nomic-embed-text` | E-4 |
| 2.2 | TheEights | Embedding availability check tests the model itself; degraded search is reported as degraded; backfill unembedded memories | E-4 |
| 2.3 | TheEights | HITL tickets: expiry, pagination and filters on `hitl.list` | E-9 |
| 2.4 | TheEights | `verifyChain` in bounded batches with no held iterator; long scans off the main thread; missing indexes | E-7, E-11 |
| 2.5 | TheEights | Retention: checkpointed archive of old ledger segments that keeps the hash chain verifiable; jsonl rotation; fresh-home bootstrap | E-10, E-8 |
| 2.6 | AgentSmith | Hydra tail scan: incremental, bounded, single-flight, with eviction. Lease names its owner and never kills a process it did not spawn. Connect and call timeouts to pair-programmer and TheEights from config | AS-3, AS-2, AS-4, AS-5 |
| 2.7 | Hydra | Retention for `.hydra` workflow directories (44,926) and `checkpoints.db` (451 MB). Persist the TheEights breaker state in a small file so it survives across CLI calls | RT, C-09 |
| 2.8 | all | Hook hygiene: a timeout on every hook; the per-prompt hooks stop booting daemons; session-start checks call a real health tool | RT |
| 2.9 | all | Confirm each daemon's bundled SQLite includes the WAL-reset corruption fix (3.51.3 or a backport). Not yet checked locally | research |

Operator decisions needed in this phase: the 1,141 pending HITL tickets (2.3) and the roughly 973,000 flood
residue rows in the ledger (2.5). Both are governed records. Whether the operator key reaches the TheEights
daemon is unresolved between two reports, and 2.3 depends on it.

### Phase 3: CLI as the critical path (no broker required)

Order matters: correctness before traffic. By operator decision (section 6), 3.2 follows 3.1 directly and
everything here must hold under both the terminal CLI and the desktop app.

- 3.1 Close the idempotency gaps: a killed `step` must not open a second pair-programmer run; `record_attempt`
  gets a token; `finalize` writes its marker first; a retried PLAN submit must not mint a second PLAN envelope
  (C-05, C-06, Hydra#108).
- 3.2 Jobs as described in section 3, item 4: `step`, `submit-host-result`, `finalize`, `plan`; then
  `hydra job status <id>`.
- 3.3 Missing verbs: `repo resolve`, `health`, `envelope record`, `venom cross-check`, `cockpit audit`, and
  `hydra tool list|describe|call` through `governed_call` (approved W3.1, W3.3). Align the output shapes of
  `squads`, `repo list` and `trace`. `--json` and exit codes on every verb.
- 3.4 Lazy LangGraph import (C-04).
- 3.5 Operator capability for gate-clearing verbs, with the operator key out of the ambient environment
  (approved W3.4). Only then:
- 3.6 Runbooks and hooks name CLI verbs. This is a prose change in four runbooks; the judge agents keep their
  MCP tool or get a narrow CLI route.

Exit criteria: an attended run completes plan to finalize with the gateway disconnected, apart from the judge
call; no verb blocks the host for more than 60 s.

### Phase 4: shared broker (conditional)

Start only if, after one week on Phases 1 and 2, one of these targets is missed: zero dropped connections;
`not_ready` under 1%; p95 verb overhead under 5 s; no more than one watcher set per daemon.

If it starts, it is approved W3.3 to W3.5 with these additions, each of which needs its own design note first:
- Depends on approved W1.1 (RBAC, redaction and venom checks lifted out of the dispatcher) and W3.2, and on
  3.5 above, before the broker accepts anything but read-only tools.
- Lifecycle on Windows: a broker started from inside a Claude session's process tree dies with that session.
  Either keep the approved scope of one set per session, or run one per user from an at-logon scheduled task.
- Named pipe with a per-user ACL in preference to loopback plus a token file.
- A build stamp checked per call, with restart when the code changes.
- Calls are concurrent per backend and no lock is held across a backend call, because the engine child calls
  back into the broker.
- No in-process fallback that would start a second daemon set against the same databases.
- pair-programmer and AgentSmith use the broker when it is present and their own stdio path otherwise.

### Phase 5: MCP optional

The gateway remains for discovery and for clients that want MCP. Nothing on the attended critical path depends
on it.

## 5. Risks

- 1.3 without the recycle rule would turn "drop, reconnect, retry succeeds" into a second 30-minute hang on
  the same wedged backend. The recycle rule is part of the fix, not an extra.
- 1.12 changes behaviour: a deadline that actually fires may fail critiques that succeed slowly today. 1.5
  records real durations first.
- 1.10 before 1.9 would raise ledger junk and watcher load roughly 3.5 times. The order is fixed.
- The host half of the cancel fix has been tested only against a test host. 1.3 includes a run against Claude
  Code itself.

## 6. Operator decisions

Decided 2026-11-15:

- **Implementation route: attended Hydra runs.** The sibling repos must be registered in `~/.hydra/repos.json`
  before their items start. No direct edits to engine source.
- **Phase 0 issues: file them** on each owning repo.
- **Clients: the terminal CLI and the desktop app must work equally.** Consequences for this plan:
  - The 60 s ceiling in the Phase 3 exit criteria is a requirement for both clients, not a target for one.
    The desktop app is reported to cut stdio calls at about 60 s (unconfirmed for this version), so no verb
    may rely on a long host timeout.
  - Phase 3.2 (jobs) moves to the front of Phase 3, directly after 3.1.
  - Phase 1.1's timeout table must not assume `MCP_TOOL_TIMEOUT` is honoured; the inner caps are set so the
    gateway answers first under the shorter limit as well.
  - Phase 1.3's host-side test and every Phase 1 and Phase 3 exit criterion are run under both clients. The
    desktop app has no MCP log for the gateway today, so 1.2's gateway log is the only record there.

Still open:

1. Approval to start Phase 1.
2. HITL backlog (2.3) and ledger residue (2.5): options are in section 6.1.
3. If Phase 4 is ever started: one daemon set per session (as approved) or per user.

### 6.1 Options for the governed records

**HITL backlog.** 1,140 pending `hydra_gate` tickets across 1,003 runs (June 7, July 851, September 189,
October 53, November 41), plus one other. Only 8 resolves were ever recorded, the last on 2026-06-04. Each
ticket carries its own `expires_at`; the schema names an `expired` status that nothing sets. Resolves never
arrive because the resolve path starts with a fresh spawn that the readiness gate refuses (E-1). No option
deletes a ticket.

| Option | What happens | Cost and risk |
|---|---|---|
| A. Expire by each ticket's own `expires_at` | The sweep in 2.3 marks lapsed tickets `expired`, one audit event each. Unexpired tickets stay pending | Smallest. Records what is true: the gate lapsed unanswered. Loses nothing |
| B. Reconcile with Hydra, then A | For each ticket, read the workflow's checkpoint. Where Hydra holds an approve or reject, record that outcome with the operator key; expire the rest | Most accurate audit trail. Needs the operator key to reach the daemon (unresolved) and a one-off script |
| C. Bulk operator resolve | One keyed pass marks all as rejected or abandoned with one reason | Fast, but records an operator decision per ticket that was never made |
| D. Leave pending | Only add filters and pagination so the list stops costing 955 KB per call | No governance change; the backlog stays and keeps growing |

Recommendation (v1, superseded): drain the spooled writes first, then A.

**Correction, 2026-11-15.** The table above treats this as a manual cleanup. It is not. Hydra already has the
proper path, added for finding E2-17: `hydra resume`, `abort` and `hydra reap --apply` close the ledger row on a
terminal transition, and `hydra eights-hitl-reconcile [--apply]` sweeps the pending queue against the
checkpoint store (`hydra_core/eights/hitl_reconcile.py`). Option B is that tool. The backlog exists because the
path is broken, and the earlier recommendation was wrong on one fact: no resolves are waiting in the spool.

Defects in the proper path (verified in code and by listing the spool):

1. Every entry point starts a fresh TheEights process, which refuses the first call. The reconciler returns
   `eights_unreachable` (lebobo88/TheEights#8, Hydra#123).
2. `hitl.resolve` needs a token signed with `HYDRA_OPERATOR_KEY`. With the key unset the mint degrades to an
   unsigned token that the daemon rejects (`attestation.py:112-135`). The key is unset in the session shell.
3. The reconciler records every stale ticket as `rejected`, including gates of workflows that reached `done`
   (`hitl_reconcile.py:57-63`, `:158-167`).
4. Failed resolves from the gate-only resume path are lost, not spooled: zero `hitl.resolve` entries among
   1,997 spooled and dead-lettered calls.
5. The spool dead-letters entries older than 24 hours without a replay attempt, and nothing drains it
   automatically: 1,368 dead letters, 629 pending. Replaying the 166 stale `hitl.request` entries as they are
   would file new tickets for finished workflows.
6. TheEights never sets `expired`, and `hitl.list` has no filter (lebobo88/TheEights#15).

Items 2 to 4 are filed as Hydra#141 and item 5 as Hydra#140.

Revised recommendation: fix the path, then let it clear the backlog. Order: TheEights#8 (1.9, 1.10);
provision and verify the operator key; correct the reconciler's decision mapping; then run
`hydra eights-hitl-reconcile` as a dry run, review the counts by phase, and `--apply`. TheEights' expiry sweep
(2.3) keeps it clear afterwards. Two operator decisions remain: the decision recorded for a `done` workflow's
stale gate (proposed: `approved`, noted as inferred), and what to do with the 1,368 dead letters, of which
1,205 are audit records (`constitution.attest`, `envelope.record`) that never reached the ledger.

**Ledger residue.** `events` holds 1.23 million rows in about 660 MB: 973,222 `memory.add` rows from the old
pp-watcher flood (their memories were reclaimed, the events were not) and 168,637 `evolution.propose.rejected`.
The chain is intact; a full verify takes 5.4 s and boot verifies only the tail, so the size costs disk and
backup time, not reliability.

| Option | What happens | Cost and risk |
|---|---|---|
| A. Leave the rows, stop the growth | 1.9 removes about 99% of current growth; rotate and compress the jsonl mirror and logs | No governed record touched. Database stays near 880 MB |
| B. Archive a prefix with an anchor | Export events before a cut-off to compressed jsonl; keep an anchor row holding the last archived hash; verification seeds from the anchor | Reclaims most of the 660 MB and stays verifiable with the archive. Needs a design and a proof on a copy first |
| C. Delete the flood rows | Remove the 973,222 rows | Breaks the hash chain and needs a new genesis. Not recommended |

Recommendation: A now. B later, only if disk or backup size becomes a problem, and only after it passes on a
copy.

## 7. What changed from draft v1

Draft v1 was written from the investigators' summaries. The adversarial review (`plan-review.md`) found these
errors, all corrected above:

- v1 stated PP-1 as the cause of the hang. The logs do not support that; the cause is open (R-1).
- v1 described the cancel mechanism wrongly and set an exit criterion that could not be met (R-2).
- v1's cancel fix omitted the backend recycle and could have made retries worse (R-3).
- v1 gave 13 and 4 for the drop counts; the logs give 14 and 3 (R-4).
- v1's pair-programmer fix ("run codex without the shim, or a Job Object") would not have worked (R-6).
- v1 scheduled the TheEights bounded wait before the fixes it depends on (R-7).
- v1 left PP-2 without an item and deferred C-02 and C-08 behind a broker they do not need (R-12).
- v1 made a per-machine broker the centre of the design without declaring that it departs from the approved
  "one set per session", and without a lifecycle, identity or stale-code design (R-15 to R-18). v2 makes the
  broker conditional and puts the CLI path ahead of it.

Noted, not acted on: an orphan `python -` process (pid 35920) has used one core for 31 hours; pair-programmer
has logged five `better_sqlite3` native-module mismatch errors since 2026-10-24; TheEights has an uncommitted
`better-sqlite3` version bump on branch `fable-audit-2`; three `%TEMP%\eights-probe-*` directories and a
rewritten `~/.eights/eights.pid` are left over from the investigation's cold-start probes.
