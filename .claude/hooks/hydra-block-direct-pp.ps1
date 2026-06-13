# hydra-block-direct-pp.ps1 — PreToolUse hook (matcher: Skill)
# Blocks direct /pp:* action-skill invocations. All PP work must route through Hydra.
# Read-only pp skills are allowed; the pair-programmer repo itself is exempt.
# Kill-switch: set HYDRA_ENFORCE_ROUTING to anything but '1' to disable.

$ErrorActionPreference = 'SilentlyContinue'

# --- Enforcement gate -------------------------------------------------------
if ($env:HYDRA_ENFORCE_ROUTING -ne '1') { exit 0 }

$raw = $input | Out-String
if (-not $raw) { exit 0 }

try {
    $json = $raw | ConvertFrom-Json
} catch {
    exit 0
}

# Only intercept the Skill tool
if ($json.tool_name -ne 'Skill') { exit 0 }

# --- Exempt the pair-programmer repo itself ---------------------------------
# Working directly in the PP repo with /pp:* stays legal. Equality or
# prefix-plus-separator only — a bare StartsWith would also exempt siblings
# like pair-programmer-old.
# Resolve the AI-app base dynamically so the path is portable across machines.
# Preference order: AIAPP_BASE env -> parent of CLAUDE_PROJECT_DIR -> parent of
# the Hydra repo root derived from $PSScriptRoot. The hook lives at
# <HydraRoot>/.claude/hooks/, so three Split-Ups give HydraRoot and one more
# gives the base directory that holds both Hydra and pair-programmer.
$base = $null
if ($env:AIAPP_BASE) {
    $base = $env:AIAPP_BASE
} elseif ($env:CLAUDE_PROJECT_DIR) {
    $base = Split-Path $env:CLAUDE_PROJECT_DIR
} else {
    $hydraRoot = Split-Path (Split-Path (Split-Path $PSScriptRoot))
    $base = Split-Path $hydraRoot
}
$ppRepo = (Join-Path $base 'pair-programmer').Replace('/', '\').TrimEnd('\').ToLowerInvariant()
$cwd = $json.cwd
if (-not $cwd) { $cwd = (Get-Location).Path }
if ($cwd) {
    $norm = $cwd.Trim().Replace('/', '\').TrimEnd('\').ToLowerInvariant()
    if ($norm -eq $ppRepo -or $norm.StartsWith("$ppRepo\")) { exit 0 }
}

# --- Normalize the skill name ------------------------------------------------
$skillName = $null
if ($json.tool_input -and $json.tool_input.skill) {
    $skillName = "$($json.tool_input.skill)".Trim().TrimStart('/')
    $skillName = ($skillName -split '\s+')[0].ToLowerInvariant()
}
if (-not $skillName) { exit 0 }

# --- Allow read-only pp skills -----------------------------------------------
$readOnlyPp = @(
    'pp:status', 'pp:doctor', 'pp:budget', 'pp:checklist', 'pp:master',
    'pp:profile', 'pp:rubrics', 'pp:taxonomy', 'pp:teams', 'pp:claudemd',
    'pp:constitution'
)
if ($readOnlyPp -contains $skillName) { exit 0 }

# --- Block pp action skills ----------------------------------------------------
# Exit 2 blocks the tool call; Claude Code feeds STDERR (not stdout) back to
# the model, so the redirect message must go to the error stream.
if ($skillName -match '^pp:') {
    [Console]::Error.WriteLine("[hydra] BLOCKED: Direct /$skillName invocation not allowed.")
    [Console]::Error.WriteLine("[hydra] Use /hydra:run instead. Hydra dispatches to pair-programmer via the engineering squad.")
    [Console]::Error.WriteLine("[hydra] Example: /hydra:run `"your goal here`"")
    exit 2
}

exit 0
