#requires -Version 5.1
<#
.SYNOPSIS
    Hydra portability bootstrap (Windows / PowerShell).

.DESCRIPTION
    Implements the AIAPP_BASE convention for the Hydra ecosystem so the repo
    locates itself + siblings dynamically, with NO machine-specific absolute
    paths baked into source. Mirror of scripts/setup.sh.

    Resolution order:
      1. Per-repo env override (AIAPP_BASE).
      2. Anchor-relative auto-detect: HYDRA_ROOT = parent of this scripts/ dir.
      3. Siblings under AIAPP_BASE env, else dirname(HYDRA_ROOT).
      4. If unresolved -> FAIL LOUD naming the env var. Never fall back to a
         literal C:/AiAppDeployments.

    Actions (all idempotent):
      - (Re)create the 5 squads/marketing-* symlinks into MarketBliss.
      - Generate ~/.hydra/backends.json from scripts/backends.template.json,
        substituting {{AIAPP_BASE}} and {{HYDRA_ROOT}}.

.NOTES
    Symlink creation on Windows may require Developer Mode or an elevated shell.
    A clear hint is printed if creation fails.
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'

function Write-Section($msg) { Write-Host ""; Write-Host "== $msg ==" -ForegroundColor Cyan }
function Write-Ok($msg)      { Write-Host "  [ok]   $msg" -ForegroundColor Green }
function Write-Skip($msg)    { Write-Host "  [skip] $msg" -ForegroundColor DarkGray }
function Write-Warn2($msg)   { Write-Host "  [warn] $msg" -ForegroundColor Yellow }

# Normalize a path to forward slashes (cross-platform consistency in emitted JSON).
function ConvertTo-ForwardSlash([string]$p) { return ($p -replace '\\', '/') }

# --- (2) HYDRA_ROOT = parent of this scripts/ dir, from the script's own location.
$ScriptDir = $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($ScriptDir)) {
    throw "Cannot resolve `$PSScriptRoot. Run this as a saved .ps1 file (not pasted into the console)."
}
$HydraRoot = (Resolve-Path (Join-Path $ScriptDir '..')).Path

# --- (1)/(3) AIAPP_BASE = env override, else parent of HYDRA_ROOT.
$AiappBase = $env:AIAPP_BASE
if ([string]::IsNullOrWhiteSpace($AiappBase)) {
    $parent = Split-Path -Parent $HydraRoot
    if ([string]::IsNullOrWhiteSpace($parent)) {
        # --- (4) FAIL LOUD.
        throw "AIAPP_BASE is not set and could not be derived from HYDRA_ROOT ('$HydraRoot'). " +
              "Set the AIAPP_BASE environment variable to the directory containing all repos."
    }
    $AiappBase = $parent
}
$AiappBase = (Resolve-Path $AiappBase).Path

$HydraRootFwd = ConvertTo-ForwardSlash $HydraRoot
$AiappBaseFwd = ConvertTo-ForwardSlash $AiappBase

Write-Section "Resolved paths"
Write-Host "  HYDRA_ROOT  = $HydraRootFwd"
Write-Host "  AIAPP_BASE  = $AiappBaseFwd"

# --- Symlinks: squads/marketing-* -> <AIAPP_BASE>/MarketBliss/squads/marketing-*
Write-Section "Marketing squad symlinks"
$marketingNames = @('creative', 'ops', 'production', 'research', 'strategy')
$squadsDir      = Join-Path $HydraRoot 'squads'
$mbSquadsDir    = Join-Path $AiappBase 'MarketBliss\squads'

if (-not (Test-Path $squadsDir)) { New-Item -ItemType Directory -Path $squadsDir -Force | Out-Null }

$mbPresent = Test-Path $mbSquadsDir
if (-not $mbPresent) {
    Write-Warn2 "MarketBliss not found at $(ConvertTo-ForwardSlash $mbSquadsDir) -- skipping symlink creation."
}

foreach ($name in $marketingNames) {
    $linkName   = "marketing-$name"
    $linkPath   = Join-Path $squadsDir $linkName
    $targetPath = Join-Path $mbSquadsDir $linkName

    if (-not $mbPresent) {
        Write-Skip "$linkName (MarketBliss missing)"
        continue
    }

    $item = Get-Item -LiteralPath $linkPath -Force -ErrorAction SilentlyContinue
    if ($null -ne $item -and $item.LinkType -eq 'SymbolicLink') {
        $current = $item.Target | Select-Object -First 1
        if ((ConvertTo-ForwardSlash $current).TrimEnd('/') -ieq (ConvertTo-ForwardSlash $targetPath).TrimEnd('/')) {
            Write-Skip "$linkName already linked correctly"
            continue
        }
        Remove-Item -LiteralPath $linkPath -Force
    }
    elseif ($null -ne $item) {
        # Exists but not a symlink (real dir/file) -- remove and recreate.
        Remove-Item -LiteralPath $linkPath -Recurse -Force
    }

    try {
        New-Item -ItemType SymbolicLink -Path $linkPath -Target $targetPath -Force | Out-Null
        Write-Ok "$linkName -> $(ConvertTo-ForwardSlash $targetPath)"
    }
    catch {
        Write-Warn2 "Failed to create symlink '$linkName': $($_.Exception.Message)"
        Write-Warn2 "  Hint: enable Windows Developer Mode (Settings > Privacy & security > For developers)"
        Write-Warn2 "        or run this script from an elevated (Administrator) PowerShell."
    }
}

# --- backends.json generation from template.
Write-Section "backends.json"
$templatePath = Join-Path $ScriptDir 'backends.template.json'
if (-not (Test-Path $templatePath)) {
    throw "Template not found: $(ConvertTo-ForwardSlash $templatePath)"
}

$hydraDir    = Join-Path $env:USERPROFILE '.hydra'
$backendsOut = Join-Path $hydraDir 'backends.json'
if (-not (Test-Path $hydraDir)) { New-Item -ItemType Directory -Path $hydraDir -Force | Out-Null }

if (Test-Path $backendsOut) {
    $bak = "$backendsOut.bak"
    Copy-Item -LiteralPath $backendsOut -Destination $bak -Force
    Write-Ok "backed up existing backends.json -> $(ConvertTo-ForwardSlash $bak)"
}

$content = Get-Content -LiteralPath $templatePath -Raw
$content = $content.Replace('{{AIAPP_BASE}}', $AiappBaseFwd)
$content = $content.Replace('{{HYDRA_ROOT}}', $HydraRootFwd)
# Write UTF-8 without BOM.
[System.IO.File]::WriteAllText($backendsOut, $content, (New-Object System.Text.UTF8Encoding($false)))
Write-Ok "wrote $(ConvertTo-ForwardSlash $backendsOut)"

# --- Summary.
Write-Section "Summary"
Write-Host "  HYDRA_ROOT       : $HydraRootFwd"
Write-Host "  AIAPP_BASE       : $AiappBaseFwd"
Write-Host "  backends.json    : $(ConvertTo-ForwardSlash $backendsOut)"
Write-Host ""
Write-Host "  Export this so other repos + tools agree on the base:" -ForegroundColor Cyan
Write-Host "    setx AIAPP_BASE `"$AiappBaseFwd`"      # persistent (new shells)"
Write-Host "    `$env:AIAPP_BASE = `"$AiappBaseFwd`"    # current session"
Write-Host ""
Write-Host "Done." -ForegroundColor Green
