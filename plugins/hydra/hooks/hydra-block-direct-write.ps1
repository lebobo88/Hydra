# hydra-block-direct-write.ps1 — PreToolUse hook (matcher: Write|Edit|NotebookEdit)
#
# Closes the gap the codex review surfaced: nothing actually blocked the
# supervisor LLM from hand-writing engine source (it wrote index.html/styles.css
# directly in a campaign with zero resistance). Engineering code MUST be produced
# by the pair-programmer harness (via /hydra:run -> ingest -> pp stage loop),
# never by the supervisor's own Write/Edit.
#
# Policy (only when HYDRA_ENFORCE_ROUTING=1):
#   BLOCK  a Write/Edit/NotebookEdit whose target is an ENGINE-SOURCE file.
#   ALLOW  design docs / prose / config (.md/.txt/.json/.yaml/...), so the
#          in-host design agents can still persist GDD.md and squad docs.
#   ALLOW  anything under a harness / worktree / vcs / build dir — that is where
#          the legitimate pp `engineer` generator writes candidate code.
#   ALLOW  when HYDRA_PP_STAGE_ACTIVE=1 — the harness sets this while an engineer
#          stage is generating, so the deterministic codegen path is never blocked.
#   EXEMPT the pair-programmer repo itself (working there with code is legal).
#
# Kill-switch: set HYDRA_ENFORCE_ROUTING to anything but '1' to disable.

$ErrorActionPreference = 'SilentlyContinue'

if ($env:HYDRA_ENFORCE_ROUTING -ne '1') { exit 0 }
# RA-1: Harness-driven engineer stage: only bypass when a real active-stage marker
# exists. A bare HYDRA_PP_STAGE_ACTIVE=1 (leaked or set outside a stage) must NOT
# silently disable enforcement session-wide.
if ($env:HYDRA_PP_STAGE_ACTIVE -eq '1') {
    $_stagedActive = $false
    try {
        # Resolve project root: CLAUDE_PROJECT_DIR if set, else 3 levels up from
        # the canonical plugins/hydra/hooks directory.
        $_projRoot = $env:CLAUDE_PROJECT_DIR
        if (-not $_projRoot) { $_projRoot = Split-Path (Split-Path (Split-Path $PSScriptRoot)) }
        if ($_projRoot) {
            # Run-scoped stage marker: hydra_core.host_bridge.begin_stage WRITES
            # .harness\stage-active at stage start and CLEARS it at finalize/abort,
            # so its presence is tied to the CURRENT active run only. The old
            # "Marker 1" (any attended-* worktree directory exists under
            # .harness\worktrees) is retired: stale worktrees accumulate across
            # completed/aborted runs (17 were observed live in one session) and
            # that check became permanently true, silently disabling enforcement
            # repo-wide. Directory enumeration is no longer trusted; the sentinel
            # written by the harness is the sole source of truth.
            if (Test-Path (Join-Path $_projRoot '.harness\stage-active') -PathType Leaf) {
                $_stagedActive = $true
            }
        }
    } catch { $_stagedActive = $true }   # internal error → preserve old bypass behavior
    if ($_stagedActive) { exit 0 }
    Write-Host '[hydra-hook] bare HYDRA_PP_STAGE_ACTIVE=1 ignored (no active stage marker) — enforcement stays ON'
}

$raw = $input | Out-String
if (-not $raw) { exit 0 }
try { $json = $raw | ConvertFrom-Json } catch { exit 0 }

$tool = "$($json.tool_name)"
if ($tool -ne 'Write' -and $tool -ne 'Edit' -and $tool -ne 'NotebookEdit') { exit 0 }

