# Contributing a Squad Pack

Adding a squad to Hydra means writing one YAML file, picking an
entrypoint adapter, wiring its tools, and teaching the router how to
recognize work for it. There is no Hydra-core change required —
`squad_loader.discover_squads()` auto-discovers any
`squads/<slug>/squad.yaml` under the project root.

This guide walks through the full activation path. Reference squads:
`squads/engineering/` (MCP entrypoint, PP delegation),
`squads/executive/` (agent-impersonation, ES delegation),
`squads/garland/` (Claude-skill, RLM delegation),
`squads/legal-compliance/` (stub template).

## a) Create the squad folder

```
squads/
  <slug>/
    squad.yaml
    README.md           # optional, human notes
    prompts/            # optional, persona prompts
    rubrics/            # optional, rubric yaml files
```

`<slug>` is kebab-case, matches the router keyword fingerprint key, and
becomes the squad's identity in `HydraEnvelope.origin_squad` /
`target_squad` and in MCP namespace prefixes (`hydra-<slug>.*`).

## b) The squad.yaml schema

The full schema, with every required and optional field. See
`hydra_core/squad_loader.py` for the loader and the dataclasses
(`SquadPack`, `AgentSpec`, `ToolSpec`, `GateSpec`).

```yaml
name: <Human-readable name>
description: >
  One paragraph: what this squad does, what pack it wraps, what kind
  of envelopes it consumes and produces.
source_pack: <absolute path to upstream pack, or null for stub>
entrypoint: <mcp | subprocess | agent-impersonation | claude-skill | stub>
industries: [tag, tag, tag]            # used by router industry-boost
version: <semver, default 1.0.0>       # optional
deprecated_after: <ISO date or null>   # optional; Iolaus refuses dispatch on/after
best_of_n: <int, default 0>            # optional; 0/1 = off, N>=2 = best-of-N (see below)

agents:
  - slug: <kebab>
    role: "<one-line role description, surfaces in HITL prompts>"
    authority: <advisory | execute | gatekeeper>

tools:
  - name: <tool-name as exposed by MCP server or skill>
    mcp_server: <server name from .mcp.json>   # omit for non-mcp
    privilege: <read | write | execute>

accepts:
  - <Envelope type, e.g. PRD, CREATIVE_BRIEF>
  - "*"                                  # wildcard, only for executive

emits:
  - <Envelope type>

gates:
  - rubric_id: <rubric slug>
    hitl_required: <true | false>        # default false
    when: "<python-expression-over-state-and-constraints>"   # optional

invoke:
  # Free-form block. Hydra's adapter for `entrypoint` reads from here.
  # Conventional keys per adapter are documented in section (c).
```

`agents[*].authority` semantics:

- `advisory` — produces analysis, never closes a gate.
- `execute` — can call write/execute tools within its privilege scope.
- `gatekeeper` — can block a phase; required for any HITL gate.

`industries` are normalized lowercase tags. They feed the router's
industry-boost — pre-classified workflows (e.g. via a tenant profile)
will up-weight squads whose `industries` overlap.

`best_of_n` — **opt-in best-of-N sampling.** The loader
(`squad_loader.py#_coerce_pack`) parses it as `int(data.get("best_of_n", 0)
or 0)`, so `0`, `1`, or an absent field all mean *no best-of-N* (single-shot
dispatch). A value of **N ≥ 2** makes the supervisor route the squad through
`_dispatch_best_of_n` (`supervisor.py`): it produces N candidate outputs,
judges each with the cross-vendor critique client, Borda-ranks them
(`hydra_core/judge/borda.py` — meaningful tie-breaking needs N ≥ 3), returns
the winner, and archives the losers to artifacts. Two extra guards apply:

- Real judging only fires when the squad is in `judge_policy.enabled_squads`
  (the Phase-2 allowlist) — otherwise N ≥ 2 would burn judge calls for no
  gain; the branch falls back to single-shot.
