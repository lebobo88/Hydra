# MCP Setup Guide

Hydra's connected systems communicate via MCP (Model Context Protocol) servers. Each system can run standalone or be unified through Hydra's gateway.

## Deployment Modes

### Standalone (no Hydra)

Each system registers its own MCP server directly in `~/.claude.json`:

```json
{
  "mcpServers": {
    "pp_harness": {
      "type": "stdio",
      "command": "node",
      "args": ["<path-to>/pair-programmer/daemon/dist/index.js", "mcp"]
    },
    "eights": {
      "type": "stdio",
      "command": "node",
      "args": ["<path-to>/TheEights/daemon/dist/index.js"],
      "env": {"EIGHTS_LOG_LEVEL": "info"}
    }
  }
}
```

Tools appear as `mcp__pp_harness__start_run`, `mcp__eights__memory_add`, etc.

### Gateway Mode (with Hydra)

Register only `hydra_gateway` in `~/.claude.json`. The gateway proxies all backend servers:

```json
{
  "mcpServers": {
    "hydra_gateway": {
      "type": "stdio",
      "command": "python",
      "args": ["-m", "mcp_servers.hydra_gateway"],
      "cwd": "<path-to>/Hydra",
      "env": {"PYTHONPATH": "<path-to>/Hydra"}
    }
  }
}
```

Tools appear as `mcp__hydra_gateway__pp_harness__start_run`, etc. The gateway:
- Discovers backends from `~/.hydra/backends.json`
- Proxies tool calls with RBAC enforcement
- Adds analytics tracking on every call
- Degrades gracefully when backends are unavailable

### Which Mode to Use

| Scenario | Mode | Registration |
|---|---|---|
| Single system (e.g., just pair-programmer) | Standalone | Register that system directly |
| Hydra + all connected systems | Gateway | Register only `hydra_gateway` |
| Hydra + some systems | Gateway | Register `hydra_gateway`; only installed backends appear |
| Mixed (not recommended) | — | Duplicate tools and ambiguous hook matching |

## Migration: Standalone → Gateway

```bash
# 1. Backup current config
python -m hydra_core.cli gateway-backup

# 2. Export backend specs to ~/.hydra/backends.json
python -m hydra_core.cli gateway-export-backends

# 3. Register gateway via Claude Code /mcp dialog
#    Add: hydra_gateway → python -m mcp_servers.hydra_gateway

# 4. Update hook matchers for gateway prefix
python -m hydra_core.cli gateway-migrate-hooks

# 5. Verify gateway health (start a new Claude Code session)
#    Call: mcp__hydra_gateway__gateway.health

# 6. Remove old backend entries from ~/.claude.json
python -m hydra_core.cli gateway-remove-old-backends
```

## Rollback: Gateway → Standalone

```bash
python -m hydra_core.cli gateway-rollback
# Restores ~/.claude.json and settings.json from the most recent backup
```

## Fresh Machine Setup

Run the portability bootstrap, which resolves `AIAPP_BASE`/`HYDRA_ROOT`
dynamically (no hardcoded paths) and generates `~/.hydra/backends.json` from
`scripts/backends.template.json`:

```powershell
# Windows / PowerShell
cd <AIAPP_BASE>\Hydra
.\scripts\setup.ps1
```

```bash
# macOS / Linux / Git Bash
cd <AIAPP_BASE>/Hydra
bash scripts/setup.sh
```

This also (re)creates the `squads/marketing-*` symlinks. See
[`PORTABILITY.md`](./PORTABILITY.md) for the full `AIAPP_BASE` convention and
resolution order.

```bash
# Alternatively, detect-and-generate via the CLI (legacy path):
python -m hydra_core.cli gateway-setup
```

## Two-Layer Registry Architecture

| Layer | File | Read by |
|---|---|---|
| Claude-visible | `~/.claude.json` mcpServers | Claude Code (tool discovery) |
| Backend registry | `~/.hydra/backends.json` | Hydra gateway + internal dispatcher |

In gateway mode, `~/.claude.json` contains only `hydra_gateway`. The actual backend specs live in `~/.hydra/backends.json`, which the gateway reads to discover and proxy to backends. Hydra's internal dispatcher (supervisor, judge, squad_node) also reads `backends.json` as a fallback, so internal Python-level calls still work.

`~/.hydra/backends.json` is machine-local and **never committed** (it holds resolved absolute paths). The committed source of truth is `scripts/backends.template.json`, which uses `{{AIAPP_BASE}}` / `{{HYDRA_ROOT}}` placeholders. Regenerate it by re-running `scripts/setup.{ps1,sh}` — never hand-edit absolute paths into the registry. See [`PORTABILITY.md`](./PORTABILITY.md).

