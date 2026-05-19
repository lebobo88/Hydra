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
Write-Host "Next steps (run these inside Claude Code, any directory):"
Write-Host "  /plugin marketplace add $root"
Write-Host "  /plugin install hydra@hydra-local"
Write-Host "  /reload-plugins"
Write-Host ""
Write-Host "Then verify with:"
Write-Host "  /mcp                  (4 servers connected)"
Write-Host "  /hydra:hydra-squads   (8 squads listed)"
Write-Host "  /doctor               (0 plugin errors)"
Write-Host ""
Write-Host "Wire pair-programmer separately: ensure node + the pp daemon are installed"
Write-Host "(see C:\AiAppDeployments\pair-programmer)"