- best-of-N tasks are **excluded from the cross-repo fleet** fan-out
  (`supervisor.py` filters `pack.best_of_n >= 2`), since N parallel
  candidates against one repo would collide on the `.harness/.lock`.

If anything in the best-of-N path errors, it falls back safely to single
dispatch. Currently `executive` and `garland` set `best_of_n: 3`. (Note:
this squad-level Borda field is distinct from the engineering squad's
`invoke.mode: pp_best_of`, which selects best-of inside the pair-programmer
harness rather than via Hydra's `_dispatch_best_of_n`.)

## c) Choosing an entrypoint

Pick exactly one. The choice determines how `squad_node.execute_squad`
invokes the squad.

| Entrypoint | When to use | Adapter |
|---|---|---|
| `mcp` | Upstream pack is a long-lived daemon exposing tools over MCP. Best for stateful runtimes (pair-programmer harness, vector databases, custom servers). | `_via_mcp` opens an MCP client session, calls tools by name, parses MCP results into a `SquadResult`. |
| `subprocess` | Upstream is a CLI tool with no MCP surface; you accept the cold-start cost. Best for one-shot generators, batch tools. | `_via_subprocess` spawns the command, captures stdout, expects JSON or a known artifact path. |
| `agent-impersonation` | Squad is itself a set of Claude Code sub-agents — the work happens inside the same Claude session, not in a child process. Best for persona packs (ExecutiveSuite, deliberation panels). | `_via_impersonation` emits a Claude prompt naming the agent slug; the sub-agent takes over the turn. |
| `claude-skill` | Squad is a Claude Code skill bundle (`.claude/skills/`) and you want skill invocation semantics (slash-command-style). Best for creative pipelines, structured generators (RLM, frontend-design). | `_via_claude_skill` issues `/skill-name` with arg payload. |
| `stub` | Squad declared but not yet implemented. Loader still indexes it for routing, but `execute_squad` returns a no-op `SquadResult` of type stub. Use for scaffolding. | `_stub` returns an inert result. |

**`invoke:` conventions per entrypoint.**

`mcp`:

```yaml
invoke:
  mode: pp_run                       # adapter-specific
  default_team: feature-team
  forum_for_review: change-advisory-board
  project_path: "${project_root}"
  command_hint: "/pp:run"
```

`subprocess`:

```yaml
invoke:
  cmd: ["python", "-m", "mypack.cli", "run"]
  env:
    MYPACK_PROFILE: enterprise
  expect: json                       # or: artifact_path
  timeout_s: 600
```

`agent-impersonation`:

```yaml
invoke:
  command_hint: "/board-meeting"
  fallback_commands:
    - "/capital-decision"
    - "/crisis-mode"
  output_dir: "output/{domain}/{topic-kebab}-{date}.md"
```

`claude-skill`:

```yaml
invoke:
  command_hint: "/rlm-team"
  fallback_commands:
    - "/rlm-feature-design"
    - "/cinematic-landing"
  output_dir: "RLM/output/{phase}/{topic}-{date}.md"
```

`stub`:

```yaml
invoke:
  notes: "Activate by adding implementing agents + MCP server + commands."
```

## d) Wiring the squad's runtime

Depending on entrypoint:

**MCP server.** Add an entry to the project's `.mcp.json`:

```json
{
  "mcpServers": {
    "myhealth-daemon": {
      "command": "python",
      "args": ["-m", "myhealth.server"],
      "env": {}
    }
  }
}
```

Then reference `mcp_server: myhealth-daemon` in each tool. The Hydra
host opens one isolated client session per `(squad, server)` pair —
two squads referencing the same server still get separate sessions
for RBAC isolation.

**Subprocess.** Make sure the entrypoint CLI is on `PATH` (or use an
absolute `cmd[0]`). The adapter pipes stdout / parses JSON; emit a
single JSON document on completion and write artifacts to paths
referenced in that JSON.

**Agent-impersonation.** The sub-agent definitions must live under
`<source_pack>/.claude/agents/` or be registered with Claude Code as
a plugin. Hydra emits the prompt; it does not load the agent itself.

**Claude-skill.** Skills must be under `<source_pack>/.claude/skills/`
or registered as a plugin. The `command_hint` is the slash command
Hydra invokes; `fallback_commands` are tried in order if the primary
errors.

## e) Rubrics, gates, and HITL

Each `gates[*]` entry attaches a rubric to a phase. Rubric ids should
match a rubric yaml under `squads/<slug>/rubrics/` or under the shared
`hydra_core/rubrics/` library. Example:

```yaml
gates:
  - rubric_id: gdpr-art-25-privacy-by-design
    hitl_required: true
  - rubric_id: open-source-license-compatibility
    hitl_required: false
  - rubric_id: eu-ai-act-classification
    hitl_required: true
    when: "'ai-platform' in constraints.industries"
```

A gate with `hitl_required: true` whose `when` evaluates true (or
which has no `when`) will set `state.requires_human_approval = True`
and populate `state.pending_hitl`. The supervisor halts at the next
`interrupt_before` boundary. The user resumes with
`/hydra:approve <workflow_id>`.

The `when` expression is evaluated against the current `HydraState`
and the inbound envelope's `constraints`. Keep expressions pure —
no I/O, no time-dependent comparisons that aren't already in state.

## f) Router keyword fingerprints

Open `hydra_core/router.py` and extend the `_KEYWORDS` dictionary:

```python
_KEYWORDS: dict[str, tuple[str, ...]] = {
    ...,
    "myhealth": (
        "claim", "denial", "prior auth", "icd-10", "cpt code",
        "hcc", "risk adjustment", "medicare advantage",
    ),
}
```

Tips:

- Use lowercase, word-boundary-friendly tokens. Multiword tokens are
  matched as literal substrings inside `\b...\b` so "prior auth"
  works.
- 8 to 20 keywords is the sweet spot. Fewer misses common phrasing;
  more inflates false positives.
- Each hit contributes `0.15` to the squad's score (capped at 1.0).
  Three or more hits clears the default `min_confidence=0.25` threshold.
- The `industries:` block boosts the score independently when the
  caller has pre-tagged the workflow.

If the deterministic pass returns no high-confidence squad, the router
falls back to an LLM classifier (pluggable via `classify_callable`).
A bad fingerprint hurts mostly by sending easy work to the LLM
fallback — not by misrouting.

## g) Testing the squad

```powershell
# 1. Verify discovery and schema parse
python -m hydra_core.cli doctor

# 2. Inspect the loaded pack as JSON
python -m hydra_core.cli squads

# 3. Force-select the squad for a smoke run
python -m hydra_core.cli run "Smoke test for myhealth squad" --squad myhealth

# 4. Tail the trace
python -m hydra_core.cli trace <workflow_id>
```

`hydra doctor` will print the squad as a `·` (stub) until you flip the
entrypoint off `stub`. `hydra squads` dumps the parsed `SquadPack` so
you can confirm `accepts`, `emits`, agents, and tool list.

For richer end-to-end testing, exercise the slash command from a
Claude Code session:

```
/hydra:run "Triage incoming HIPAA-flagged ticket and propose response"
/hydra:status
/hydra:approve <workflow_id>
```

Verify the trace at `<project>/.hydra/<workflow_id>/trace.jsonl`
contains `node_start` / `tool_call` / `node_end` events from your
adapter.

## h) Pre-activation checklist

Before flipping `entrypoint:` away from `stub`, confirm:

- [ ] `squad.yaml` parses cleanly under `hydra doctor` with no warnings.
- [ ] At least one agent with `authority: gatekeeper` exists if any
      gate has `hitl_required: true`.
- [ ] Every entry in `tools:` resolves — MCP server registered, CLI
      on path, or skill installed.
- [ ] `accepts:` includes every envelope type the planner can route
      to this squad. Conversely, every type listed in `emits:` has a
      matching schema in `hydra_core/schemas.py`.
- [ ] Rubric ids in `gates:` map to actual rubric yamls.
- [ ] Router keyword fingerprint added to
      `hydra_core/router.py:_KEYWORDS` and exercised by a sample goal.
- [ ] Adapter emits `telemetry.emit(...)` events at node_start,
      tool_call, and node_end. (See ARCHITECTURE.md section 10.)
- [ ] Adapter calls `governance.record_cost(state, usd, tokens)` after
      billable model calls.
- [ ] Adapter wraps outbound text in
      `governance.redact_for_squad_boundary(...)`.
- [ ] Adapter persists results via `memory.append_episodic(...)` and
      returns handles, not blobs.
- [ ] Smoke run with `hydra run "..." --squad <slug>` produces a
      `DECISION_RECORD` and a non-empty trace.
- [ ] `/hydra:replay <workflow_id>` reconstructs the run end-to-end.

When all boxes are checked, change `entrypoint: stub` to the real
adapter name and update the status table in `HYDRA.md` section 1.

## g) Adding a Python MCP shim for a markdown-only squad

`agent-impersonation` and `claude-skill` packs (ExecutiveSuite, RLM,
etc.) are pure Claude-Code markdown — no Python daemon to wrap.
Hydra still exposes them through an MCP server so the dispatcher can
fetch live roster/skill metadata and persist outputs as
`MemoryRef` handles. Use this template:

```
mcp_servers/<slug>/
  __init__.py        # empty
  __main__.py        # from .server import main; main()
  server.py          # tool handlers; calls run_server(...)
```

`server.py` defers all stdio plumbing to
`mcp_servers/_pack_shim.py`, which provides:

- `resolve_root(env_var, default)` — sandbox path (override via
  `HYDRA_<SQUAD>_ROOT` so tests are hermetic).
- `list_dir(root, relative, suffix=…, only_dirs=…)` and
  `read_markdown(root, relative)` — read-only pack introspection
  with path-trust enforcement.
- `write_output(root, relative_dir, topic, content)` /
  `read_output(...)` — sandboxed writer that emits
  `{path, relative, bytes}` so dispatchers can synthesize a
  `MemoryRef(key=f"<slug>:output:{relative}")`.
- `run_server(name, handlers)` — preferred MCP SDK runner, with a
  bare-stdio JSON-RPC fallback when `mcp` is not installed or when
  `HYDRA_MCP_BARE=1` (used by `tests/test_pack_shims.py`).

Naming convention for tools: `<short-slug>.<resource>.<verb>`
(e.g. `es.roster.list`, `rlm.output.write`). Every shim must
expose a `<short-slug>.ping` no-arg tool — `hydra doctor` calls it
to verify reachability alongside `pp-daemon` and `hydra-memory`.

Register the new server in three places:

1. **`.mcp.json`** — add a stanza with
   `"command": "python", "args": ["-m", "mcp_servers.<slug>"]`
   and a `HYDRA_<SLUG>_ROOT` env var pointing at the source pack.
2. **`hooks.json`** — add a `PreToolUse` doctor matcher for
   `mcp__<slug>__.*` (and a `PostToolUse` audit stub for
   `*.output.write` if the shim writes anything).
3. **`hydra_core/squad_node.py`** — in the squad's dispatcher
   branch, call the shim via `_mcp_call_safe(...)` so the run
   degrades cleanly when the server is offline (synthetic
   `MemoryRef` fallback) instead of failing the workflow.

Reference shims: `mcp_servers/executive_suite/`,
`mcp_servers/rlm_creative/`. Reference tests:
`tests/test_pack_shims.py`.
