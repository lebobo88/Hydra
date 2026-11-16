# Hydra gateway — runtime forensics (read-only)

Collected 2026-11-15 20:34–21:00 EST (UTC-5; host MCP logs are UTC). No process was signalled, no file modified, no `mcp__hydra_gateway__*` tool called. SQLite opened `mode=ro` + `query_only`. Only processes spawned and ended by me: timing probes (2 gateway cold starts, closed via stdin EOF, exit rc 0).

Working files (same scratchpad dir): `census.txt`, `survey_all.txt`, `detail.txt`, `eights_logs.txt`, `traces.txt`, scripts `survey.py detail.py recon.py pino.py e2.py db.py tr.py probe.py`.

---

# PART A — FACTS

## A1. Process census (20:34 EST)

82 python/node/claude/uv/codex processes, 5.66 GB working set total (Claude desktop app itself ~2.1 GB; a vitest pool of 15 node workers from an attended RLMplatform run ~1.2 GB).

Hydra-related processes:

| PID | Parent | Started (EST) | WS | CPU | Handles | Command |
|---|---|---|---|---|---|---|
| 33492 | 18020 claude CLI (RLMplatform session, started 11-14 08:23) | 11-15 13:57:11 | 16 MB | 3 s | 173 | `python -m mcp_servers.hydra_gateway` |
| 26220 | 33492 | 11-15 14:00:00 | 16 MB | 4 s | 159 | `python -m mcp_servers.hydra_control` |
| 32448 | 33492 | 11-15 14:32:01 | 38 MB | 2 s | 224 | `node pair-programmer/daemon/dist/index.js mcp-codex` |
| 41176 | 35488 claude CLI (Hydra session, started 11-14 10:40) | 11-15 15:57:11 | 28 MB | 3 s | 174 | `python -m mcp_servers.hydra_gateway` |
| 25808 | 41176 | 11-15 15:58:54 | 58 MB | 2 s | 224 | `node … mcp-codex` |
| 41904 | 41176 | 11-15 16:00:12 | 28 MB | 2 s | 160 | `python -m mcp_servers.hydra_control` |
| 40116 | 41960 claude-code 2.1.325 (desktop session) | 11-15 20:24:45 | 71 MB | 3 s | 187 | `python -m mcp_servers.hydra_gateway` (no children) |
| 17632 | 41004 claude-code 2.1.325 (this session) | 11-15 20:30:09 | 70 MB | 5 s | 185 | `python -m mcp_servers.hydra_gateway` (no children) |

Counts: gateway 4 (one per Claude session — 2 CLI, 2 desktop-hosted); hydra_control 2; pp `mcp-codex` 2; **pp_harness 0, eights 0, agentsmith 0, pp_agy 0, all other Python backends 0**. A 5th gateway (PID 39144, parent 31360) appeared at 20:39:15 — another investigator's probe, not the operator's.

- Backends are spawned lazily: two of four gateways have no children; the others only the backends they have used.
- Both long-lived gateways were created today *after* a connection drop (13:57:11 EST follows the RLMplatform drop at 18:54:45Z; 15:57:11 EST follows the Hydra drop at 20:36:57Z). The prior gateway processes and their children are gone — no leaked gateway trees.
- Orphans: none among Hydra daemons. One unrelated orphan chain: `sh.exe` 37740 (parent 29716 gone) → `C:\Python314\python.exe -` PID 35920, started 11-14 13:28:58, **100,137 s CPU** (measured 2.9 s CPU per 3 s wall, 1 thread — pegging one core for ~31 h). Origin unknown (a `python3 -` heredoc from a shell tool call). System CPU load at sample: 64 % (14 logical cores, 31.5 GB RAM, 7.2 GB free).
- Stale pid/lease files: `~/.eights/eights.pid` = 45340 (written 20:32:10, process not alive); `~/.agentsmith/bridge-singletons/eights-stdio-daemon.json` pid 32232 claimed 16:56:32Z (not alive).
- blender-mcp runs in 7 separate trees (2 from the desktop extension, 1 per Claude Code session ×4, 1 under Codex) — it is registered standalone *and* as a gateway backend.

## A2. Registration

