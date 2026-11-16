# Research: MCP gateway reliability and CLI-over-daemon practices (2026-11-15)

Method: primary docs fetched 2026-11-15; GitHub issues read via `gh`. "UNCONFIRMED" = not verified from a primary source. Claude Code behaviour is version-dependent (docs reference v2.1.2xx-2.1.3xx); check `claude --version`.

## 1. Claude Code MCP client

Source: https://code.claude.com/docs/en/mcp , https://code.claude.com/docs/en/env-vars

- **Startup timeout**: `MCP_TIMEOUT` (ms). Startup/connection timeout; for HTTP/SSE it also feeds a per-request timer (max of 60 s, tool timeout, MCP_TIMEOUT). The default value was not shown in the fetched excerpt (UNCONFIRMED).
- **Tool-call timeout**: `MCP_TOOL_TIMEOUT` (ms), docs say default "about 28 hours" when unset. Per-server `"timeout"` (ms, >=1000) in the server entry overrides it, and is a hard wall-clock limit that progress notifications do NOT extend.
- **Idle timeout** `CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT` (needs v2.1.187+; stdio exempt before v2.1.203): aborts a call with no response AND no progress notification for the window. Default 5 min for HTTP/SSE/WS/claude.ai connectors, 30 min for stdio; `0` disables. Progress notifications DO reset the idle clock, not the wall clock.
- **Output**: `MAX_MCP_OUTPUT_TOKENS` default 25,000 (warn at 10,000); text results >50,000 chars are saved to a file regardless; server may raise per tool via `_meta["anthropic/maxResultSizeChars"]` (ceiling 500,000).
- **Crash/disconnect**: the docs describe five reconnect attempts, exponential backoff from 1 s, for dropped remote servers. For **stdio**, interactive mode marks the server failed and requires `/mcp` -> Reconnect (or `/mcp reconnect all`, v2.1.284+). In `claude -p` and the Agent SDK, a stdio server that exited is restarted on the next tool call. First-connection stdio failures are not retried. `tools/list` discovery is retried up to 3x on transient errors, but not on timeouts. If a `list_changed` refresh fails, the previous tool list is kept.
- **Windows stdio shutdown**: closes stdin, then ends the process tree after 300 ms (v2.1.296+; earlier versions killed the tree immediately).
- **Tool search**: on by default for Sonnet/Opus/Haiku 4+. Only tool names and server instructions load at start; "no fixed per-server tool cap; the practical limit is your context window." `ENABLE_TOOL_SEARCH=auto|auto:N|true|false`; `alwaysLoad: true|false` per server. ~300 tools are therefore fine for context; the cost is a search step and name discoverability (server `instructions` field is used for this).
- **Bash tool** (https://code.claude.com/docs/en/env-vars): `BASH_DEFAULT_TIMEOUT_MS` default 120000 (2 min); `BASH_MAX_TIMEOUT_MS` default 600000 (10 min); `BASH_MAX_OUTPUT_LENGTH` default 30000 chars (max 150000). Background execution via `run_in_background` (and Monitor tool); in unattended sessions a ceiling >2 h also sets the background limit (v2.1.285+). `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1` removes it and stops auto-backgrounding of timed-out shell commands. Timed-out shell commands are auto-moved to background per that variable's description.
- **Known issues (GitHub, anthropics/claude-code)**:
  - Desktop app ignores `MCP_TOOL_TIMEOUT`/per-server timeout and cuts stdio calls at ~60 s: #63379 (closed, locked), #105524 (open, Code tab). The CLI honours the settings. This matches "calls >~120 s fail through Claude desktop".
  - Windows Desktop: calls hard-terminated at ~4 min, then the bridge wedges for ALL servers after repeated timeouts: #65643 (open).
  - Progress notifications not extending timeouts in Cowork: #58687 (closed, locked, no fix noted).
  - Desktop on Windows spawns the same stdio server twice on cold start: #90596 (open). Consistent with "each client spawns its own copy".
  - stdio: 3 s `server/discover` probe (2026-07-28 era) falls back to `initialize` on the same process if the server is slow to answer: #102942 (open, v2.1.302). A slow-to-start server can end up with "tools fetch failed".
  - stdio progress notification written just before the result is rejected as "unknown token" and logged as connection dropped: #102831 (open).
  - Stale stdio tool list across sessions: #92858; shutdown order (signals before stdin close): #108491; Desktop can't reconnect MCP servers: #108202.

**Implications for this system**
- Treat Claude desktop as a ~60 s (or ~4 min on Windows) client for MCP calls regardless of env vars; the CLI/Claude Code terminal honours `MCP_TOOL_TIMEOUT`/per-server `timeout`. Any call that can exceed ~60 s must use start->poll (job id), not a long blocking MCP call.
- For the CLI path: Bash default is 2 min, max 10 min; long operations should return a job id quickly and be polled by short commands, or run with `run_in_background`. Never rely on one blocking `hydra ...` call >2 min unless the agent passes a larger timeout.
- Gateway `initialize`/`server/discover`/first `tools/list` must answer in well under 3 s: serve a static cached catalog, never wait on backends (backends start lazily).
- Set `timeout` per server and `MCP_TOOL_TIMEOUT` explicitly (do not depend on defaults), and add `ENABLE_TOOL_SEARCH` awareness: 300 tools is OK with tool search, but give the gateway a good `instructions` string.
- Expect stdio drops to need manual `/mcp` Reconnect in interactive Claude Code; the CLI path avoids that class of failure.

## 2. MCP specification

Latest revision: **2026-07-28** (https://modelcontextprotocol.io/specification/latest ; changelog https://modelcontextprotocol.io/specification/2026-07-28/changelog). Clients/servers still commonly speak 2025-11-25 and earlier; Claude Code evidently probes for the new era (#102942). Check what your pinned Python `mcp` supports (PyPI shows 2.3.0 as latest; an issue tracker entry mentions 2026-07-28 support, https://github.com/modelcontextprotocol/python-sdk/issues/3767). The behaviour of your installed SDK version is UNCONFIRMED here.

- **Stateless base protocol**: `initialize` handshake and `Mcp-Session-Id` removed; every request carries protocol version + capabilities in `_meta`; `server/discover` added. **`ping` removed**. Servers no longer send JSON-RPC requests (sampling/elicitation via "multi round-trip" results). Roots/Sampling/Logging deprecated.
- **Streamable HTTP** (https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http): POST only; SSE stream per request; GET stream removed; **SSE resumability (`Last-Event-ID`) removed** ("a broken response stream loses the in-flight request; clients MUST re-issue as a new request"); MUST validate `Origin`; local servers SHOULD bind 127.0.0.1; SHOULD authenticate; closing the response stream = cancellation. In 2025-11-25 and earlier, sessions and `Last-Event-ID` resumability existed (older revisions).
- **stdio** (https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/stdio): server MUST NOT write anything but valid MCP messages to stdout; stderr free for logs; client shutdown = close stdin, wait, then force-kill (Windows: TerminateProcess or Job Objects); servers SHOULD exit on stdin EOF ("the only portable" graceful signal); on unexpected termination "client SHOULD restart it" and retry (protocol is stateless). Custom transports over sockets/pipes SHOULD reuse stdio newline-JSON framing.
- **Cancellation/timeouts/progress** (https://modelcontextprotocol.io/specification/2026-07-28/basic/patterns/cancellation , .../progress): implementations SHOULD time out all requests and SHOULD allow per-request config; MAY reset the timeout on progress but SHOULD keep a maximum timeout; stdio cancellation is `notifications/cancelled`; servers SHOULD stop work.
- **Tasks**: moved out of core into the official extension `io.modelcontextprotocol/tasks` (https://modelcontextprotocol.io/extensions/tasks/overview ; repo https://github.com/modelcontextprotocol/ext-tasks). Flow: server returns `CreateTaskResult` (taskId, ttlMs, pollIntervalMs), client polls `tasks/get`, `tasks/update` for input, `tasks/cancel` cooperative; server decides per request; durable task ID survives client disconnect. Stability: the ext-tasks repo lists schema 2026-07-28 as "Stable"; the older core tasks were "experimental". Client support varies (see https://modelcontextprotocol.io/extensions/client-matrix). Claude Code's docs mention "MCP tasks" running in background, but the extent of support is UNCONFIRMED. Python SDK implementation is an open issue (SEP-2663, python-sdk #2806).
- `tools/list` results gain `ttlMs`/`cacheScope` and servers SHOULD return a deterministic order.

**Implications for this system**
- stdout hygiene is a hard rule: every child and the gateway must keep logs on stderr; any Node/Python import that prints to stdout (banners, `print`) corrupts the channel. Add a startup self-test that the process emits only JSON lines.
- No protocol-level resumability: reconnect = re-issue. So long calls need application-level idempotency keys and job ids; do not expect the SDK/spec to resume a broken stream.
- Do not build on MCP `ping`/Last-Event-ID; use your own health tool/endpoint.
- Tasks extension is the spec-native analogue of start->poll, but client support is uncertain; implement start->poll as ordinary tools/CLI verbs and optionally map to Tasks later.
- Emit `ttlMs` and stable ordering in `tools/list` if/when the SDK supports it; helps caching.

## 3. Python `mcp` SDK failure modes (proxy-relevant)

Issues: https://github.com/modelcontextprotocol/python-sdk/issues/<n>. All below were open at fetch time unless stated.

- **anyio cancel scopes are task-bound**: `RuntimeError: Attempted to exit cancel scope in a different task` when clients are torn down out of LIFO order or from a different task: #577. A pool of stdio clients created/destroyed from different request handlers is the exact trigger. Also #3801 (`ClientSessionGroup.connect_to_server` leaves caller's task cancelled on unreachable HTTP server), #3522 (ExceptionGroup on cleanup after interrupted response), #3713 (ClientSessionGroup keeps old tools after disconnect).
- **Server receive loop dies on cancellation**: #2610, `RequestResponder.__exit__` leaks CancelledError on a cancelled request; subsequent requests hang -> clients report "Connection closed". Reproduced through 1.30.0 on Windows 11. Trigger: a client timeout sends `notifications/cancelled` while the server is busy. Directly relevant to a gateway that is itself a server and receives cancels from Claude.
- **stdio server won't exit/cancel**: #3808 (stdin read in an anyio worker thread is not cancellable while the client holds stdin open), #3674 (never exits on stdin EOF if client leaves stdout unread), #3551 (ignores Ctrl+C). Orphans are therefore possible; do not rely on graceful exit.
- **Windows**: #3411 (closed; CPython asyncio self-pipe bug on Proactor/Selector loops causes 100% CPU after suspend/resume, upstream cpython#156333), #3788 (subprocess spawned in a tool handler hangs when it inherits server stdin; fix `stdin=DEVNULL`), #3335 (stdio client closes after initialize when `PROGRAMDATA` missing from the env), #2433 (CRLF corruption from TextIOWrapper in `stdio_server`), #3089 (heavy sync init starves stdio even with threadpool, Windows GIL contention), #2233 (pywin32 dependency).
- **Process-tree termination**: the SDK's Windows stdio client creates a Job Object with `KILL_ON_JOB_CLOSE` and assigns the child to it (`src/mcp/os/win32/utilities.py`); it needs `pywin32` and falls back otherwise, and children spawned before assignment can escape. Verify `pywin32` is installed in the gateway venv.
- **Client hangs**: #3804 (requests hang forever after a server closes stdout but keeps running), #1577 (tool call hangs forever if the server/connection dies, streamable-http), #3747 (progress callbacks concurrent/out of order and after `call_tool` returns).
- **Recommended pattern** (derived, not an official doc): one long-lived asyncio task per backend that owns its `AsyncExitStack`, enters `stdio_client`+`ClientSession` and exits them in the same task; the rest of the system talks to it through a queue or futures. Restart = cancel/finish that task and spawn a new one. Do NOT enter contexts from request handlers or exit them from another task (#577). Every `call_tool` gets `read_timeout_seconds` and an outer `asyncio.timeout`. (FastMCP's own proxy uses a fresh isolated backend session per request, https://gofastmcp.com/servers/proxy .)

**Implications for this system**
- Make each backend a supervisor task that owns its session lifecycle (enter/exit in one task). Replace any "shared AsyncExitStack" teardown across handlers.
- Put a hard timeout around every backend call and treat "no response" as backend-down: mark it, kill its process tree, restart (don't await a cancelled session).
- Child spawn must use `stdin=DEVNULL` for any grandchildren; ensure `pywin32` present; assert one Job Object per backend and `KILL_ON_JOB_CLOSE`.
- Pin and test the SDK version (1.30.x vs 2.x); #2610 means gateway should not run long work in the same task as the receive loop and should tolerate `notifications/cancelled`.
- Set `PYTHONUNBUFFERED`/binary stdout, `PROGRAMDATA`, `SystemRoot` in child env explicitly (#3335).

## 4. Aggregator/gateway patterns

Public docs are thin on failure handling; only confirmed items listed.

- **FastMCP proxy/mount** (https://gofastmcp.com/servers/proxy): lazy (no backend contact at creation); each request gets an isolated backend session; `ProxyProvider` caches the backend's tools/resources/prompts, shared across sessions, `cache_ttl` default 300 s, refreshed on explicit list calls; provider failure `provider_error_strategy` default "warn" = log and skip the failed provider, so a dead backend does not block the client; no documented reconnect/timeouts.
- **mcp-proxy (sparfenyuk)** (https://github.com/sparfenyuk/mcp-proxy): named servers under `/servers/NAME/`; a failing named server is marked `failed` in `/status` while others continue; `--initialization-timeout` default unlimited; closing client stdin ends sessions; restart of crashed children and post-startup failure handling NOT documented (UNCONFIRMED). Reverse HTTP client: a 404 on an established session marks it lost and the failed request is not retried.
- **MetaMCP** (https://github.com/metatool-ai/metamcp): pre-allocates idle sessions per server (default 1) "to reduce cold start"; namespace-level enable/disable of servers and tools; health/restart/timeouts NOT documented in README (UNCONFIRMED).
- **Docker MCP Gateway** (https://docs.docker.com/ai/mcp-gateway/): starts a server container on demand when a tool is requested; profiles select servers; isolation; health/timeouts/caching not documented on that page (UNCONFIRMED; repo https://github.com/docker/mcp-gateway not read).
- **IBM ContextForge** (https://github.com/IBM/mcp-context-forge): README mentions health endpoints, Redis-backed federation/caching, retry/timeout policies for REST adapters, `mcpgateway.translate` stdio->SSE/HTTP wrapper; circuit breaker, reconnect and supervision NOT documented in README (UNCONFIRMED; deeper docs not read).
- **Generic patterns** (standard practice, not a single source): lazy start; per-backend supervisor with exponential backoff + jitter and a circuit breaker; cached/static catalog so list never blocks; bulkheads (per-backend concurrency limits and timeouts); aggregated health endpoint. Prior art for readiness: https://kubernetes.io/docs/concepts/configuration/liveness-readiness-startup-probes/ .

**Implications for this system**
- Persist the tool catalog (name, schema, owning backend) to disk at build/refresh time; `tools/list` and the CLI `hydra tools` read it with zero backend I/O, refreshed in background (FastMCP-style TTL cache plus "warn and skip" for failed backends).
- Per backend: state machine {stopped, starting, ready, degraded, failed(cooldown)}, backoff 1,2,4...60 s with full jitter, breaker opens after N consecutive failures, half-open probe via the backend's own ping/readiness tool.
- Bulkhead: per-backend semaphore + per-call deadline so the three Node daemons and slow `eights`-style audit calls cannot starve the rest.
- Expose `gateway_health` (already exists) as cheap, non-blocking, from cached state; also a CLI `hydra doctor`.

## 5. CLI over a local daemon

- **Precedents**: Docker CLI to dockerd, Bazel server, gopls (daemon mode), watchman, Gradle daemon (all "thin client + long-lived server, auto-start on first use"). I did not fetch primary docs for each (UNCONFIRMED per-project details); the pattern itself is well known.
- **Anthropic guidance**: best-practices guide says to prefer a CLI when the system has one ("usually the easiest way in") and MCP otherwise (https://code.claude.com/docs/en/best-practices , per search summary; also https://www.anthropic.com/engineering/claude-code-best-practices). Skills and MCP are positioned as complementary: skills teach how, MCP gives access (https://claude.com/blog/extending-claude-capabilities-with-skills-mcp-servers , https://code.claude.com/docs/en/features-overview). Context cost: with tool search MCP definitions are deferred, so the cost gap has narrowed (https://code.claude.com/docs/en/mcp). Code-execution-over-MCP trade-offs (token savings vs sandbox/ops overhead): https://www.anthropic.com/engineering/code-execution-with-mcp. I found no official Anthropic page that directly compares CLI vs MCP reliability.
- **Windows IPC**:
  - AF_UNIX on Windows exists since Windows 10 build 17063, stream sockets only, no fd passing/ancillary data, no `socketpair` (https://devblogs.microsoft.com/commandline/af_unix-comes-to-windows/). Python `socket` supports AF_UNIX on Windows; asyncio support is not guaranteed (UNCONFIRMED); Node `net` supports Unix-domain paths on Windows via named pipes semantics (UNCONFIRMED).
  - Named pipes: ACL-able (DACL to current user) and no TCP port exposure; native in Node (`net`, path `\\.\pipe\name`) and Python (`asyncio` Proactor, or `multiprocessing.connection` pipes).
  - Loopback HTTP (127.0.0.1): simplest, works with every language and `curl`; needs a token because any local process can connect; MCP spec itself says validate Origin, bind localhost, authenticate (Streamable HTTP page above).
- **Auto-start**: client tries connect; on failure acquire a lock (exclusive-create lockfile or named mutex), re-check, spawn the daemon detached (Windows `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP`, and note a parent-owned Job Object can kill it, so use `CREATE_BREAKAWAY_FROM_JOB` if allowed), wait for readiness with a bound. Stale-lock recovery: store pid + start time + port/pipe name; if the pid is dead or the endpoint does not answer a health check, take over. (Standard practice; no single primary source.)
- **Auth for loopback**: random token written to a per-user file with restrictive ACL (`icacls /inheritance:r /grant:r %USERNAME%:F`), sent as a bearer header; rotate on daemon start.
- **Long ops**: `hydra job start <verb> --idempotency-key K` returns `{job_id,status}` immediately; `hydra job wait <id> --timeout 90` (short blocking poll, under Bash 2 min default) / `hydra job get`; results persisted so they survive client death; same key returns the same job (mirrors MCP Tasks semantics: durable handle, polling, cooperative cancel).
- **Machine-readable**: `--json` on every verb, one JSON document on stdout, human text on stderr, stable exit codes (0 ok; 2 usage; 3 not-ready/retryable; 4 backend-down; 5 denied; 124 timeout-style; 75 EX_TEMPFAIL for retryable is a convention, UNCONFIRMED as a standard).

**Implications for this system**
- Anchor the CLI on one broker process per user (named pipe or loopback+token), owning all backend children; MCP gateway becomes a thin shim that calls the same broker (keeps MCP optional, removes duplicated Node daemon spawns per client).
- Return in <2 s always; anything slower becomes a job. Agent's Bash tool then never hits the 2 min default.
- `--json`, deterministic exit codes, and a `retryable` field let the agent loop without parsing prose.
- Choose loopback HTTP+token if multi-language tooling matters; named pipe if you want no port and ACL-only security. Avoid AF_UNIX+asyncio on Windows (support uncertain).
- Add a skill/CLAUDE.md snippet describing `hydra` verbs (skills teach usage, per Anthropic positioning).

## 6. SQLite across processes on Windows

Source: https://sqlite.org/wal.html , https://sqlite.org/lang_transaction.html , https://sqlite.org/lang_vacuum.html , https://github.com/WiseLibs/better-sqlite3/blob/master/docs/performance.md , .../docs/threads.md

- WAL: one writer at a time; readers don't block writer and vice versa; all processes must be on the same host (shared-memory wal-index); no network filesystems.
- **Checkpoint starvation**: a checkpoint cannot recycle WAL past the oldest active reader; overlapping readers => WAL grows without bound. Default auto-checkpoint ~1000 pages (~4 MB). better-sqlite3 docs suggest polling WAL size and running `wal_checkpoint(RESTART)` when it gets large.
- SQLITE_BUSY still occurs for writers, during recovery, or when the last connection is cleaning up. `BEGIN DEFERRED` that reads then writes is upgraded and can fail with SQLITE_BUSY; `BEGIN IMMEDIATE` takes the write lock up front (may fail with BUSY at start if another writer is active). Set `busy_timeout` (SQLite C API / `PRAGMA busy_timeout`; better-sqlite3 option `timeout`, default 5000 ms per its API docs, UNCONFIRMED here).
- **WAL-reset bug**: SQLite 3.7.0 through 3.51.2 can corrupt a database when two or more connections (threads or processes) write/checkpoint at the same instant; fixed in 3.51.3 and backports 3.44.6 and 3.50.7; developers call it rare but a 2026-08-24 update says a non-test-hook reproducer exists. Check the SQLite version bundled with each daemon's better-sqlite3 (`select sqlite_version()`).
- better-sqlite3 is synchronous and fast enough for the main thread for most apps; for slow queries use worker threads (pool example in threads.md). A long sync query blocks the Node event loop, so a daemon doing a full audit-chain verification will be unresponsive ("not_ready"/timeouts).
- **VACUUM INTO** writes a consistent snapshot to a new file (target must not exist or be empty); plain VACUUM can need up to 2x disk and fails if another connection blocks writes; `auto_vacuum` reclaims free pages without a full rebuild but does not compact partly filled pages. The fetched page did not cover `incremental` mode details (UNCONFIRMED here; see https://sqlite.org/pragma.html#pragma_auto_vacuum).
- Hash-chained append-only logs: incremental verification = persist a verified watermark (seq, hash) and only re-verify rows after it; periodic full verification offline from a `VACUUM INTO` snapshot or in a worker thread. Standard design (no external primary source cited).

**Implications for this system**
- Highest-leverage fix: ONE writer process per SQLite file (the broker/one daemon instance), clients go through it; N Node daemons spawned per client sharing one file is the root of busy/starvation/WAL-reset exposure.
- Until then: WAL + `busy_timeout` >= 10 s, short write transactions with `BEGIN IMMEDIATE`, never hold read transactions open during slow work, schedule `wal_checkpoint(TRUNCATE/RESTART)` from the single owner.
- Verify bundled SQLite >= 3.51.3 (or backport); upgrade better-sqlite3 otherwise.
- Convert audit verify to watermark/tail verification run in a worker thread; run full verify against a `VACUUM INTO` copy off the hot path.
- Add retention/compaction (archive old chain segments with a chained checkpoint hash, then `VACUUM INTO` and swap in a maintenance window).

## 7. Supervising user daemons on Windows without admin

- **Task Scheduler** supports "When a user logs on" and "when the task is registered" triggers (https://learn.microsoft.com/en-us/windows/win32/taskschd/task-scheduler-start-page). Per-user at-logon tasks for the current user generally do not need elevation (standard knowledge; not stated on the fetched page, UNCONFIRMED there). Settings like "restart on failure" and "do not start a new instance" exist in the task settings (UNCONFIRMED from fetched page).
- **WinSW**: "Most commands require Administrator privileges" (https://github.com/winsw/winsw). So services via WinSW/NSSM need admin (NSSM not fetched; same class). Not suitable if no admin.
- **pm2** has no native Windows startup; docs point to pm2-installer (https://pm2.keymetrics.io/docs/usage/startup/). Works as a process supervisor in the user session, but needs its own at-logon launcher.
- **Job Objects** (https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects): children inherit the job; `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` kills the whole tree when the last handle closes; nested jobs since Windows 8; breakaway flags matter if a child must escape. Python: `pywin32`'s `win32job` (what the MCP SDK uses), or `ctypes`; Node: no built-in; use a small native helper or have the Python/launcher process own the job (UNCONFIRMED for specific npm packages).
- **Parent death detection**: stdio EOF on stdin is the portable signal (MCP stdio page above); Python: read stdin in a thread/`add_reader` equivalent and exit on EOF (note SDK #3808/#3674 stdin-thread issues, so add your own watchdog); Node: `process.stdin.on('end'|'close')`, and for non-stdio daemons poll parent pid (`process.kill(ppid,0)`) or tie to a Job Object. Windows has no PDEATHSIG; the Job Object is the reliable mechanism.

**Implications for this system**
- Run the broker as an at-logon Task Scheduler task (current user, no admin), single-instance via lock/named mutex, restart-on-failure in task settings plus its own internal supervisor.
- The broker creates one Job Object (KILL_ON_JOB_CLOSE) holding all backend children; if the broker dies, everything dies, no orphans; clients then auto-start a fresh broker.
- Every child must also exit on stdin EOF and a ppid-watchdog (belt and braces; SDK EOF handling has bugs).
- Avoid WinSW/NSSM (admin) unless the operator is willing to elevate once.

## 8. Readiness vs liveness

Source: https://kubernetes.io/docs/concepts/configuration/liveness-readiness-startup-probes/ (startup probe protects slow starters; readiness controls traffic without restart; liveness restarts stuck processes and mis-tuned liveness can cascade). Retry/jitter: AWS Builders' Library "Timeouts, retries and backoff with jitter" (https://aws.amazon.com/builders-library/timeouts-retries-and-backoff-with-jitter/ , redirected; content not fetched, UNCONFIRMED here but widely cited: retry only idempotent calls, cap retries, use jittered exponential backoff).

- Guidance synthesis: a server with slow init should be **live immediately** (process answers), report **not ready** with a retryable code and a hint (`retry_after_ms`), and let clients/gateway wait a **bounded** time; do not block `initialize`/`tools/list` for readiness. Blocking forever hides failure; fail-fast without hints causes retry storms.
- MCP detail: Claude Code probes stdio servers with a ~3 s `server/discover` and falls back to `initialize` on the same process (#102942), so "ready" must not gate protocol handshake.

**Implications for this system**
- The daemon that returns `not_ready` should expose a cheap `ready` check (or `retry_after_ms` in the error) and the broker should block-with-bound (e.g. up to 10-15 s total) before surfacing a retryable error; CLI exit code 3 / `retryable:true` after the bound.
- Client retries: only for idempotent verbs or those with idempotency keys; exponential backoff base 0.5 s, factor 2, full jitter, cap 5-8 s, max 3-4 attempts; no retry on validation/denied errors.
- Separate probes: `live` (process answers, <50 ms), `ready` (backend initialised), `healthy` (recent successful calls); the circuit breaker uses `healthy`, restart policy uses `live`.
- Pre-warm the three Node daemons in the broker at logon so first-use "not_ready" never reaches an agent.

## Cross-cutting caveats
- Claude Code behaviours are fast-moving (version numbers above); re-check against installed version.
- Python SDK 2.x vs 1.x differences and the exact spec-era each speaks were not verified for the installed version.
- Many aggregator-failure behaviours are undocumented in READMEs; read source if a design decision depends on them.
