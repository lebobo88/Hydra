# hydra-doctor.ps1 — plugin-hook wrapper for hydra_core.cli
# Runs hydra_core.cli from any cwd by anchoring PYTHONPATH + --project to the
# plugin root (works both from the repo and from the versioned plugin cache,
# since both contain hydra_core/ and squads/).
# ADVISORY by design: these hooks report health, they do not guard anything,
# so a doctor failure must never block a session/tool call — hence exit 0.

$ErrorActionPreference = 'SilentlyContinue'

$root = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent  # plugin root (two up from .claude/hooks)
$env:PYTHONPATH = "$root;$env:PYTHONPATH"
python -m hydra_core.cli --project "$root" @args
exit 0
