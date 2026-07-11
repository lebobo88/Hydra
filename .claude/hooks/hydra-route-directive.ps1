# hydra-route-directive.ps1 — UserPromptSubmit hook
# Classifies user prompt and emits mandatory/advisory routing directive.
# Fires on every prompt at user scope; degrades gracefully when ecosystem unavailable.

$ErrorActionPreference = 'SilentlyContinue'

$raw = $input | Out-String
if (-not $raw) { exit 0 }

try {
    $json = $raw | ConvertFrom-Json
    $prompt = $json.prompt
} catch {
    exit 0
}

if (-not $prompt) { exit 0 }

$promptLower = $prompt.ToLower().Trim()

# --- Meta: already a slash command or talking about the system ---
if ($promptLower.StartsWith('/')) { exit 0 }

# --- Check ecosystem availability (fast install-marker, NOT a WMI process scan) ---
# The previous check ran `Get-Process node | Where { $_.CommandLine -match ... }`.
# Reading .CommandLine forces a per-process WMI Win32_Process lookup (~6s with a
# handful of node procs, worse under a busy ecosystem) — this was the
# UserPromptSubmit 10s-timeout culprit. The pp daemon dist that every sibling pp
# hook invokes is a fast, faithful proxy: on disk => ecosystem installed at user
# scope. Base resolved portably (env override -> project dir -> hook-anchored),
# mirroring the other Hydra hooks.
$ecosystemAvailable = $false
$base = if ($env:AIAPP_BASE) { $env:AIAPP_BASE }
        elseif ($env:CLAUDE_PROJECT_DIR) { Split-Path $env:CLAUDE_PROJECT_DIR }
        else { Split-Path (Split-Path (Split-Path $PSScriptRoot)) }
if (Test-Path (Join-Path $base 'pair-programmer/daemon/dist/index.js')) {
    $ecosystemAvailable = $true
} elseif (Test-Path "$env:USERPROFILE\.pair-programmer\state.db") {
    # Fallback: pp's real state DB (paths.ts) — present once the daemon has run at least once.
    $ecosystemAvailable = $true
}

# --- Classify prompt ---
$actionPatterns = @(
    '\b(build|create|implement|fix|refactor|add|remove|deploy|migrate)\b',
    '\b(update|change|write|edit|scaffold|generate|design|plan)\b',
    '\b(review|test|security|debug|optimize|configure|setup|install)\b',
    '\b(delete|rename|move|copy|merge|rebase|commit|push)\b',
    '\b(wire|hook|register|integrate|connect|enable|disable)\b'
)

$queryPatterns = @(
    '^\s*(what|how|why|where|which|when|who|can you|does|is there|tell me|show me|explain|describe|list)\b',
    '\?\s*$'
)

$metaPatterns = @(
    '\b(hydra|slash command|hook|settings|claude\.md|agents\.md)\b'
)

$isAction = $false
$isQuery = $false
$isMeta = $false

foreach ($p in $metaPatterns) {
    if ($promptLower -match $p) { $isMeta = $true; break }
}

foreach ($p in $queryPatterns) {
    if ($promptLower -match $p) { $isQuery = $true; break }
}

foreach ($p in $actionPatterns) {
    if ($promptLower -match $p) { $isAction = $true; break }
}

# Action overrides query if both match (e.g., "can you fix this bug?")
if ($isAction -and $isQuery) { $isQuery = $false }

# Meta about the system itself — pass through
if ($isMeta -and -not $isAction) { exit 0 }

# --- Emit directive ---
if ($isAction) {
    if ($ecosystemAvailable) {
        Write-Output "[HYDRA ROUTING — MANDATORY]"
        Write-Output "Classification: ACTION"
        Write-Output "You MUST invoke /hydra:run BEFORE doing any work. Hydra routes to the correct squad(s)."
        Write-Output "  /hydra:run `"goal`" — primary entry point (routes to engineering, executive, garland, or multi-squad)"
        Write-Output "  /hydra:campaign `"goal`" — for cross-functional work spanning multiple squads"
        if ($env:HYDRA_HOST_DRIVEN -eq '1') {
            Write-Output "  Mode: ATTENDED (HYDRA_HOST_DRIVEN=1) — drive engineering in-context with visible engineer/judge subagents via /hydra:drive (plan -> step -> submit_host_result). Engine stays authoritative (ledger/budget/judge)."
        } else {
            Write-Output "  Mode: DETACHED (HYDRA_HOST_DRIVEN unset) — /hydra:run hands engineering to a headless background subprocess. Set HYDRA_HOST_DRIVEN=1 for attended follow-along."
        }
        Write-Output "Do NOT invoke /pp:run, /pp:team, or other PP commands directly. Hydra dispatches to pair-programmer via the engineering squad."
        Write-Output "Direct Edit/Write to ENGINE SOURCE (.ts/.py/.html/.css/...) is BLOCKED when HYDRA_ENFORCE_ROUTING=1 — the pair-programmer harness writes code, not you. Route engineering via /hydra:run. (Design docs / .md are allowed.)"
        Write-Output ""
        Write-Output "Also use proactively during the workflow:"
        Write-Output "  AgentSmith (mcp__agentsmith__*) — artifact validation, audit, inspection"
        Write-Output "  TheEights (mcp__eights__*) — memory queries, governance, evolution"
        Write-Output "  ExecutiveSuite (mcp__executive_suite__*) — strategic framing, executive briefs"
        Write-Output "  Hydra Memory (mcp__hydra_memory__*) — episodic recall, semantic search"
    } else {
        Write-Output "[HYDRA ROUTING — ADVISORY]"
        Write-Output "Hydra orchestration is available at user scope. Consider /hydra:run for structured work."
    }
} elseif ($isQuery) {
    if ($ecosystemAvailable) {
        Write-Output "[HYDRA ROUTING — ADVISORY]"
        Write-Output "This appears to be a question. You may answer directly using read-only tools."
        Write-Output "For any follow-up work that modifies files, invoke /hydra:run."
        Write-Output "Query TheEights (mcp__eights__*) and Hydra Memory (mcp__hydra_memory__*) for relevant prior context."
    }
}
# else: unclassified — pass through silently

exit 0
