# Hydra control plane: CLI / MCP parity investigation

Read-only investigation, 2026-11-15. Worktree `C:\AiAppDeployments\Hydra\.claude\worktrees\hydra-gateway-reliability-6ee52e` (HEAD 9b6241e). No repo file edited, no process killed, no gateway tool called. Two local experiments were run (import timing; a stdin-inheritance reproduction, script at `scratchpad\exp\stdin_inherit.py`).

Evidence tags: **VERIFIED** = read in code and/or reproduced/measured; **INFERRED** = follows from the code but not observed running.

## 0. Headline

1. `hydra_control` is already a thin wrapper over the CLI. `plan`, `step`, `submit_host_result`, `finalize`, `resume` (attended), `budget` and `worktree.sweep_status` all run `python -m hydra_core.cli ...` as a child via `_run_cli_json` (`mcp_servers/hydra_control/server.py:729-846`). For the attended critical path, CLI parity exists today; MCP adds two process hops and two extra timeouts, not capability.
2. The slow verbs are slow in the engine child, not in MCP. Moving to the CLI does not make them faster, and the host Bash tool has the same 120 s default window (600 s max). The CLI path needs start → poll for long work or it reproduces the same failure.
3. Two specific root causes were found and verified:
   - `hydra.repo.resolve` over MCP hangs because its `git` child inherits the server's stdin pipe (F-01).
   - `hydra.workflow.step` runs the whole `pytest tests/` baseline synchronously, up to 600 s, before the cursor is saved. 38 of 70 baseline cache entries in the main checkout are timeout markers, four of them from today (F-02).
4. The unified-orchestration plan puts the loopback broker inside the gateway process. That ties the CLI's good path to the component that is dropping (F-10).

## 1. Parity matrix

### 1a. hydra_control tools (17 handlers, `server.py:1651-1669`)

