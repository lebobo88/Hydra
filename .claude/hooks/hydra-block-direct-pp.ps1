# hydra-block-direct-pp.ps1 — PreToolUse hook (matcher: Skill)
# Blocks direct /pp:* skill invocations. All PP work must route through Hydra.

$ErrorActionPreference = 'SilentlyContinue'

$raw = $input | Out-String
if (-not $raw) { exit 0 }

try {
    $json = $raw | ConvertFrom-Json
} catch {
    exit 0
}

$toolName = $json.tool_name
$toolInput = $json.tool_input

# Only intercept the Skill tool
if ($toolName -ne 'Skill') { exit 0 }

# Check if the skill being invoked is a pp:* command
$skillName = $null
if ($toolInput -and $toolInput.skill) {
    $skillName = $toolInput.skill
}

if ($skillName -and $skillName -match '^pp:') {
    Write-Output "[hydra] BLOCKED: Direct /$skillName invocation not allowed."
    Write-Output "[hydra] Use /hydra:run instead. Hydra dispatches to pair-programmer via the engineering squad."
    Write-Output "[hydra] Example: /hydra:run `"your goal here`""
    exit 2
}

exit 0
