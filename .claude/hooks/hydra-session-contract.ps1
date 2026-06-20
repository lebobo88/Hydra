# hydra-session-contract.ps1 -- SessionStart hook
# Announces the Hydra enforcement contract at session start.

$ErrorActionPreference = 'SilentlyContinue'

$ecosystemAvailable = $false
try {
    $ppProcs = Get-Process -Name 'node' -ErrorAction Stop |
        Where-Object { $_.CommandLine -and $_.CommandLine -match 'pair-programmer' }
    if ($ppProcs) { $ecosystemAvailable = $true }
} catch {}

if (-not $ecosystemAvailable) {
    if (Test-Path "$env:USERPROFILE\.pp\harness.db") {
        $ecosystemAvailable = $true
    }
}

if ($ecosystemAvailable) {
    try {
        $cwd = (Get-Location).Path
        $agentsPath = Join-Path $cwd 'AGENTS.md'
        $claudePath = Join-Path $cwd 'CLAUDE.md'
        $looksLikeRepo = (Test-Path (Join-Path $cwd '.harness')) -or (Test-Path (Join-Path $cwd '.git'))

        if (-not $looksLikeRepo -and $env:AIAPP_BASE) {
            $cwdFull = [System.IO.Path]::GetFullPath($cwd)
            $baseFull = [System.IO.Path]::GetFullPath($env:AIAPP_BASE)
            $basePrefix = $baseFull.TrimEnd('\', '/') + '\'
            if (
                $cwdFull.Equals($baseFull, [System.StringComparison]::OrdinalIgnoreCase) -or
                $cwdFull.StartsWith($basePrefix, [System.StringComparison]::OrdinalIgnoreCase)
            ) {
                $looksLikeRepo = $true
            }
        }

        if ($looksLikeRepo -and -not (Test-Path $agentsPath)) {
            Set-Content -Path $agentsPath -Value @(
                '# AGENTS'
                ''
                'Hydra bootstrap placeholder. Add repository-specific agent guidance here.'
            )
            if ((Test-Path $agentsPath) -and -not (Test-Path $claudePath)) {
                Set-Content -Path $claudePath -Value '@AGENTS.md'
            }
        }
    } catch {}

    Write-Output '[hydra] Hydra Enterprise Agent Mesh is ACTIVE.'
    Write-Output '[hydra] ALL productive work must route through /hydra:run (not /pp:run).'
    Write-Output '[hydra] Available: /hydra:run, /hydra:campaign, /hydra:status, /hydra:squads, /hydra:approve, /hydra:resume, /hydra:replay, /hydra:budget, /hydra:add-squad'
    Write-Output '[hydra] Direct file edits are BLOCKED without an active Hydra workflow.'
    Write-Output '[hydra] Connected systems (use proactively): AgentSmith, TheEights, ExecutiveSuite, Hydra Memory.'
} else {
    Write-Output '[hydra] Hydra orchestration available but ecosystem not fully detected.'
    Write-Output '[hydra] Start the PP daemon or work in advisory mode.'
}

exit 0
