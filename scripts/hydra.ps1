# Thin PowerShell wrapper for the hydra CLI.
# Usage: .\scripts\hydra.ps1 <subcommand> [args]
#
# Resolves an interpreter that can actually import hydra_core: prefers the
# Windows launcher (`py -3.12`), falls back to `python` on PATH. This avoids
# the F-3 failure mode where bare `python` resolves to an interpreter
# without hydra_core installed (ModuleNotFoundError).

param([Parameter(ValueFromRemainingArguments)] [string[]] $Args)

$candidates = @(
    @{ Cmd = 'py';     BaseArgs = @('-3.12') },
    @{ Cmd = 'python'; BaseArgs = @() }
)

foreach ($c in $candidates) {
    if (-not (Get-Command $c.Cmd -ErrorAction SilentlyContinue)) { continue }
    & $c.Cmd @($c.BaseArgs + @('-c', 'import hydra_core')) 2>$null
    if ($LASTEXITCODE -eq 0) {
        & $c.Cmd @($c.BaseArgs + @('-m', 'hydra_core.cli') + $Args)
        exit $LASTEXITCODE
    }
}

Write-Error "No Python interpreter with hydra_core found (tried: py -3.12, python). Install with scripts/install.ps1 under Python 3.11+."
exit 1