| Where | Finding |
|---|---|
| `~/.claude.json` top-level `mcpServers` | `hydra_gateway`: stdio, `python -m mcp_servers.hydra_gateway`, cwd `C:/AiAppDeployments/Hydra`, env keys `[PYTHONPATH]`, no timeout fields. Also `atelier`, `blender` (`uv --directory C:\blender_mcp\mcp run blender-mcp`), `DaVinci Resolve`. |
| pp / eights / agentsmith standalone | **Not registered** anywhere in `~/.claude.json`. The Hydra project entry's `disabledMcpServers` still lists `agentsmith, eights, executive_suite, hydra_memory, pp_codex, pp_gemini, pp_harness, rlm_creative` (leftover names; no definitions). No duplicate daemon trees from registration. |
| `~/.claude/settings.json` env | `MCP_TOOL_TIMEOUT=1800000`, `HYDRA_ENFORCE_ROUTING=0`, `PP_ALLOW_AD_HOC=0`, `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`. No `MCP_TIMEOUT`, no `MAX_MCP_OUTPUT_TOKENS`. |
| Session env (observed) | `MCP_TOOL_TIMEOUT=1800000`, `MCP_CONNECTION_NONBLOCKING=true`, `MCP_SERVER_CONNECTION_BATCH_SIZE=8`. Host log: "Starting connection with timeout of 30000ms" (default connect timeout). |
| `Hydra/.claude/settings.json` | env only: `HYDRA_ENFORCE_ROUTING=1`, `HYDRA_DISABLE_CLAUDE_ENGINEER=1`. |
| `Hydra/.mcp.json`, `Hydra/plugins/hydra/.mcp.json` | absent. No hydra `.mcp.json` under `~/.claude/plugins`. |
| Claude desktop `claude_desktop_config.json` | keys `coworkUserFilesPath, preferences` only — no `mcpServers`. |
| `~/.hydra/backends.json` (16 backends) | pp_harness/pp_codex/pp_agy/eights/agentsmith = `node …/dist/index.js [mode]`; 10 Python `mcp_servers.*` with cwd Hydra; blender = `uv … run blender-mcp`. `HYDRA_OPERATOR_KEY`/`HYDRA_OPERATOR_ID` present (values not read). No per-backend timeout fields. |
| User-scope hooks (`~/.claude/settings.json`) | **35 hook commands**, none with a `timeout`. Per user prompt: 6 × `node pair-programmer/daemon/dist/index.js hook UserPromptSubmit …` (incl. `eights-recall-request`) + 1 pwsh. SessionStart: 6 node + 2 pwsh. Several PreToolUse/PostToolUse node hooks on `mcp__hydra_gateway__pp_*` calls. |

## A3. Host-side MCP logs (Claude Code)

Source: `%LOCALAPPDATA%\claude-cli-nodejs\Cache\<project>\mcp-logs-hydra-gateway\*.jsonl` — 248 project dirs, 943 files, 4.06 MB, 2026-05-22 → now. Files per month: May 12, Jun 187, Jul 421, Aug 184, Sep 75, Oct 8, Nov 56.

### Event totals (all time)

| Event | Count |
|---|---|
| `Calling MCP tool` | 4323 |
| completed successfully | 4243 |
| `Starting connection` / `Successfully connected` | 987 / 973 |
| `STDIO connection closed … (cleanly)` (session end) | 691 |
| **`STDIO connection dropped after Ns uptime`** | **17** |
| **`Connection error: Received a response for an unknown message ID … "Request cancelled"`** | **17** |
| `Tool … failed …: Connection closed` | 17 (+1 `-32000` in Jul) |
| Tool timed out after 1800s | 17 (12 `pp_codex__critique`, 5 `hydra_control__hydra.workflow.submit_host_result`) |
| Connect timeout (30 s limit) | 10 (Jun 5, Jul 4, Sep 1; none since 09-26) |
| `Server stderr: WARNING:root:Failed to validate request … 'server/discover'` | 17 (Oct–Nov; new host probe, followed by a normal connect) |
| Input-validation tool errors | 43 (Jun–Jul) |
| Python tracebacks / EPIPE / BrokenPipe on gateway stderr | **0** |

### By month

| Month | connects | calls | dropped | tool timeout (1800 s) | connect failed |
|---|---|---|---|---|---|
| 05 | 10 | 8 | 0 | 0 | 1 (ModuleNotFoundError) |
| 06 | 200 | 1785 | 0 | 0 | 5 |
| 07 | 431 | 584 | 0 | 0 | 4 |
| 08 | 182 | 217 | 0 | 0 | 0 |
| 09 | 80 | 870 | 0 | 0 | 1 |
| 10 | 19 | 423 | **8** | 6 | 0 |
| 11 | 65 | 436 | **9** | 11 | 0 |

