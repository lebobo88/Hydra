# Hydra installer — Windows PowerShell
# Creates ~/.hydra, installs hydra_core in editable mode, prints next-steps.

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
Write-Host "Installing Hydra from $root"

$hydraDir = Join-Path $env:USERPROFILE ".hydra"
if (-not (Test-Path $hydraDir)) {
    New-Item -ItemType Directory -Path $hydraDir | Out-Null
    Write-Host "Created $hydraDir"
}

# hydra-core declares requires-python >= 3.11. Prefer the Windows launcher
# pinned to a known-good version, probing that the pin actually resolves
# (the launcher may be installed without that version); fall back to 3.11,
# then bare `python`.
$py = $null
foreach ($candidate in @(@('py', '-3.12'), @('py', '-3.11'), @('python'))) {
    $cmd = $candidate[0]
    if (-not (Get-Command $cmd -ErrorAction SilentlyContinue)) { continue }
    & $cmd @(@($candidate | Select-Object -Skip 1) + @('-c', 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)')) 2>$null
    if ($LASTEXITCODE -eq 0) { $py = $candidate; break }
}
if (-not $py) {
    Write-Error "No Python >= 3.11 found (tried: py -3.12, py -3.11, python). Install Python 3.11+ first."
    exit 1
}
$pyCmd = $py[0]; $pyArgs = @($py | Select-Object -Skip 1)
Write-Host "Using interpreter: $($py -join ' ')"

Push-Location $root
try {
    Write-Host "Installing hydra-core (editable) ..."
    & $pyCmd @($pyArgs + @('-m', 'pip', 'install', '-e', '.[langgraph]')) 2>&1 | Out-Host
}
finally {
    Pop-Location
}

Write-Host ""
Write-Host "Smoke-testing the registry ..."
& $pyCmd @($pyArgs + @('-m', 'hydra_core.cli', 'doctor'))

Write-Host ""
Write-Host "Hydra installed."
Write-Host ""
Write-Host "Next steps (run these inside Claude Code, any directory):"
Write-Host "  /plugin marketplace add $root"
Write-Host "  /plugin install hydra@hydra-local"
Write-Host "  /reload-plugins"
Write-Host ""
Write-Host "Then verify with:"
Write-Host "  /mcp                  (4 servers connected)"
Write-Host "  /hydra:hydra-squads   (13 squads listed)"
Write-Host "  /doctor               (0 plugin errors)"
Write-Host ""
Write-Host "Wire pair-programmer separately: ensure node + the pp daemon are installed"
Write-Host "(see https://github.com/lebobo88/pair-programmer)"
