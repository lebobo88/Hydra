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
#   - python -c ... open(..., 'w' | 'a') — one-liner writing to any file
#   - sed -i (in-place edit) when a blocked extension appears in the command
#   - Set-Content / Out-File with a blocked-extension path argument
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
# Harness-driven engineer stage: the deterministic codegen path is sanctioned.
if ($env:HYDRA_PP_STAGE_ACTIVE -eq '1') { exit 0 }

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

function Test-BlockedDest {
    param([string]$dest)
    if (-not $dest) { return $false }
    $norm = $dest.Trim('"''').Replace('/', '\').ToLowerInvariant()
    foreach ($frag in $allowDirFragments) {
        if ($norm.Contains($frag)) { return $false }
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

# 4. python -c with open() in write or append mode
#    e.g.  python -c "open('foo.py','w').write('...')"
if (-not $matched) {
    if ($cmd -match 'python[0-9.]*\s[^;|&\n]*-c\s[^;|&\n]*\bopen\s*\([^)]*[''"][wa][''"]') {
        $matched = $true
        $reason  = 'python -c with open() in write/append mode'
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

# 6. PowerShell Set-Content / Out-File with a blocked-extension path argument
#    e.g.  Set-Content -Path foo.py -Value '...'
#          Get-Template | Out-File -FilePath src/index.ts
if (-not $matched) {
    $hits = [regex]::Matches($cmd, '\b(?:Set-Content|Out-File)\b[^;|&\n]*?[''"]?([^\s''";|&<>]+)')
    foreach ($hit in $hits) {
        if (Test-BlockedDest $hit.Groups[1].Value) {
            $matched = $true
            $reason = "Set-Content/Out-File to '$($hit.Groups[1].Value)'"
            break
        }
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
