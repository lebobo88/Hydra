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
# EFFECTIVE-CWD RULE (E2-18) — a relative destination is resolved against the
# cwd the command has actually reached, not blindly against the payload's
# session cwd. The command string is scanned for `cd` / `pushd` /
# `Set-Location` directory tokens (quoted or bare, separated by `&&`, `;`,
# `||`, or newline); the LAST such directory BEFORE the write idiom wins, and
# a relative `cd` composes onto the previous effective cwd (starting from
# $json.cwd). Without this, `cd <worktree> && echo x >> tests/p.py` resolved to
# <projectRoot>\tests\p.py and was BLOCKED while the Edit tool on the very same
# file was ALLOWED by hydra-block-direct-write.ps1 — the two guards disagreed.
# An unresolvable `cd` target (variable/`-`) leaves the effective cwd unchanged,
# which keeps the resolution inside the project root: fail CLOSED.
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
# Worktree roots where the pp/hydra engineer legitimately writes candidate code.
# MUST stay in LOCKSTEP with hydra_core.host_bridge.resolve_worktree_root (twin
# of hydra-block-direct-write.ps1): HYDRA_WORKTREE_ROOT >
# <AIAPP_BASE>\.hydra-worktrees > <parent of repo_root>\.hydra-worktrees, each
# namespaced per-repo (StartsWith on the base covers every <repo_id>\attended-*
# child). The legacy <projectRoot>\.harness\worktrees is retained for
# pair-programmer's own candidate worktrees.
function _bwNormRoot([string]$p) {
    if (-not $p) { return $null }
    try { $full = [System.IO.Path]::GetFullPath($p) } catch { $full = $p }
    return $full.Replace('/', '\').TrimEnd('\').ToLowerInvariant()
}
$_bwWtRoots = New-Object System.Collections.Generic.List[string]
if ($env:HYDRA_WORKTREE_ROOT) {
    $_r = _bwNormRoot $env:HYDRA_WORKTREE_ROOT
    if ($_r) { [void]$_bwWtRoots.Add($_r) }
}
if ($env:AIAPP_BASE) {
    $_r = _bwNormRoot (Join-Path $env:AIAPP_BASE '.hydra-worktrees')
    if ($_r) { [void]$_bwWtRoots.Add($_r) }
}
if ($_bwProjRootNorm) {
    $_r = _bwNormRoot (Join-Path (Split-Path $_bwProjRootNorm) '.hydra-worktrees')
    if ($_r) { [void]$_bwWtRoots.Add($_r) }
    [void]$_bwWtRoots.Add("$_bwProjRootNorm\.harness\worktrees")
}

# --- E2-18: effective cwd derived from cd/pushd/Set-Location in the command ---
# Test-BlockedDest used to join a relative destination with $_bwCwd (the
# payload's session cwd), ignoring any directory change earlier in the SAME
# command. Build an ordered table of (end-offset -> effective cwd) so each
# write idiom resolves against the directory in force at its own position.
$_bwCdPat = '(?i)(?:^|[;&|\n]|\bthen\b|\bdo\b)\s*(?:cd|pushd|Set-Location|sl)\s+(?:-\w+\s+)*(?:"([^"]+)"|''([^'']+)''|([^\s;&|]+))'
$_bwCdPoints = New-Object System.Collections.Generic.List[object]
if ($_bwCwd) {
    $_bwEff = $_bwCwd
    foreach ($_cdm in [regex]::Matches($cmd, $_bwCdPat)) {
        $_cdDir = $null
        foreach ($_gi in 1, 2, 3) {
            if ($_cdm.Groups[$_gi].Success) { $_cdDir = $_cdm.Groups[$_gi].Value; break }
        }
        if (-not $_cdDir) { continue }
        $_cdDir = $_cdDir.Replace('/', '\')
        # Unresolvable targets (shell/PS variable expansion, `cd -`, `~`): leave
        # the effective cwd unchanged. That keeps resolution anchored where it
        # already was rather than optimistically relocating it — fail CLOSED.
        if ($_cdDir -eq '-' -or $_cdDir -eq '~' -or
            $_cdDir.Contains('$') -or $_cdDir.Contains('%') -or $_cdDir.Contains('`')) { continue }
        try {
            if ([System.IO.Path]::IsPathRooted($_cdDir)) { $_bwEff = $_cdDir }
            else { $_bwEff = (Join-Path $_bwEff $_cdDir) }
            $_bwEff = [System.IO.Path]::GetFullPath($_bwEff)
        } catch { continue }
        [void]$_bwCdPoints.Add([pscustomobject]@{ Index = $_cdm.Index + $_cdm.Length; Cwd = $_bwEff })
    }
}

function _bwEffCwdAt([int]$atIndex) {
    $eff = $_bwCwd
    foreach ($p in $_bwCdPoints) {
        if ($p.Index -le $atIndex) { $eff = $p.Cwd } else { break }
    }
    return $eff
}

# --- SHELL-AWARE ARGUMENT RECONSTRUCTION (security hardening, 2026-09) -------
# ROOT CAUSE this closes: every regex above that captured a destination with a
# character class like `[^\s''";|&<>]+` EXCLUDES quote characters, so it stops
# dead at the first quote boundary. A destination written as adjacent quoted
# fragments — e.g. `'/protected/hydra_core/sup'"ervisor.py"` — is ONE shell
# argument (the shell concatenates adjacent quoted/unquoted runs with no
# separating whitespace) but the regex only ever captured `/protected/hydra_
# core/sup`, silently losing the `ervisor.py` suffix. Test-BlockedDest then
# saw a value with no blocked extension and returned "not blocked" — a false
# negative proven against the hook as it existed at 29dbe89, for EVERY
# destination-extracting branch (redirect, tee, cp/mv, Path.write_text,
# Set-Content/Out-File, heredoc) plus python open()'s argument parsing.
#
# The fix is a real (small) shell-argument reader used by EVERY branch below
# instead of ad-hoc per-branch capture groups: Read-ShellArgument walks the
# command text char-by-char, tracks single-quote / double-quote / backslash-
# escape state, and treats unquoted whitespace as the only separator — so a
# single-quoted prefix immediately followed by a double-quoted suffix (or any
# other quote-adjacency shape) reconstructs into ONE fully-unquoted argument.
# Get-ShellArgsInRange repeats it to collect every argument of an invocation
# (needed for cp/mv's "last argument is the destination" and for Set-Content/
# Out-File's positional-or-flag scan). Read-PyStringLiteral does the analogous
# job one layer down, for adjacent Python string literals inside a
# `python -c "..."` one-liner (implicit literal concatenation), which is what
# let a fragmented open()/Path()/shutil argument dodge the old single-quote-
# pair regexes entirely (the regex just failed to match, so the call wasn't
# recognised as a write idiom at all).
function Read-ShellArgument {
    # Reconstructs ONE shell argument starting at/after $start, concatenating
    # adjacent quoted and unquoted runs. Returns $null Value when the cursor
    # (after skipping whitespace) lands on a separator or end-of-string, so
    # callers can detect "no more arguments" without an out-of-band sentinel.
    param([string]$s, [int]$start)
    $n = $s.Length
    $i = [Math]::Max(0, $start)
    while ($i -lt $n -and $s[$i] -match '[ \t]') { $i++ }
    $beginIdx = $i
    if ($i -ge $n -or $s[$i] -match '[;|&<>\r\n]') {
        return [pscustomobject]@{ Value = $null; Start = $beginIdx; End = $i }
    }
    $sb = New-Object System.Text.StringBuilder
    while ($i -lt $n) {
        $c = $s[$i]
        if ($c -eq "'") {
            $i++
            while ($i -lt $n -and $s[$i] -ne "'") { [void]$sb.Append($s[$i]); $i++ }
            if ($i -lt $n) { $i++ }
            continue
        }
        if ($c -eq '"') {
            $i++
            while ($i -lt $n -and $s[$i] -ne '"') {
                if ($s[$i] -eq '\' -and ($i + 1) -lt $n -and
                    ($s[$i + 1] -eq '"' -or $s[$i + 1] -eq '\')) {
                    [void]$sb.Append($s[$i + 1]); $i += 2; continue
                }
                [void]$sb.Append($s[$i]); $i++
            }
            if ($i -lt $n) { $i++ }
            continue
        }
        if ($c -match '[ \t;|&<>\r\n]') { break }
        if ($c -eq '\' -and ($i + 1) -lt $n) {
            [void]$sb.Append($s[$i + 1]); $i += 2; continue
        }
        [void]$sb.Append($c); $i++
    }
    return [pscustomobject]@{ Value = $sb.ToString(); Start = $beginIdx; End = $i }
}

function Get-ShellArgsInRange {
    # Collects every reconstructed argument from $startIdx up to (not past) a
    # statement separator (unquoted ; | & < >), a newline, or $endIdx.
    param([string]$s, [int]$startIdx, [int]$endIdx)
    $result = New-Object System.Collections.Generic.List[object]
    $i = $startIdx
    while ($i -lt $endIdx) {
        $tok = Read-ShellArgument $s $i
        if ($null -eq $tok.Value) { break }
        if ($tok.End -ge $endIdx) {
            # Argument may extend past the caller's nominal end (e.g. end of
            # the whole command) — still valid, just clamp the loop.
            [void]$result.Add($tok)
            break
        }
        [void]$result.Add($tok)
        if ($tok.End -le $i) { break }   # safety against zero-length loops
        $i = $tok.End
    }
    return $result
}

function Read-PyStringLiteral {
    # Reconstructs one or more ADJACENT Python string literals (implicit
    # literal concatenation, e.g. 'sup' 'ervisor.py' -> 'supervisor.py') into
    # one value, so a python -c one-liner can't dodge detection by splitting
    # an open()/Path()/shutil argument across literals.
    param([string]$s, [int]$start)
    $n = $s.Length
    $i = [Math]::Max(0, $start)
    $sb = New-Object System.Text.StringBuilder
    $any = $false
    while ($true) {
        while ($i -lt $n -and $s[$i] -match '[ \t]') { $i++ }
        if ($i -ge $n -or ($s[$i] -ne "'" -and $s[$i] -ne '"')) { break }
        $q = $s[$i]; $i++
        while ($i -lt $n -and $s[$i] -ne $q) {
            if ($s[$i] -eq '\' -and ($i + 1) -lt $n) { [void]$sb.Append($s[$i + 1]); $i += 2; continue }
            [void]$sb.Append($s[$i]); $i++
        }
        if ($i -lt $n) { $i++ }
        $any = $true
    }
    if (-not $any) { return $null }
    return [pscustomobject]@{ Value = $sb.ToString(); Start = $start; End = $i }
}

function Test-BlockedDest {
    param([string]$dest, [int]$atIndex = [int]::MaxValue)
    if (-not $dest) { return $false }
    # $dest arrives already fully unquoted/reconstructed from Read-ShellArgument
    # or Read-PyStringLiteral; Trim() here is a harmless no-op safety net for
    # any caller that still passes a raw single-quote-wrapped literal.
    $raw = $dest.Trim('"''').Replace('/', '\')
    $norm = $raw.ToLowerInvariant()

    $_effCwd = _bwEffCwdAt $atIndex
    $absNorm = $norm
    try {
        if ($_effCwd -and -not [System.IO.Path]::IsPathRooted($raw)) {
            $absNorm = ([System.IO.Path]::GetFullPath((Join-Path $_effCwd $raw))).Replace('/', '\').ToLowerInvariant()
        }
    } catch { $absNorm = $norm }

    $underProjRoot = $_bwProjRootNorm -and ($absNorm -eq $_bwProjRootNorm -or $absNorm.StartsWith("$_bwProjRootNorm\"))
    $underWorktreeRoot = $false
    foreach ($_wr in $_bwWtRoots) {
        if ($_wr -and ($absNorm -eq $_wr -or $absNorm.StartsWith("$_wr\"))) { $underWorktreeRoot = $true; break }
    }
    # Under an isolated worktree root the engineer writes/commits real engine
    # source by design — allow unconditionally. Within the project root itself,
    # only the build / vcs / harness sub-dirs are exempt.
    if ($underWorktreeRoot) { return $false }
    if ($underProjRoot) {
        foreach ($frag in $allowDirFragments) {
            if ($absNorm.Contains($frag)) { return $false }
        }
        # docs/plans carve-out (P2 plan artifact writer, hydra_core.artifact_
        # store.write_repo_artifact allow-lists docs/plans for .html alongside
        # the already-globally-allowed .md/.json/.txt). Segment-bounded
        # (trailing \) so 'docs\plansomething\' cannot slip through, and only
        # for the exact .html suffix — no other blocked extension is exempted.
        if ($absNorm -match '\.html$') {
            $_bwPlansFrag = "$_bwProjRootNorm\docs\plans\"
            if ($absNorm -eq $_bwPlansFrag.TrimEnd('\') -or $absNorm.StartsWith($_bwPlansFrag)) { return $false }
        }
    }
    return [bool]($norm -match $blockExtPat)
}

$matched = $false
$reason  = ''

# 1. Output redirection: > or >> followed by a filename
#    e.g.  echo "..." > foo.py    cat src.txt >> dest.ts
#    Destination is reconstructed via Read-ShellArgument (not a truncating
#    capture group) so a fragment-concatenated path can't dodge detection.
if (-not $matched) {
    $hits = [regex]::Matches($cmd, '>{1,2}\s*')
    foreach ($hit in $hits) {
        $tok = Read-ShellArgument $cmd ($hit.Index + $hit.Length)
        if ($tok.Value -and (Test-BlockedDest $tok.Value $tok.Start)) {
            $matched = $true
            $reason = "output redirection to '$($tok.Value)'"
            break
        }
    }
}

# 2. tee [flags] filename
#    e.g.  cmd | tee output.py    cmd | tee -a file.ts
if (-not $matched) {
    $hits = [regex]::Matches($cmd, '\btee\s+(?:-[ai]\s+)*')
    foreach ($hit in $hits) {
        $tok = Read-ShellArgument $cmd ($hit.Index + $hit.Length)
        if ($tok.Value -and (Test-BlockedDest $tok.Value $tok.Start)) {
            $matched = $true
            $reason = "tee to '$($tok.Value)'"
            break
        }
    }
}

# 3. cp / mv / copy / move — the LAST reconstructed argument of the invocation
#    (up to the next statement separator) is the destination.
#    e.g.  cp template.py src/newfile.py    mv old.js new.ts
if (-not $matched) {
    $hits = [regex]::Matches($cmd, '\b(?:cp|mv|copy|move)\b')
    foreach ($hit in $hits) {
        $argsList = Get-ShellArgsInRange $cmd ($hit.Index + $hit.Length) $cmd.Length
        if ($argsList.Count -ge 2) {
            $destTok = $argsList[$argsList.Count - 1]
            if (Test-BlockedDest $destTok.Value $destTok.Start) {
                $matched = $true
                $reason = "cp/mv/copy/move to '$($destTok.Value)'"
                break
            }
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
    # Locate `open(` then reconstruct arg1 (path, may be fragmented across
    # adjacent literals) and arg2 (mode) via Read-PyStringLiteral rather than a
    # single quote-pair regex — the old regex demanded arg1 be EXACTLY one
    # quoted literal immediately followed by a comma, so splitting the path
    # across two literals (`'sup' 'ervisor.py'`) made the whole pattern fail to
    # match, silently un-detecting the write regardless of mode.
    $openHits = [regex]::Matches($cmd, '\bopen\s*\(\s*')
    foreach ($oh in $openHits) {
        $arg1 = Read-PyStringLiteral $cmd ($oh.Index + $oh.Length)
        if (-not $arg1) { continue }
        $j = $arg1.End
        while ($j -lt $cmd.Length -and $cmd[$j] -match '[ \t]') { $j++ }
        if ($j -ge $cmd.Length -or $cmd[$j] -ne ',') { continue }
        $j++
        while ($j -lt $cmd.Length -and $cmd[$j] -match '[ \t]') { $j++ }
        $arg2 = Read-PyStringLiteral $cmd $j
        if ($arg2 -and ($arg2.Value -match '[wax]')) {
            $matched = $true
            $reason  = "python -c with open() in write/append/exclusive mode ('$($arg2.Value)') targeting '$($arg1.Value)'"
            break
        }
    }
}
#    4b. pathlib.Path(...).write_text / write_bytes — scan directly for the
#        method call pattern and test the captured filename.  Does not rely on
#        the -c prefix so it works even when the Python code contains semicolons
#        (which would stop a [^;|&\n]* lookahead before reaching the call).
#        e.g.  python -c "from pathlib import Path; Path('x.py').write_text('...')"
if (-not $matched) {
    $plHits = [regex]::Matches($cmd, '\bPath\s*\(\s*')
    foreach ($ph in $plHits) {
        $arg = Read-PyStringLiteral $cmd ($ph.Index + $ph.Length)
        if (-not $arg) { continue }
        $j = $arg.End
        while ($j -lt $cmd.Length -and $cmd[$j] -match '[ \t]') { $j++ }
        if ($j -ge $cmd.Length -or $cmd[$j] -ne ')') { continue }
        $j++
        if ($cmd.Substring($j) -notmatch '^\s*\.\s*write_(?:text|bytes)\b') { continue }
        if (Test-BlockedDest $arg.Value $arg.Start) {
            $matched = $true
            $reason = "pathlib.Path.write_text/write_bytes to '$($arg.Value)'"
            break
        }
    }
}
#    4c. shutil.copy*/move with a blocked-extension destination.
#        e.g.  python -c "import shutil; shutil.copy('tmpl.py','src/real.py')"
#        Primary check reconstructs the destination argument (fragment-
#        concatenation-proof); the original whole-command heuristic is kept as
#        a fallback so an unusual call shape that the arg reader can't line up
#        still degrades to the old (broader, presence-only) behaviour rather
#        than going undetected.
if (-not $matched) {
    $shHits = [regex]::Matches($cmd, '\bshutil\s*\.\s*(?:copy2?|copyfile|copytree|move)\s*\(\s*')
    foreach ($sh in $shHits) {
        $a1 = Read-PyStringLiteral $cmd ($sh.Index + $sh.Length)
        if (-not $a1) { continue }
        $j = $a1.End
        while ($j -lt $cmd.Length -and $cmd[$j] -match '[ \t]') { $j++ }
        if ($j -ge $cmd.Length -or $cmd[$j] -ne ',') { continue }
        $j++
        while ($j -lt $cmd.Length -and $cmd[$j] -match '[ \t]') { $j++ }
        $a2 = Read-PyStringLiteral $cmd $j
        if ($a2 -and (Test-BlockedDest $a2.Value $a2.Start)) {
            $matched = $true
            $reason = "python -c shutil write to '$($a2.Value)'"
            break
        }
    }
    if (-not $matched) {
        if (($cmd -match 'python[0-9.]*\s[^;|&\n]*-c\s[^;|&\n]*\bshutil\s*\.\s*(?:copy2?|copyfile|copytree|move)\b') -and
            ($cmd -match $blockExtPat)) {
            $matched = $true
            $reason  = 'python -c shutil write to engine source'
        }
    }
}

# 5. sed -i (in-place file edit). Primary check reconstructs sed's own
#    arguments and tests each non-flag token as a possible destination — this
#    catches a fragmented filename (e.g. 'file.'"py") that would no longer
#    appear as a contiguous blocked extension in the raw command text. The
#    original whole-command heuristic (blocked extension appears anywhere +
#    -i flag present) is kept as an OR, not a replacement, so nothing that
#    used to block stops blocking.
if (-not $matched) {
    $sedHits = [regex]::Matches($cmd, '\bsed\b')
    foreach ($sh in $sedHits) {
        $argsList = Get-ShellArgsInRange $cmd ($sh.Index + $sh.Length) $cmd.Length
        $hasInPlace = $false
        foreach ($t in $argsList) { if ($t.Value -match '^-[a-zA-Z]*i') { $hasInPlace = $true; break } }
        if ($hasInPlace) {
            foreach ($t in $argsList) {
                if ($t.Value -notmatch '^-' -and (Test-BlockedDest $t.Value $t.Start)) {
                    $matched = $true
                    $reason = "sed -i (in-place edit) targets '$($t.Value)'"
                    break
                }
            }
        }
        if ($matched) { break }
    }
    if (-not $matched) {
        if (($cmd -match '\bsed\s+[^;|&\n]*-i') -and ($cmd -match $blockExtPat)) {
            $matched = $true
            $reason  = 'sed -i (in-place edit of engine source)'
        }
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
    $scMatches = [regex]::Matches($cmd, '\b(?:Set-Content|Out-File)\b')
    foreach ($m in $scMatches) {
        $argsList = Get-ShellArgsInRange $cmd ($m.Index + $m.Length) $cmd.Length
        $nextIsPathValue = $false
        foreach ($tok in $argsList) {
            if ($nextIsPathValue) {
                if (Test-BlockedDest $tok.Value $tok.Start) {
                    $matched = $true
                    $reason = "Set-Content/Out-File to '$($tok.Value)'"
                    break
                }
                $nextIsPathValue = $false
            } elseif ($tok.Value -match '^-(?:Path|FilePath|LiteralPath)(?::(.*))?$') {
                # Flag with inline value (-Path:foo.py) or flag expecting next token
                $inline = $Matches[1]
                if ($inline) {
                    if (Test-BlockedDest $inline $tok.Start) {
                        $matched = $true
                        $reason = "Set-Content/Out-File to '$inline'"
                        break
                    }
                } else {
                    $nextIsPathValue = $true
                }
            } elseif ($tok.Value -notmatch '^-') {
                # Positional argument (not a flag name or flag value)
                if (Test-BlockedDest $tok.Value $tok.Start) {
                    $matched = $true
                    $reason = "Set-Content/Out-File to '$($tok.Value)'"
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
    $hits = [regex]::Matches($cmd, '<<[''"]?\w+[''"]?[^;|&\n]*?>{1,2}\s*')
    foreach ($hit in $hits) {
        $tok = Read-ShellArgument $cmd ($hit.Index + $hit.Length)
        if ($tok.Value -and (Test-BlockedDest $tok.Value $tok.Start)) {
            $matched = $true
            $reason = "heredoc into '$($tok.Value)'"
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
