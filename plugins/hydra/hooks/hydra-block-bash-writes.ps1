# hydra-block-bash-writes.ps1 — PreToolUse hook (matcher: Bash)
#
# Extends the routing enforcement to the Bash tool: catches command strings that
# attempt to write engine-source files through shell write idioms, bypassing the
# Write/Edit PreToolUse hook. Fires only when HYDRA_ENFORCE_ROUTING=1 and no
# active pp stage is in progress.
#
# Detected write idioms (command string inspection):
#   - Output redirection (> or >>) targeting a blocked-extension file
#   - tee command with a blocked-extension destination
#   - cp / mv / copy / move whose resolved destination has a blocked extension
#   - python -c ... open(..., write/append/exclusive mode) — one-liner writing
#   - python -c ... pathlib.Path(...).write_text/write_bytes(...)
#   - python -c ... shutil.copy*/move with a blocked-extension destination
#   - sed -i (in-place edit) when a blocked extension appears in the command
#   - Set-Content / Out-File — scans ALL tokens for -Path/-FilePath/-LiteralPath
#     flag values AND positional arguments (fixes first-token-is-flag false neg)
#   - Shell heredoc (<<WORD) redirected into a blocked-extension file
#   - PowerShell here-string (@'...'@ or @"..."@) piped to Set-Content/Out-File
#
# RESIDUAL LIMITS — this is a guardrail, not a sandbox:
#   - Obfuscated writes (variable indirection, eval, base64 payloads,
#     pipes to write-capable sub-processes) can evade detection.
#   - Multi-line commands joined on one line may confuse some regex patterns.
#   - The hook sees raw command TEXT only; it cannot resolve shell variables or
#     evaluate expressions, so e.g. `> "$DEST"` where $DEST=foo.py is missed.
#   - For a genuine isolation boundary, use OS-level sandboxing (containers,
#     seccomp, etc.); this hook is an LLM-routing guardrail only.
#
# ALLOW exceptions (mirrors hydra-block-direct-write.ps1):
#   - Writes into harness / worktree / vcs / build dirs are allowed (that is
#     where the legitimate pp engineer generator writes candidate code).
#   - HYDRA_PP_STAGE_ACTIVE=1 fully bypasses enforcement (harness sets this).
#   - HYDRA_ENFORCE_ROUTING != '1' disables the hook entirely.

$ErrorActionPreference = 'SilentlyContinue'

if ($env:HYDRA_ENFORCE_ROUTING -ne '1') { exit 0 }
# RA-1: Harness-driven engineer stage: only bypass when a real active-stage marker
# exists. A bare HYDRA_PP_STAGE_ACTIVE=1 (leaked or set outside a stage) must NOT
# silently disable enforcement session-wide.
if ($env:HYDRA_PP_STAGE_ACTIVE -eq '1') {
    $_stagedActive = $false
    try {
        # Resolve project root: CLAUDE_PROJECT_DIR if set, else 3 levels up from
        # the canonical plugins/hydra/hooks directory.
        $_projRoot = $env:CLAUDE_PROJECT_DIR
        if (-not $_projRoot) { $_projRoot = Split-Path (Split-Path (Split-Path $PSScriptRoot)) }
        if ($_projRoot) {
            # Run-scoped stage marker: hydra_core.host_bridge.begin_stage WRITES
            # .harness\stage-active at stage start and CLEARS it at finalize/abort,
            # so its presence is tied to the CURRENT active run only. The old
            # "Marker 1" (any attended-* worktree directory exists under
            # .harness\worktrees) is retired: stale worktrees accumulate across
            # completed/aborted runs (17 were observed live in one session) and
            # that check became permanently true, silently disabling enforcement
            # repo-wide. Directory enumeration is no longer trusted; the sentinel
            # written by the harness is the sole source of truth.
            if (Test-Path (Join-Path $_projRoot '.harness\stage-active') -PathType Leaf) {
                $_stagedActive = $true
            }
        }
    } catch { $_stagedActive = $true }   # internal error → preserve old bypass behavior
    if ($_stagedActive) { exit 0 }
    Write-Host '[hydra-hook] bare HYDRA_PP_STAGE_ACTIVE=1 ignored (no active stage marker) — enforcement stays ON'
}

$raw = $input | Out-String
if (-not $raw) { exit 0 }
try { $json = $raw | ConvertFrom-Json } catch { exit 0 }

