# Hydra gateway reliability investigation (gateway process + stdio client plumbing)

Date: 2026-11-15. Read-only. No repo file edited, no live process touched, no `mcp__hydra_gateway__*` tool called.

## Method and provenance

- Code read in the worktree `C:\AiAppDeployments\Hydra\.claude\worktrees\hydra-gateway-reliability-6ee52e` at `9b6241e`.
- The LIVE gateway runs from `C:\AiAppDeployments\Hydra`, branch `fix/plan-agents-location-and-invocable-skills` at `1285033`. `git diff 1285033 9b6241e -- mcp_servers hydra_core` is empty and the main checkout has no tracked modifications, so every line cited below is identical in the live code. (New sessions will run whatever branch that checkout is on at spawn time.)
- Runtime: Python 3.14.0, `mcp` 1.27.1, `anyio` 4.11.0, pywin32 present (Job Objects work).
- Registration: user-scope `~/.claude.json` -> `hydra_gateway` = `python -m mcp_servers.hydra_gateway`, cwd `C:/AiAppDeployments/Hydra`. `~/.claude/settings.json` sets `MCP_TOOL_TIMEOUT=1800000` (1800 s). No `MCP_TIMEOUT` override.
- `~/.hydra/backends.json`: 16 backends (5 node: pp_harness, pp_codex, pp_agy, eights, agentsmith; 10 python `mcp_servers.*`; 1 `uv run blender-mcp`). Values not inspected beyond command/args/env key names.
- Dynamic verification: I ran the worktree's unmodified gateway as a scratch subprocess with `HYDRA_BACKENDS` pointed at a scratch file containing fake backends, and drove it with a raw JSON-RPC host. Scripts: `C:\Users\robob\AppData\Local\Temp\claude\85c926522de1347b\f5d2e23a-80ce-4f72-bd61-b0be289bc61c\scratchpad\gwrepro\` (`host.py`, `fake_backend.py`, `t_a.py`, `t_b.py`, `t_b3.py`, `t_f.py`, `t_f2.py`, `t_h.py`; `gw_patched.py` is a scratch copy of server.py used to test the fix for GW-1). "REPRODUCED" below means observed in those runs.

## Findings

### GW-1 — Any host-side cancellation of an in-flight tool call kills the gateway's host connection
- Severity: **crit**. Confidence: high. **VERIFIED + REPRODUCED** (gateway side); host side INFERRED (see below).
- Evidence:
  - `mcp_servers/hydra_gateway/server.py:478` `except (asyncio.CancelledError, Exception)` in `AsyncBackendPool.call_tool` converts cancellation into a returned `{"status":"failed"}` dict; `:863` (meta-tool branch) and `:903` (backend branch) of `_call_tool` do the same.
  - `mcp/shared/session.py` `RequestResponder.cancel()` (called when the host sends `notifications/cancelled`, `session.py:403-406`) cancels the handler's scope, sets `_completed = True` and sends the "Request cancelled" error itself.
  - `mcp/server/lowlevel/server.py:773-783` expects the handler to raise the cancellation (`if message.cancelled: return`). Because the gateway handler swallows it and returns normally, control reaches `server.py:800` `await message.respond(response)` -> `session.py:129` `assert not self._completed, "Request already responded to"` -> `AssertionError` escapes `_handle_message` into the `server.run` task group (`server.py:673`) -> `server.run` raises -> gateway logs `Gateway server exiting` (`hydra_gateway/server.py:920-921`) and runs `pool.close()` (kills all backends).
  - Observed stderr: `AssertionError: Request already responded to` -> `Gateway server exiting: unhandled errors in a TaskGroup`.
  - After the crash the process does NOT exit: it sits in `stdio_server().__aexit__` waiting on the stdin reader thread. Observed 25 s alive, stdout open, zero backends, answering nothing. It exits (rc 1, `BrokenResourceError`) only when the host writes another line or closes stdin. This matches #117's "the gateway process was still running" while the host reported it disconnected.
  - Same result for a cancelled meta-tool (`gateway.discover`).
