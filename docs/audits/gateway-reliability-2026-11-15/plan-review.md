# Adversarial review: gateway-reliability-plan.md

Reviewed 2026-11-15. Read-only. Inputs: the draft plan, all six reports in
`docs/audits/gateway-reliability-2026-11-15/`, `docs/plans/hydra-unified-orchestration.html`, and direct checks of:
`mcp_servers/hydra_gateway/server.py`, installed `mcp` 1.27.1 (`shared/session.py`, `server/lowlevel/server.py`),
pair-programmer `daemon/src/mcp/cli-runner.ts` + `util/shutdown.ts`, `%APPDATA%\npm\node_modules\@openai\codex\bin\codex.js`,
the host MCP logs (`%LOCALAPPDATA%\claude-cli-nodejs\Cache\*\mcp-logs-hydra-gateway\*.jsonl`, 17,764 lines),
`~/.pair-programmer/logs/pp-daemon-2026-1[01]-*.log`, and one Hydra workflow trace. No process touched, no gateway tool called.

Severity: **must-fix** = the plan is wrong or will not deliver its exit criteria as written; **should-fix**; **note**.

Short version of what breaks:
- The root cause the plan names for the hang (PP-1) is not supported by pair-programmer's own logs (issue 1).
- Fix 1.1 as written removes the forced backend recycle that today makes the retry succeed (issue 3).
- Fix 1.2's "run codex without the .cmd shim" does not give a killable direct child (issue 5).
- Fix 1.8 before 2.1/2.2 multiplies ledger junk and watcher processes (issue 7).
- The broker changes "one set per session" to "one per machine" without saying so, and has unaddressed
  lifecycle, identity and stale-code problems (issues 12 to 16).

---

## A. Causal chain (§1.1)

### 1. must-fix — "The critique hang is PP-1" is asserted as fact; production logs point elsewhere
Plan §1.1 step 1: "The critique hang is PP-1: `codex` resolves to the npm shim `codex.cmd`, pair-programmer's timeout
kills only `cmd.exe`, and the daemon keeps waiting on pipes the real process holds."

What the reports say: inv-pp-smith PP-1 is "VERIFIED (mechanism reproduced; the specific #77 incident was not captured,
so attribution to it is INFERRED)". inv-runtime H2: "the pp_codex daemon logs nothing during the window, which points at
either a silent `codex` CLI child or a stalled request/response inside the gateway→backend stdio hop; runtime evidence
cannot separate them."