# --- Resolve the target path from the tool input (Write/Edit/NotebookEdit) ----
$target = $null
if ($json.tool_input) {
    if ($json.tool_input.file_path)     { $target = "$($json.tool_input.file_path)" }
    elseif ($json.tool_input.notebook_path) { $target = "$($json.tool_input.notebook_path)" }
    elseif ($json.tool_input.path)      { $target = "$($json.tool_input.path)" }
}
if (-not $target) { exit 0 }
$norm = $target.Trim().Replace('/', '\').ToLowerInvariant()

# --- Exempt the pair-programmer repo itself (portable base resolution) --------
$base = $null
if ($env:AIAPP_BASE) {
    $base = $env:AIAPP_BASE
} elseif ($env:CLAUDE_PROJECT_DIR) {
    $base = Split-Path $env:CLAUDE_PROJECT_DIR
} else {
    # Hook lives at <HydraRoot>\plugins\hydra\hooks\ — four Split-Ups give
    # the base directory that holds Hydra and pair-programmer.
    $base = Split-Path (Split-Path (Split-Path (Split-Path $PSScriptRoot)))
}
$ppRepo = (Join-Path $base 'pair-programmer').Replace('/', '\').TrimEnd('\').ToLowerInvariant()
if ($norm -eq $ppRepo -or $norm.StartsWith("$ppRepo\")) { exit 0 }

# --- Allow harness / worktree / vcs / build dirs (where pp writes candidates) -
# Segment-bounded fragments (each begins AND ends with '\') so a stray filename
# like 'worktree.ts' or a dir 'myworktreehack' can NOT bypass the block — the
# pp engineer's candidate worktrees live under '.harness\worktrees\' /
# '.claude\worktrees\' (codex review item 4).
#
# Anchored (2026-08): a bare $norm.Contains($frag) matched ANY path anywhere on
# disk that merely contained one of these segments (e.g. 'C:\elsewhere\dist\x.py'
# would bypass). The allow-list now requires the target to resolve UNDER the
# project root, OR under an explicit worktree root, before the fragment check
# is even consulted. HYDRA_WORKTREE_ROOT overrides the default
# '<projectRoot>\.harness\worktrees' resolution for non-standard layouts.
$_arProjRoot = $env:CLAUDE_PROJECT_DIR
if (-not $_arProjRoot) { $_arProjRoot = Split-Path (Split-Path (Split-Path $PSScriptRoot)) }
$_arProjRootNorm = $null
if ($_arProjRoot) {
    $_arResolved = (Resolve-Path -LiteralPath $_arProjRoot -ErrorAction SilentlyContinue)
    if ($_arResolved) { $_arProjRootNorm = $_arResolved.Path.Replace('/', '\').TrimEnd('\').ToLowerInvariant() }
}
$_arWorktreeRootNorm = $null
if ($env:HYDRA_WORKTREE_ROOT) {
    $_arWtResolved = (Resolve-Path -LiteralPath $env:HYDRA_WORKTREE_ROOT -ErrorAction SilentlyContinue)
    if ($_arWtResolved) { $_arWorktreeRootNorm = $_arWtResolved.Path.Replace('/', '\').TrimEnd('\').ToLowerInvariant() }
} elseif ($_arProjRootNorm) {
    $_arWorktreeRootNorm = "$_arProjRootNorm\.harness\worktrees"
}
$_arUnderProjRoot = $_arProjRootNorm -and ($norm -eq $_arProjRootNorm -or $norm.StartsWith("$_arProjRootNorm\"))
$_arUnderWorktreeRoot = $_arWorktreeRootNorm -and ($norm -eq $_arWorktreeRootNorm -or $norm.StartsWith("$_arWorktreeRootNorm\"))

$allowDirFragments = @(
    '\.harness\', '\.hydra\', '\worktrees\', '\node_modules\', '\.git\',
    '\dist\', '\build\', '\__pycache__\', '\.venv\', '\site-packages\'
)
if ($_arUnderProjRoot -or $_arUnderWorktreeRoot) {
    foreach ($frag in $allowDirFragments) {
        if ($norm.Contains($frag)) { exit 0 }
    }
}

# --- Allow prose / docs / config so in-host design agents can persist GDDs ----
$allowExt = @(
    '.md', '.markdown', '.mdx', '.txt', '.rst', '.json', '.jsonc', '.yaml', '.yml',
    '.toml', '.ini', '.cfg', '.csv', '.lock', '.gitignore', '.env', '.example'
)
$ext = [System.IO.Path]::GetExtension($norm)
if (-not $ext) { exit 0 }                       # extensionless (LICENSE, etc.)
if ($allowExt -contains $ext) { exit 0 }

# --- Block engine-source extensions ------------------------------------------
$blockExt = @(
    '.ts', '.tsx', '.js', '.jsx', '.mjs', '.cjs', '.py', '.go', '.rs', '.java',
    '.kt', '.kts', '.c', '.cc', '.cpp', '.cxx', '.h', '.hpp', '.cs', '.rb', '.php',
    '.swift', '.m', '.mm', '.vue', '.svelte', '.html', '.htm', '.css', '.scss',
    '.sass', '.less', '.sql', '.sh', '.bash', '.lua', '.gd', '.glsl', '.hlsl',
    '.shader', '.dart', '.scala', '.clj', '.ex', '.exs'
)
if ($blockExt -contains $ext) {
    [Console]::Error.WriteLine("[hydra] BLOCKED: direct $tool to engine source '$target'.")
    [Console]::Error.WriteLine("[hydra] Engineering code MUST go through the pair-programmer harness, not a supervisor Write.")
    [Console]::Error.WriteLine("[hydra] Route it: /hydra:run `"<goal>`" (or submit a DEV_TASK via the ingest bridge).")
    [Console]::Error.WriteLine("[hydra] Design docs (.md) are allowed. Kill-switch: set HYDRA_ENFORCE_ROUTING != 1.")
    exit 2
}

exit 0