- Root cause: the handlers deliberately catch `CancelledError` (class docstring `server.py:106-110`, comment `:872-878`) on the theory that letting it escape crashes the gateway. With mcp 1.27 the opposite holds: swallowing a host-initiated cancel is what crashes it.
- Host side (INFERRED, not observed in Claude Code itself): the host sends `notifications/cancelled` when the user interrupts a running tool call, when a subagent/parallel batch is aborted, and when its own request timeout fires. The configured host timeout is 1800 s (`MCP_TOOL_TIMEOUT`), while the gateway's own cap for `generate`/`critique`/`plan`/`step`/`submit_*`/`launch`/`resume` is 3000 s (`server.py:320`) and hydra_control's submit cap is 1800 s (`mcp_servers/hydra_control/server.py:725`). So for every long tool the host always fires first, and that cancel takes the gateway down. This fits both #117 incidents: "`submit_host_result` ... once to the 1800 s kill" and "`pp_codex.critique` hung for 1800 s and its retry returned `Connection closed`, host reported hydra_gateway disconnected".
- Smallest fix: in all three places, handle `asyncio.CancelledError` separately and re-raise (in `pool.call_tool`: `task.cancel(); raise`, without tearing the backend down from inside the cancelled scope). Keep `except Exception`. Verified on a scratch copy (`gw_patched.py`): after a cancel the host gets "Request cancelled", `gateway.health` and the next backend call succeed, backend stays connected.
- Test that proves the property: in-process `Server` over anyio memory streams (or subprocess as in `t_b.py`) with a fake backend whose tool blocks; send `tools/call`, then `notifications/cancelled` for that id; assert a subsequent `gateway.health` request gets a result. Remove the re-raise and the test must fail with the AssertionError. Parametrise over a backend tool and a meta-tool.

### GW-2 — One call's timeout/error tears down the shared backend session; sibling in-flight calls are orphaned and hang to their own cap, then cascade
- Severity: **high**. Confidence: high. **VERIFIED + REPRODUCED**.
- Evidence: `server.py:468-471` (timeout) and `:478-480` (any exception) call `_teardown_backend(server)`, which pops the one shared `ClientSession` for that backend (`:131-140`). All concurrent calls to that backend share it (`:238-239`).
  - `t_f2.py`: call A (7 s of work, cap 20 s) and call B (hangs, cap 4 s) on the same backend. B times out at 4.7 s and the backend process is killed. A never receives "Connection closed"; it gets no answer until its own cap at 20.3 s.
  - Cascade: when orphan A's cap fires, its handler runs `_teardown_backend(server)` again, which now destroys the NEW, healthy session opened after B's failure (backend pid observed dead at t=20). Call C (25 s of work on the new session, cap 60 s) was thereby orphaned and failed at its 60 s cap instead of succeeding at ~31 s.
  - In production the orphan cap is 3000 s for critique/generate/step/submit and the host cap is 1800 s, so an orphaned sibling becomes a host timeout -> cancel -> GW-1.
- Root cause: teardown is keyed by server name, not by the session the failing call used; closing the exit stack from a task other than the one that opened it does not fail the session's pending requests; nothing wakes the siblings.
- Smallest fix: (a) capture the session object used by the call and tear down only if `self._sessions.get(server) is session` (stops the cascade); (b) track in-flight tasks per session and cancel/fail them with a structured `backend_reset` error at teardown; (c) on a per-call timeout prefer leaving the session up when other calls are in flight, tearing down only when the transport itself is dead. (a)+(b) are a few lines each.
- Test: fake backend with `sleep` and `slow` tools; start `slow` (cap 60), then `sleep` (cap 4); assert `slow` returns (success or a structured error) within a few seconds of the teardown, and that a later call on the reconnected session is not killed when an earlier call's cap expires.