All 17 drops fall between 2026-10-03 and 2026-11-15 (8 distinct days). Since Oct 1: 84 connection starts in 64 log files; 20 mid-session reconnects, of which 16 follow a drop, 3 follow a host-initiated close, 1 other.

### The drop signature (identical in all 17)

`…\C--AiAppDeployments-Hydra\mcp-logs-hydra-gateway\2026-11-14T15-40-12-953Z.jsonl`:

```
17:51:16.830Z  Calling MCP tool: pp_codex__critique
18:21:16.841Z  Tool 'pp_codex__critique' failed after 1800s: MCP server "hydra_gateway" tool "pp_codex__critique" timed out after 1800s
18:21:17.018Z  STDIO connection dropped after 56032s uptime
18:21:17.018Z  Connection error: Received a response for an unknown message ID: {"jsonrpc":"2.0","id":70,"error":{"code":0,"message":"Request cancelled"}}
18:21:29.509Z  Calling MCP tool: pp_codex__critique
18:21:30.197Z  Tool 'pp_codex__critique' failed after 0s: Connection closed
18:22:46.861Z  Starting connection with timeout of 30000ms
18:22:51.154Z  Successfully connected (transport: stdio) in 4295ms
18:23:32.688Z  Calling MCP tool: pp_codex__critique
18:23:50.864Z  Tool 'pp_codex__critique' completed successfully in 18s
```

- 15 of 17 drops: host timeout at exactly 1800 s (= `MCP_TOOL_TIMEOUT`), then 80–980 ms later the gateway's `"Request cancelled"` error reply, then the drop.
- 2 of 17 (croswell, 10-27T21:07:58Z and 10-29T03:39:45Z): same `Request cancelled` → drop, but only 11 m 35 s and 2 m 22 s into a `pp_codex__critique` with **no** host timeout — i.e. a cancellation that was not a timeout.
- Calls in flight at the drop: `pp_codex__critique` 13, `hydra_control__hydra.workflow.submit_host_result` 4.
- 2 further 1800 s timeouts of `submit_host_result` (warerender-gta 11-14T19:38:04Z; Hydra 11-15T16:08:12Z) produced **no** `Request cancelled` reply and **no** drop; the next call 42 s / 142 s later succeeded on the same connection.
- Drop uptimes: 4368 s … 93008 s (no fixed-interval pattern).
- After the drop the host does not reconnect by itself before the next call: the next tool call fails `Connection closed` in 0–1 s; a new connection appears 13 s – 3 h 47 m later.
- Drop timestamps: 10-03 15:50, 10-26 15:56, 10-27 01:42, 10-27 12:32, 10-27 21:07, 10-28 17:53, 10-28 20:45, 10-29 03:39, 11-07 12:00, 11-07 17:01, 11-11 16:31, 11-14 16:54, 11-14 20:35, 11-14 20:55, 11-15 18:21, 11-15 18:54, 11-15 20:36 (UTC).

### Call durations (4243 completed)

≥30 s: 615; ≥120 s: 241; ≥600 s: 21. Calls ≥120 s by tool: `pp_codex__critique` 136, `submit_host_result` 20, `workflow.step` 17, `workflow.plan` 11, `eights.hydra.envelope.record` 10, `pp_agy__critique` 8, `repo.resolve` 8, `workflow.finalize` 7. Slowest successes: `pp_codex__critique` 1527 s, 1127 s, 1109 s; `submit_host_result` 1112 s (11-15), 900 s ×2; `workflow.step` 645/625/621/611/610 s (11-14…11-16).

### Time to ready (host-measured connect)

| Month | n | min | median | p90 | max |
|---|---|---|---|---|---|
| 06 | 194 | 0.96 s | 5.7 s | 15.2 s | 24.6 s |
| 07 | 425 | 1.06 s | 5.9 s | 12.9 s | 29.7 s |
| 09 | 79 | 0.94 s | 5.8 s | 10.3 s | 22.8 s |
| 10 | 19 | 1.02 s | 3.5 s | 7.7 s | 12.7 s |
| 11 | 65 | 1.03 s | 6.9 s | 15.8 s | 20.1 s |

