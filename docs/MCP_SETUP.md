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

## Available Backends

| Backend | Source | Standalone server | Required? |
|---|---|---|---|
| `pp_harness` | pair-programmer | Node.js daemon | No |
| `pp_codex` | pair-programmer | Node.js daemon | No |
| `pp_gemini` | pair-programmer | Node.js daemon | No |
| `eights` | TheEights | Node.js daemon | No |
| `agentsmith` | AgentSmith | Node.js daemon | No |
| `hydra_memory` | Hydra (in-repo) | Python shim | Yes (with Hydra) |
| `executive_suite` | Hydra (in-repo) | Python shim | No |
| `rlm_creative` | Hydra (in-repo) | Python shim | No |
| `senate` | Hydra (in-repo) | Python shim | No |