What I checked. PP-1 has a log signature. While a CLI child's execa promise is unsettled its entry stays in
`ACTIVE_CHILDREN` (`cli-runner.ts:318-327`), so at daemon shutdown `abortAllInFlightChildren` logs
`shutdown: drain pass — aborting in-flight CLI children` (`:372-373`), and with an unkillable grandchild it ends in
`shutdown: unconfirmed child(ren) remain after cap — retaining ALL locks` (`shutdown.ts:137-140`).
In `pp-daemon-2026-10-*/11-*.log`:
- 7 `pp_codex` daemons lived 1800 s ±31 s and then logged a clean `stdin_end` shutdown (these are the engine-path
  per-call daemons hitting the dispatcher's 1800 s cap): started 11-08T19:30:17, 11-14T15:52:39, 11-14T16:05:55,
  11-14T18:48:36, 11-15T14:12:21, 11-15T14:48:51, 11-15T15:35:50 (UTC).
- 6 of the 7 logged **no drain pass**: no CLI child was in flight when the daemon was told to stop after 30 minutes.
- The 1 that had a child in flight (pid 34616) confirmed it terminated in the same second.
- `retaining ALL locks` occurs 0 times in those files.
So in 6 of 7 observed 30-minute critique hangs the daemon was not waiting on a codex process at all. Either the request
never reached the handler, the handler was stuck before spawn or after the child exited, or the response was written
and never read by the Python client (research §3 lists python-sdk #3804 for that class). PP-1 is a real defect; it is
not shown to be the cause of these hangs.

Limits of this check: it covers engine-path daemons (identified by the exact 1800 s lifetime). Gateway-held `pp_codex`
children mostly have no shutdown line at all (46 never logged one), so they neither confirm nor refute.

Change to the plan:
- §1.1 step 1: replace the PP-1 sentence with "Cause of the hang not established. PP-1 is a verified defect on this
  path; pair-programmer logs show no in-flight codex child in 6 of 7 sampled 1800 s hangs."
- Add Phase 1 item 1.0 (before 1.2): "pair-programmer: log one line at `critique`/`generate` request receipt, at CLI
  spawn (pid), at CLI exit, and at response write. Hydra: gateway and dispatcher log request sent / response received
  per backend call (this is 1.6 for the gateway; add the dispatcher). Reproduce one hang and name the stalled hop."
- Exit criteria: add "the stalled hop in a 30-minute critique hang is identified from logs".

### 2. must-fix — step 3 misdescribes the mechanism, and the exit criterion built on it cannot be met
Plan: "The SDK has already answered the cancelled request, so the second reply is for an unknown id (GW-1...)".
- inv-gateway GW-1: the second reply is never sent. `respond()` hits `assert not self._completed` and the
  `AssertionError` ends `server.run`. Confirmed in `shared/session.py` (`cancel()` sets `_completed = True` and sends
  `{"code":0,"message":"Request cancelled"}`; `respond()` asserts).
- inv-runtime: the "unknown message ID" line in the host log is that first, SDK-generated reply
  (`{"id":70,"error":{"code":0,"message":"Request cancelled"}}`), arriving after the host already gave up on the id.
Consequence: after fix 1.1 the SDK still sends that reply on every cancel (inv-gateway's patched run: "the host gets
'Request cancelled'"). The host will keep logging `STDIO connection dropped after Ns uptime` + `Connection error:
Received a response for an unknown message ID`. Exit criterion "zero 'unknown message ID' drops over one week" will fail
even if the fix works.

Supporting observation from the host logs: in all 17 cases the "dropped" line and the "Connection error" line share a
millisecond, and the next call fails `Connection closed` 0.2 to 0.7 s after it is sent rather than instantly
(e.g. 18:21:29.509 call, 18:21:30.197 failure). That fits GW-7 (zombie exits when the host next writes to stdin) and
suggests the "dropped" line is the host's error-handler log, not the close itself. This is inference from timing; I did
not read host source.

Change: rewrite step 3 as "The SDK answers the cancel itself. The gateway handler swallows the cancellation and
returns; the SDK's second `respond()` asserts, `server.run` ends, backends are closed, and the process lingers until the
host next writes to it (GW-1, GW-7)." Replace the exit criterion with "after a host timeout or interrupt, the next call
on the same connection succeeds; zero `Connection closed` tool failures over one week".

### 3. must-fix — fix 1.1 as written can turn "drop, reconnect, retry succeeds" into repeated 30-minute hangs
inv-gateway's fix text: "`task.cancel(); raise`, without tearing the backend down". Checked:
- The Python client does not forward a cancel to the backend when a `call_tool` is cancelled locally
  (`shared/session.py send_request` has no `CancelledNotification` send; the only reference is the receive side, `:403`).
- pair-programmer ignores cancels anyway (PP-2: no `extra.signal`, no abort).
So after 1.1 the backend session stays up with the abandoned request still pending. Today the retry works in 18 s
because the crash forces a fresh `pp_codex`. If the backend is the wedged party (issue 1), the retry goes to the same
wedged daemon and waits another 1800 s. If the backend is healthy but slow, the retry starts a second codex in the same
cwd (PP-2's stated consequence).

Change 1.1 to: "Re-raise `CancelledError` at `server.py:478`, `:863`, `:903`. On a host cancel of a backend call,
recycle that backend session from a task outside the cancelled scope when no sibling call is in flight on it;
otherwise mark it for recycle when the siblings finish. Exit the process when `server.run` ends." Test: fake backend
whose first call never answers; cancel; assert the next call to that backend succeeds within the connect timeout and
runs on a new backend pid.

### 4. should-fix — the numbers in step 1 and the "one chain explains every drop" claim
- Plan: "In 13 of 17 drops the call was `pp_codex.critique`; in 4 it was `submit_host_result`." This copies
  inv-runtime, whose own figures do not add up (17 timeouts = 12 critique + 5 submit; 2 submit timeouts did not drop;
  2 drops were not timeouts). Recount from the logs: **14 critique, 3 submit**. Submit drops: CineOS 10-03T15:50:46Z,
  croswell 10-26T15:56:35Z, strata-notes 11-07T12:00:07Z.
- Plan title "one causal chain explains every recorded drop" and step 2 "Nothing in the chain times out before the
  host does". 2 of 17 drops (croswell 10-27T21:07:58Z, 10-29T03:39:45Z) had no timeout (11 m 35 s and 2 m 22 s into a
  critique). They are cancels from an interrupt or abort. Steps 3 and 4 cover them; steps 1 and 2 do not.
- On 10-28 and 10-29 two critiques were in flight on the same connection at the drop, so one cancel killed a second
  live call. Worth stating: this is the GW-1 blast radius.

Change: "14 critique, 3 submit. 15 followed the 1800 s host timeout; 2 followed a cancel with no timeout."

### 5. Answers to the four causal questions asked
(a) **What hung in the submit drops, and is it fixed?** For strata-notes 11-07 the trace
`C:\AiAppDeployments\Hydra\.hydra\915c5bbb-9b6e-48ee-8c92-202669ac56dd\trace.jsonl` shows `ingest.plan_drafted` at
11:30:10Z, then nothing until the retry at 12:04:39Z, which reached `plan_judge.artifact_rerendered` and completed in
30 s. The first call sat in the plan-judge critique (engine path, `pp_codex.critique`, 1800 s cap per inv-cli-parity
§2). So it is the same unexplained critique hang, reached through the dispatcher, not the gateway. No plan item is
known to fix it (issue 1). The other two submit drops were not traced. The retry also minted a second PLAN envelope
(`461cbfb0…` then `31993a1e…`), which is the #108 duplicate; see issue 10.
(b) **The 2 cancels with no timeout:** covered by 1.1 if GW-1 is the whole mechanism, subject to issue 3. Not covered
by 1.4.
(c) **Two 1800 s timeouts that did not drop:** consistent with GW-1. Both are `submit_host_result`, whose
`hydra_control` cap is 1800 s, equal to the host limit. Hydra 11-15T16:08:12Z shows the host timeout, no cancel reply,
no unknown-id line, and the next call 142 s later succeeding on the same connection. The simplest reading is that
`hydra_control` returned its in-band timeout at the same moment, the request completed, and there was nothing to
cancel. That is a coin-flip between two equal timers (3 lost, 2 won). It is also direct evidence that ordering the
timeouts (1.4) prevents the timeout-triggered drops by itself.
(d) **Is the gateway re-raise right given python-sdk #2610?** For installed 1.27.1, yes on the main path:
`server/lowlevel/server.py:773-783` catches the cancellation inside `_handle_request` and returns when
`message.cancelled`, so nothing escapes `with responder`. Residual exposure: `RequestResponder.__exit__` discards the
return value of `self._cancel_scope.__exit__()` (`shared/session.py`), so a cancel that lands outside the handler's
`try` (during `message.respond()`) propagates out of the `with` block. Whether that can end the receive loop on
Windows is what #2610 claims and I did not reproduce it. Also unproven: the host half. inv-gateway tested with its own
raw JSON-RPC host, not Claude Code.
What settles it: (i) run real Claude Code against a scratch gateway registration with a per-server `"timeout": 5000`
and a fake backend that sleeps, unpatched then patched, and check the next call; (ii) parametrise the GW-1 test over
cancel timing (during backend wait, during a meta-tool, immediately after the backend answered); (iii) pin `mcp` to the
tested version in the gateway's requirements. Add all three to 1.1.

---

## B. Phase 1 fix soundness

### 6. must-fix — 1.2 "run `codex` without the `.cmd` shim" does not work as worded
- `codex.cmd` runs `node …\@openai\codex\bin\codex.js`, and `codex.js:247-248` does
  `spawn(binaryPath, …, { stdio: "inherit" })` on Windows (`execve` is used only off Windows). Skipping the `.cmd` and
  running `node codex.js` still leaves `codex.exe` as a grandchild holding the same pipes. Only spawning the vendored
  `codex.exe` directly gives a killable direct child.
- execa 9 and Node have no Job Object API and no tree kill on Windows. "Place each child in a kill-on-close Job
  Object" needs a native addon or a helper launcher. That is not a small fix.
- The plan says "Not a pid-based tree sweep (withdrawn before for pid-reuse risk)". `cli-runner.ts:287-303` gives three
  reasons for the withdrawal: a backstop timer that overflowed Node's 32-bit timer limit, a registry guard that could
  not detect pid reuse, and a `forceKillAfterDelay: false` contract violation. The plan also silently rejects
  inv-pp-smith's recommended fix (a guarded `taskkill /PID <pid> /T /F`) without saying so.
- The pid-reuse hazard is specific to killing after the shim is dead. `taskkill /T` issued while the direct child is
  still alive and its exit not yet observed walks a live tree. The old sweep ran after execa had already killed the
  shim.
- Missing from 1.2: the tool call must settle at the deadline whether or not the kill worked (race the execa promise
  against a wall-clock timer and destroy the child's stdio). That is the part that removes the hang.
- Behaviour change not mentioned: `DEFAULT_CLI_TIMEOUT_MS` is 5 min × 2 attempts (PP-2), yet the host logs record
  successful critiques of 1527 s, 1127 s and 1109 s, and 21 calls over 600 s. Either callers pass a larger
  `timeout_ms` or the ineffective timeout is what lets long critiques finish. Check before enabling a real kill, or
  working critiques start failing.
- `tdd-gate.ts:552-565` has the same exposure for `npx`/`npm` (PP-1) and is not in the plan.

Change 1.2 to: "(a) Resolve `codex` to the vendored `codex.exe` and spawn it directly. (b) Own deadline in
`runCliWithRetry` that fires before execa's: `taskkill /PID /T /F` while the direct child has not exited, then destroy
its stdio and settle with the existing `timeout` status. (c) Record the timeout values callers pass today and set the
critique default accordingly before (b) ships. (d) Same wrapper for `tdd-gate`. No Job Object." Move it out of "small".

### 7. must-fix — 1.8 before 2.1 and 2.2 makes TheEights worse on growth and load
Today 86% of boots refuse one call and exit having done nothing (inv-runtime A5: 3,557 of 4,147). With a bounded wait
every one of them completes bootstrap. Per inv-eights that means, per boot: about 75 junk audit events (F3), a
0.66 to 1.4 s prompt re-scan (F2), and `startBackgroundWork()` with all watchers (F5: "Every process that passes the
gate starts everything"). Boots reaching the gate go from 28% to about 100%, so junk ledger writes rise roughly 3.5×
and every short-lived hook process starts a watcher set.
Change: move 2.1 (gate first) and 2.2 (`eights.system` actor) into Phase 1 ahead of 1.8, and add the F5 stop-gap
"`EIGHTS_DISABLE_WATCHERS=1` for every spawner except the gateway's eights backend" to Phase 1. inv-eights rates F5
high and the plan defers it entirely to Phase 3.4.

### 8. should-fix — 1.8 wait bound versus client timeouts; failed gate
- inv-eights proposes a wait of "about 10-15 s". Client budgets are shorter: pair-programmer probe 3 s and call 8 s
  with a 3-failure breaker and sticky `unavailable` (PP-3), AgentSmith 10 s (AS-5), Hydra's gate-only resume 8 s
  (inv-cli-parity §2). A server that waits longer than the client turns a structured `not_ready` into a client
  timeout, which trips breakers. Gate-open is p50 0.82 s, p99 3.0 s, max 6.3 s.
- No deadlock found: the handler awaits a promise resolved by the bootstrap on the same loop, and bootstrap does not
  call tools. But the plan text omits inv-eights' second half: return `failed` immediately when the gate has failed.
  Without it a broken chain looks like slowness.
Change 1.8 to: "Wait for a pending gate up to 2.5 s (below the smallest client call timeout), then `not_ready` with the
hint; return `failed` at once when verification failed; `eights.health` stays ungated." Then 1.9 shrinks to: Hydra maps
`not_ready` to its own status instead of `eights_unreachable`, and pair-programmer's `unavailable` state expires
(PP-3). Retry loops in three repos are not needed once the server waits.

### 9. should-fix — 1.5 is not small, and half of it should wait for the broker
- MCP Python client semantics: one `ClientSession` multiplexes concurrent requests by id. A per-call timeout or cancel
  only pops that request's response stream (`send_request` `finally`); a late answer is dropped harmlessly. So
  "tear down only on transport failure" is safe for the session.
- It is not safe for the backend: the abandoned call keeps running there (no cancel forwarded, PP-2). The rule has to
  be "recycle when no sibling is in flight", same as issue 3.
- "Fail in-flight siblings at once" needs a per-session registry of in-flight tasks. Closing the exit stack from
  another task does not fail pending requests (GW-2), and research §3 (#577) warns against exiting `stdio_client`
  contexts from a task other than the one that entered them. Doing this properly is the owner-task-per-backend
  restructure, which the Phase 3 broker replaces.
Change: split 1.5. Phase 1 keeps (a) the session-identity guard (`self._sessions.get(server) is session`) and (b) a
per-session in-flight set cancelled with a `backend_reset` error at teardown. Drop "tear down only on transport
failure"; use "recycle when idle". Owner-task supervision moves to Phase 3.2.

### 10. should-fix — 1.4 needs numbers, and is the cheapest fix for 15 of 17 drops
Issue 5(c) shows that an inner layer answering first avoids the drop. Lowering the gateway long cap from 3000 s to
below the host limit and `hydra_control` submit from 1800 s is a constants change. The plan lists 1.4 fourth and gives
no values. Also: research §1 says a per-server `timeout` is a hard wall clock "that progress notifications do NOT
extend", and anthropics/claude-code #102831 says a stdio progress notification written just before the result is
"rejected as 'unknown token' and logged as connection dropped". §3.4 "Progress is emitted while a call is alive" can
therefore add a failure mode and does not extend `MCP_TOOL_TIMEOUT`.
Change: put 1.4 first, with values, e.g. host 1800 > gateway 1740 > `hydra_control` submit 1680 > engine critique 1500.
Cut "Progress is emitted" from §3.4 or mark it "not before the host's handling is confirmed".

### 11. should-fix — other Phase 1 items
- 1.6 (gateway log) must ship first or with 1.1. There is no gateway-side record today, and issue 1 cannot be closed
  without one. Add the dispatcher.
- 1.3: say where. inv-cli-parity's fix is the default in `hydra_core.proc.run_text`, which covers every in-process git
  call. Plan §1.1 says "Reproduced: 49 s hang". 49 s is when the test's stdin holder closed (inv-cli-parity F-01); in
  production the hang lasts until the gateway's 120 s cap tears the backend down.
- 1.8 "rebuild `dist`": inv-eights F12 notes the TheEights working tree has an uncommitted `better-sqlite3` bump
  (`^11.5.0` to `^13.0.3`) on branch `fable-audit-2`. Say which commit is built, and add the build id to `eights.health`.
- 1.10: AgentSmith's exit path must also release the singleton lease (AS-1: lease file names dead pid 32232).
- 1.11 overlaps approved W0.3 ("In-repo catalogs derived from server registries; advertise the seven missing tools").
  Reference W0.3 so the staleness check is not built twice.

---

## C. Omissions and ordering

### 12. must-fix — high-rated findings with no item, or fixed only in Phase 4
| Finding | Rated | In the plan |
|---|---|---|
| PP-2: no cancellation, no progress, retry default doubles the worst case | high | none. §3.4 mentions progress; no phase item |
| C-02: `step` runs `pytest tests/` up to 600 s before saving the cursor; 38 of 70 baseline entries are timeout markers, four on 11-15 | high | listed in §1.1 as contributing; fixed only in 4.3, behind the broker. inv-cli-parity's fix (save cursor first, detach the baseline with the existing #70 smoke-job machinery) needs neither broker nor jobs |
| C-08: CLI project root defaults to cwd; a `step` from a session worktree can open a duplicate stage | high | 4.5, after 4.1 and 4.3 put more traffic on the CLI. Must precede any CLI-driven verb |
| C-05: killed `step` opens a second pp run | high | 4.4, after jobs (4.3). Jobs make kills and retries routine; 4.4 must come first |
| #108: retried PLAN submit mints a second PLAN envelope (seen in the strata trace, issue 5a) | "Partial" in inv-cli-parity §6 | absent from 4.4, which lists only `step`, `record_attempt`, `finalize` |
| E-5/F5: every process runs every watcher | high | only via Phase 3.4 (issue 7) |
| E-4: `ollama pull nomic-embed-text` | high | 2.3 covers code; the one-line operator action is not listed |
| AS-4: 2 s connect budget to pp; AS-5: 10 s hard-coded call timeout | medium | none |
| inv-runtime A4: `~/.hydra/eights-pending` has 628 spooled writes and is growing | fact | no drain step after 1.8 (`hydra eights-drain` exists) |

Change: add PP-2(a)+(c) to Phase 1 next to 1.2; move C-02 and C-08 to Phase 1; reorder Phase 4 to 4.5, 4.4 (plus the
PLAN case), then 4.3; add a "drain the spool" step to the Phase 1 exit.

### 13. should-fix — "Phase 1 is independent small fixes" is false
Dependencies: 1.1 ↔ 1.5 (recycle rule), 1.4 → needs the host limit as input, 1.8 → 2.1, 2.2, 1.9 → 1.8, 1.2 → 1.0
diagnosis, everything → 1.6 for verification. 1.2, 1.5 and 1.9 are multi-day changes in code that three cross-vendor
reviews have already rejected once (1.2). Suggested order: 1.4, 1.6, 1.1 (with recycle), 1.3, 1.7, 1.0; then
2.1, 2.2, watcher stop-gap, 1.8; then 1.2, 1.5a/b, 1.10, 1.11.

### 14. should-fix — §1.4 "ruled out" overstates two items
- "duplicate MCP registration": true for pp/eights/agentsmith. inv-runtime A1: blender-mcp "is registered standalone
  *and* as a gateway backend" and runs in 7 trees.
- "SQLite lock contention (no `SQLITE_BUSY` in the logs)": inv-eights lists this under "Could not determine" because
  tool errors are logged without messages (F13), and reports 5,418 `This database connection is busy executing a query`
  errors (F7). inv-pp-smith calls it "a latent cost, not a demonstrated current failure". Say "not observed", not
  "ruled out".

---

## D. Design risks in §3

### 15. must-fix — "one broker per machine" is a second, undeclared amendment
Approved plan, Figure 8: "Backends one set per session". W3.5: "one pp daemon per session"; acceptance "One pp daemon
across step, submit and finalize". Draft §3.1: "owns exactly one copy of each backend daemon" per machine; Phase 3 exit
"one process per daemon regardless of session count". §6.1 asks approval only for "standalone broker; gateway as its
client". Per-machine sharing changes things the per-session design did not have to solve:
- pair-programmer project locks are owned by the pid that ran `start_run`, and `shutdownAndExit` releases every lock
  the process holds (PP-6). With one shared `pp_harness` all sessions' locks have one owner.
- Single-threaded Node daemons become a shared bottleneck: one `detect_drift` (30 s, sync) or full verify blocks every
  session's eights calls (inv-eights F7, F11).
- Four concurrent sessions were observed; multi-session concurrency in the broker is the normal case, not an edge.
Change: either keep the approved scope (one set per session, broker owned by that session's first client) and state
it, or add "per machine" to §6.1 as an explicit decision with the three consequences above.

### 16. must-fix — broker identity, RBAC and the operator key are waved through as "untouched"
- Today RBAC is enforced in `MCPStdioDispatcher` on the engine path; the gateway is a flat passthrough (AGENTS.md
  Security; Hydra#94, #95). §3.2 makes the dispatcher "a thin client". Enforcement then has to live in the broker's
  `governed_call`, which needs approved W1.1 ("Lift redaction, RBAC and venom checks to module functions"; Phase 3
  "needs W1.1"). The draft's Phase 3 does not list that dependency.
- A loopback token file proves "same Windows user", nothing more. Any agent with Bash can read it and call the broker
  with whatever caller identity it likes. Approved Figure 7 relies on a hook-stamped ticket "bound to this agent";
  a self-made loopback request never passes through that hook. The per-squad whitelist is only as strong as that stamp.
- The broker inherits backend env from `backends.json`, which holds `HYDRA_OPERATOR_KEY` (inv-runtime A2). A broker
  that will run gate-clearing tools for any token holder weakens AGENTS.md hard rule 2 (never bypass HITL) until W3.4
  is in. The draft schedules W3.4 only as a precondition for 4.7.
Change: Phase 3 header "needs W1.1"; 3.1 lands W3.2 enforcement before the broker accepts non-read-only tools; W3.4
(operator key out of the broker's ambient env, gate-clearing verbs require an operator capability) is a precondition
of 3.2, not of 4.7. Prefer a named pipe with a per-user DACL over loopback plus token: no port, no token file, same
single-user guarantee.

### 17. must-fix — broker lifecycle on Windows is one line where it needs a design
- "Auto-start on first use": the first user is a `hydra` call inside a Claude Code Bash tool, or the gateway. Both sit
  inside the host session's process tree. Research §1: Claude Code on Windows "closes stdin, then ends the process tree
  after 300 ms"; the Python SDK puts children in a kill-on-close Job Object. A broker started that way dies with the
  session that started it and takes the other sessions' daemons with it. Research §5 and §7 give the answer the plan
  omits: `CREATE_BREAKAWAY_FROM_JOB` if permitted, otherwise a per-user at-logon Task Scheduler task (no admin).
- Stale code. A long-lived broker with a warm engine (4.6) keeps running the code it loaded. Attended runs merge into
  the main checkout many times a day; today each verb is a fresh child and picks the change up. The same applies to
  long-lived Node daemons and `dist` (E-12 is this bug already). Need a build stamp checked per call and a restart when
  it changes.
- "The CLI can still run a verb in-process when the broker is down" (§5) is the private-daemon fallback that the plan
  rejects in §3.1 as C-10. Two daemon sets against the same SQLite files is the failure being designed out.
Change 3.2 to name: start mechanism (scheduled task, or breakaway with a tested fallback), discovery file contents
(pid, start time, endpoint, build stamp), restart on stamp change, and "broker down = auto-start, wait bounded, then a
retryable exit code". Remove the in-process fallback except for verbs that need no daemon (`plan`, `status`, `budget`,
gate-only `resume` per inv-cli-parity §3).

### 18. should-fix — "the broker keeps the engine warm" (4.6) contradicts "holds no authoritative state" (§5) and creates re-entrancy
- Path today: caller → `hydra_control` → CLI child → dispatcher → pp/eights. With the dispatcher as a broker client
  the CLI child calls back into the broker while the broker's own `hydra_control` call is still open. That works only
  if the broker has no per-backend serialisation and no global lock. `MCPStdioDispatcher._run_lock` (DISP-1) is
  exactly such a lock, and research §4 recommends per-backend semaphores. AgentSmith adds a second loop
  (smith → hydra_control → engine → broker).
- If the engine runs inside the broker, the broker holds the resume lock and checkpoint handles, and a broker crash
  kills every session's in-flight verb.
Change: 4.6 keeps only "lazy LangGraph import". State in 3.2: "calls are concurrent per backend; no lock is held across
a backend call; a re-entrant call from a broker-owned child is a normal client call". Add a test with a fake
`hydra_control` that calls back into the broker.

### 19. should-fix — the job pattern has no home for its state and overstates what the key guarantees
§3.3: "returns a job id at once ... with an idempotency key so a retry never repeats work". §5: the broker "holds no
authoritative state". Then where is the job table, and what survives a broker restart? An idempotency key dedupes job
*creation*. It does nothing for a job killed halfway; that is C-05/C-06/#108 and needs 4.4 first (issue 12).
A cheaper shape that fits the existing design: a job is the existing CLI child started detached, exactly as the #70
smoke job is (`host_bridge.py:2972`, `:3240`); state is a file under `<project>/.hydra/<wf>/jobs/` written with
temp + `os.replace` like the cursor; the key is the existing `call_key`; mutual exclusion is the existing resume lock;
`hydra job status` reads the file. No broker involvement.
Also state that a job is one verb under the resume lock and is not "detached mode": CLAUDE.md says detached is
"automation-only and gated by `HYDRA_ALLOW_DETACHED=1`" and approved W5.1 adds an engine-level detached gate. A job
runner that can carry a whole stage loop unattended would walk around both.

### 20. should-fix — 3.4 conflicts with AGENTS.md's standalone guarantee
AGENTS.md Security: "Each connected system (pair-programmer, TheEights, AgentSmith, ExecutiveSuite, RLM-Creative) can
also run independently without Hydra by registering its MCP server directly." 3.4 makes pair-programmer and AgentSmith
reach TheEights "and each other" through Hydra's broker. Either they keep a stdio fallback (the private-daemon path
again) or they stop working without Hydra. inv-eights' own fix for F5 is a leader lease inside TheEights, which gives
a single watcher set with no new transport in three repos.
Change 3.4 to: "TheEights: leader lease for background work (F5). pair-programmer and AgentSmith use the broker when
its discovery file is present and their existing stdio path otherwise, with watchers disabled on that path."

### 21. note — §6.2 offers a bypass of the contract that is not needed
"directly for Phase 1, since Phase 1 repairs the path that attended runs depend on." CLAUDE.md: "NEVER hand-write
engine source". The attended verbs already run from a shell (`hydra step|submit-host-result|finalize`, inv-cli-parity
§1a), and the gateway completed 4,243 of 4,323 calls. Phase 1 can go through attended runs if `--project` is pinned
(C-08) and the judge keeps its MCP tool. Present option 2 as an explicit operator override of a hard rule, not as a
neutral choice. Sibling-repo fixes need each repo registered in `~/.hydra/repos.json`.

---

## E. Cut or defer (single-operator workstation)

### 22. should-fix — gate the broker on measurement
Phase 1 plus the Phase 2 TheEights items address every reported symptom. What the broker then adds is cold-start
saving (0.7 to 1.7 s per daemon), breaker persistence (inv-cli-parity's own fix for C-09 is a small file under
`~/.hydra/`), and a single SQLite writer, where no `SQLITE_BUSY` has been observed. Add to §5: "After Phase 1 and 2,
measure drops, `not_ready` rate and per-verb latency for one week. Start Phase 3 only if a target is missed", and name
the targets.

### 23. note — smaller cuts
- Phase 5 "Evaluate the MCP Tasks extension": cut. Research: Claude Code support UNCONFIRMED, Python SDK support is an
  open issue.
- 3.2 "per-backend supervision with backoff and a circuit breaker": lazy spawn with a cool-down (GW-5's fix) is enough
  for one operator. Keep the breaker out until a restart storm is observed.
- 1.9 across three repos: shrink per issue 8.
- Phase 0 "About 40 issues": the standing rule requires them, but group by root cause. I did not check the claim that
  C-01, C-02, GW-3, C-07, GW-1, GW-2 have no issue; inv-gateway ties GW-3 and GW-1 to incidents already in #117.
- 2.12 (SQLite WAL-reset version check): keep, it is one query per daemon.
- §6.5 (desktop app limits): the census shows 2 of 4 gateways are desktop-hosted (inv-runtime A1) and there is no
  desktop MCP log for the gateway at all (A8). If desktop sessions drive attended runs, the 60 s limit in research §1
  already applies; ask the operator now rather than leaving it as a late decision.

---

## F. Smaller factual points
| Plan text | Source says |
|---|---|
| §1.4 "catalog size (272 tools, about 95 KB)" | inv-gateway "~98 KB"; inv-runtime "92,167 bytes" |
| §1.2 "724 MB unrotated jsonl" | inv-eights 724 MB; inv-runtime 758 MB. Reports disagree |
| §5 "`backends.json` holds an operator key in plain text" | inv-runtime A2 says the key names are present; inv-eights F9 says `backends.json` "has no `env` block" and infers the key may not reach the eights daemon. Unresolved, and 2.4 (HITL resolve/expiry) depends on it |
| §1.3 "user-scope hooks spawn six pair-programmer processes per prompt" | correct, plus one pwsh (inv-runtime A2) |
| §1.1 "fix reproduced on a scratch copy" | correct, against inv-gateway's raw host only; host half INFERRED |
| 4.7 "(15 files)" | 15 files name the verbs; inv-cli-parity: "a prose change in four runbooks, not a rename sweep" |