if ($json.tool_name -ne 'Bash') { exit 0 }

$cmd = "$($json.tool_input.command)"
if (-not $cmd) { exit 0 }

# --- Blocked engine-source extension pattern (same as hydra-block-direct-write.ps1) ---
$blockExtPat = '\.(ts|tsx|js|jsx|mjs|cjs|py|go|rs|java|kt|kts|c|cc|cpp|cxx|h|hpp|cs|rb|php|swift|m|mm|vue|svelte|html|htm|css|scss|sass|less|sql|sh|bash|lua|gd|glsl|hlsl|shader|dart|scala|clj|ex|exs)(?=[''"\s;|&<>]|$)'

# --- Allow-path fragments (harness / worktree / vcs / build dirs) ----------------
# Writes into these dirs are legitimate pp-engineer outputs; do not block them.
# Segment-bounded (begin + end with \) so a name like 'worktree.ts' can't bypass.
$allowDirFragments = @(
    '\.harness\', '\.hydra\', '\worktrees\', '\node_modules\', '\.git\',
    '\dist\', '\build\', '\__pycache__\', '\.venv\', '\site-packages\'
)

# --- Anchored allow-list resolution (2026-08, twin of hydra-block-direct-write.ps1) ---
# A bare fragment-Contains() matched ANY destination anywhere on disk that
# merely contained one of the fragments above (e.g. 'C:\elsewhere\dist\x.py').
# The destination is now resolved to an absolute path (relative to the
# command's cwd when not already rooted) and the fragment check only fires
# when that absolute path sits under the project root or the worktree root.
# HYDRA_WORKTREE_ROOT overrides the default '<projectRoot>\.harness\worktrees'.
#
# $_bwCwd is taken ONLY from the payload's own $json.cwd (the actual Bash
# tool's working directory, as Claude Code reports it) — it must NEVER fall
# back to this hook process's own ambient (Get-Location).Path. That fallback
# was a fail-OPEN hole: the hook script's own process cwd is incidental (e.g.
# the shell that happens to invoke pwsh, or — concretely — a test/session
# already running from inside a real .harness\worktrees\attended-*
# directory), not a trustworthy signal of where the Bash TOOL CALL intended
# to write. A relative destination with no reported cwd stays UNANCHORED
# below and falls straight through to the plain extension check — fail
# CLOSED, never waved through.
$_bwCwd = $null
if ($json.cwd) { $_bwCwd = "$($json.cwd)" }
$_bwProjRoot = $env:CLAUDE_PROJECT_DIR
if (-not $_bwProjRoot) { $_bwProjRoot = Split-Path (Split-Path (Split-Path $PSScriptRoot)) }
$_bwProjRootNorm = $null
if ($_bwProjRoot) {
    $_bwResolved = (Resolve-Path -LiteralPath $_bwProjRoot -ErrorAction SilentlyContinue)
    if ($_bwResolved) { $_bwProjRootNorm = $_bwResolved.Path.Replace('/', '\').TrimEnd('\').ToLowerInvariant() }
}
$_bwWorktreeRootNorm = $null
if ($env:HYDRA_WORKTREE_ROOT) {
    $_bwWtResolved = (Resolve-Path -LiteralPath $env:HYDRA_WORKTREE_ROOT -ErrorAction SilentlyContinue)
    if ($_bwWtResolved) { $_bwWorktreeRootNorm = $_bwWtResolved.Path.Replace('/', '\').TrimEnd('\').ToLowerInvariant() }
} elseif ($_bwProjRootNorm) {
    $_bwWorktreeRootNorm = "$_bwProjRootNorm\.harness\worktrees"
}

function Test-BlockedDest {
    param([string]$dest)
    if (-not $dest) { return $false }
    $raw = $dest.Trim('"''').Replace('/', '\')
    $norm = $raw.ToLowerInvariant()

    $absNorm = $norm
    try {
        if ($_bwCwd -and -not [System.IO.Path]::IsPathRooted($raw)) {
            $absNorm = (Join-Path $_bwCwd $raw).Replace('/', '\').ToLowerInvariant()
        }
    } catch { $absNorm = $norm }

    $underProjRoot = $_bwProjRootNorm -and ($absNorm -eq $_bwProjRootNorm -or $absNorm.StartsWith("$_bwProjRootNorm\"))
    $underWorktreeRoot = $_bwWorktreeRootNorm -and ($absNorm -eq $_bwWorktreeRootNorm -or $absNorm.StartsWith("$_bwWorktreeRootNorm\"))
    if ($underProjRoot -or $underWorktreeRoot) {
        foreach ($frag in $allowDirFragments) {
            if ($absNorm.Contains($frag)) { return $false }
        }
    }
    return [bool]($norm -match $blockExtPat)
}

$matched = $false
$reason  = ''

# 1. Output redirection: > or >> followed by a filename
#    e.g.  echo "..." > foo.py    cat src.txt >> dest.ts
if (-not $matched) {
    $hits = [regex]::Matches($cmd, '>{1,2}\s*[''"]?([^\s''";|&<>]+)')
    foreach ($hit in $hits) {
        if (Test-BlockedDest $hit.Groups[1].Value) {
            $matched = $true
            $reason = "output redirection to '$($hit.Groups[1].Value)'"
            break
        }
    }
}

# 2. tee [flags] filename
#    e.g.  cmd | tee output.py    cmd | tee -a file.ts
if (-not $matched) {
    $hits = [regex]::Matches($cmd, '\btee\s+(?:-[ai]\s+)*[''"]?([^\s''";|&<>\-][^\s''";|&<>]*)')
    foreach ($hit in $hits) {
        if (Test-BlockedDest $hit.Groups[1].Value) {
            $matched = $true
            $reason = "tee to '$($hit.Groups[1].Value)'"
            break
        }
    }
}

# 3. cp / mv / copy / move — heuristic: last space-delimited token before end or
#    shell separator is the destination.
#    e.g.  cp template.py src/newfile.py    mv old.js new.ts
if (-not $matched) {
    $hits = [regex]::Matches($cmd,
        '\b(?:cp|mv|copy|move)\b\s+\S.*?\s+([''"]?[^\s''";|&<>]+[''"]?)(?=\s*(?:$|[;&|]))')
    foreach ($hit in $hits) {
        if (Test-BlockedDest $hit.Groups[1].Value) {
            $matched = $true
            $reason = "cp/mv/copy/move to '$($hit.Groups[1].Value)'"
            break
        }
    }
}

# 4. python -c write idioms:
#    4a. open() in write/append/exclusive mode.
#        Matches open( <first-quoted-arg> , <second-quoted-arg-containing-w/a/x> )
#        so the mode check applies to the MODE string only, not the filename.
#        False-positive guard: open('data.py','r') → filename has 'a' but mode
#        is 'r' — NOT blocked.  open('data.py','w') → mode is 'w' — BLOCKED.
#        e.g.  python -c "open('foo.py','w').write('...')"
#              python -c "open('bar.ts','wb').write(b'...')"
#              python -c "open('q.py','a+').write('...')"
if (-not $matched) {
    # Capture: open( '<path>' , '<mode-with-w/a/x>' )
    # First arg: any quoted string. Second arg: quoted string that contains w, a, or x.
    $openMatches = [regex]::Matches($cmd,
        "\bopen\s*\(\s*[`"'][^`"']*[`"']\s*,\s*[`"']([^`"']*[wax][^`"']*)[`"']")
    if ($openMatches.Count -gt 0) {
        $matched = $true
        $reason  = "python -c with open() in write/append/exclusive mode ('$($openMatches[0].Groups[1].Value)')"
    }
}
#    4b. pathlib.Path(...).write_text / write_bytes — scan directly for the
#        method call pattern and test the captured filename.  Does not rely on
#        the -c prefix so it works even when the Python code contains semicolons
#        (which would stop a [^;|&\n]* lookahead before reaching the call).
#        e.g.  python -c "from pathlib import Path; Path('x.py').write_text('...')"
if (-not $matched) {
    $plMatches = [regex]::Matches($cmd,
        "\bPath\s*\(\s*[`"']([^`"']+)[`"']\s*\)\s*\.\s*write_(?:text|bytes)\b")
    foreach ($pm in $plMatches) {
        if (Test-BlockedDest $pm.Groups[1].Value) {
            $matched = $true
            $reason = "pathlib.Path.write_text/write_bytes to '$($pm.Groups[1].Value)'"
            break
        }
    }
}
#    4c. shutil.copy*/move with a blocked-extension filename in the command
#        e.g.  python -c "import shutil; shutil.copy('tmpl.py','src/real.py')"
if (-not $matched) {
    if (($cmd -match 'python[0-9.]*\s[^;|&\n]*-c\s[^;|&\n]*\bshutil\s*\.\s*(?:copy2?|copyfile|copytree|move)\b') -and
        ($cmd -match $blockExtPat)) {
        $matched = $true
        $reason  = 'python -c shutil write to engine source'
    }
}

# 5. sed -i (in-place file edit) — block only when a blocked extension also
#    appears in the command (best-effort: target filename may not be parseable).
if (-not $matched) {
    if (($cmd -match '\bsed\s+[^;|&\n]*-i') -and ($cmd -match $blockExtPat)) {
        $matched = $true
        $reason  = 'sed -i (in-place edit of engine source)'
    }
}

# 6. PowerShell Set-Content / Out-File with a blocked-extension path argument.
#    Scans ALL tokens of each invocation: named params (-Path, -FilePath,
#    -LiteralPath) consume the NEXT token as the destination; positional args
#    (tokens not beginning with '-') are also tested.  This avoids the false
#    negative where the first token is a flag name, not the filename.
#    e.g.  Set-Content -Path foo.py -Value '...'    → blocked
#          Get-Template | Out-File -FilePath src/index.ts  → blocked
#          Set-Content foo.py 'content'              → blocked (positional)
if (-not $matched) {
    $scMatches = [regex]::Matches($cmd, '\b(?:Set-Content|Out-File)\b([^;|&\n]*)')
    foreach ($m in $scMatches) {
        $invocation = $m.Groups[1].Value
        $tokens = ($invocation -split '\s+') | Where-Object { $_ -ne '' }
        $nextIsPathValue = $false
        foreach ($tok in $tokens) {
            if ($nextIsPathValue) {
                if (Test-BlockedDest $tok) {
                    $matched = $true
                    $reason = "Set-Content/Out-File to '$tok'"
                    break
                }
                $nextIsPathValue = $false
            } elseif ($tok -match '^-(?:Path|FilePath|LiteralPath)(?::|$)') {
                # Flag with inline value (-Path:foo.py) or flag expecting next token
                $inline = ($tok -replace '^-(?:Path|FilePath|LiteralPath):', '')
                if ($inline -and ($inline -ne $tok)) {
                    if (Test-BlockedDest $inline) {
                        $matched = $true
                        $reason = "Set-Content/Out-File to '$inline'"
                        break
                    }
                } else {
                    $nextIsPathValue = $true
                }
            } elseif ($tok -notmatch '^-') {
                # Positional argument (not a flag name or flag value)
                if (Test-BlockedDest $tok) {
                    $matched = $true
                    $reason = "Set-Content/Out-File to '$tok'"
                    break
                }
            }
        }
        if ($matched) { break }
    }
}

# 7a. Shell heredoc redirected into a blocked-extension file
#     e.g.  cat <<'EOF' > src/index.ts ... EOF
if (-not $matched) {
    $hits = [regex]::Matches($cmd, '<<[''"]?\w+[''"]?[^;|&\n]*?>{1,2}\s*[''"]?([^\s''";|&<>]+)')
    foreach ($hit in $hits) {
        if (Test-BlockedDest $hit.Groups[1].Value) {
            $matched = $true
            $reason = "heredoc into '$($hit.Groups[1].Value)'"
            break
        }
    }
}

# 7b. PowerShell here-string piped to Set-Content / Out-File with blocked dest
#     e.g.  @'...'@ | Set-Content foo.py
if (-not $matched) {
    if (($cmd -match "@'|@`"") -and
        ($cmd -match '\b(?:Set-Content|Out-File)\b') -and
        ($cmd -match $blockExtPat)) {
        $matched = $true
        $reason = 'PowerShell here-string write to engine source'
    }
}

if ($matched) {
    [Console]::Error.WriteLine("[hydra] BLOCKED: Bash write idiom ($reason) targets engine source.")
    [Console]::Error.WriteLine("[hydra] Engineering code MUST go through the pair-programmer harness, not a Bash write.")
    [Console]::Error.WriteLine("[hydra] Route it: /hydra:run `"<goal>`" (or submit a DEV_TASK via the ingest bridge).")
    [Console]::Error.WriteLine("[hydra] Design docs (.md) are allowed. Kill-switch: set HYDRA_ENFORCE_ROUTING != 1.")
    exit 2
}

exit 0
