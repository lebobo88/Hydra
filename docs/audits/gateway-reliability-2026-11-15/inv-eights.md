# TheEights reliability and performance investigation

Date: 2026-11-15. Read-only. Repo `C:\AiAppDeployments\TheEights` at `dd4d253` (branch `fable-audit-2`), state at `~/.eights`.
Paths below are relative to `C:\AiAppDeployments\TheEights\daemon\src\` unless absolute.

## How this was done

- Read the daemon source, GitHub issues #3, #4 (closed) and #7 (open).
- Parsed the daemon's own pino logs in `~/.eights/logs` (142 files) per pid: boot, gate-open, refusals, tool calls.
- Opened `~/.eights/state.db` read-only (`readonly: true`, `query_only=1`) for counts, `dbstat`, `EXPLAIN QUERY PLAN` and timings. No content dumped.
- Cold-start probes:
  - Real DB, 3 runs: killed right after `initialize` + `tools/list`, inside the 250 ms grace window, so the deferred bootstrap never ran. Side effect: each run rewrote `~/.eights/eights.pid` and appended a few log lines.
  - Temp home, 2 runs (`USERPROFILE` redirected, DB pre-migrated): full boot to ready, then stdin EOF.
- Probed local Ollama (`/api/tags`, `/api/ps`, `/api/embeddings` with the string "ping").
- Scratch scripts are in the scratchpad dir: `eights-db*.mjs`, `coldstart.mjs`.

## Headline numbers

| Measure | Value |
|---|---|
| Daemon boots, 2026-11-14..16 logs | 526 |
| Boots that never opened the readiness gate | 413 |
| Tool calls refused `not_ready` / served, Nov 14..16 | 460 / 38 |
| Tool calls refused `not_ready` / served, Oct 25..31 | 2,139 / 111 (1,825 boots) |
| Gate-open time after boot, Nov | p50 965 ms, p90 1,923 ms, max 6,160 ms |
| Gate-open time after boot, Oct | p50 648 ms, p90 994 ms, max 3,709 ms |
| `initialize` response, real DB, 3 runs | 615 / 730 / 751 ms from spawn (about 600-700 ms is Node module load) |
| `tools/list` | 7-10 ms after `initialize`, 71 tools, 39 KB |
| Graceful shutdowns logged ("shutting down") | 0 in the 2,351 boots sampled |
| `state.db` | 879 MB, 214,646 pages, freelist 0, WAL 2.5 MB |
| `events` table | 1,227,141 rows, 660.6 MB, plus 65.7 MB in its two indexes |
| `memories` | 18,515 rows, 52.7 MB |
| `hitl_queue` | 1,149 rows, 1,141 pending |
| `~/.eights/events/*.jsonl` mirror | 724 MB, never rotated |
| Full chain verify, warm cache, read-only | 5.4 s for 1.227M rows, chain intact |
| Eights daemon processes alive at inspection | 0 (pidfile names dead pid 45340) |

---

## Findings

### F1. The per-spawn readiness gate refuses the first call, and no client retries
Severity: critical (reliability). Confidence: high. VERIFIED (logs and code).

Evidence
- `mcp/server.ts:185-195`: any gated tool called before the gate opens returns `isError` with `{status:"not_ready", retry_after_ms:2000}`.
- `index.ts:168-193`: the gate state is per process, so every stdio spawn starts closed.
- `index.ts:334-400`: the gate opens only after the whole deferred bootstrap.
- Logs: of 526 boots on Nov 14..16, 385 received a tool call 50-270 ms after boot, were refused, and logged nothing further, with the last log line under 1 s after boot (426 of 526).
- Refused tools, Nov: `memory.search` 238, `audit.verify` 55, `memory.add` 46, `budget.charge` 45, `hitl.list` 28, `hydra.envelope.record` 24, `cells.classify` 20.
- No consumer honours the hint. Grep for `not_ready|retry_after_ms|eights.health` in Hydra `hydra_core/`, `mcp_servers/`, and in the AgentSmith and pair-programmer daemon sources finds no eights caller that retries.
- Hydra maps the refusal to "unreachable": `hydra_core/eights/attestation.py:961-964` returns `None` on a non-success result, and `hydra_core/cli.py:1256-1257` turns `None` into `eights_unreachable`.
- `eights.health` (ungated, added for E2-4) was never called in the sampled logs.

Root cause
- Short-lived stdio clients spawn, call once and exit. The daemon answers the handshake in about 50 ms but needs 0.6-6 s before it will serve anything. E2-4 (issue #4) made the refusal structured but left the retry to callers that do not implement it.
- This is the mechanism behind issue #7 and Hydra #123: the daemon is reachable, and through a long-lived gateway child the gate is already open.

Smallest fix
- In `createToolCallHandler`, when the gate is pending (not failed), wait for it to open, bounded by about 10-15 s or the tool deadline, then run the tool. Return `not_ready` only on that timeout and `failed` immediately.
- No audited operation runs before verification, so fail-closed is kept. Tool names, schemas and result shapes are unchanged.

Test
- Spawn the daemon, send `initialize`, then immediately `tools/call eights.governance.hitl.list`. Assert a normal result and no `not_ready`.
- With a deliberately broken chain, assert `status:"failed"` straight away.
- Remove the wait and show the first test fails.

### F2. The gate opens late because bootstrap does non-verification work first
Severity: high (multiplies F1, drives growth). Confidence: high. VERIFIED.

Evidence
- Order in `index.ts:334-378`: `migrate`, `vec.load`, `graph.open`, `registerActor`, `seedCriticalResources`, `seedEvalRubrics`, `seedConstitutions`, `promptRegistrar.run`, then `audit.verifyChain`.
- Timeline for pid 27472: transport +53 ms, graph opened +466, constitutions +521..586, prompt registrar done +1252, gate open +1255.
- The incremental verify itself takes about 3-4 ms. The prompt registrar for `pp` alone takes 0.66-1.4 s on every boot (`registrars/prompts.ts:37-75`: walks four repos and reads every agent, command and skill file).
- `stores/graph.ts:18-30` imports the native `kuzu` module at boot and then installs `StubGraphImpl`. That costs 0.2-0.75 s for nothing.

Root cause
- Idempotent seeding and a filesystem re-scan sit in front of a 4 ms tail verify, and are repeated by every one of the roughly 250-450 spawns per day.

Smallest fix
- Run `migrate`, `vec.load`, `verifyChain`, then `auditGate.pass()`, then seeds and registrar. That is also more faithful to the stated rule that nothing audited runs on an unverified chain.
- Throttle the registrar (for example skip when a `daemon_meta` stamp is under N minutes old).
- Do not import `kuzu` while the implementation is a stub.

Test
- On a seeded DB, assert `eights.health` reports `ready` within 500 ms of `initialize`, and that a second boot within the throttle window writes zero `events` rows.

### F3. Every boot writes about 75 junk audit events: `eights.system` is never a registered actor
Severity: high (growth, write contention, and a silent functional failure). Confidence: high. VERIFIED.

Evidence
- `engines/identity.ts:19`: `registerActor` rewrites any name not starting with `act_` to `act_<nanoid>_<name>`. `index.ts:348` calls `registerActor("eights.system","system")` on every boot.
- `actors` has 5,804 `system` rows, all `act_*`. No row has `actor_id` equal to `eights.system`, `eights.iolaus`, `miner`, `pp-watcher` or `eights.memory-steward`.
- `engines/evolution.ts:400-406`: `propose()` rejects unregistered actors and audits the rejection.
- The boot registrar reaches `propose()` through `registrars/common.ts:147-150` and `evolution.ts:1183-1187` for every drifted source file. Result: "prompt-registrar complete … pp … errors:69" on every boot.
- All 54,629 `evolution.propose.rejected` events in the last 60k events have reason `actor not registered` and trace `seed-prompts`.
- Last 20 days of events: `evolution.propose.rejected` 52,568 and `constitution.get` 4,458 (five per boot from `constitution.ts:59,66`), out of about 57.7k. Roughly 99% of recent ledger growth is boot noise.
- Logs also show `iolaus propose failed … actor 'eights.iolaus' is not registered`.

Root cause
- The actor id is mangled at registration, so the actor-authorisation gate in `propose()` rejects the system's own internal actors.

Consequences
- Each bootstrapped spawn runs about 75 `BEGIN IMMEDIATE` transactions and 75 `appendFileSync` calls.
- Source-drift proposals from the registrar, and Iolaus proposals, are never created.

Smallest fix
- Upsert the literal ids (`eights.system`, `eights.iolaus`, `miner`, …) at boot the way the operator actor already is (`index.ts:357-360`).
- Stop auditing `constitution.get` from the internal `seed()` path.

Test
- Boot twice on a DB with one drifted prompt file. Assert no `evolution.propose.rejected` rows with trace `seed-prompts`, exactly one pending proposal, and an unchanged `actors` count on the second boot.

### F4. Embeddings have been dead since 2026-09-19 and the daemon reports them healthy
Severity: high (memory is functionally degraded). Confidence: high. VERIFIED.

Evidence
- Default provider is local Ollama with `nomic-embed-text`, 768 dimensions (`embeddings.ts:23-28`).
- `GET /api/tags` lists only `qwen3:4b`. `POST /api/embeddings` returns 404 `model "nomic-embed-text" not found` in 0.21 s.
- `available()` checks only that `/api/tags` answers (`embeddings.ts:33-42`), so the boot log says `embedAvail:true`.
- DB: the last embedded memory is dated 2026-09-19. October has 0 of 1,176 embedded, November 0 of 182. The last 2,000 `memory.add` events all have `embedded:0`.
- `memory.search` events carry `embed_error: Ollama 404…` and `path: episodic`.
- The fallback is `content LIKE '%<whole query>%'` (`engines/memory.ts:236-250`), which only matches a verbatim substring.

Root cause
- The model is missing from Ollama, and the availability probe does not check for the model.

Smallest fix
- Operational: `ollama pull nomic-embed-text`.
- Code: make `available()` require the configured model in `/api/tags`, expose `embedder: ok|missing_model|down` in `eights.health`, and tokenise the fallback query.
- Later: backfill embeddings for rows with `embedding_id IS NULL`.

Test
- Point the embedder at a fake Ollama whose tags omit the model. Assert `available()` is false, health shows `missing_model`, and a multi-word query still returns hits containing any of its terms.

Not determined: why the eight successful `memory.search` calls in October took 4-7 s. The 404 round trip is 0.2 s and the SQL is under 100 ms warm.

### F5. One process per client, each with the full background stack, and no leader
Severity: high (performance, contention). Confidence: high. VERIFIED.

Evidence
- No singleton by design: `index.ts:82-146`. The pidfile is a breadcrumb, not a lock. There is no HTTP or socket transport in `daemon/src`.
- Spawners: the Hydra gateway (`~/.hydra/backends.json`, stdio, no `env`), Hydra's Python dispatcher, AgentSmith, pair-programmer, `cli/src/mcp-client.ts:56`, `web/server/eights-client.ts:77`, `integrations/hydra/eights_memory.py:94`.
- Every process that passes the gate starts everything (`index.ts:303-314`):

| Job | Interval | Source |
|---|---|---|
| pp-watcher | 5 s | `pp-watcher.ts:62,86` |
| execsuite-watcher | 15 s (`readdir` + `stat` of the output tree) | `execsuite-watcher.ts:38,49,76-97` |
| rlm-watcher, xenia-watcher | 20 s | `rlm-watcher.ts:47` |
| miner | 1 h | `miner.ts:33,39` |
| memory-steward | 6 h | `memory-steward.ts` |
| cost-analyst, iolaus, full audit verify | 24 h | `cognitive/*.ts` |

- Logs: up to 10 watcher-running daemons at once (2026-10-28 05:38Z), 4 at once on 2026-11-15, longest life 21.7 h.
- The web bridge already works around this with `EIGHTS_DISABLE_WATCHERS=1` and explains why (`web/server/eights-client.ts:65-76`).

Root cause
- Background producers are tied to the per-client transport process.

Smallest fix
- A leader lease (a `daemon_meta` row with pid and heartbeat, taken under `BEGIN IMMEDIATE`, or an exclusively opened lock file). Only the lease holder calls `startBackgroundWork()`. Others serve tools only and take over when the lease goes stale.
- Stop-gap: set `EIGHTS_DISABLE_WATCHERS=1` for every spawner except one designated host.

Test
- Start three daemons on one home. Assert exactly one logs `pp-watcher started`. Kill it and assert another takes over within the lease TTL.

### F6. After the client disconnects, a ready daemon never exits
Severity: high. Confidence: high. VERIFIED by experiment.

Evidence
- Temp-home probe, two runs: after `stdin.end()` the process was still alive 6 s later and had to be killed.
- Nothing listens for stdin `end`/`close` or transport close (`mcp/server.ts:285-286`, `index.ts:471-472` handle only SIGINT and SIGTERM).
- Watcher and job timers are not `unref`'d: `pp-watcher.ts:86`, `execsuite-watcher.ts:49`, `rlm-watcher.ts:47`, `xenia-watcher.ts:53`, `miner.ts:39`, `memory-steward.ts:31`, `cost-analyst.ts:28`, `iolaus.ts:40`, `otel-sink.ts:55`.
- On Windows the parent's kill is `TerminateProcess`, so the `shutdown()` path (`index.ts:457-470`: stop watchers, TRUNCATE checkpoint, close DB, clear pidfile) has not run once in the sampled logs.

Root cause
- Process lifetime is held by background timers, not by the transport.

Smallest fix
- Call `shutdown("stdin-eof")` from `process.stdin.on("end"|"close")` and the transport's `onclose`, and `unref()` the timers.

Test
- Spawn, wait for ready, close stdin. Assert exit code 0 within 2 s, a "shutting down" log line, and no pidfile.

Not determined: whether orphans accumulate in practice. None were alive at inspection, so parents currently kill their children.

### F7. An open iterator in `verifyChain` makes every other query on the connection throw
Severity: medium-high (hard tool errors during any long verify). Confidence: high. VERIFIED (code and logs).

Evidence
- `engines/audit.ts:135-156`: `.iterate()` is held open across `await setImmediate` yields every 5,000 rows. better-sqlite3 rejects any other statement on that connection while an iterator is open.
- Logs contain 5,418 occurrences of `TypeError: This database connection is busy executing a query`, on pp-watcher and memory-steward ticks.
- The web bridge serialises its calls and documents the same error (`web/server/index.ts:88-93`).
- Triggers: `eights.audit.verify {full:true}`, the 24 h `AuditVerifierJob` in every long-lived process (`cognitive/audit-verifier.ts:38,51`), and a boot verify with a long tail.
- Duration today is about 5.4 s. At the earlier 3.5 GB size it was about 120 s. The open iterator is also a read transaction that pins the WAL snapshot for that long.

Smallest fix
- Verify in keyset chunks with `.all()` (`WHERE event_id > ? ORDER BY event_id LIMIT 5000`) so no statement is open across a yield, or use a dedicated read-only connection.

Test
- On a 20k-event DB, start `verifyChain({full:true})` and call `audit.record()` and `hitlList()` from interleaved macrotasks. Assert no throw. Revert to `.iterate()` and show it throws.

### F8. A fresh `EIGHTS_HOME` cannot boot
Severity: medium (install, recovery, hermetic tests; also a trap for the next migration). Confidence: high. VERIFIED by experiment.

Evidence
- Temp-home probe on an empty directory: `eights-daemon failed to start: SqliteError: no such table: events … at new AuditEngine`.
- `engines/audit.ts:55-74` prepares statements in the constructor at `index.ts:156`. `sql.migrate()` only runs later, inside the deferred bootstrap (`index.ts:335`).

Root cause
- The transport-first change (`c0a7f3f`) moved `migrate()` behind constructors that prepare SQL.

Smallest fix
- Prepare statements lazily in `AuditEngine`, or run the V1 `CREATE TABLE IF NOT EXISTS` block in the `SqliteStore` constructor.

Test
- Boot on an empty temp home and assert `eights.health` reaches `ready`.

### F9. HITL tickets only accumulate: 1,141 pending
Severity: medium-high. Confidence: high for the facts, medium for the key hypothesis.

Evidence (VERIFIED)
- 1,140 pending rows of kind `hydra_gate` across 1,003 runs (2026-06: 7, 07: 851, 09: 189, 10: 53, 11: 41), plus one other pending row.
- Only 8 `governance.hitl.resolve` events exist, the last on 2026-06-04.
- No expiry: the schema comment lists `expired` (`stores/sqlite.ts:364`) but nothing sets it, although each payload carries `expires_at`.
- `hitlList` (`engines/governance-state.ts:249-255`) has no `kind`, `run_id` or `limit` filter. It returns all 1,141 rows (955 KB of JSON), and Hydra filters client-side. Every list also writes an audit event: 26,137 `governance.hitl.list` events, 25,982 of them in June.
- Resolve needs an operator capability token (`governance-state.ts:232-239`) signed with `HYDRA_OPERATOR_KEY` (`auth/capability.ts:149-155`).

Why they accumulate
- Requests get through: 26 `hitl.request` calls succeeded on Nov 14..16, probably because the same connection had already been refused once and stayed open.
- The resolve path starts with a fresh spawn and `hitl.list`, which F1 refuses, so no resolve ever arrives.

INFERRED
- `HYDRA_OPERATOR_KEY` is not set in User or Machine environment, nor in `backends.json` (which has no `env` block). If the daemon really has no key, every resolve, `cap.set` and `breaker.reset` would fail even once reachable. I could not see the launcher's effective environment.

Smallest fix
- F1 first.
- Add optional `kind`, `run_id` and `limit` arguments to `hitl.list`. This is additive, so the contract holds.
- Add an expiry sweep that sets `status='expired'` when `payload.expires_at` has passed, audited.
- The backlog needs a one-off operator-keyed bulk resolve or expire, after the triage issue #7 asks for.

Test
- Insert a pending row with a past `expires_at`, run the sweep, and assert `expired` plus one audit event. Assert `hitl.list {kind, run_id}` returns only matching rows.

### F10. Growth: the ledger is 75% of the DB and nothing is ever compacted
Severity: medium. Confidence: high. VERIFIED.

Evidence
- `events` by kind: `memory.add` 973,222 (the pp-watcher flood; the corresponding `memories` rows were reclaimed down to 18,515, the events were not), `evolution.propose.rejected` 168,637, `constitution.get` 26,566, `governance.hitl.list` 26,137.
- Events per month: May 667k, June 372k, July 14k, August 3k, September 51k, October 102k, November 17k so far.
- Each `audit.record` is a `BEGIN IMMEDIATE` transaction plus a synchronous `appendFileSync` to the jsonl mirror (`engines/audit.ts:77-105`).
- Reads are audited too: `memory.search`, `hitl.list`, `envelope.query`, `cells.distribution`, `evolution.read`.
- No retention anywhere: `memories.expires_at` is never set (0 rows), 142 log files (55 MB) and the 724 MB jsonl mirror are never rotated.
- 2,231 orphan vectors: `mem_vec_rowids` 10,531 against 8,300 referenced. `memory.add` is three separate autocommits (`engines/memory.ts:132,153,187`).
- Miner memories have no idempotency key (`miner.ts:83-90`): 9,035 rows, all from May and June.

Smallest fix
- F3 and F5 remove about 99% of current growth.
- Compaction of an append-only chain needs a design decision: archive a prefix to compressed jsonl and keep an anchor row holding the last archived hash, so `verifyChain` seeds from the anchor. I am not proposing to delete audit rows without that.
- Cheap now: rotate or compress logs and the jsonl mirror, and give miner and steward writes an `idempotency_key`.

Test
- After archiving a prefix on a copy, `verifyChain({full:true})` passes from the anchor and the row count drops.

### F11. Synchronous O(N) work on the event loop
Severity: medium. Confidence: mixed, marked per item.

- VERIFIED: `eights.evolution.detect_drift` took 29.5 s and 33.1 s (logs). It re-reads and hashes about 1,783 source files per call (`evolution.ts:806-878`).
- VERIFIED: `eights.audit.trace` by `trace_id` or `run_id` runs `json_extract` over all 1.2M events with no index (`mcp/audit.ts:43-50`). Measured 1.3 s warm, fully blocking.
- VERIFIED: the per-hit lookup in `memory.search` has no index on `memories(embedding_id)` (plan: `SCAN memories`, `engines/memory.ts:256-258`). Measured 52-63 ms for 20 lookups warm. Small today, linear in table size.
- INFERRED: `IolausJob` runs `COUNT(*) FROM events WHERE payload_json LIKE ? AND ts > ?` once per stale resource (`cognitive/iolaus.ts:66-69`). That is 1,629 candidates at about 0.1 s each, roughly 160 s of blocked event loop per run, per long-lived process, daily. It has run four times in total.
- VERIFIED by reading: `busy_timeout=5000` on a synchronous driver (`stores/sqlite.ts:42`) means lock contention freezes the whole process, including the JSON-RPC reader, for up to 5 s per statement. No `SQLITE_BUSY` appears in the October and November logs, so this is latent.
- VERIFIED by reading: the tool deadline is a `Promise.race` and cannot interrupt a synchronous handler (`mcp/server.ts:160-166`).

Smallest fix
- `CREATE INDEX idx_memories_embedding ON memories(embedding_id)`.
- An expression index on `json_extract(envelope_json,'$.trace_id')`, or accept the cost and document it.
- Cache drift hashes by file mtime and size.
- Give Iolaus one pass over recent events instead of one query per resource.

Test
- `EXPLAIN QUERY PLAN` assertions for the two indexes, and a wall-clock bound on `detect_drift` over an unchanged tree.

### F12. The running build is stale relative to source
Severity: medium. Confidence: high. VERIFIED.

Evidence
- `dist/*` is dated 2026-09-01. `src/schemas/hydra-envelope.ts` and `src/mcp/hydra.ts` changed on 2026-09-16 (`ec2c8b2`), adding `PLAN`, `SUPPORT_TICKET`, `PORTABLE_CONTEXT`, `VOC_REPORT` and `JUDGE_VERDICT`.
- `grep -c JUDGE_VERDICT dist/schemas/hydra-envelope.js` returns 0. `dist` is git-ignored.
- The running daemon therefore rejects `eights.hydra.envelope.record` for those five types at Zod validation, and Hydra spools them.
- The working tree also has an uncommitted `better-sqlite3` bump from `^11.5.0` to `^13.0.3` (13.0.3 is installed and loads on Node v26.12.1).

Smallest fix
- `npm run build` in `daemon/`, and have `eights.health` report a build identifier.

Test
- Record a `PLAN` envelope against the built daemon and assert success.

### F13. Failure handling gaps
Severity: low-medium. Confidence: medium.

- VERIFIED: there are no `unhandledRejection` or `uncaughtException` handlers (only `index.ts:471-472`).
- INFERRED: `miner.ts:39,63` calls `audit.record` outside its try block under `void this.runOnce()`. A busy-connection or `SQLITE_BUSY` throw there becomes an unhandled rejection, which exits Node by default.
- VERIFIED: tool errors are logged without their message (`mcp/server.ts:233-240` logs only `isError`). There are 7,935 `isError:true` lines from June to September with no cause recorded.
- VERIFIED (negative): no stdout pollution. There is no `console.log` in the daemon path, and five probe runs produced zero non-JSON stdout lines.
- VERIFIED: the log filename is fixed at boot (`logger.ts:8-9`), so a long-lived process writes several days into one file.

Smallest fix
- Add both handlers (log, then continue or exit cleanly), move `miner.run` auditing inside the try, and log `error` on failed tool calls.

Test
- Make `audit.record` throw inside `miner.runOnce` and assert the process survives and logs it.

### F14. Usability surface
Severity: low. Confidence: medium. VERIFIED by reading.

- Most tools require a six-field envelope (`schemas/envelope.ts`: `actor_id`, `project_id`, `domain`, `trace_id` required). `actor_id` is not checked against `actors`, except in `propose()` and the operator operations.
- `zod-to-json.ts:62` emits `{}` for any Zod type it does not know (effects, preprocess), so some published input schemas are empty.
- Breakers latch: three failures trip one, and only an operator-keyed reset clears it (`governance-state.ts:281-317`). `breaker_state` currently has 0 rows, so none is latched.
- `EIGHTS_SKIP_AUDIT_CHECK` is the only override for a broken chain. The chain is intact today.

---

## Answers to the eight questions

1. **Process model.** One process per client, no singleton, stdio only. All open the same `state.db` in WAL with `synchronous=NORMAL`, `busy_timeout=5000`, `wal_autocheckpoint=1000`, a 256 MB `journal_size_limit`, plus a PASSIVE checkpoint every 5 min (`stores/sqlite.ts:25-44`, `index.ts:442-500`). WAL is healthy now: 243 checkpoint log lines in October and November, none busy, largest WAL 5.9 MB. The earlier 12.7 GB WAL is described only in a code comment. The long-lived reader risk is F7.
2. **Startup.** Before `initialize`: module load, pidfile, logger, DB open and pragmas, constructors, tool registration. No migration, verify or model load. After a 250 ms grace: the F2 sequence, then the gate, then watchers.
3. **Event-loop blocking.** F7 and F11.
4. **Growth.** F3, F9, F10.
5. **Background work.** F5, F6.
6. **Failure handling.** F6, F8, F13.
7. **Embeddings.** F4.
8. **Unusable.** F1 first, then F4, F9, F12, F14.

## Could not determine

- Which parent processes produce the sub-second spawns. The log has no parent pid, and none were alive.
- Whether the Hydra gateway keeps a persistent eights child. No eights daemon was running at inspection.
- Whether `HYDRA_OPERATOR_KEY` reaches the daemon in practice (F9).
- The cause of the 4-7 s `memory.search` calls in October (F4).
- Real cross-process write contention. No `SQLITE_BUSY` in recent logs, but failed tool calls are logged without messages (F13).
- Cold page-cache timings. All DB timings here are warm.

## Ranked causes

Reliability
1. F1: the first call on every fresh spawn is refused and nobody retries. This is the "unreachable" symptom.
2. F2: the gate opens 0.6-6 s late because of boot work unrelated to verification.
3. F4: embeddings silently dead, so memory search is a verbatim substring match.
4. F9: HITL tickets cannot be resolved or expired, 1,141 pending.
5. F12: stale build rejects five envelope types.
6. F7: any long verify makes concurrent calls throw.
7. F6: daemons outlive their client and never shut down cleanly.
8. F8: fresh home cannot boot.
9. F13: no crash handlers, error messages not logged.

Performance
1. F5: up to 10 processes each running every watcher and job against one DB.
2. F3: about 75 write transactions and file appends per boot, across 250-450 boots a day.
3. F2: the registrar re-reads four repos and loads an unused native module on every boot.
4. F11: 30 s `detect_drift`, 1.3 s unindexed `audit.trace`, Iolaus scan, 5 s busy-wait on a synchronous driver.
5. F10: 660 MB ledger that is mostly flood residue and boot noise, plus 724 MB of unrotated jsonl.
6. F9: `hitl.list` returns 955 KB unfiltered on each call.
