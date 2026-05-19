# Hydra — Enterprise Agent Mesh

Hydra is a Claude Code plugin plus a Python LangGraph supervisor that routes
work across heterogeneous AI agent squads — executive (C-suite), engineering,
creative production, and stubs for legal, healthcare, sales, research, and
customer support. It is the central state engine that classifies a goal,
decomposes it into typed cross-squad messages, dispatches subgraphs in
parallel, and synthesizes the result into a single decision record. Hydra
does not re-implement the underlying squad packs — it sits above them and
delegates.

The squads themselves remain independently shippable: the engineering squad
delegates to the **pair-programmer (PP)** harness; the executive squad
delegates to **ExecutiveSuite (ES)** via Claude Code's sub-agent
impersonation pattern; the creative squad delegates to **RLM-CLI-Starter**
via Claude Code skills. New squads are added by dropping a folder under
`squads/` — no Hydra-core change required. See `HYDRA.md` for the full
architecture spec and `ARCHITECTURE.md` for the engineering view.

## Prerequisites

- Python 3.11 or newer
- Node.js 20 or newer (Claude Code CLI runtime)
- Claude Code CLI installed and authenticated
- Optional: `langgraph` (`pip install langgraph`) — without it, Hydra falls
  back to a pure-python supervisor runner with the same state contract
- Optional: `chromadb` for the semantic memory tier (default vector store)

## Quick install

Hydra ships as a Claude Code plugin distributed via a **local marketplace** that
lives in the repo itself (`.claude-plugin/marketplace.json`). Installation is a
two-part process: install the Python runtime, then register the plugin with
Claude Code so its slash commands, agents, skills, and MCP servers are
available in any project session.

### 1. Install the Python runtime

```powershell
# clone or sit at the existing checkout
cd C:\AiAppDeployments\Hydra

# install python deps + create %USERPROFILE%\.hydra state dir
.\scripts\install.ps1
```

This pip-installs `hydra-core` in editable mode and runs
`python -m hydra_core.cli doctor` to confirm squads are discovered.

### 2. Install the plugin into Claude Code (user scope)

Inside any Claude Code session, from any directory:

```
/plugin marketplace add C:\AiAppDeployments\Hydra
/plugin install hydra@hydra-local
/reload-plugins
```

This registers the local marketplace, copies the plugin into
`%USERPROFILE%\.claude\plugins\cache\hydra-local\hydra\<version>\`, and enables
it in `%USERPROFILE%\.claude\settings.json`. Slash commands are now available
in every Claude Code session, not just sessions opened from the Hydra repo.

Verify with:

```
/mcp                   # expect 4 servers connected: pp-daemon, hydra-memory, executive-suite, rlm-creative
/hydra:hydra-squads    # expect 8 squads listed
/doctor                # expect 0 plugin errors
```

### Updating after edits

Because Claude Code copies the plugin into its cache, edits to the repo do not
auto-propagate. After bumping the `version` field in
`.claude-plugin/plugin.json`:

```
/plugin marketplace update hydra-local
/plugin install hydra@hydra-local       # or /plugin update hydra@hydra-local
/reload-plugins
```

For active development with live edits, launch Claude Code with `--plugin-dir`
instead of relying on the installed copy:

```powershell
claude --plugin-dir C:\AiAppDeployments\Hydra
```

`hydra doctor` lists every discovered squad pack, confirms `pydantic` is
importable, and warns if `langgraph` is missing. A clean output looks like:

```
OK: 8 squad(s) discovered:
  ✓ executive             entrypoint=agent-impersonation  agents=24  industries=['cross-industry-strategic', 'finance', 'mna']
  ✓ engineering           entrypoint=mcp                  agents=5   industries=['software', 'saas', 'ai-platform']
  ✓ creative              entrypoint=claude-skill         agents=7   industries=['media', 'marketing', 'advertising']
  · legal-compliance      entrypoint=stub                 agents=6   industries=['legal', 'compliance', 'governance']
  ...
OK: langgraph installed
OK: pydantic available
```

## Slash commands

Once the plugin is registered with Claude Code, the following commands are
available in any project session:

| Command | Purpose |
|---|---|
| `/hydra:run "<goal>"` | Start a new workflow. Goes through intake -> planning -> approval -> dispatch -> synthesis. |
| `/hydra:status [<workflow_id>]` | List recent workflows, or dump the trace for one. |
| `/hydra:squads` | Show the discovered squad registry (slugs, entrypoints, accepts/emits). |
| `/hydra:approve <workflow_id>` | Resume a workflow paused at the HITL approval gate. |
| `/hydra:resume <workflow_id>` | Resume a workflow from its last checkpoint (e.g. after a crash). |
| `/hydra:budget [<workflow_id>]` | Show the budget ledger — usd spent, tokens, per-squad share, downgrade events. |
| `/hydra:campaign <name>` | Open or attach a long-running multi-workflow campaign (shared episodic memory). |
| `/hydra:replay <workflow_id>` | Reconstruct prompts, model versions, and artifact hashes for a deterministic replay. |
| `/hydra:add-squad <slug>` | Scaffold a new `squads/<slug>/squad.yaml` from the stub template. |

The CLI mirrors a subset of these for headless use:

```powershell
python -m hydra_core.cli run "Refactor billing service auth" --squad engineering
python -m hydra_core.cli status
python -m hydra_core.cli trace 7f3c...-...
```

## End-to-end example

User goal:

> Launch a Q3 campaign for the new billing microservice — needs a press kit,
> in-app announcement, and pricing-page update.

What happens:

1. `/hydra:run "..."` invokes intake. The router (`hydra_core/router.py`)
   keyword-matches `campaign`, `press kit`, `pricing`, `microservice` and
   selects `executive` + `engineering` + `creative`.
2. The planner emits a `CSuiteDecisionPacket`. ExecutiveSuite's `boardroom`
   pattern (CEO + CFO + CMO impersonation) produces a `DECISION_RECORD`
   with a per-squad budget split.
3. The approval gate triggers an HITL interrupt. The user runs
   `/hydra:approve <workflow_id>` after reviewing the budget.
4. The dispatcher fans out in parallel:
   - `creative` receives a `CreativeBrief` and invokes RLM skills
     (`/rlm-team`, `gemini-image`, `comfyui`) — emits `AssetJob` results.
   - `engineering` receives a `PRD` and invokes pair-programmer
     (`/pp:team feature-team`) — emits a PR url plus smoke-test pass.
   - `executive/cfo` parks a dependency to re-validate pricing once both
     squads finish.
5. The synthesizer consolidates into a `DECISION_RECORD v2` with a go-live
   runbook, appends to episodic memory, and patches `PROJECT_MASTER.md`.

The full trace is at `<project>/.hydra/<workflow_id>/trace.jsonl` and can
be replayed with `/hydra:replay`.

## Where to learn more

- `HYDRA.md` — top-level architecture spec (squads, state graph, governance).
- `ARCHITECTURE.md` — engineering deep-dive (state machine, schemas, MCP
  host, failure modes).
- `CONTRIBUTING-SQUADS.md` — how to scaffold and activate a new squad pack.
- `Enterprise Master AI Orchestration System Architecture.md` — the upstream
  research doc Hydra implements.
- `squads/<slug>/squad.yaml` — the canonical declaration of every active or
  stub squad.
- `hydra_core/` — the supervisor, router, governance plane, and memory
  fabric in Python.

## What Hydra is not

Hydra is not a code generator, not a model gateway, and not a competitor to
PP/ES/RLM. It is a routing layer with strong typing, checkpointed state,
human-in-the-loop interrupts, and a budget ledger — nothing more.