### GW-3 — Gateway cap for `hydra.workflow.finalize` (and other slow hydra_control verbs) is 120 s; expiry kills the hydra_control backend and its CLI child
- Severity: **high**. Confidence: high. **VERIFIED** (computed from the live catalog with the worktree code).
- Evidence: `_CONTROL_PLANE_FINAL_SEGMENTS` (`server.py:341-344`) omits `finalize`. Computed caps: `finalize`, `repo.resolve`, `cockpit.audit`, `envelope.record` = 120 s; hydra_control's own finalize cap is 600 s (`hydra_control/server.py:726`), so the in-band `finalize_timeout` can never fire. At 120 s the gateway tears hydra_control down (`:470`), the Job Object kills the `python -m hydra_core.cli finalize` child mid-run (matches #117: "No finalize child process remained", `hydra_control` listed under `failed`), and per GW-2 any concurrent hydra_control call is orphaned.
- The comment at `server.py:331-336` also cites stale subprocess caps (STEP=300/SUBMIT=2700; actual defaults 900/1800).
- Smallest fix: add `finalize` to the long class (and decide a class for `audit`); better, derive the gateway cap for hydra_control verbs from the same env values hydra_control uses plus headroom, and make a per-call timeout not kill the backend (GW-2c).
- Test: extend `tests/test_gateway_timeout.py::test_control_plane_tools_get_long_timeout` with every verb in hydra_control's handler table that shells out, asserting gateway cap > that verb's subprocess cap.

### GW-4 — Timeout ordering is inverted and there is no keepalive: host 1800 s < gateway 3000 s
- Severity: **high** (it is the trigger for GW-1). Confidence: high for the config, medium for host behaviour. **VERIFIED** config; INFERRED host behaviour.
- Evidence: `~/.claude/settings.json` `MCP_TOOL_TIMEOUT=1800000`; `server.py:320-321` long cap 3000 s, hard max 3600 s; hydra_control submit 1800 s exactly equals the host limit. The gateway never sends progress notifications (no `send_progress_notification` anywhere in `mcp_servers/hydra_gateway`).
- Smallest fix: keep gateway caps strictly below the host limit (e.g. read `MCP_TOOL_TIMEOUT` from env when present, cap = host - 60 s), so the gateway returns a structured timeout instead of being cancelled. Independent of GW-1 but removes the common trigger.
- Test: unit test that `_resolve_tool_timeout` never exceeds the configured host limit minus headroom.