Limit is 30 s; 10 connects exceeded it (30.0–32.1 s), all Jun–Sep, several in pairs within 2 s of each other.

## A4. Gateway / Hydra-side logs

- **The gateway writes no log file.** `mcp_servers/hydra_gateway/server.py` only has `logging.getLogger(__name__)`; nothing under `~/.hydra` is a gateway log. Its stderr reaches the host log only as the 17 `server/discover` warnings. There is no gateway-side record of backend spawns, timeouts or breaker state.
- `~/.hydra`: 1364 empty workflow dirs (all June), `checkpoints.db` 451 MB, `episodic.db` 17.7 MB, `gateway_schemas.json` 162 KB **last written 2026-09-20**, 10 `backends.json` backups.
- `~/.hydra/eights-pending` (spool of eights writes that failed): 628 files, growing — 11-14: 16, 11-15: 24; Oct 19–21: 134. Reasons in the files: `"stub"` and `failed:{'status': 'not_ready', … 'reason': 'audit verification in progress', 'retry_after_ms': 2000 …`. `eights-pending-dead` 1368 files (Aug 22–Sep 24); archive-20260901 13,901 files.
- Real traces live in `C:\AiAppDeployments\Hydra\.hydra\<wf>\trace.jsonl`: **44,926 workflow dirs**, 43,864 traces, 89 MB (Sep alone 25,958; Nov 1,916). Last 21 days = 1,996 traces. Notable kinds: `rbac_violation` 1013, `budget.cost_unavailable` 605, `venom.cross_check_error` 324 (159 containing "unreachable", all 11-15), `judge.skeleton_fallback` 305, `hitl_resumed` with "spooled" 148, `plan_judge.error` timeout 8.
- No `Traceback`, `cancel scope`, `BrokenPipe`, `RuntimeError`, `event loop` or `breaker` strings in the last 21 days of traces.
- Engine-side timeout equals host timeout:
  `{"ts":"2026-11-15T16:05:50Z","kind":"plan_judge.error", … "error":"… pp critique call failed (vendor=codex, server=pp_codex): {'status':'failed','timeout':True,'error':\"tool 'critique' on 'pp_codex' timed out after 1800.0s\", … 'timeout_s': 1800.0}"}`
  and `2026-11-14T20:01:36Z plan_judge.error … 'call_tool raised after connect: MCPError: Connection closed', 'server': 'pp_codex'`.

## A5. Daemon state and logs

### TheEights (`~/.eights`)

| Item | Value |
|---|---|
| `state.db` | 879 MB, WAL (2.5 MB wal), 214,646 pages × 4096, freelist 0 |
| Rows | `events` 1,227,141; `memories` 18,515; `actors` 5,808; `hydra_envelopes` 2,076; `resources` 1,733; `hitl_queue` 1,149; `proposals` 759 |
| `events/` jsonl | 121 files, **758 MB** (May 422 MB, Jun 215 MB, Oct 46 MB, Nov 8 MB) |
| `logs/` | 142 files, 56.8 MB |
| Process RSS (`proc gauge`) | median 102 MB, max 129 MB |

Daemon log, 2026-10-18 → 11-16 (20 active days, `eights_logs.txt`):

| Metric | Value |
|---|---|
| `eights-daemon booting` | **4,147** (up to 430/day) |
| Boots that reached `audit readiness gate open` | 1,158 (28 %) |
| Boot → gate open | median 0.82 s, p90 1.57 s, p99 3.0 s, max 6.3 s |
| Boot → first incoming tool call | median 0.069 s, p90 0.19 s |
| `mcp tool refused — readiness gate closed` | **4,516** |
| `mcp tool call` (served) | 368 |
| Boots that served ≥1 call | 208 (5 %) |
| Boots that only refused | 3,557 (86 %) |
| Boot → last log line | median 0.13 s |
| `mcp tool slow` (3.5–4.3 s, `eights.memory.search`) | 22 |
| SQLITE_BUSY / uncaught exceptions / level ≥ 50 | 0 |

Refusals by tool: `memory.search` 2076 (trace_id `(prompt)`), `memory.add` 1012, `cells.classify` 411, `governance.budget.charge` 404, `hydra.envelope.record` 231, `audit.bom` 160, `audit.verify` 128, `hitl.list` 87. Representative lines (`eights-daemon-2026-11-16.log`, pid 45340): boot at t, `MCP tools registered tool_count 71` +48 ms, `stdio MCP transport active` +71 ms, `mcp tool refused — readiness gate closed tool=eights.governance.budget.charge status=not_ready` +159 ms.

