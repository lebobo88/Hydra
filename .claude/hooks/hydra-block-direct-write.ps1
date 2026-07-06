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
        # Resolve project root: CLAUDE_PROJECT_DIR if set, else 2 levels up from PSScriptRoot.
        $_projRoot = $env:CLAUDE_PROJECT_DIR
        if (-not $_projRoot) { $_projRoot = Split-Path (Split-Path $PSScriptRoot) }
        if ($_projRoot) {
            # Marker 1: any attended-* worktree directory under .harness\worktrees
            $_wtDir = Join-Path $_projRoot '.harness\worktrees'
            if ((Test-Path $_wtDir -PathType Container) -and
                (Get-ChildItem -Path $_wtDir -Directory -Filter 'attended-*' -ErrorAction SilentlyContinue)) {
                $_stagedActive = $true
            }
            # Marker 2: .harness\stage-active sentinel file
            if (-not $_stagedActive -and
                (Test-Path (Join-Path $_projRoot '.harness\stage-active') -PathType Leaf)) {
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
    # Hook lives at <HydraRoot>\.claude\hooks\ — three Split-Ups: hooks ->
    # .claude -> HydraRoot -> base (the dir that holds Hydra and pair-programmer).
    $base = Split-Path (Split-Path (Split-Path $PSScriptRoot))
}
$ppRepo = (Join-Path $base 'pair-programmer').Replace('/', '\').TrimEnd('\').ToLowerInvariant()
if ($norm -eq $ppRepo -or $norm.StartsWith("$ppRepo\")) { exit 0 }

# --- Allow harness / worktree / vcs / build dirs (where pp writes candidates) -
# Segment-bounded fragments (each begins AND ends with '\') so a stray filename
# like 'worktree.ts' or a dir 'myworktreehack' can NOT bypass the block — the
# pp engineer's candidate worktrees live under '.harness\worktrees\' /
# '.claude\worktrees\' (codex review item 4).
$allowDirFragments = @(
    '\.harness\', '\.hydra\', '\worktrees\', '\node_modules\', '\.git\',
    '\dist\', '\build\', '\__pycache__\', '\.venv\', '\site-packages\'
)
foreach ($frag in $allowDirFragments) {
    if ($norm.Contains($frag)) { exit 0 }
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
