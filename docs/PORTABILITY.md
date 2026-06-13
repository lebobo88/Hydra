# Portability — the `AIAPP_BASE` convention

Hydra and its sibling repos (MarketBliss, pair-programmer, TheEights, AgentSmith,
ExecutiveSuite, Senate, Xenia, RLM-Creative) must run on any machine without
machine-specific absolute paths baked into source. This document is the **source
of truth** other repos in the ecosystem follow.

## The convention

`AIAPP_BASE` = the directory that **contains all the ecosystem repos**.

```
<AIAPP_BASE>/
  Hydra/            <- this repo (HYDRA_ROOT)
  MarketBliss/
  pair-programmer/
  TheEights/
  AgentSmith/
  ExecutiveSuite/
  Senate/
  Xenia/
  RLM-Creative/
```

On the current authoring machine that happens to be `C:/AiAppDeployments`, but
**nothing in source may hardcode that**. Resolve it at runtime instead.

## Resolution order (everywhere)

Every module/script that needs to locate a repo MUST resolve in this order and
**fail loud** rather than guess:

1. **Per-repo env override** — `HYDRA_ROOT`, `HYDRA_XENIA_ROOT`, `HYDRA_MB_ROOT`,
   `HYDRA_ES_ROOT`, etc. If the relevant variable is set, use it verbatim.
2. **Anchor-relative auto-detect** — walk up from the module's own file location
   to the repo root, recognized by a sentinel (`.git/`, `CONSTITUTION.md`, or
   `package.json`). The setup scripts use their own location: PowerShell
   `Split-Path $PSScriptRoot`, bash `cd "$(dirname "$0")/.." && pwd`.
3. **Siblings** — under `AIAPP_BASE` if that env var is set; otherwise
   `dirname(detected_repo_root)`.
4. **Unresolved → FAIL LOUD.** Emit a clear error that names the missing env var.
   **Never** silently fall back to a literal `C:/AiAppDeployments` (or any other
   absolute path).

## Fresh-clone setup

After cloning the repos side-by-side under one parent directory:

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

The setup script:

1. Resolves `HYDRA_ROOT` from its own location (the parent of `scripts/`).
2. Resolves `AIAPP_BASE` from the env var, else the parent of `HYDRA_ROOT`.
3. (Re)creates the 5 `squads/marketing-*` symlinks into
   `<AIAPP_BASE>/MarketBliss/squads/marketing-*` (idempotent — skips correct
   links, repairs wrong ones, warns if MarketBliss is absent).
4. Generates `~/.hydra/backends.json` from `scripts/backends.template.json`,
   substituting `{{AIAPP_BASE}}` and `{{HYDRA_ROOT}}` with the resolved absolute
   paths (forward slashes, cross-platform). Backs up any existing file to
   `backends.json.bak` first.
5. Prints a summary and the `AIAPP_BASE` export you should persist.

Re-running is safe — every action is idempotent.

## Environment variables

| Var | Purpose | Resolution if unset |
|---|---|---|
| `AIAPP_BASE` | Directory containing all repos | `dirname(HYDRA_ROOT)` |
| `HYDRA_ROOT` | This repo's root | parent of `scripts/` |
| `HYDRA_MB_ROOT` | MarketBliss root | `<AIAPP_BASE>/MarketBliss` |
| `HYDRA_ES_ROOT` | ExecutiveSuite root | `<AIAPP_BASE>/ExecutiveSuite` |
| `HYDRA_RLM_ROOT` | RLM-Creative root | `<AIAPP_BASE>/RLM-Creative` |
| `HYDRA_XENIA_ROOT` | Xenia root | `<AIAPP_BASE>/Xenia` |
| `HYDRA_SENATE_ROOT` | Senate root | `<AIAPP_BASE>/Senate` |

Persist `AIAPP_BASE` so every repo and tool agrees on the base:

```powershell
setx AIAPP_BASE "<AIAPP_BASE>"        # Windows, persistent
$env:AIAPP_BASE = "<AIAPP_BASE>"      # Windows, current session
```

```bash
export AIAPP_BASE="<AIAPP_BASE>"      # add to ~/.bashrc or ~/.zshrc
```

## Symlinks

The 5 `squads/marketing-*` directories are **filesystem symlinks** into the
MarketBliss checkout. They are machine-local and git-ignored (see `.gitignore`);
`scripts/setup.{ps1,sh}` regenerates them on each machine.

**Windows note:** creating symlinks may require **Developer Mode**
(Settings → Privacy & security → For developers) or an elevated (Administrator)
PowerShell. If `New-Item -ItemType SymbolicLink` fails, the setup script prints a
hint and continues; the rest of setup still completes.

## `backends.json` regeneration

`~/.hydra/backends.json` is the machine-local backend registry the Hydra gateway
and internal dispatcher read. It is **never committed** — it holds resolved
absolute paths. The committed source of truth is `scripts/backends.template.json`,
which uses `{{AIAPP_BASE}}` / `{{HYDRA_ROOT}}` placeholders. The `${USERPROFILE}`
tokens in the env blocks are intentionally left intact — they are expanded at
runtime by the consuming process, not by the setup script.

To regenerate after moving repos or editing the template, just re-run setup.

## Cross-platform hooks (Windows + POSIX)

Path resolution across the ecosystem is OS-agnostic (env override → anchor-relative
→ sibling → fail loud, with forward slashes). The one remaining OS-specific surface
is the Claude Code **hooks**, which are authored in PowerShell (`.claude/hooks/*.ps1`)
and registered with `pwsh -NoProfile -File "$CLAUDE_PROJECT_DIR/.claude/hooks/<name>.ps1"`.

- The hook **commands no longer hardcode absolute paths** — every repo's hook
  registration now uses `$CLAUDE_PROJECT_DIR`, which Claude Code injects at
  hook-execution time. So the *path* is portable.
- To **run** the PowerShell hooks on macOS/Linux you need **PowerShell 7+ (`pwsh`)**
  installed and on `PATH` (`brew install powershell` / `apt install powershell`).
  `pwsh` is itself cross-platform, so the existing `.ps1` hooks run unmodified.
- Some repos already ship `.sh` siblings for their hooks (e.g. **Xenia**); **RLM-Creative**
  hooks already resolve their root via `$PSScriptRoot` and are portable as-is.
  Porting every `.ps1` hook to bash is intentionally **out of scope** here — the
  path-portability goal is met, and `pwsh` covers POSIX execution without
  duplicating hook logic.

## Known limitations / follow-ups

- **`pair-programmer/.claude/settings.json` is git-ignored** (machine-local), so its
  hook-path fix (`$CLAUDE_PROJECT_DIR/daemon/dist/index.js`) was applied to the local
  working copy but is **not tracked**. A fresh clone gets its own ignored copy. To
  propagate the portable form via git, the repo would need a tracked
  `settings.template.json` (or to un-ignore `settings.json`) that the daemon's hook
  installer renders with `$CLAUDE_PROJECT_DIR`. Flagged for a follow-up in that repo.
- A few `*.mcp.json.example` templates (e.g. in TheEights) still show an illustrative
  absolute path. They are examples, not runtime config; templatizing them is optional.
