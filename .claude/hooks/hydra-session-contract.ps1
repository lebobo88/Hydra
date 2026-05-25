# hydra-session-contract.ps1 — SessionStart hook
# Announces the Hydra enforcement contract at session start.

$ErrorActionPreference = 'SilentlyContinue'

$ecosystemAvailable = $false
try {
    $ppProcs = Get-Process -Name "node" -ErrorAction Stop |
        Where-Object { $_.CommandLine -and $_.CommandLine -match 'pair-programmer' }
    if ($ppProcs) { $ecosystemAvailable = $true }
} catch {}

if (-not $ecosystemAvailable) {
    if (Test-Path "$env:USERPROFILE\.pp\harness.db") {
        $ecosystemAvailable = $true
    }
}

if ($ecosystemAvailable) {
    Write-Output "[hydra] Hydra Enterprise Agent Mesh is ACTIVE."
    Write-Output "[hydra] ALL productive work must route through /hydra:run (not /pp:run)."
    Write-Output "[hydra] Available: /hydra:run, /hydra:campaign, /hydra:status, /hydra:squads, /hydra:approve, /hydra:resume, /hydra:replay, /hydra:budget, /hydra:add-squad"
    Write-Output "[hydra] Direct file edits are BLOCKED without an active Hydra workflow."
    Write-Output "[hydra] Connected systems (use proactively): AgentSmith, TheEights, ExecutiveSuite, Hydra Memory."
} else {
    Write-Output "[hydra] Hydra orchestration available but ecosystem not fully detected."
    Write-Output "[hydra] Start the PP daemon or work in advisory mode."
}

exit 0