### pair-programmer (`~/.pair-programmer`)

`state.db` 26.7 MB WAL (4.2 MB wal): `missability_checks` 21,253, `artifacts` 5,519, `runs` 3,760. Logs since 10-18: `pp_harness MCP server running on stdio` 1,606; `pp_codex …` 203; `eights-client: connected` 3,126; **`eights-client: TheEights unavailable, pp running standalone` 625** (reason `eights call timeout`); `codex returned non-zero` 30; `agy returned non-zero` 7; `reaped stale project lock at acquire` 78; `shutdown: child did not exit after SIGTERM grace; sending SIGKILL` 2; tool error `better_sqlite3.node was compiled against a different Node.js version` 5 (first 10-24); project-lock-held error 3.
During the hung `pp_codex__critique` windows (e.g. 11-15 17:51–18:21Z) the pp log has **no line at all** from the pp_codex process — no start, no codex exit, no error. pp_codex logs `shutdown … reason: stdin_end` only when the gateway dies (11-14 20:35:13Z).

### AgentSmith (`~/.agentsmith`)

`decisions.jsonl` 1.0 MB. Logs sparse (48 files, 200 KB; only 2 days in Nov). Since Aug: `N8: attest transport-degraded — retrying` 11; `degraded … eights.constitution.attest … not_ready … audit verification in progress` 8; `mcp-client connect failed … Connection closed` 6; `broken pipe — reconnecting` 3; `N8: constitution attested — real tools active` 12. On 11-15 attestation succeeded 1.0 s after the not_ready refusal.

### Hydra DBs

`checkpoints.db` 451 MB WAL: `writes` 146,100, `checkpoints` 16,936 (count(*) on `writes` 1.2 s). `episodic.db` 17.7 MB: 30,802 rows.

## A6. Timing probes

| Probe | Result |
|---|---|
| `python -c pass` | 0.15 s |
| `python -c "import mcp"` | 2.0 s |
| `python -c "import mcp_servers.hydra_gateway.server"` (cwd Hydra, ×3) | 0.65–0.73 s |
| `import mcp_servers.hydra_control.server` | 0.31 s |
| `python -m hydra_core.cli --help` | 3.5 s, 3.1 s |
| `node -e 0` | 0.08 s |
| Gateway cold start → `initialize` response | 1.9 s, 2.6 s |
| → `tools/list` response | +0.01 s (served from cache), warm 6–10 ms |
| `tools/list` | **272 tools, 92,167 bytes**, no pagination |
| Backends spawned by initialize + tools/list | **none** (checked 1 s and 3 s after) |
| Exit after stdin close | 0.18–0.24 s, rc 0, stderr empty |

Tools per prefix: pp_harness 76, eights 65, agentsmith 29, hydra_control 15, 9 each for executive_suite/marketbliss/rlm_creative/rlm_gaming/senate/xenia/xenia_tickets, hydra_memory 6, gateway meta 6, blender 4, xenia_kb 4, pp_agy 2, pp_codex 2. The live eights daemon registers 71 tools and agentsmith 30, versus 65 and 29 in the gateway's list.

## A7. Environment

- Python 3.14.0 (`C:\Python314\python.exe`); MCP SDK **mcp 1.27.1**, anyio 4.11.0, pydantic 2.12.3, langgraph 1.2.0 (user site-packages). Node v26.12.1. Claude Code 2.1.325 (desktop-bundled), desktop app 5.11492.5.
- Host negotiates protocol `2025-11-25`; gateway is flagged `protocolEra: legacy`.
- Defender real-time protection ON; exclusion lists unreadable without admin.
- Sleep/standby: one S3 resume (10-18 02:11Z); modern-standby 506/507 events only on 10-17/18 and 11-01…11-03. **None within hours of any of the 17 drops.** 10 boots in 30 days (last 11-11 15:16).

## A8. Not found / not available

- No gateway-side log of any kind (see A4).
- No Claude **desktop** MCP log for hydra_gateway (`%APPDATA%\Claude\logs` has only Blender/Context7/Windows-MCP logs, last written 08-20); `~/.claude/logs` absent; `~/.claude/debug` has one file from March.
- Defender exclusions (needs admin).
- Whether the `codex` child process was alive during the 30-minute hangs (no log line; process gone by the time of collection).
- Origin of the runaway `python -` PID 35920.

