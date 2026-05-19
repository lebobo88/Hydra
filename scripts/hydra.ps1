# Thin PowerShell wrapper for the hydra CLI.
# Usage: .\scripts\hydra.ps1 <subcommand> [args]

param([Parameter(ValueFromRemainingArguments)] [string[]] $Args)
& python -m hydra_core.cli @Args
