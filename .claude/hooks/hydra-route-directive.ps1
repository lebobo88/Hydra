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

# --- Check ecosystem availability ---
$ecosystemAvailable = $false
try {
    $ppProcs = Get-Process -Name "node" -ErrorAction Stop |
        Where-Object { $_.CommandLine -and $_.CommandLine -match 'pair-programmer' }
    if ($ppProcs) { $ecosystemAvailable = $true }
} catch {}

# If ecosystem not available, try a lighter check (daemon port or harness DB)
if (-not $ecosystemAvailable) {
    if (Test-Path "$env:USERPROFILE\.pp\harness.db") {
        $ecosystemAvailable = $true
    }
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
        Write-Output "Do NOT invoke /pp:run, /pp:team, or other PP commands directly. Hydra dispatches to pair-programmer via the engineering squad."
        Write-Output "Direct Edit/Write calls WILL BE BLOCKED without an active workflow."
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