| MCP tool | CLI equivalent | Status | Argument differences | Output / exit differences |
|---|---|---|---|---|
| `hydra.control.ping` | none (`hydra doctor --quick` is the nearest; 4.4 s) | missing | n/a | n/a |
| `hydra.workflow.plan` | `hydra plan "<goal>" [--squad --budget --risk --rigor --repo --repos --subdir --workflow-id]` (`cli.py:8230-8253`, `_cmd_plan` :813) | exists, same code | MCP `repo_subpath` → `--subdir`; MCP always pre-allocates `workflow_id` (:864); MCP-only input checks: goal ≤ 8000 chars, squad/repo regexes (:1190-1228) | Identical JSON on success (MCP passes stdout through). CLI errors go to stderr with exit 1; MCP turns that into `{ok:false,error:"plan_failed",detail:<stderr tail>}` (:831-833) |
| `hydra.workflow.step` | `hydra step <wf> [--verbose]` (:8255, `_cmd_attended_step` :4210) | exists, same code | none | Identical. `resume_in_progress` is `ok:false` with exit 0 (:4235-4239 region) |
| `hydra.workflow.submit_host_result` | `hydra submit-host-result <wf> --run-id --call-key --result FILE` (:8261, :5993) | exists, same code | CLI takes a file; MCP takes an object and writes `.hydra/<wf>/attended/hostresult-<key>.json` with strict JSON (:897-915) | CLI prints structured JSON on stdout with exit 1 for `stale_call_key` (:6176-6178) and `checkpoint_persist_failed` (:6202-6208). MCP discards stdout on non-zero exit and returns only the stderr tail, so the host loses that payload (F-07) |
| `hydra.workflow.finalize` | `hydra finalize <wf>` (:8271, :6355) | exists, same code | none | Same; `no_decision_record` exits 1 with JSON on stdout (:6544), lost over MCP (F-07) |
| `hydra.workflow.resume` | `hydra resume <wf> --action A [--option] [--critique-ref] [--gate-only \| --live]` (:8307-8352) | exists | MCP chooses the transport: `--gate-only` in-band when `HYDRA_ALLOW_DETACHED` is unset, detached `--live` otherwise (:488-554). CLI caller must choose. `hydra approve <wf>` is resume-approve without `--gate-only` and re-enters the graph; the approve skill says not to use it (`skills/approve/SKILL.md:50`) | Operator identity (`HYDRA_OPERATOR_ID`/`HYDRA_OPERATOR_KEY`) is read from the `hydra_control` backend's env block in `~/.hydra/backends.json` (`skills/approve/SKILL.md:17-20`, `cli.py:1648-1736`). A shell CLI call needs them in the shell env, where the agent's Bash can also read them (plan W3.4 / #97) |
| `hydra.workflow.launch` | `hydra run "<goal>" --live --workflow-id ID ...` (:8188-8229) | partial | CLI has no `HYDRA_ALLOW_DETACHED` gate and no fleet exemption (`server.py:661`; no such check in `_cmd_run` :624) | MCP detaches and returns `{ok,launched,pid,workflow_id,log}`; CLI runs in the foreground until the workflow ends |
| `hydra.workflow.submit_envelopes` | `hydra ingest <wf> --envelopes FILE --live` (:8356) | partial | MCP normalises and validates synchronously, all-or-nothing, max 100 (:1066-1091), then detaches | CLI runs the whole pp stage loop in the foreground |
| `hydra.workflow.budget` | `hydra budget [wf] [--set USD]` (:8414) | exists, same code | none | Same. Tool is not advertised through the gateway (plan §2.4; absent from this session's tool list) |
| `hydra.worktree.sweep_status` | `hydra sweep-worktrees` (dry-run default; `--apply` is CLI-only) (:8295) | exists, same code | MCP ignores any apply flag (:1413-1437) | Same. Not advertised through the gateway |
| `hydra.squad.list` | `hydra squads` (:575) | partial | none | Different shape. MCP: `{ok, squads:[{slug,name,entrypoint,active,version}]}`. CLI: dict keyed by slug with `name, entrypoint, industries, accepts, emits, agents`; no `ok`, `active`, `version` |
| `hydra.repo.list` | `hydra repo list` (:8533, :616) | partial | none | MCP: `{ok, builtin, registered}` (:1387-1391). CLI: `{ok, repos}` with registered ids only, no built-ins |
| `hydra.repo.resolve` | none | missing | n/a | In-process equivalent costs 0.46 s (measured) |
| `hydra.telemetry.tail` | `hydra trace <wf>` (:7314) | partial | CLI has no `limit`, `since` cursor, or cross-workflow tail | CLI dumps the raw JSONL file; error is plain text `no trace at ...` with exit 1, not JSON |
| `hydra.envelope.record` | none | missing | n/a | n/a |
| `hydra.venom.cross_check` | none | missing | n/a | n/a |
| `hydra.cockpit.audit` | none | missing | n/a | n/a |

CLI-only verbs with no MCP tool on `hydra_control`: `status`, `doctor`, `verify`, `reap`, `replay`, `eights-drain`, `eights-hitl-reconcile`, `repo register|unregister`, `memory query|tag`, `gateway-*`, `approve`.

### 1b. Gateway meta-tools (`mcp_servers/hydra_gateway/server.py:683-736`, dispatch :939-992)

| Meta-tool | CLI equivalent | Notes |
|---|---|---|
| `gateway.discover` | none | Planned: `hydra tool list` (W3.3 / #96) |
| `gateway.search` | none | Planned: `hydra tool list` (no search flag specified) |
| `gateway.describe` | none | Planned: `hydra tool describe <server> <tool>` |
| `gateway.navigate` | none | Not in the plan |
| `gateway.scope` | none | Not in the plan |
| `gateway.health` | none | `hydra doctor` probes backends by spawning its own daemons, which is not the running gateway's health |

### 1c. CLI contract facts that matter for a host driving it from a shell

- **Exit code is not the success signal.** `ok:false` with exit 0 for `resume_in_progress` (:6124-6127, :6381-6384), `tasks_pending` (:6443-6450), `workflow_terminal` (:6433-6440), `unjudgeable` (:6478-6488, :8577-8595). `ok:false` on stderr with exit 1 for input errors. `ok:false` on stdout with exit 1 for persist failures. A caller must parse stdout JSON and read `ok`. **VERIFIED.**
- **Stdout is not guaranteed to be one clean JSON document**; `_run_cli_json` carries a fallback that scans for the first `{` (`server.py:834-846`). **VERIFIED** that the fallback exists; which output precedes the JSON was not determined.
- **Project root defaults to cwd** (`Path(args.project) if args.project else Path.cwd()`, e.g. :828, :6035, :6373). `hydra_control` pins `cwd=_HYDRA_ROOT` (:744). See F-08.
- **`hydra` on PATH** resolves to `C:\Users\robob\AppData\Roaming\Python\Python314\Scripts\hydra.exe`, and from a neutral cwd `hydra_core` imports from `C:\AiAppDeployments\Hydra\hydra_core` (the main checkout). `python -m hydra_core.cli` from a worktree cwd imports the worktree copy. **VERIFIED.** No `plugins/hydra/bin/` exists.

## 2. Why the verbs exceed 120 s

Common to every verb called through MCP: host → gateway → `hydra_control` (to_thread, :2187) → `subprocess.run(python -m hydra_core.cli ...)` → new `MCPStdioDispatcher` (`cli.py:3545-3553`) → stdio daemons spawned on first use. `pp_harness` and `eights` are pooled only within one process (`dispatcher.py:205`); `pp_codex`/`pp_agy` get a fresh daemon per call (`_async_call`, :619-685).

| Verb | Synchronous work, in order | Dominant cost | Could be start → poll? |
|---|---|---|---|
| `repo.resolve` | registry lookup, `git rev-parse` with a 10 s timeout (`repo_registry.py:315-322`) | Hang on inherited stdin (F-01). Intrinsic cost 0.46 s | Not needed. Fix the stdin inheritance |
| `step` (engineering) | resume lock; build supervisor; read checkpoint; poll/self-heal open cursors; `pp_harness.start_run` (:4574); `ensure_agents_md` (:4590); `start_stage` (`host_bridge.py:2350`); `git worktree add` (:2367, 60 s git timeout :647); **`pytest tests/` baseline, 600 s cap** (:2383, :1995-2003, :661); cursor saved last (:2440-2441) | Baseline suite on a cache miss per HEAD sha (F-02); then pp daemon cold start | Yes. Return once `start_stage` and the worktree exist and the cursor is saved; run the baseline as a detached job like the smoke (#70). It runs in `repo_root`, not the engineer's worktree (:1937-1939), so it does not race the engineer |
| `submit` (generate) | lock; supervisor build + checkpoint read (:6147-6155); `archive_artifact`, `record_attempt`, `gate_eligible_judges` (`host_bridge.py:2548-2604`); cursor save | pp daemon cold start; normally short | Low value |
| `submit` (judge) | `record_verdict` (:3644); smoke launched detached (#70) and polled; `finalize_stage` (:3936); `finalize_run` (:4018); git commit + merge back (:807-833); checkpoint reconcile and budget charge (`cli.py:5166`) | Short since #70, unless the smoke is in immediate mode | Already polled |
| `submit` (planning squad result) | apply squad result; persist artifact; ingest PLAN; plan judge via `pp_codex.critique` with an 1800 s tool timeout (`dispatcher.py:185-189`) | Plan judge (#108) | Yes. Park at `await_plan_judge`, poll via `step` |
| `finalize` | lock; materialise; `sup.update_state(as_node="judge_per_squad")`; up to 6 `sup.invoke` through synthesis, judge_synthesis (live critique client, `supervisor.py:664-677`), postcheck (:6497-6509) | Synthesis judge critique; eights calls at postcheck | Yes. Same pattern |
| `resume` (gate-only) | identity precheck; capability mint; checkpoint patch; spool prune; one bounded eights round trip (8 s) | Python cold start plus the 8 s bound; 45 s cap (`server.py:404`) | Not needed |

Making these resumable does not move authority: the same CLI child does the work under the same resume lock; only the point at which it returns to the caller changes. The #70 smoke job is the existing precedent (`host_bridge.py:2972`, `:3240`).

## 3. Per-call cost and state across CLI invocations

Measured on this machine (warm disk, Python 3.14):

| Command | Wall time |
|---|---|
| `python -c pass` | 0.19 s |
| `python -c "import hydra_core.cli"` | 3.2 to 3.6 s |
| of which `hydra_core.supervisor` (langgraph + langchain_core) | 1.75 s cumulative; `langgraph.graph` 1.44 s |
| `python -m hydra_core.cli squads` | 3.1 s |
| `python -m hydra_core.cli repo list` | 3.7 s |
| `python -m hydra_core.cli status` | 4.2 s |
| `python -m hydra_core.cli doctor --quick` | 4.4 s |
| `resolve_repo_path('hydra')` in-process, including Python start | 0.46 s |

- Every CLI invocation imports LangGraph, including `squads`, `repo list` and `doctor --quick`. The import is eager: a module-level `try: from .supervisor import build_supervisor` at `cli.py:163-170`. The comment at `cli.py:41` ("imported lazily by `run`") and the `--quick` help text (":8181-8183, skips the langgraph import") are both wrong. **VERIFIED** (`'langgraph' in sys.modules` is True after `import hydra_core.cli`).
- Daemons: not forked at start. `MCPStdioDispatcher.__init__` only loads config (`dispatcher.py:217-231`). Daemons spawn on the first `call_mcp` to each server. `plan`, `status`, `budget`, gate-only `resume` use `_NullDispatcher` and spawn none. `step`, `submit`, `finalize` spawn `pp_harness` (and `eights` through the attestor) every invocation. **VERIFIED** by reading; daemon start time not measured (would have forked daemons).
- `step`, `submit` and `finalize` do not call `close_pooled_sessions`; the only call site is the gate-only resume helper (`cli.py:1389`). Whether the pooled daemon exits when the CLI child exits was **not determined**. #98 and #108 report leftover daemons.

State that lives only in the process and is lost between invocations:

| State | Where | Consequence |
|---|---|---|
| Pooled MCP sessions (pp_harness, eights) | `dispatcher.py:227` | Cold daemon per verb |
| Eights circuit breakers | instance fields, `eights/attestation.py:164-196` | With eights down, every invocation re-pays the guard timeouts; the breaker never stays open across verbs |
| Handoff grants, tool-usage tracker | `dispatcher.py:225-226` | Not carried over |
| Venom registry | process-local, reloaded | Cost only |

State that is durable:

| State | Location |
|---|---|
| Workflow checkpoint | SQLite at `HYDRA_CHECKPOINT_DB` or `~/.hydra/checkpoints.db` (`supervisor.py:5036`). Global, not per project |
| Attended cursor | `<project>/.hydra/<wf>/attended/<run_id>.json`, temp + `os.replace` (`host_bridge.py:1853-1890`) |
| Host results | `<project>/.hydra/<wf>/attended/hostresult-<call_key>.json` |
| Trace, resume lock | `<project>/.hydra/<wf>/trace.jsonl`, `resume.lock` (PID-liveness reclaim, `cli.py:963-1021`) |
| Baseline cache | `<repo>/.harness/baseline/<sha>.json` and `<sha>.timeout.json` |
| Eights spool | `~/.hydra/eights-pending` |
| pp ledger | pair-programmer's own database (not examined) |

## 4. Blast radius of moving the critical path to the CLI

Files under `plugins/hydra` that name `hydra_control` verbs or MCP tool names: **15** (7 skills, 6 agents, 2 hooks).

| File | Matches | Nature |
|---|---|---|
| `skills/run/SKILL.md` | 15 | Runbook; dotted names `hydra.workflow.*` |
| `skills/resume/SKILL.md` | 11 | Runbook; also documents the CLI form |
| `skills/approve/SKILL.md` | 10 | Runbook; also documents the CLI form |
| `skills/drive/SKILL.md` | 9 | Runbook |
| `skills/campaign/SKILL.md`, `skills/budget/SKILL.md`, `skills/cross-squad-message/SKILL.md` | 1 each | Mentions |
| `agents/hydra-supervisor.md` | 6 | Slated for deletion (W2.5) |
| `agents/hydra.md` | 3 | Names `hydra.workflow.finalize`, `hydra.squad.list`, `hydra.repo.list`, `hydra.repo.resolve` |
| `agents/engineer.md` | 5 | Prose only; tools are `Read, Write, Edit, Glob, Grep, Bash` |
| `agents/hydra-plaza.md` | 2 | Mentions |
| `agents/judge-cross-vendor.md` | 8 | Hard dependency (frontmatter) |
| `agents/judge-same-vendor.md` | 4 | Hard dependency (frontmatter) |
| `hooks/hooks.json` | 2 | Matchers `mcp__hydra_memory__.*` (:77, :129); pre-gateway names |
| `hooks/hydra-route-directive.ps1` | 4 | Advisory text with pre-gateway names |

The literal prefix `mcp__hydra_gateway__` appears in only 5 files under `plugins/`, `hydra_core/`, `mcp_servers/`: `hydra_core/cli.py` (11), `plugins/hydra/agents/judge-cross-vendor.md` (4), `judge-same-vendor.md` (1), `mcp_servers/hydra_gateway/server.py` (2), `mcp_servers/xenia_tickets/server.py` (1); plus `CLAUDE.md` (5) and `AGENTS.md` (1). Runbooks use the dotted names, so a CLI switch is a prose change in four runbooks, not a rename sweep.

Agent definitions with gateway tools in `tools:` frontmatter:

- `plugins/hydra/agents/judge-cross-vendor.md:5`: `mcp__pp_codex__critique, mcp__pp_agy__critique, mcp__pp_harness__get_rubric, mcp__hydra_gateway__pp_codex__critique, mcp__hydra_gateway__pp_agy__critique, mcp__hydra_gateway__pp_harness__get_rubric`
- `plugins/hydra/agents/judge-same-vendor.md:5`: `mcp__pp_harness__get_rubric, mcp__hydra_gateway__pp_harness__get_rubric, Read, Glob`

Neither judge has Bash. The cross-vendor judge therefore cannot reach its critique tool without MCP. This is the one hard MCP dependency on the critical path that CLI verbs for `hydra_control` do not remove. Outside this repo, rlm-gaming agents list `mcp__hydra_gateway__eights__*` and `...rlm_creative__*` tools (seen in this session's agent roster; pack files not read).

Hooks: `PreToolUse` on `Bash` runs `hydra-block-bash-writes.ps1` on every shell command (`hooks.json:47-51`), so each CLI verb also pays that hook. `SessionStart` and three `PreToolUse` matchers run `hydra-doctor.ps1 doctor --quick` (4.4 s each).

## 5. The unified-orchestration plan against this problem

Already covered:

- Plan §2.4 records that plan/step/submit/finalize/budget share one code path with the CLI, that no shell route to arbitrary gateway tools exists, and that `hydra.workflow.budget` and `hydra.worktree.sweep_status` are not advertised.
- W3.3 / #96: `hydra tool list|describe|call` over a loopback broker, with `plugins/hydra/bin/hydra`.
- W3.4 / #97: curated verbs for CLI gaps; gate-clearing tools need an operator capability; deny them from the agent's Bash; move the operator key out of ambient env.
- W3.5 / #98: `BrokerDispatcher`, one pp daemon per session.
- #117 fix plan item 2 already proposes making `step` return once the cursor is durable.

Missing or underspecified for reliability:

| Gap | Detail |
|---|---|
| Where the broker lives | "The running gateway exposes a loopback broker" (#96). No lifecycle, discovery (port/pipe file), authentication of the loopback caller, or behaviour when the gateway restarts mid-call |
| Process supervision | Nothing restarts the gateway or a backend, and nothing owns daemon lifetime outside a Claude Code session |
| Health | No CLI health verb; `gateway.health` has no shell equivalent and no last-disconnect record (#117 item 4 asks for it) |
| Long calls | No start → poll contract for verbs. W3.5 removes daemon cold start only; baseline, plan judge and synthesis judge remain in-call |
| Idempotent retry | No per-verb retry contract. Present state is in §6 |
| Host shell limits | The Bash tool default is 120 s, max 600 s. `HYDRA_SUBMIT_TIMEOUT_S` is 1800 s. Not mentioned |
| Project root | `hydra` from the session cwd (a worktree) resolves a different `.hydra/` than `hydra_control` does (F-08). The launcher must pin the root |
| CLI output contract | No statement that stdout is one JSON document and that exit code is not the signal |
| Judge agents | No route for the cross-vendor judge without MCP |
| Stdio fallback | "Falls back to stdio only when no broker is present": that is today's behaviour (a private daemon set per call), and it is the path taken exactly when the gateway is down |

Where the plan's own design is fragile:

1. **Broker inside the gateway (F-10).** The CLI's fast path depends on the process whose drops prompted this work. When it drops, the CLI silently changes mode to stdio and forks a second daemon set against the same pp/eights SQLite stores while the gateway's set may still be alive.
2. **Prefix change in Phase 6** (`mcp__plugin_<plugin>_<server>__<tool>`) touches every matcher and both judge frontmatters. The plan notes it; it lands after the CLI work and will need the same files edited twice.
3. **W3.4 ordering.** `hydra resume --gate-only` is already callable from a shell today. If runbooks move to the CLI before W3.4, the operator key has to sit in the shell env, which is the exposure W3.4 exists to close.

## 6. Idempotency under a host timeout

What a 120 s host or gateway timeout does: the caller stops waiting; the CLI child keeps running and keeps the resume lock. For gateway-capped tools the gateway also tears the backend down (`hydra_gateway/server.py:331-336` comment). The child is killed only when `hydra_control`'s own cap fires (`subprocess.run(timeout=...)`, :743-756).

| Verb | Retry while the first call is still running | Retry after the first call finished server-side | Retry after the child was killed mid-flight | Ledger |
|---|---|---|---|---|
| `plan` | No lock. Same `workflow_id` twice is undefined (`cli.py:624-633` comment covers `run`; `plan` has the same shape) | A second `plan` with a new id creates a second workflow | Partial checkpoint for that id | None. Caller must reuse the pre-allocated `workflow_id` and check `status` first |
| `step` | `{ok:false,status:"resume_in_progress"}`, exit 0 | Safe: re-issues the same `host_action` for an open cursor; polls `await_smoke`; self-heals terminal cursors (:4436-4495) | Not safe. `start_run`/`start_stage`/worktree exist but the cursor is saved last (`host_bridge.py:2440`) and `open_pp_runs` is persisted after `begin_stage` (`cli.py:4609-4625`). The next `step` opens a second pp run. The server names this itself as `stale_state` (`server.py:790-800`) | None for stage opening (F-05) |
| `submit` (generate) | `resume_in_progress` | Safe: `call_key` no longer matches `pending_action.call_key`, result returned with `ignored` (`host_bridge.py:5030-5048`) | `record_attempt` carries no idempotency token (:2569-2577) and the cursor is saved at the end (:5104). A kill in between duplicates the attempt row on retry | Cursor `call_key` only (F-06) |
| `submit` (judge) | `resume_in_progress` | Safe | Safe: payload persisted before the call (:3633-3635), `idempotency_token=call_key` (:3631), `verdict_recorded_for` marker (:3648-3650), `stalled_infra` hold | Yes: pp idempotency token + cursor marker + `charged` flag + `attended_checkpoint_reconciled` (`cli.py:5202-5336`) |
| `submit` (squad / PLAN) | `resume_in_progress` | Safe by `call_key` | Trace says applied, checkpoint does not; retry mints a second PLAN envelope id for the same revision (#108) | Partial |
| `finalize` | `resume_in_progress` | Safe: `already_finalized` (:6414-6420) | Not safe. `attended_finalized_record_id` is written only at the end (:6519-6521). A retry re-materialises and re-enters at synthesis; the code's own comment says re-synthesis duplicates episodic rows (:6408-6410) | Single end-of-run marker (F-06) |
| `resume` (gate-only) | lock | Safe: `no_pending_gate` no-op | Safe, documented (`server.py:811-829`) | Yes |
| `submit_envelopes` / `ingest` | lock | Safe: claim-before-dispatch ledger (`ingest.py:1011`) | Not examined in detail | Yes |
| budget charge | n/a | Exactly once: cursor `charged` flag written before the checkpoint charge, with a repair path (`cli.py:5322-5336`) | Covered by the repair path | Yes |

So: exactly-once holds for verdicts, charges, gate resolution and ingest claims. It does not hold for stage opening, `record_attempt`, or a killed `finalize`. The lock makes a retry during a still-running call harmless but uninformative: the caller gets `resume_in_progress` with no way to wait for, or read, the first call's result.

## 7. Findings

### F-01 `hydra.repo.resolve` hangs over MCP: git child inherits the server's stdin
- Severity: high. Confidence: high. **VERIFIED** by reproduction.
- Evidence: `resolve_repo_path` runs `subprocess.run(["git", ...], capture_output=True, timeout=10, ...)` with no `stdin=` (`hydra_core/repo_registry.py:315-322`). The handler calls it in-process inside the MCP server (`server.py:1404-1405`), whose stdin is the gateway pipe with a read always pending. The same hazard is documented for Python children at `server.py:745-750` (MU8) and fixed there with `stdin=subprocess.DEVNULL`.
- Reproduction (`scratchpad\exp\stdin_inherit.py`: a thread blocked reading a stdin pipe, then the same `subprocess.run`):
  - `mingw64\bin\git.exe`, inherited stdin: `TimeoutExpired` after 10.02 s. With `stdin=DEVNULL`: rc 0 in 0.06 s.
  - `C:\Program Files\Git\cmd\git.exe` (the wrapper a normal Windows PATH resolves), inherited stdin: the 10 s timeout did not release the call. It returned after 49.29 s, exactly when the 50 s stdin holder closed the pipe. With `stdin=DEVNULL`: rc 0 in 0.09 s.
- Root cause: the wrapper's grandchild blocks on the inherited stdin handle and holds the stdout pipe, so `communicate()` after the kill never completes. Under the gateway the pipe closes only when the gateway's 120 s cap fires and it tears the backend down. That matches #117 ("hit the gateway's 120 s tool cap") and would also look like a gateway drop.
- Not determined: which `git.exe` the live `hydra_control` process resolves.
- Smallest fix: `stdin=subprocess.DEVNULL` in `resolve_repo_path`, and default it in `hydra_core.proc.run_text` so every in-process git call in any MCP server is covered. Test: hermetic test that runs the handler with a pipe stdin and a pending read.

### F-02 `step` runs the full pytest baseline synchronously, up to 600 s, before the cursor is durable
- Severity: high. Confidence: high. **VERIFIED** (code + on-disk evidence).
- Evidence: `begin_stage` calls `_capture_baseline_failures` (`host_bridge.py:2383`), which runs `python -m pytest tests/ --no-header -q --tb=no` with `timeout=_baseline_timeout_s()`, default 600 (:1995-2003, :661-675). Cache is per HEAD sha (:1961-1985), so every merged stage invalidates it. The cursor is saved after it (:2440-2441). In `C:\AiAppDeployments\Hydra\.harness\baseline\`, 38 of 70 entries are `<sha>.timeout.json`; four are dated 2026-11-15 (11:24, 12:24, 18:16, 20:16), including `ea72397...` and `1285033...`, both in today's git log. Each marker is a `step` that spent the full 600 s and got no baseline.
- Root cause: a minutes-long, best-effort measurement sits on the synchronous path of opening a stage. #117 attributes the time to `start_run` and worktree creation; the baseline is not mentioned there.
- Smallest fix: save the cursor and return `awaiting_host` once the stage and worktree exist; run the baseline as a detached job keyed by sha with the #70 smoke-job machinery, and have the smoke-excuse step wait for or read it. It runs in `repo_root`, so it does not touch the engineer's worktree.

### F-03 Gateway caps `finalize` at 120 s while `hydra_control` allows 600 s
- Severity: medium. Confidence: high. **VERIFIED** by reading.
- Evidence: `_CONTROL_PLANE_FINAL_SEGMENTS = {plan, step, submit_host_result, submit_envelopes, launch, resume}` (`hydra_gateway/server.py:341-344`); `finalize` is absent, so it gets `_DEFAULT_TOOL_TIMEOUT = 120.0` (:316, :421-423). `_FINALIZE_TIMEOUT_S` is 600 (`hydra_control/server.py:726`). The gateway comment says a gateway-side timeout "returns a hard `failed` AND tears the backend down" (:334-336).
- Also stale: the same comment cites `STEP=300 / SUBMIT=2700`; the code has 900 and 1800.
- Smallest fix: add `finalize` (and `budget`, `sweep_status` if advertised) to the set; add a test that every `_run_cli_json` label has a gateway class above its subprocess cap.

### F-04 Every CLI invocation pays about 3.2 to 3.6 s to import LangGraph, including read-only verbs and the hook doctor
- Severity: medium. Confidence: high. **VERIFIED** by measurement.
- Evidence: §3 table; eager import at `cli.py:163-170`; stale claims at `cli.py:41` and `:8181-8183`. `hooks.json` runs `doctor --quick` on SessionStart and on three PreToolUse matchers.
- Smallest fix: make the supervisor import lazy inside the verbs that build a supervisor. Expected floor for `squads`, `repo list`, `doctor --quick` is under 1 s (state + pydantic).

### F-05 A killed `step` leaves an orphan pp run and worktree, and the retry opens a second run
- Severity: high. Confidence: medium. **INFERRED** from ordering; the server's own timeout payload lists this as `stale_state` (`server.py:790-800`).
- Evidence: `start_run` at `cli.py:4574`; `open_pp_runs` appended in memory at :4609 and persisted at :4625 after `begin_stage`; cursor saved at `host_bridge.py:2440` after the baseline. With F-02 the window is up to 600 s wide and `_STEP_TIMEOUT_S` is 900.
- Smallest fix: persist `open_pp_runs` and a minimal `opening` cursor immediately after `start_run`; on the next `step`, adopt or abort that run instead of starting another. F-02's fix shrinks the window to seconds.

### F-06 `record_attempt` and `finalize` are not exactly-once under a mid-flight kill
- Severity: medium. Confidence: medium. **INFERRED.**
- Evidence: `record_attempt` payload has no idempotency token (`host_bridge.py:2569-2577`) and the cursor is saved at :5104. `finalize` writes its idempotency marker last (`cli.py:6519-6521`) after up to six graph invokes; the comment at :6408-6410 states the duplicate-episodic consequence.
- Smallest fix: pass `call_key` as an idempotency token to `record_attempt` (pp side must honour it; not checked) or save `attempt_recorded_for` on the cursor before the call as the verdict path does. For `finalize`, write a `finalize_started` marker with the materialised patch before the first invoke and resume from `snap.next` instead of re-patching `as_node`.

### F-07 MCP wrapper drops structured stdout when the CLI exits non-zero
- Severity: medium. Confidence: high. **VERIFIED** by reading.
- Evidence: `if proc.returncode != 0: return {"ok": False, "error": f"{err_label}_failed", "detail": proc.stderr[-2000:]}` (`server.py:831-833`). The CLI prints full JSON on stdout with exit 1 for `stale_call_key` (`cli.py:6176-6178`), `checkpoint_persist_failed` (:6202-6208, also in `step` :4455-4461) and `no_decision_record` (:6544).
- Consequence: over MCP the host sees `submit_failed` with an empty detail for exactly the cases where it most needs `checkpoint_persist_errors`. The CLI path is more informative than the MCP path here.
- Smallest fix: on non-zero exit, try to parse stdout first and return it with `ok:false`.

### F-08 CLI project root defaults to cwd; the checkpoint is global but cursors, locks and traces are per project
- Severity: high for a CLI-driven host. Confidence: medium. **INFERRED** (not executed).
- Evidence: `project = Path(args.project) if args.project else Path.cwd()` in every verb; checkpoint at `~/.hydra/checkpoints.db` (`supervisor.py:5036`); cursor path under `<project>/.hydra/<wf>/attended/` (`host_bridge.py:1853-1861`); `hydra_control` pins `cwd=_HYDRA_ROOT` (`server.py:744`). `step` skips an open run whose cursor file is missing (`cli.py:4447-4449`) and goes on to task selection.
- Consequence: `hydra step <wf>` run from a session worktree finds the workflow in the global checkpoint, finds no cursor or lock under the worktree's `.hydra/`, and can open a duplicate stage. Module skew is also possible: `hydra.exe` imports the main checkout; `python -m hydra_core.cli` imports whatever is in cwd.
- Smallest fix: the `plugins/hydra/bin/hydra` launcher (W3.3) sets `--project` and `PYTHONPATH` from the plugin root as `hydra-doctor.ps1:10-12` already does; the CLI refuses a workflow verb when `<project>/.hydra/<wf>/` is absent but the checkpoint exists.

### F-09 In-process breaker and pool state resets on every verb
- Severity: medium. Confidence: high. **VERIFIED** by reading.
- Evidence: breaker timestamps are instance fields (`eights/attestation.py:164-196`); pools at `dispatcher.py:227`.
- Consequence: with TheEights slow or down, each verb re-pays the guard timeouts; ties into #123.
- Smallest fix within the plan: W3.5's shared daemon removes the cold start; persist breaker-open-until in a small file under `~/.hydra/` so short-lived CLI processes honour it.

### F-10 The planned broker lives inside the gateway process
- Severity: high (design). Confidence: medium. **INFERRED** from #96 and plan §4.
- Evidence: #96 "The running gateway exposes a loopback broker; the CLI connects to it ... falls back to stdio only when no broker is present."
- Consequence: gateway down means broker down; the CLI then forks private daemons (today's slow path) with no signal to the operator that it changed mode, and possibly alongside surviving daemons from the dead gateway.
- Smallest change that keeps the plan's shape: make the broker its own long-lived process that owns the backend pool and `governed_call`; the MCP gateway becomes one client of it and the CLI another. Specify discovery (a file under `~/.hydra/` with pid, endpoint, token), a `hydra broker status|start` verb, and that the CLI reports which transport it used in every response.

### F-11 No CLI route for the cross-vendor judge
- Severity: medium. Confidence: high. **VERIFIED** for this repo's two judge agents.
- Evidence: `judge-cross-vendor.md:5` lists only MCP critique tools; no Bash.
- Consequence: with MCP down the attended loop stops at the judge step even if every `hydra_control` verb has a CLI form.
- Smallest fix: once `hydra tool call` exists (W3.3), have the engine run the critique itself on `submit` of a judge request, or grant the judge a narrowly scoped `Bash(hydra tool call pp_codex critique *)`. This needs a decision under W3.2's caller-identity design.

### F-12 CLI gaps for a host that drives entirely by shell
- Severity: low to medium. Confidence: high. **VERIFIED.**
- Missing: `repo resolve`, `ping`/health, `telemetry tail` with cursor, `envelope record`, `venom cross-check`, `cockpit audit`, any gateway meta-tool. Partial: `squads` and `repo list` shapes differ from the MCP tools; `trace` is not JSON; `run --live` has no detached gate; exit codes do not track `ok`.
- Smallest fix: W3.4 curated verbs should add `repo resolve`, `squads --json-v2` (or align the MCP shape), `trace --tail N --since CURSOR`, and document "parse stdout, read `ok`".

## 8. Not determined

- Why the gateway itself disconnected (out of scope here; F-01 and F-03 each produce a gateway-initiated backend teardown that could look like one).
- Which `git.exe` the live `hydra_control` resolves, and therefore whether production sees the 10 s or the until-teardown variant of F-01.
- Daemon cold-start times for pp_harness and eights (not measured to avoid forking daemons).
- Whether pooled daemons exit with the CLI child or are orphaned.
- Whether pair-programmer's `record_attempt` accepts an idempotency token, and how `start_run` behaves when a prior orphan run holds the `.harness` lock.
- What non-JSON output can precede the JSON document on CLI stdout.
- `hydra run` output shape and `ingest` crash-retry behaviour beyond the claim ledger.
- Squad-pack agent files outside this repo were not read; the rlm-gaming gateway-tool dependency is from the session's agent roster only.