### GW-5 — No negative cache, backoff or health check for a dead backend; callers queue serially behind the connect lock
- Severity: med. Confidence: high. **VERIFIED + REPRODUCED**.
- Evidence: `_connect` (`server.py:230-267`) never consults `_failed`; every call re-spawns. Waiters hold the per-server lock in turn, each paying a full connect timeout. `t_f.py`: three concurrent calls to a backend that never answers `initialize` (timeout 3 s) returned at 5.0, 10.1, 15.1 s. With the 20 s default, six parallel calls take ~130 s for the last one. There is no proactive liveness probe: a backend that exited while idle is only discovered by the next call (non-idempotent tools then fail once with `McpError: Connection closed`; idempotent ones retry once, `:454-484`).
- The error text is `backend 'x' not connected` with no reason; reasons go only to stderr (`:190`, `:260`, the timeout message is an empty string).
- Smallest fix: record `(last_failure_time, reason)` per backend; waiters that queued behind a failed attempt return that failure instead of re-attempting; short cool-down (e.g. 5 s) before the next spawn; include the reason in the returned error and in `gateway.health` (this is #117 fix-plan item 4).
- Test: fake backend that never initialises; fire N concurrent calls; assert total wall time < 2 x connect timeout and that each error carries the reason.

### GW-6 — No operator-readable log; INFO never emitted
- Severity: med (diagnosability). Confidence: high. **VERIFIED**.
- Evidence: no `logging.basicConfig`/handler in `mcp_servers/hydra_gateway` (grep empty). `logger.info("Connected to backend")` (`:187`) is dropped; warnings and the `Gateway server exiting` critical go only to stderr via the last-resort handler, i.e. into the host's MCP log, not a file. `gateway.health` (`:508-514`) has no last-disconnect, reason, pid or uptime fields.
- Smallest fix: a rotating file handler under `~/.hydra/logs/gateway-<pid>.log` (stderr only, never stdout), log connect/teardown/timeout/cancel with backend and reason, and expose last failure per backend in `gateway.health`.
- Test: trigger a backend timeout; assert the log file contains the backend name and reason and `gateway.health` reports it.

### GW-7 — After `server.run` fails the process lingers as a deaf zombie
- Severity: med. Confidence: high. **VERIFIED + REPRODUCED** (part of GW-1 run).
- Evidence: `server.py:916-923` catches the failure and falls out of `server.run` but remains inside `async with stdio_server()`, whose exit waits for the stdin reader. Observed alive 25 s with stdout open.
- Smallest fix: after logging the critical, exit the process explicitly (`os._exit(1)` after `pool.close()`), so the host sees a clean EOF and can auto-restart.
- Test: force an exception out of `server.run` (monkeypatch); assert the subprocess exits within 5 s.

### GW-8 — `_extract_result` discards `isError`
- Severity: low (correctness, not availability). Confidence: high. **VERIFIED**.
- Evidence: `server.py:662-677` returns `{"status":"done"}` for any result with content, never reading `result.isError` (the dispatcher does, `hydra_core/dispatcher.py:749`). Backend tool errors reach the host as success.
- Fix: mirror the dispatcher's `isError is True` branch. Test: fake tool that raises; assert `status == "failed"`.

### GW-9 — JSON-RPC-level errors from a backend kill that backend
- Severity: med. Confidence: medium. **INFERRED** (code path read; not reproduced because the Python fake converts tool exceptions to `isError` results).
- Evidence: `session.call_tool` raises `McpError` for a JSON-RPC error response; `server.py:478-480` treats every exception as a transport failure and tears the backend down (then GW-2 applies). Whether the node daemons (pp, eights, agentsmith) answer bad arguments with a JSON-RPC error or an `isError` result was not checked.
- Fix: on `McpError` whose code is not `CONNECTION_CLOSED`, return the error without teardown. Test: raw fake backend that answers `tools/call` with a `-32602` error; assert the backend pid is unchanged on the next call.

### DISP-1 — MCPStdioDispatcher (used by the per-verb `hydra_core.cli` children, not by the gateway)
- Severity: med. Confidence: medium. **VERIFIED by reading only**.
- `hydra_core/dispatcher.py:553-554, 559-560`: `_run_lock` serialises every `call_mcp` on one dispatcher; one long call blocks all others, and `close_pooled_sessions` can block on it without bound (documented at `:976-986`).
- `:848-853` pooled path: a timeout drops the shared pooled session (same class of collateral as GW-2, but calls are already serialised so no sibling is in flight).
- `:185` long cap is 1800 s here vs 3000 s in the gateway; the two timeout tables are maintained separately.
- Each attended verb is a fresh process with a fresh dispatcher and cold daemons (#117 analysis); live process list confirms the gateway itself held only `hydra_control` and `pp_codex` children, i.e. pp_harness/eights used by the verbs are spawned outside the gateway.

## Answers to the seven questions

1. **Lifecycle.** Backends are lazy: spawned on first `call_tool`/`list_tools`/`gateway.discover` (`server.py:812-835`, `:457`). Nothing is spawned at startup or in `tools/list`. Catalog is static: `build_default_shed()` + `~/.hydra/gateway_schemas.json` overlay (`:817-821`). Measured: module import 0.52 s, catalog build 0.11 s, `initialize` answered 1.9 s after process spawn, `tools/list` ~0 s. Well inside the host's default startup timeout. Startup is not a cause. First call to a backend pays its cold start under a 20 s connect cap (`:53`); a slow cold start (e.g. eights audit verify) returns `not connected` with no automatic retry for non-idempotent tools.
2. **Backend failure.** Exit mid-call: clean `McpError: Connection closed`, next call respawns (REPRODUCED). Garbage on backend stdout: logged, session survives (REPRODUCED). Hang: capped, but see GW-2/GW-3. No restart policy, backoff or health check (GW-5). Isolation is per-backend for crashes, but one backend's hang can take the whole gateway down through GW-2 -> host timeout -> GW-1. Backend stderr is inherited from the gateway's stderr (SDK `errlog=sys.stderr`), so it cannot fill a gateway-owned pipe; it depends on the host draining stderr (not tested).
3. **Concurrency.** No global lock; requests run concurrently (SDK `tg.start_soon`). Only a per-backend connect lock (`:243`). No blocking I/O on the loop on the call path; meta-tools (`shed.search`, `tree.navigate`, `compute_tool_scope`) are synchronous but in-memory. An 8 MB backend response passed through in 2.2 s without stalling `gateway.health`. The one unhandled-exception path that ends the server is GW-1. I did not reproduce the anyio "exit a cancel scope" RuntimeError the comments describe; cross-task `stack.aclose()` worked (backend process was killed) but does not fail pending requests (GW-2).
4. **Timeouts/cancel.** Caps: 120 s default, 3000 s long class, 3600 s max, per-call `timeout_ms` may only raise (`:402-438`). Host cancel -> GW-1. >120 s on a short-class tool -> backend killed (GW-3). Orphans -> GW-2. No progress/keepalive (GW-4).
5. **Windows.** Children are created by the SDK in a Job Object with kill-on-close. REPRODUCED: host closes stdin with a call in flight -> gateway exits in 0.3 s, backend gone; gateway hard-killed (`TerminateProcess`) -> backend gone; no orphans. Live process list shows four gateway processes, each parented by a live host session (one per session, expected), none orphaned. Console encoding: not investigated beyond noting the SDK default is utf-8 strict.
6. **Stdout hygiene.** Nothing in the gateway or its import chain (`toolshed`, `router`, `squad_loader`, `tool_scope`, `strict_json`, `backends_env`, `dispatcher`) prints to stdout. `sys.stdout.write` in `hydra_control`, `hydra_memory`, `hydra_toolshed`, `_pack_shim` is confined to their bare-stdio fallback loops; `refresh_schemas.py` and `xenia_tickets/mint_for_tool.py` print but are CLIs. I did not audit third-party libraries loaded by `hydra_memory` (vector store) for stdout noise; a stray line there is survivable per the garbage test.
7. **Size.** 266 backend tools + 6 meta = 272 tools; `tools/list` payload ~98 KB. 96 of the 266 advertise an uninformative `{"type":"object"}`-style schema. The schema cache is dated Sep 20 and holds 307 entries vs 266 advertised (stale, harmless for stability). No evidence that size affects host stability.

## Test coverage

Covered (pure functions only): timeout class resolution and env overrides (`tests/test_gateway_timeout.py`), connect-timeout resolution (`tests/test_ra_fixes_2026_07.py:300-348`), idempotency classification (`tests/test_reliability_fixes.py:1087-1161`), catalog/schema overlay (`tests/test_gateway_catalog_complete.py`), dispatcher connect-retry and teardown-after-success with mocks (`tests/test_reliability_fixes.py:101-245, 974-1055`).

Not covered at all: `AsyncBackendPool.call_tool`/`_connect`/`_teardown_backend` behaviour against a real or fake stdio backend; host cancellation; concurrent calls on one backend; backend exit/hang/garbage; the `server.run` loop; process exit after failure. No test starts the gateway. `finalize` is absent from the long-timeout parametrisation.

## Could not determine

- Whether Claude Code sends `notifications/cancelled` in every abort case (user interrupt, subagent stop, 1800 s timeout, moving a call to the background). GW-1's gateway half is proven; the host half rests on standard MCP client behaviour and the 1800 s coincidence in #117.
- What `hydra.repo.resolve` waited on for 120 s (#117 item 1); out of this scope.
- Whether the node daemons return JSON-RPC errors for bad arguments (GW-9).
- Whether pid 10188 in #117 was the gateway for the affected session or another session's gateway (several run at once).
- The first #117 disconnect (no detail recorded).

## Ranked most likely causes of the "goes down, must reconnect" symptom

1. **GW-1** triggered by the host's 1800 s tool timeout or a user/subagent interrupt on any in-flight gateway call. Deterministic, reproduced, and leaves exactly the observed state (process alive, host disconnected).
2. **GW-2 + GW-4** feeding GW-1: a hung or slow backend call (pp_codex critique, hydra_control submit/step) orphans siblings to 3000 s, which outlives the 1800 s host limit, so the host cancels and the gateway dies.
3. **GW-3**: `finalize` and other 120 s-class hydra_control verbs get the backend killed; seen by the operator as hydra_control "failed" and lost work rather than a full disconnect, but concurrent hydra_control calls then hang (GW-2).
4. **GW-5**: a cold/slow backend producing serial `not connected` failures that look like the gateway being down.
5. Not supported by evidence: startup timeout, tools/list size, stdout corruption, orphaned children, event-loop blocking.
