# Investigation: pair-programmer and AgentSmith daemons behind the Hydra gateway

Date: 2026-11-15. Read-only. Repos at `C:\AiAppDeployments\pair-programmer` (HEAD ffb35b1) and
`C:\AiAppDeployments\AgentSmith` (HEAD fbbec34). No source edited, no daemon restarted or killed, no
gateway or pp tool called. Experiments ran only against processes I spawned myself with isolated
homes (`PP_HOME`, `AGENTSMITH_HOME`, `AGENTSMITH_CONSUMER_BASE`, `AGENTSMITH_BRIDGE_SINGLETON_DIR`
pointed into the scratchpad). Scripts: `...\scratchpad\ppsmith\{coldstart.mjs,scan.mjs,shim\t.mjs}`.

Labels: VERIFIED = reproduced or measured here, or read directly in code with no inference step.
INFERRED = follows from code but not reproduced.

## 0. Headline

1. **pp: CLI timeouts do not work for `codex` on Windows (PP-1, VERIFIED).** `codex` resolves to the npm
   shim `codex.cmd`. execa 9.6.1 kills only `cmd.exe` on timeout, then keeps waiting on the stdout/stderr
   pipes the grandchild still holds. A 3 s timeout had not settled after 20 s; it settled the instant the
   grandchild died. This is the root cause of pair-programmer#77 (1800 s hang).
2. **pp: every long tool is designed to exceed the host window (PP-2, VERIFIED in code).** Default CLI
   budget 5 min x 2 attempts, no progress notifications, no cancellation handling anywhere in the daemon.
3. **Smith: never exits on stdin EOF (AS-1, VERIFIED).** Still alive 12 s after stdin closed; pp exits in
   about 20-30 ms.
4. **Smith: the "singleton" eights lease kills other Smith instances' eights children (AS-2, VERIFIED in
   code, not reproduced live).** Machine-global lease file plus `taskkill /T /F`.
5. **Smith: the Hydra tail scan never finishes inside its own interval (AS-3, VERIFIED).** 44,926 dirs under
   `Hydra\.hydra`; one scan takes 10.0-12.6 s, scheduled every 5 s, so it runs continuously.
6. **There is no shared daemon for either system (PP-3/AS-0, VERIFIED).** Every stdio client is a full
   process. Each pp process that touches eights spawns its own TheEights daemon; 253 such spawns on
   2026-11-15, 102 of them (40%) timed out at the 3 s probe.

---

## 1. pair-programmer daemon

### 1.1 Process model (Q1)

- **No long-lived daemon.** `daemon/src/index.ts:47-104` dispatches on argv: `mcp`, `mcp-codex`, `mcp-agy`
  each call `server.connect(new StdioServerTransport())` in the invoked process
  (`mcp/harness-server.ts:1584-1585`, `mcp/codex-server.ts:1199-1200`). No socket, pipe, or pid file.
  `PID_LOCK` is declared at `util/paths.ts:15` and referenced nowhere else (grep).
- `pp-daemon serve` (HTTP on 127.0.0.1:7878, read-only, idle-shutdown 10 min; `index.ts:71-74`,
  `http/server.ts`) exists but nothing in hooks/settings starts it (grep) and stdio servers do not use it.