### Referencing secrets from the environment

A backend spec's `env` map is handed verbatim to the child process, so writing a
secret directly into `~/.hydra/backends.json` leaves long-lived key material in
a plaintext config that every dispatcher reads and `hydra gateway-backup`
copies. Reference the launching environment instead:

```jsonc
"xenia_tickets": {
  "env": {
    "PYTHONPATH": "/path/to/Hydra",
    "HYDRA_OPERATOR_KEY": "${HYDRA_OPERATOR_KEY}",
    "PP_TIER": "${PP_TIER:-standard}"
  }
}
```

Both the gateway backend pool and Hydra's internal dispatcher resolve these
references through `hydra_core/backends_env.py` just before building the stdio
process parameters, so the two paths cannot drift. The same expansion applies to
`args`, `command`, and `cwd`.

| Form | Resolves to |
|---|---|
| `${VAR}` | the value of `VAR`, or `""` plus a WARNING naming the variable when unset |
| `${VAR:-default}` | the value of `VAR` when set and non-empty, otherwise `default` (never warns) |
| `$${VAR}` | the literal text `${VAR}` |

Expansion is fail-soft: a missing variable never crashes a dispatch. The backend
fails on its own terms instead, which for `HYDRA_OPERATOR_KEY` means WS-AUTH
degrades and Xenia's `send_response` / `execute_approved` reject.

Set the variable before starting Claude Code so it is inherited by the gateway:

```bash
export HYDRA_OPERATOR_KEY="…"      # macOS / Linux / Git Bash
```

```powershell
$env:HYDRA_OPERATOR_KEY = "…"      # PowerShell (or set it as a user env var)
```

`hydra gateway-export-backends` rewrites any inline 64-hex env value it finds to
the matching `${VAR}` reference on the way out, and `hydra doctor` warns when
`backends.json` still holds one. Both are shape checks — neither ever prints the
value. `hydra doctor` reports `source=env` when the operator key arrives through
a `${VAR}` reference, and `source=backends.json` only when it is still inline.

## Available Backends

The 16 backends below mirror `scripts/backends.template.json` — the **single
canonical template** for the backend registry. If the template and this table
disagree, the template wins; regenerate `~/.hydra/backends.json` from it via
`scripts/setup.{ps1,sh}`.

| Backend | Source | Standalone server | Required? |
|---|---|---|---|
| `pp_harness` | pair-programmer | Node.js daemon | No |
| `pp_codex` | pair-programmer | Node.js daemon | No |
| `pp_agy` | pair-programmer | Node.js daemon | No |
| `eights` | TheEights | Node.js daemon | No |
| `agentsmith` | AgentSmith | Node.js daemon | No |
| `hydra_memory` | Hydra (in-repo) | Python shim | Yes (with Hydra) |
| `executive_suite` | Hydra (in-repo) | Python shim | No |
| `rlm_creative` | Hydra (in-repo) | Python shim | No |
| `rlm_gaming` | Hydra (in-repo) | Python shim | No |
| `xenia` | Hydra (in-repo) | Python shim | No |
| `senate` | Hydra (in-repo) | Python shim | No |
| `xenia_kb` | Hydra (in-repo) | Python shim | No |
| `xenia_tickets` | Hydra (in-repo) | Python shim | No |
| `marketbliss` | Hydra (in-repo) | Python shim | No |
| `hydra_control` | Hydra (in-repo) | Python shim | No |
| `blender` | 3rd-party (`uvx blender-mcp`) | Python (uvx) | No (optional) |

`blender` is a newly registered optional 3rd-party backend (launched via
`uvx blender-mcp`, connecting to a local Blender instance on `127.0.0.1:9876`).
The former `hydra-cockpit` entry was removed — it was a dead non-MCP entry and
is not part of the registry.

## Gateway Connection Timeout

The gateway applies a per-backend connection timeout when opening a backend
stdio process. Tunable via:

| Level | Mechanism | Notes |
|---|---|---|
| Per-backend | `connect_timeout_s` key in the backend's `~/.hydra/backends.json` spec | Highest precedence |
| Global env | `HYDRA_GATEWAY_CONNECT_TIMEOUT_S` (float seconds) | Overrides built-in default |
| Built-in default | 20 s | Lowest precedence |

See `mcp_servers/hydra_gateway/server.py` `AsyncBackendPool._connect_timeout_for`.