---

# PART B — INTERPRETATION

## Ranked hypotheses for "gateway goes down and must be reconnected"

**H1 — A cancelled request makes the gateway send a reply the host rejects, and the host tears down the connection. Confidence: high (~90 %).**
Every one of the 17 recorded drops is immediately preceded by `Received a response for an unknown message ID: {… "error":{"code":0,"message":"Request cancelled"}}`. The gateway process itself never crashes (no stderr trace, clean exit in probes); the host closes the pipe. Trigger is any cancellation: 15× the 1800 s host timeout, 2× an earlier cancel. The reply shape is what the Python MCP SDK (1.27.1) emits for a cancelled in-flight request; Claude Code 2.1.x treats it as a transport error. No drops before October despite far more calls in June–September suggests a host-version behaviour change (first seen 10-03), not a gateway regression — the log cannot prove that part. The two timeouts that did *not* drop are consistent: there the gateway never sent the cancel reply.

**H2 — Calls hang to the 1800 s ceiling instead of failing fast, which is what fires H1. Confidence: high that it happens; medium on where.**
`pp_codex__critique` is in flight in 13 of 17 drops; a retry of the same call seconds later finished in 18 s, so these are hangs, not long work. The pp_codex daemon logs nothing during the window, which points at either a silent `codex` CLI child or a stalled request/response inside the gateway→backend stdio hop; runtime evidence cannot separate them. The engine's own inner timeout for `pp_codex.critique` is also 1800.0 s (A4), so `submit_host_result` calls that wrap a critique race the host timeout and lose (4 drops).

**H3 — No auto-recovery makes each drop look like "it's down". Confidence: high.**
After a drop the next call fails `Connection closed` instantly and the connection stays closed until something reconnects (13 s to 3 h 47 m later). 16 of 20 mid-session reconnects since October are post-drop.

**H4 — Slow startup / connect timeout. Confidence: low for the current complaint.**
Isolated cold start is 2–2.6 s, but the host sees a 6–7 s median and 15 s p90 against a 30 s limit — the gap is startup contention (8 servers in parallel, 8+ hook processes, Defender). It caused 10 failed connects in Jun–Sep, none since 09-26.

**H5 — Sleep/resume, gateway crash, memory pressure, duplicate registration. Confidence: ruled out / very low.**
No standby event near any drop; zero tracebacks; gateways are 16–71 MB; pp/eights/agentsmith are not registered standalone.

## TheEights "unreachable / slow / heavy"

**E1 — Cold-start-per-call races the readiness gate. Confidence: high.**
Eights is not a resident daemon: 4,147 boots in 20 active days, none running at census time. Callers send their request ~70 ms after boot; the audit gate opens at ~820 ms; the call is refused `not_ready` and the process is gone in ~130 ms. 86 % of boots served nothing; 4,516 refusals vs 368 served calls. Downstream evidence matches: pp logged `TheEights unavailable` 625×, Hydra spooled 628 pending writes with the same `not_ready` reason, AgentSmith logged attest degradation. The `retry_after_ms: 2000` hint is evidently not honoured by callers.

**E2 — "Heavy" is process churn, not a slow database. Confidence: medium-high.**
Each boot is a ~100 MB node process that opens an 879 MB DB and verifies the audit chain (0.4–6 s). The largest single source is the `UserPromptSubmit eights-recall-request` hook (2,076 refused `memory.search` with trace `(prompt)`), i.e. one wasted eights boot per user prompt, alongside 6 other node hook spawns. Queries themselves are fast when served (22 slow calls, max 4.3 s). The 1.2 M-row `events` table and 758 MB of event jsonl are bloat but not shown to cause failures.

## Secondary observations (not causes of the drops)

- `gateway_schemas.json` is two months stale; the advertised tool list is missing 6 eights tools and 1 agentsmith tool.
- 272 tools / 92 KB in one `tools/list` is a large context and parse cost per session.
- `Hydra/.hydra` holds 44,926 workflow dirs; `checkpoints.db` is 451 MB and unpruned.
- pp `better_sqlite3` native-module mismatch errors (5) since 10-24 — Node v26 vs the build.
- PID 35920 has burned one core for 31 hours.
