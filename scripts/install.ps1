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

Push-Location $root
try {
    Write-Host "Installing hydra-core (editable) ..."
    & python -m pip install -e ".[langgraph]" 2>&1 | Out-Host
}
finally {
    Pop-Location
}

Write-Host ""
Write-Host "Smoke-testing the registry ..."
& python -m hydra_core.cli doctor

Write-Host ""
Write-Host "Hydra installed."
Write-Host ""
Write-Host "Next steps:"
Write-Host "  1. Open Claude Code in this directory: cd $root"
Write-Host "  2. Verify the plugin loaded:   /hydra:squads"
Write-Host "  3. Run a smoke workflow:        /hydra:run 'Audit our GDPR posture'"
Write-Host "  4. Wire pair-programmer:        ensure node + the pp daemon are installed"
Write-Host "                                  (see C:\AiAppDeployments\pair-programmer)"