- The code says so itself: `db/database.ts:13-14` "Every tool call spawns a fresh stdio daemon process
  (pp_harness isn't pooled)".
- **What "[pp] daemon ok" probes:** nothing running. The hook is
  `node .../daemon/dist/index.js hook SessionStart daemon-up` (user scope, `~/.claude/settings.json`).
  Handler `hooks/dispatcher.ts:410-424` runs `doctor()` in that fresh process and prints
  `[pp] daemon ok.` (`:419`) if the SQLite file opens. It proves the DB file is openable and nothing about
  any gateway child.
- **Process count.** User-scope MCP registration is only `hydra_gateway` (`~/.claude.json`); pp and Smith
  are gateway backends (`~/.hydra/backends.json`: pp_harness, pp_codex, pp_agy, agentsmith, eights).
  Observed at 20:33 local: 4 `python -m mcp_servers.hydra_gateway` processes, each parented by a live
  `claude.exe` (4 sessions), two of them each holding their own `mcp-codex` child (pids 32448, 25808).
  So: per Claude session, 1 gateway + up to 3 pp processes + 1 Smith + 1 eights (spawned lazily), plus
  per pp process one more TheEights child when it touches eights, plus Smith's own eights / hydra_control /
  pp_harness children when those bridges are used (section 3). The engine path (`MCPStdioDispatcher`)
  adds its own copies; I did not read the Python side.
- **Hooks are processes too.** User-scope hooks spawn `node dist/index.js hook ...`: 6 on SessionStart,
  6 on every UserPromptSubmit, 5 on every Edit/Write, 2-3 around each pp tool call, 2 on Stop. No hook has
  a `timeout`. Measured (isolated home, ecosystem disabled): 1.8-2.7 s per hook process; `--help` alone
  1.55 s, so most of it is module load. `pp-daemon-2026-11-15.log` shows **292 distinct pids** that day.
- **SQLite** (`~/.pair-programmer/state.db`, 26.7 MB, WAL 4.2 MB): `journal_mode=WAL`
  (`db/database.ts:11`), `busy_timeout=15000` (`:24`). Every process open runs `conn.exec(SCHEMA_SQL)`,
  `applyMigrations`, and an unconditional `INSERT OR REPLACE INTO daemon_meta` (`:25-29`), so every hook
  and every server takes the write lock at first DB touch. `txImmediate` is `BEGIN IMMEDIATE` (`:277-288`).
  Only `recordVerdict` uses `txImmediateWithRetry` (3 attempts, 60 s wall cap, `:355-375`); the retry sleeps
  with `Atomics.wait` (`:311-314`), blocking the event loop. Every other write throws SQLITE_BUSY to the
  caller after 15 s. I found zero `SQLITE_BUSY` / `database is locked` lines in the 11-10..11-16 logs, so
  contention is a latent cost, not a demonstrated current failure.

### 1.2 Startup (Q2)

Measured cold start, spawn to `initialize` response (isolated `PP_HOME`):

| mode | initialize | tools/list | exits on stdin EOF |
|---|---|---|---|
| `mcp` (93 tools) | 1591 ms, 1044 ms warm | +10-20 ms | yes, 31 ms |
| `mcp-codex` (3 tools) | 1744 ms | +11 ms | yes, 23 ms |
| `mcp-agy` (3 tools) | 1275 ms | +5 ms | yes, 19 ms |

Nothing blocks before `initialize`: no DB open (lazy `db()`), no eights probe, no CLI probe, no network.
The 1.0-1.7 s is static import of the whole module graph from `index.ts:2-12`. stdout stayed clean (0 non-JSON
lines, 0 stderr bytes); logging is pino to a file with `sync: true` (`util/logger.ts:12-15`).

### 1.3 Findings

#### PP-1 CLI timeout is ineffective for `.cmd`-shimmed CLIs; hung codex hangs the tool call indefinitely
- Severity: **critical**. Confidence: high. **VERIFIED** (mechanism reproduced; the specific #77 incident
  was not captured, so attribution to it is INFERRED but consistent).
- Evidence: `mcp/cli-runner.ts:726-733` passes `timeout` to execa via `trackedExeca`; bin is the bare name
  `"codex"` (`mcp/codex-server.ts:454`). `Get-Command codex` gives `%APPDATA%\npm\codex.cmd`, whose last
  line launches `node ...\@openai\codex\bin\codex.js`. The limitation is acknowledged at
  `cli-runner.ts:287-303` ("grandchild can outlive the direct child").
- Reproduction (`shim\t.mjs`, pp's own execa 9.6.1): `fakecli.cmd` -> `node sleeper.mjs`, `timeout: 3000`.
  Result: `after 20051ms: settled=false grandchild_alive=true`. Second run: killed the grandchild at t=8 s,
  execa settled at 8041 ms with `timedOut=true`. So the promise resolves only when the real process exits.
- Consequences: `critique`/`generate` have no effective deadline; `abortAllInFlightChildren`
  (`cli-runner.ts:358-490`) awaits the same never-settling promise, hits `ABORT_TOTAL_CAP_MS=15000`
  (`:94`), then `util/shutdown.ts:136-146` retains all project locks and exits, leaving `codex.exe`
  orphaned and possibly still writing a worktree under `--sandbox workspace-write`. `tdd-gate.ts:552-565`
  has the same exposure for `npx`/`npm` test commands. `agy` is a real `agy.exe`, so its timeout works.
- Root cause: kill targets the shim pid; pipes are inherited by descendants; execa waits for stream end.
- Smallest fix: in `_spawnTracked`, resolve `.cmd` npm shims to `process.execPath` + the JS entry (or the
  vendored native exe) so the tracked pid is the real process; and add a hard wall-clock race in
  `runCliWithRetry` that, on expiry, runs `taskkill /PID <pid> /T /F` once (pid captured at spawn, guarded by
  "child has not emitted exit") and destroys the child's stdio so the promise settles. Return the existing
  `timeout` attempt status; tool contract unchanged.
- Test: unit test with a `.cmd` fake that spawns a 90 s sleeper; assert `runCliWithRetry({timeout_ms:2000})`
  returns within 5 s and the sleeper pid is gone. Remove the fix and the test must hang.

#### PP-2 Long tools exceed the host tool window by design; no progress, no cancellation
- Severity: **high**. Confidence: high. **VERIFIED** (code).
- Evidence: `DEFAULT_CLI_TIMEOUT_MS = 5 min` (`config.ts:17`); `CRITIQUE_RETRY_ATTEMPTS=1`, backoff 2 s
  (`config.ts:25-26`) gives 2 attempts = 10 min worst case for `critique`/`generate`
  (`cli-runner.ts:712-768`). `generate_image` clamps at 15 min (`codex-server.ts:115-124`). `tdd_*_check`
  defaults to 5 min, max 15 (`tdd-gate.ts:73,552`); pair-programmer#40 reports the 120 s cut-off.
  `grep -rn "extra.signal|sendNotification|notifications/progress|progressToken|AbortSignal"` over
  `daemon/src` returns nothing. Handlers take only `req` (`harness-server.ts:1560-1582`,
  `codex-server.ts:1187-1197`).
- Consequence: when the host or gateway gives up at about 120 s, the daemon keeps running the CLI; a retry
  starts a second concurrent codex in the same cwd. A cancelled request is not a disconnect, so
  `shutdownAndExit` does not run either.
- Smallest fix: (a) accept the handler's second argument and abort the tracked child on
  `extra.signal` abort; (b) when the request carries `_meta.progressToken`, send a
  `notifications/progress` heartbeat every 15-20 s while a CLI child is alive; (c) default
  `retry_on_transient` to false for critique so worst case equals `timeout_ms`.
- Test: in-process MCP client; call `critique` with a fake slow CLI, send `notifications/cancelled`,
  assert the child pid exits within 5 s; separately assert at least 2 progress notifications in 45 s.

#### PP-3 No shared daemon: per-process TheEights spawn, 3 s probe, sticky "unavailable"
- Severity: **high** (load and silent data loss), medium for gateway stability. Confidence: high.
  **VERIFIED** (code + logs).
- Evidence: `ecosystem/eights-client.ts:675-690` spawns `node <TheEights>/daemon/dist/index.js mcp` over
  stdio for this process only; resolution order `:463-498` (falls back to a hard-coded
  `C:\AiAppDeployments\TheEights` at `:492`). Probe timeout 3 s for connect and again for `tools/list`
  (`config.ts:528-530`, `eights-client.ts:698-709`). On failure `state = unavailable` and
  `ensureReady()` returns null forever for that process (`:741-743`); only the idle-close path resets to
  `uninit`. Calls: 8 s cap (`config.ts:556`), breaker 3 failures / 60 s (`:550-553`), fail-open.
- Logs: `pp-daemon-2026-11-15.log`: 151 `eights-client: connected`, 102
  `TheEights unavailable ... eights call timeout` (40% of probes). 11-14: 110 / 40.
- Drivers: hooks `eights-recall-project` (SessionStart), `eights-recall-request` (every prompt),
  `eights-recall-stage` (every `start_stage`) at `hooks/dispatcher.ts:511,787,1466`, each a fresh node
  process that cold-starts a TheEights daemon against its multi-hundred-MB DB.
- Consequence: about 250 TheEights cold starts a day from pp alone, competing with the gateway's own eights
  backend for the eights write lock; a long-lived `pp_harness` whose first probe lost the 3 s race runs
  standalone for its whole life (memory writes, `hydra_envelope_query`, `list_prior_critiques` silently
  return null).
- Smallest fix: make `unavailable` expire (re-probe after `ECOSYSTEM_BREAKER_COOLDOWN_MS`); raise the
  default probe budget to match measured eights cold start (inv-eights owns that number); and gate the
  three recall hooks behind a cheap pre-check (skip when no run is active) or route them through the
  gateway's already-running eights instead of spawning one.
- Test: fake eights that answers `initialize` after 4 s on first spawn and 100 ms on second; assert a
  second `safeCall` 61 s later succeeds. Count child spawns across 10 hook invocations and assert <= 1.

#### PP-4 Hook fan-out: 1.8-2.7 s node processes, 6 per prompt, no timeouts
- Severity: medium. Confidence: high. **VERIFIED** (measured, config read).
- Evidence: `~/.claude/settings.json` hooks list (6 SessionStart, 6 UserPromptSubmit, 5 on Edit/Write
  matchers, no `timeout` on any). Timings above. Three SessionStart hooks (`daemon-up`, `vendor-matrix`,
  `cli-version-pin`; `dispatcher.ts:415,431,462`) each call `doctor()`, which probes 5 CLIs concurrently
  (`runs.ts:4010-4036`, 15 s budget each) plus `agy models` (20 s): up to 18 sub-CLI spawns per session
  start.
- Root cause: `index.ts` imports every subsystem statically; one process per hook name.
- Smallest fix: dynamic `import()` per subcommand in `index.ts`; one multiplexed hook entry per event
  (the dispatcher already has `runHookInProcess`); cache `doctor()` CLI versions in `daemon_meta` with a
  short TTL; add `timeout` to each hook entry.
- Test: assert `node dist/index.js hook UserPromptSubmit risk-flag` wall time < 500 ms in CI; assert one
  SessionStart spawns at most 5 version probes.

#### PP-5 Unconditional write on every DB open
- Severity: low-medium. Confidence: medium. **VERIFIED** (code); impact INFERRED (no BUSY in logs).
- Evidence: `db/database.ts:25-29`. With PP-4, 6 parallel processes per prompt each take the write lock.
- Fix: read `schema_version` first; skip `SCHEMA_SQL`/migrations/meta write when it already equals
  `SCHEMA_VERSION`. Test: open twice, assert `PRAGMA data_version` unchanged after the second open.

#### PP-6 Project lock is owned by whichever process ran `start_run`
- Severity: low. Confidence: medium. **VERIFIED** (code), stuck scenarios INFERRED.
- Evidence: `runs.ts:104-106` acquires `<project>/.harness/.lock` with `{pid, started_at}`
  (`util/lock.ts:76-112`); `finalizeRun` releases by path (`runs.ts:2979`). A transport close (gateway
  reconnect) runs `shutdownAndExit`, which releases every lock that process holds
  (`util/shutdown.ts:147-161`) while the run is still open; a hard kill leaves the file until the next
  acquire sees a dead pid or 30 min age (`lock.ts:90,221-231`). `force_unlock` and `janitor`
  (`harness-server.ts:638,1354`; `janitor.ts`) exist for runs abandoned by a session that ended without
  finalize, and for PP-1's "unconfirmed survivors, retain locks" path.
- Fix: key lock liveness to the run row (status in DB) instead of the pid. Test: start_run in process A,
  kill A, start_run in process B for the same project succeeds only if A's run is not `running`.

#### PP-7 Robustness notes (no defect unless stated)
- `uncaughtException` / `unhandledRejection` / SIGTERM / SIGINT all route to `shutdownAndExit`
  (`index.ts:21-30`). Any stray rejection therefore kills the whole MCP server (exit 1): correct for
  integrity, but one bad fire-and-forget promise takes the backend down. INFERRED risk, none seen in logs.
- stdin EOF and `transport.onclose` both handled (`harness-server.ts:1590-1597`). VERIFIED by measurement.
- `console.log` in `dispatcher.ts:419` is hook mode only; no stdout writes in MCP modes (0 non-JSON lines
  measured).
- Blocking calls: `spawnSync git` with 5 s timeout per codex call (`codex-server.ts:245-247`);
  `execFileSync git` with **no timeout** in `janitor.ts:261`, reachable from the `janitor` tool. Low.
- Timers per process: eights idle-close (30 s, unref'd child). No watchers. Negligible.

---

## 2. AgentSmith daemon

### 2.1 Process model (Q1)

- **No shared daemon.** `daemon/src/index.ts` builds the kernel and calls `startMcpServer` on stdio
  (`mcp/server.ts:44-45`). No socket or pid file. State is `~/.agentsmith/decisions.jsonl`
  (`appendFileSync`, `archivist/decision-store.ts:35`) and in-memory maps (replication quotas,
  `sentinel/replication-controller.ts:9-11`, lost on every restart). `better-sqlite3` is a dependency and
  `statePath` is configured (`config.ts:135`) but nothing in `src` opens it (grep), so there is no SQLite
  contention.
- **What "[smith] online. Constitution sealed." probes:** `.claude/hooks/agentsmith-doctor.ps1` only reads
  `~/.claude.json` / `~/.hydra/backends.json` for a registration entry, tests that the constitution file
  exists, and counts sibling directories. It never contacts a daemon and never attests. All six Smith
  hooks are plain PowerShell; `agentsmith-flush.ps1`/`archivist.ps1` append to `decisions.jsonl` directly.
- Process count: one Smith per gateway (lazy; none alive at sample time, 5 boots logged on 11-15), each of
  which can spawn its own eights, hydra_control, and pp_harness children.

### 2.2 Startup (Q2)

Order in `index.ts`: load config, hash constitution (sync file read; fatal if unreadable, `:44-52`),
construct lazy bridges, fill the tool map with N8-refusal stubs, **`await startMcpServer`**, then start the
background attest loop and the two tails. Nothing network-bound precedes `initialize`.

Measured (isolated, eights path deliberately unresolvable): `initialize` 2758 ms, `tools/list` +7 ms
(30 tools), follow-up latency 1-2 ms. Real boot from `agentsmith-2026-11-15.log`: transport up, first
attest refused 2.5 s later with eights `not_ready: audit verification in progress`, attested at 3.5 s.
Until then every tool returns `status:"not_ready"` (`n8-attestation.ts:38-46`). Timeouts on that path:
eights connect 20 s (`bridges/mcp-client.ts:276`), per call 10 s hard-coded (`:409`), backoff 1-5 s then
11-30 s (`n8-attestation.ts:95-99`), `reattest` answers within 8 s (`:144-145`).

### 2.3 Findings

#### AS-1 Smith does not exit when its client goes away
- Severity: **high**. Confidence: high. **VERIFIED** (measured in isolated config; production path
  INFERRED from the same handles).
- Evidence: measurement `exited_after_stdin_eof: NO (still alive 12000ms after EOF)`; pp under the same
  harness exits in about 20-30 ms. MCP SDK 1.29.0 `server/stdio.js:31-32` listens only for `data` and
  `error` on stdin. Smith has no `stdin` listener (grep: the only hit is a comment at `index.ts:288`) and
  no `process.exit` outside the fatal path (`:316`). Keeping the loop alive: `setInterval` without
  `unref` at `sentinel/hydra-tail.ts:125`, `sentinel/eights-tail.ts:120`, `sentinel/tail.ts:127`, the
  attest loop's sleeps, and any bridge child's pipes. The SIGINT/SIGTERM listeners (`index.ts:~153-154`
  and inside the tail blocks) only stop loops; registering them removes Node's default exit.
- Consequence: if the parent closes pipes or dies without a hard kill, Smith lives on with its eights
  child and its continuous scan (AS-3). No exit path also means the lease file is never released: the
  current `~/.agentsmith/bridge-singletons/eights-stdio-daemon.json` names pid 32232, which is dead.
- Smallest fix: in `mcp/server.ts`, after `connect`, add `process.stdin.once("end"|"close", shutdown)` and
  `transport.onclose = shutdown`, where `shutdown` stops tails, closes the three bridges, and calls
  `process.exit(0)`; make SIGINT/SIGTERM call the same function.
- Test: spawn the built daemon, complete `initialize`, end stdin, assert exit within 3 s and that the
  bridge child pid (fake eights) is gone. Without the fix the test times out.

#### AS-2 The eights "singleton" lease makes Smith instances kill each other's eights child
- Severity: **high**. Confidence: high on mechanism (**VERIFIED** in code), medium on live frequency
  (not reproduced; only one Smith was alive at a time on 11-15).
- Evidence: `bridges/eights-bridge.ts:202` sets `singletonKey: "eights-stdio-daemon"`. Lease path is
  machine-global, `~/.agentsmith/bridge-singletons/<key>.json` (`mcp-client.ts:134-140`). Before every
  connect, `reapSingletonLease` (`:197-246`): if the recorded pid is alive and its command line contains
  `node` + the TheEights entry path, it runs `taskkill /PID <pid> /T /F` (`:112-124`, `:236`). That
  predicate is true for any other Smith's healthy eights child. The victim's next call sees
  "Connection closed", `isBrokenPipe` (`:370-378`), reconnects, and reaps the first one's child in turn.
  Historical logs contain `mcp-client reaped stale singleton child before reconnect` on five days.
- Consequence with 2+ Smith processes (4 gateways were up at sample time): alternating kills, each costing
  an eights cold start (up to 20 s) plus the 10 s call timeout; any eights request in flight in the killed
  child is lost; `/T` also kills that eights' own children.
- Root cause: the lease was meant to reap this Smith's own orphan from a previous life, but it is not
  scoped to an owner.
- Smallest fix: store `owner_pid: process.pid` in the lease and reap only when the owner is dead (or key
  the file by owner pid). Purpose preserved: orphans of dead Smiths are still reaped.
- Test: two `McpClient`s with the same `singletonKey` and injected `SingletonOps`; connect A then B;
  assert `killProcessTree` is not called while A's owner pid is alive, and is called once it is dead.

#### AS-3 Hydra tail scan runs continuously; after boot the sentinel can never see a new workflow
- Severity: **high** (cost), medium (function). Confidence: high. **VERIFIED** (measured + code).
- Evidence: `sentinel/hydra-tail.ts:13` `SCAN_INTERVAL_MS = 5000`; `scan()` (`:78-111`) does
  `readdir(<Hydra>/.hydra)` then one `stat` per entry. Measured with an equivalent script (`scan.mjs`):
  `entries=44926 total_ms=12618` cold, `10003` warm, `trace_files=43864 fresh24h=1484`,
  max event-loop lag 12 ms. With the re-entry guard (`:113-122`) the scan restarts as soon as it ends:
  about 4,400 `stat` calls per second per Smith process, indefinitely, on the 4-thread libuv pool.
  Budget: `tail-budget.ts:24` `maxFiles = 200`; `selectTailTargets` (`:51-67`) always keeps
  already-attached paths and sorts them first, and attached tails are never dropped. With 1484 fresh
  traces the 200 slots fill on the first scan, so no workflow created after boot is ever tailed. Each
  attached tail adds an `fs.watch` handle and a 1 s poll doing `existsSync`+`statSync`
  (`tail.ts:32-37,127-131`): about 400 sync fs calls per second. `eights-tail.ts` repeats the pattern over
  `~/.eights/events` (121 files, 724 MB).
- The event loop is not blocked (E2-10 fixed that), so this is not what delays `initialize`; it is constant
  disk and CPU load per Smith instance, multiplied by sessions and by AS-1 orphans, on the same disk the
  eights and pp SQLite files live on.
- Smallest fix: watch the `.hydra` root with a single `fs.watch` (or scan only entries whose directory
  mtime is newer than the last scan), back the scan interval off to at least 2x the last scan duration,
  and evict attached tails idle for longer than `maxAgeMs` so new workflows can take a slot.
- Test: temp root with 20k dirs; assert fewer than 1k `stat` calls on the second scan; create a new
  workflow after the budget is full and assert its first line is emitted.

#### AS-4 `agentsmith.pp.*` connect budget (2 s) is at the edge of pp's cold start
- Severity: medium. Confidence: medium. Timeout **VERIFIED**, failure rate INFERRED.
- Evidence: default connect timeout for any bridge not named `eights`/`hydra` is 2000 ms
  (`mcp-client.ts:274-282`); `PpBridge` spawns a new `pp_harness` (`pp-bridge.ts:80-92`). Measured pp
  `initialize` 1.04-1.59 s on an idle machine. Under load the bridge returns
  `degraded: pp-mcp-unavailable` and `pp_best_of_start` yields an empty candidate list rather than an error.
- Fix: `AGENTSMITH_PP_CONNECT_TIMEOUT_MS`, default 10 s. Test: fake server delaying `initialize` 3 s.

#### AS-5 Hard-coded 10 s call timeout; `not_ready` from eights is classified as transport
- Severity: medium. Confidence: high. **VERIFIED** (code + log).
- Evidence: `mcp-client.ts:407-411`. Any eights tool slower than 10 s degrades (memory add, evolution
  propose spools to disk at `eights-bridge.ts:293-326`, HITL request returns `request_id:"degraded"`).
  A timeout is not `isBrokenPipe`, so the connection is kept and the abandoned request keeps running in
  eights. The 11-15 log shows eights replying `status: not_ready, retry_after_ms: 2000` and Smith logging
  it as `eights-mcp-unavailable`; it recovered on the next loop pass, but `retry_after_ms` is ignored.
- Fix: per-bridge env-backed call timeout; honour `retry_after_ms` once inside `call()`.

#### AS-6 No crash handlers; synchronous unbounded work in tool handlers
- Severity: low-medium. Confidence: medium. **VERIFIED** (code); impact INFERRED.
- No `uncaughtException`/`unhandledRejection` handlers (grep), so Node's default applies: the process dies
  with the stack on stderr and no log line in `~/.agentsmith/logs`. Tail handlers are wrapped, so I found
  no concrete unhandled path.
- `keymaker.scan` walks eight project trees with `readdirSync`/`statSync` recursion
  (`keymaker/registry.ts:55-83,101-116`); `keymakerScanBudgetMs: 500` (`config.ts:141`) is never read.
  `DecisionStore.list` reads the whole `decisions.jsonl` (1.0 MB today) synchronously per call
  (`archivist/decision-store.ts:39-54`). Both block the event loop for their duration.
- stdout is clean: pino writes to a file (`logger.ts`), fallback loggers use `console.error`. Bridge
  children inherit stderr (10.6 KB reached my harness's stderr in the isolated run), which is harmless to
  JSON-RPC but lands in the gateway's stderr.

#### AS-7 Timers and watchers per Smith process (Q6)
Two 5 s scan intervals, up to 200 + 121 one-second poll timers each with an `fs.watch` handle, the attest
loop until attested. None are `unref`'d. See AS-3 for the measured cost.

---

## 3. Dependency graph

Every edge below is a **new stdio child process**, not a connection to a shared instance.

```
Claude Code session (x N; 4 observed)
 └─ hydra_gateway (python)                       1 per session
     ├─ pp_harness  (node … mcp)                 ── spawns ─► TheEights #A   (probe 3s, call 8s, breaker 3/60s, FAIL-OPEN, sticky-unavailable)
     ├─ pp_codex    (node … mcp-codex)           ── spawns ─► codex.cmd ► node ► codex.exe   (5 min x2; timeout ineffective: PP-1)
     │                                            └─ git (spawnSync 5s)
     ├─ pp_agy      (node … mcp-agy)             ── spawns ─► agy.exe        (5 min x2; timeout effective)
     ├─ eights      (node TheEights)             gateway's own instance  #B
     ├─ agentsmith  (node AgentSmith)
     │    ├─ spawns ─► TheEights #C              (connect 20s, call 10s, 1 reconnect; boot attest FAIL-CLOSED
     │    │                                        until attested; lease reaps other Smiths' #C: AS-2)
     │    ├─ spawns ─► python -m mcp_servers.hydra_control #2   (connect 15s, call 10s; venom check FAIL-CLOSED)
     │    └─ spawns ─► pp_harness #2             (connect 2s, call 10s; FAIL-OPEN/degraded)
     │                   └─ spawns ─► TheEights #D
     └─ hydra_control (python)                   gateway's own instance

Hooks (outside the gateway, per event):
 node pp hook … x6 per prompt ── eights-recall-* ── spawns ─► TheEights #E (one per hook process)
 pwsh agentsmith-*.ps1        ── no daemon contact
```

Edges, with behaviour when the dependency is slow:

| Edge | Timeout / retry / breaker | Failure mode | Effect on caller's tools |
|---|---|---|---|
| pp -> eights | 3 s probe x2, 8 s call, breaker 3 fails / 60 s, no re-probe | fail-open (null) | most writes are fire-and-forget; recall hooks wait up to 6 s; eights features silently off |
| pp_codex -> codex | 5 min x2 nominal; unbounded in practice | error result, or hang | tool call hangs; orphan codex (PP-1, PP-2) |
| pp_agy -> agy | 5 min x2 | error result | exceeds 120 s host window (PP-2) |
| smith -> eights (attest) | 20 s connect, 10 s call, background retry | **fail-closed**: all 30 tools `not_ready` | Smith unusable while eights is slow; AgentSmith#5 history |
| smith -> eights (tools) | same, 1 reconnect on broken pipe | degraded marker / spool | up to 30 s per call (connect + call) |
| smith -> hydra_control | 15 s connect, 10 s call | **fail-closed** (venom) / empty list | `evolution_propose`/`commit` blocked when Hydra python is slow |
| smith -> pp_harness | 2 s connect, 10 s call | degraded | best-of tools return empty |

Cycles: no call cycle found in pp or Smith source (pp never calls Smith or Hydra directly; Hydra envelopes
go through eights). The loops are in the **process tree** and in **shared files**: gateway -> smith ->
hydra_control re-enters Hydra code in a second python process; gateway -> smith -> pp_harness -> eights
creates a third and fourth TheEights instance against the same `~/.eights` DB; Smith tails files that
Hydra and eights write. Whether eights spawns anything back toward pp or Smith is inv-eights' question.

Cascade that matches the operator's symptoms: several sessions -> several gateways -> many TheEights
instances (B, C, A/D per pp process, E per prompt) contending on one eights DB -> eights cold start exceeds
3 s -> pp probes time out (40% observed) and Smith attest returns `not_ready` -> Smith tools refused, pp
runs degraded -> meanwhile a codex call with no effective deadline pins a gateway request past 120 s ->
operator reconnects the gateway -> new backends cold-start into the same contention, and with AS-2 a new
Smith kills the previous Smith's eights child.

---

## 4. Not determined

- Whether the gateway hard-kills backends or only closes pipes on reconnect (decides how often AS-1
  produces live orphans). No orphan Smith existed at sample time.
- Whether TheEights itself exits on stdin EOF, and its real cold-start distribution (inv-eights).
- AS-2 was not reproduced with two live Smith processes; the claim rests on the code path and on historic
  "reaped stale singleton" log lines.
- The specific pair-programmer#77 hang was not captured live; PP-1 shows the mechanism, not that incident.
- Actual SQLite write contention on pp `state.db` (no BUSY errors in a week of logs).
- The engine-path spawns (`MCPStdioDispatcher`) and their counts: Python side not read.
- Smith with the real Hydra root was not booted (it would attest against the live eights ledger), so the
  scan cost was measured with an equivalent standalone script, not inside Smith.
- `claude -p`, Playwright and browser-validator paths in pp were only grepped, not traced.

## 5. Suggested order

1. PP-1 (real deadline + tree kill for shimmed CLIs). 2. AS-1 (exit on stdin EOF). 3. AS-2 (owner-scoped
lease). 4. PP-2 (cancellation + progress). 5. AS-3 (scan back-off, eviction). 6. PP-3 (re-probe, stop
per-prompt eights spawns). 7. PP-4/PP-5, AS-4/AS-5.
