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
#     (including `-t DIR` / `--target-directory=DIR`, whose real write target
#     is DIR joined with each source's basename, never DIR itself)
#   - python -c ... open(..., write/append/exclusive mode) — one-liner writing
#   - python -c ... pathlib.Path(...).write_text/write_bytes(...)
#   - python -c ... shutil.copy*/move with a blocked-extension destination
#   - sed -i (in-place edit) when a blocked extension appears in the command
#   - Set-Content / Out-File — scans ALL tokens for -Path/-FilePath/-LiteralPath
#     flag values AND positional arguments (fixes first-token-is-flag false neg)
#   - Shell heredoc (<<WORD) redirected into a blocked-extension file
#   - PowerShell here-string (@'...'@ or @"..."@) piped to Set-Content/Out-File
#   - dd if=... of=<dest>                       (destination = the of= operand)
#   - truncate [-s SIZE] FILE...                (every non-option operand)
#   - ln [-s] TARGET LINKNAME / TARGET... DIR    (destination = the link/dir,
#     never the target — with `-t DIR` / `--target-directory=DIR`, every
#     operand is a TARGET being read and the real write is DIR joined with
#     each operand's basename)
#   - install SOURCE... DEST / -t DIR           (coreutils file-copy form only
#     — see Test-IsCommandWord: never a package-manager `install` subcommand)
#   - python -c ... os.replace(src, dst) / os.rename(src, dst)
#   - python -c ... os.symlink(src, dst) / os.link(src, dst)
#   - python -c ... os.truncate(path, size)
#   - python -c ... Path(...).open(mode) — including mode='w' as a keyword
#   - python -c ... io.open(...) / codecs.open(...) — same shape as open()
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
#   - Obfuscated writes (eval, base64 payloads, pipes to write-capable
#     sub-processes) can still evade detection.
#   - Multi-line commands joined on one line may confuse some regex patterns.
#   - For a genuine isolation boundary, use OS-level sandboxing (containers,
#     seccomp, etc.); this hook is an LLM-routing guardrail only.
#   - RUNTIME INDIRECTION IS AN UNBOUNDED CLASS, not something this file
#     closes. This revision fails closed on ONE reachable, concrete shape —
#     an xargs replacement-string placeholder (`{}` / a custom `-I<replstr>`)
#     standing in for a destination — but a destination can equally be
#     constructed inside `sh -c` from a variable, decoded from base64, read
#     from a file, or built by a `python -c` one-liner, and no static
#     path-scanning PreToolUse hook can resolve those. Do not read the
#     placeholder fix as closing indirect execution in general.
#   - THE INVENTORY OF WRITE-CAPABLE PROGRAMS IS OPEN, NOT CLOSED. This file
#     recognizes a fixed list of idioms; it does not and cannot enumerate
#     every program that can write a file. Known-unguarded surfaces (measured
#     unguarded, tracked, deliberately not closed here) are catalogued in
#     docs/audits/WRITE-GUARD-INVENTORY-BACKLOG.md — currently: `rsync`,
#     `patch -o`, `node`'s `fs.writeFileSync`, `7z`, `unzip -o`, `perl -i`,
#     `split`/`csplit`, `tar -x`/`--extract`, PowerShell `Set-Content` /
#     `Out-File` / `Add-Content` (partially covered; audit needed), and
#     `git apply`. `git checkout -- <path>` is a DELIBERATE NON-GOAL, not an
#     oversight: it restores a tracked file from git rather than writing
#     arbitrary content, and `git checkout -- .` is a routine developer
#     command — blocking it would be a material false positive, and false
#     positives in this file have proven as costly as bypasses.
#
# FAIL-CLOSED ON UNRESOLVABLE DESTINATIONS (security hardening, 2026-09) —
# the hook does NOT run a shell and cannot know what `$(...)`, a backtick
# command, `$VAR`, or `${VAR}` expands to. Rather than let an unresolvable
# write destination fall through to "no blocked extension found, allow" (a
# hole: `D=hydra_core/supervisor.py; echo x > "$D"` previously scored 0),
# any write-detecting branch whose reconstructed destination contains a
# shell expansion in unquoted or double-quoted context BLOCKS unconditionally
# — including inside an allow-listed worktree — with its own distinct
# message. An agent operating under HYDRA_ENFORCE_ROUTING=1 has no legitimate
# need to express a write destination through a variable or a command
# substitution; it can always write a literal path, and engine source must
# go through /hydra:run regardless. Cost: an agent that legitimately wants
# to write through `"$VAR"` must inline the literal path instead — a one-line
# fix, not a workflow blocker. A backslash-newline line continuation is
# resolved (joined) up front, since that IS statically resolvable and is not
# an expansion.
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

# --- Line-continuation normalisation (security hardening, 2026-09) ----------
# A backslash immediately followed by a newline (LF or CRLF) is removed along
# with the newline, joining the two halves into one token, exactly as a shell
# does before it tokenizes. Done ONCE here so every branch below (and every
# regex/argument reader) sees the already-joined command; without this,
# `> hydra_core/supervisor.\` + newline + `py` split a blocked extension
# across lines and evaded every branch (measured 0 against the guard at
# 29dbe89). This is statically resolvable — unlike variable/command
# substitution below — so it is resolved, not blocked.
#
# QUOTE-AWARE (revision, 2026-09): a shell only splices a backslash-newline in
# UNQUOTED and DOUBLE-QUOTED context. Inside SINGLE quotes, a backslash and a
# newline are both literal characters and a real shell writes them literally.
# An earlier version of this normalisation used a blind `-replace` over the
# WHOLE command string, which spliced a backslash-newline even inside single
# quotes — so `'docs/plan\<LF>s/x.html'` (to the shell: a path literally
# containing a backslash and a newline, NOT `docs/plans/x.html`) was rewritten
# into the clean carve-out path, recognised as `docs/plans/*.html`, and
# ALLOWED — a FALSE ALLOW, not a missed block. Remove-LineContinuations walks
# the same single/double/escape quote-state machine Read-ShellArgument already
# implements and splices ONLY where a shell actually would.
function Remove-LineContinuations {
    param([string]$s)
    $n = $s.Length
    $sb = New-Object System.Text.StringBuilder
    $inSingle = $false
    $inDouble = $false
    $i = 0
    while ($i -lt $n) {
        $c = $s[$i]
        if ($inSingle) {
            [void]$sb.Append($c)
            if ($c -eq "'") { $inSingle = $false }
            $i++
            continue
        }
        if ($c -eq '\') {
            # Backslash-newline (LF or CRLF): splice, but only outside single
            # quotes (unquoted or double-quoted — the two contexts where a
            # real shell performs this splice).
            if (($i + 1) -lt $n -and $s[$i + 1] -eq "`n") { $i += 2; continue }
            if (($i + 2) -lt $n -and $s[$i + 1] -eq "`r" -and $s[$i + 2] -eq "`n") { $i += 3; continue }
            # Not a line continuation: keep the backslash and whatever it
            # escapes verbatim (e.g. `\"` inside double quotes must not end
            # the quoted run).
            [void]$sb.Append($c)
            $i++
            if ($i -lt $n) { [void]$sb.Append($s[$i]); $i++ }
            continue
        }
        if ($c -eq "'" -and -not $inDouble) { $inSingle = $true; [void]$sb.Append($c); $i++; continue }
        if ($c -eq '"') { $inDouble = -not $inDouble; [void]$sb.Append($c); $i++; continue }
        [void]$sb.Append($c)
        $i++
    }
    return $sb.ToString()
}
$cmd = Remove-LineContinuations $cmd

# --- xargs -I replacement-string placeholders (security hardening, 2026-09) ---
# `xargs -I{} sh -c 'echo x > {}'` (or any other `-I<replstr>`) substitutes
# the literal replstr token with a line read from STDIN at RUNTIME — this
# hook only ever sees the literal placeholder text (e.g. `{}`) in the static
# command string, never the real destination. That is the identical
# "cannot know the real destination" situation as a `$VAR` or `$(...)`
# expansion (see the FAIL-CLOSED header comment above) and is treated through
# the SAME mechanism and the SAME distinguishable 'expansion' refusal reason,
# not a separate code path. `{}` is xargs's own default replstr; the regex
# below also captures a custom `-I<replstr>` (named or numbered) so
# `xargs -IFILE ... FILE` and similar spellings are covered too.
$_bwPlaceholders = New-Object System.Collections.Generic.List[string]
[void]$_bwPlaceholders.Add('{}')
foreach ($_xm in [regex]::Matches($cmd, '\bxargs\b[^;|&\n]*?-I\s*(\S+)')) {
    if ($_xm.Groups[1].Success) { [void]$_bwPlaceholders.Add($_xm.Groups[1].Value) }
}

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
    #
    # Also tracks HasExpansion: whether an unescaped `$` or backtick appeared
    # in UNQUOTED or DOUBLE-QUOTED state — the two contexts where a real shell
    # would actually expand it (`$(...)`, `${...}`, `$VAR`, or a backtick
    # command substitution). Inside SINGLE quotes `$`/backtick are always
    # literal and never set this flag. A backslash-escaped `\$` or `` \` ``
    # inside double quotes is also literal (per POSIX quoting rules) and does
    # not set the flag — this mirrors the same escape set already honoured
    # for `\"`/`\\` in that branch.
    param([string]$s, [int]$start)
    $n = $s.Length
    $i = [Math]::Max(0, $start)
    while ($i -lt $n -and $s[$i] -match '[ \t]') { $i++ }
    $beginIdx = $i
    if ($i -ge $n -or $s[$i] -match '[;|&<>\r\n]') {
        return [pscustomobject]@{ Value = $null; Start = $beginIdx; End = $i; HasExpansion = $false }
    }
    $sb = New-Object System.Text.StringBuilder
    $hasExpansion = $false
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
                    ($s[$i + 1] -eq '"' -or $s[$i + 1] -eq '\' -or
                     $s[$i + 1] -eq '$' -or $s[$i + 1] -eq '`')) {
                    [void]$sb.Append($s[$i + 1]); $i += 2; continue
                }
                if ($s[$i] -eq '$' -or $s[$i] -eq '`') { $hasExpansion = $true }
                [void]$sb.Append($s[$i]); $i++
            }
            if ($i -lt $n) { $i++ }
            continue
        }
        if ($c -match '[ \t;|&<>\r\n]') { break }
        if ($c -eq '\' -and ($i + 1) -lt $n) {
            [void]$sb.Append($s[$i + 1]); $i += 2; continue
        }
        if ($c -eq '$' -or $c -eq '`') { $hasExpansion = $true }
        [void]$sb.Append($c); $i++
    }
    return [pscustomobject]@{ Value = $sb.ToString(); Start = $beginIdx; End = $i; HasExpansion = $hasExpansion }
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

function Get-ShellBasename {
    # Returns the final path segment of a (POSIX- or Windows-style) path
    # string. Used to synthesise the real write destination of a `-t DIR` /
    # `--target-directory=DIR` invocation: cp/mv/install/ln write DIR joined
    # with each source's basename, never DIR in isolation, so
    # `cp supervisor.py -t hydra_core` must be checked as
    # `hydra_core/supervisor.py`, not as `hydra_core`.
    param([string]$p)
    $trimmed = $p.TrimEnd('/', '\')
    if (-not $trimmed) { return $p }
    $idx = $trimmed.LastIndexOfAny([char[]]('/', '\'))
    if ($idx -ge 0) { return $trimmed.Substring($idx + 1) }
    return $trimmed
}

function Test-IsCommandWord {
    # PREFIX-CHAIN WALK (2026-09) — the false-positive discriminator for
    # dd/truncate/ln/install: a word like "install" only counts as ITS OWN
    # command when the ENTIRE run of tokens between it and the nearest
    # statement separator (`;`/`&`/`|`/newline, or start of string) is made
    # up exclusively of: a known wrapper command (sudo/env/nice/command/exec/
    # time/nohup/stdbuf/ionice/setsid), a `VAR=value` assignment, an option
    # token (`-x`), or the value of a value-taking option belonging to one of
    # those wrappers. The first bare word in that run that is none of those
    # IS the real command, and our word is its argument — this is what keeps
    # `npm install`, `sudo npm install`, `env FOO=1 npm install`, etc. allowed
    # while still catching `command install`, `\install` (alias suppression),
    # `env FOO=1 install`, `sudo -u root install`, `nice -n 5 install`,
    # `exec install`, and `time install`.
    #
    # The value-taking-option table is a small, closed, per-wrapper list of
    # real CLI contracts (sudo -u/-g/-p/-C, nice -n, env -u, ionice -c/-n,
    # stdbuf -i/-o/-e; command/exec/time/nohup/setsid take none). An option
    # not in this table is never assumed to take a value — the safe direction
    # for false positives, since treating its next word as a value (instead
    # of as the real command) is what could turn `sudo npm install` into a
    # false block.
    param([string]$s, [int]$wordStart)

    $wrappers = @('sudo', 'env', 'nice', 'command', 'exec', 'time', 'nohup', 'stdbuf', 'ionice', 'setsid')
    $valueOpts = @{
        'sudo'   = @('-u', '-g', '-p', '-C')
        'nice'   = @('-n')
        'env'    = @('-u')
        'ionice' = @('-c', '-n')
        'stdbuf' = @('-i', '-o', '-e')
    }

    # A `\` glued directly onto the word (no space) suppresses alias
    # expansion and does not change tokenization — the boundary check below
    # runs from the backslash's own position instead of the word's.
    $boundary = $wordStart
    if ($boundary -gt 0 -and $s[$boundary - 1] -eq '\') { $boundary-- }

    # Find the start of the statement this word lives in: the character just
    # after the nearest `;`/`&`/`|`/newline before $boundary, or 0.
    $runStart = 0
    $k = $boundary - 1
    while ($k -ge 0) {
        $ck = $s[$k]
        if ($ck -eq ';' -or $ck -eq '&' -or $ck -eq '|' -or $ck -eq "`n" -or $ck -eq "`r") { $runStart = $k + 1; break }
        $k--
    }

    $run = $s.Substring($runStart, $boundary - $runStart).Trim()
    if (-not $run) { return $true }
    $tokens = [regex]::Split($run, '\s+') | Where-Object { $_ -ne '' }

    $lastWrapper = $null
    $expectingValueFor = $null
    foreach ($t in $tokens) {
        if ($expectingValueFor) {
            $expectingValueFor = $null
            continue
        }
        if ($t -match '^[A-Za-z_][A-Za-z0-9_]*=') { continue }
        if ($t.StartsWith('-')) {
            if ($lastWrapper -and $valueOpts.ContainsKey($lastWrapper) -and ($t -in $valueOpts[$lastWrapper])) {
                $expectingValueFor = $lastWrapper
            }
            continue
        }
        if ($t -in $wrappers) { $lastWrapper = $t; continue }
        # A bare word that is none of the above IS the real command — every
        # token from here on (including our own word) is its argument.
        return $false
    }
    return $true
}

function Get-BalancedParenText {
    # Given the index of an OPENING '(' character, returns the text strictly
    # between it and its matching ')' (quote-aware, so a ')' or '(' inside a
    # string literal doesn't unbalance the count), plus the index just past
    # the matching ')'. Used to read a call's full argument list (e.g.
    # Path(...).open(mode='w')) so a keyword argument can be found anywhere
    # inside it, not just in the first positional slot.
    param([string]$s, [int]$openParenIdx)
    $n = $s.Length
    $i = $openParenIdx
    if ($i -ge $n -or $s[$i] -ne '(') { return $null }
    $depth = 0
    $start = $i + 1
    while ($i -lt $n) {
        $c = $s[$i]
        if ($c -eq "'" -or $c -eq '"') {
            $q = $c; $i++
            while ($i -lt $n -and $s[$i] -ne $q) {
                if ($s[$i] -eq '\' -and ($i + 1) -lt $n) { $i += 2; continue }
                $i++
            }
            if ($i -lt $n) { $i++ }
            continue
        }
        if ($c -eq '(') { $depth++; $i++; continue }
        if ($c -eq ')') {
            $depth--
            if ($depth -eq 0) { return [pscustomobject]@{ Text = $s.Substring($start, $i - $start); End = $i + 1 } }
            $i++; continue
        }
        $i++
    }
    return $null
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

# --- DESTINATION-EXPRESSION RESOLUTION (security hardening, 2026-09) --------
# ROOT CAUSE this closes: Read-PyStringLiteral (above) folds only ADJACENT
# literals (`'a' 'b'`), but its callers then require a `,` or `)` immediately
# after the folded run. `'hydra_core/' + 'supervisor.py'` (Python `+`
# concatenation) is not adjacency, so the whole destination-detection branch
# stopped parsing right there and the write went unexamined — proven 0 on
# BOTH 29dbe89 and every revision through c634849, for all three python -c
# branches (open/Path/shutil).
#
# Read-PyDestExpr reads a whole destination EXPRESSION — a chain of string
# literals joined by any mix of implicit adjacency and `+`, arbitrary
# whitespace allowed — and classifies it: Resolvable=$true with a folded
# Value when every operand is a plain string literal (an f-string with no
# `{...}` placeholder counts as plain), Resolvable=$false the moment ANY
# operand is not a literal (a bare name, an attribute, a call such as
# os.path.join(...), an f-string containing a placeholder, or any operand of
# `+` that isn't itself a literal). Either way End is left positioned at the
# true top-level terminator (the `,` or `)` that closes this argument), by
# walking a full paren/bracket/quote-aware skip over a non-literal operand
# rather than just stopping where recognition failed — otherwise callers
# would look for `,`/`)` in the middle of an unresolved expression and never
# reach the mode/second-argument check that decides whether to block at all.
#
# An unresolvable destination is treated through the SAME mechanism as a
# shell `$VAR`/`$(...)` expansion: the caller passes `-not $arg.Resolvable`
# as Test-BlockedDest's `$hasExpansion`, which fails closed and prints the
# same distinguishable "UNRESOLVABLE destination" refusal — not a fourth
# mechanism.
function Read-PyLiteralAtom {
    # Reads ONE (optionally prefixed: f/F/r/R/b/B/u/U, alone or paired, e.g.
    # rb/fr) Python string literal atom starting at $start. Returns $null if
    # this position is not such a literal. HasPlaceholder is set when an
    # f-string contains an unescaped `{...}` (an f-string with NO placeholder,
    # e.g. f'hydra_core/supervisor.py', is just a literal and is not flagged).
    param([string]$s, [int]$start)
    $n = $s.Length
    $i = [Math]::Max(0, $start)
    $prefixEnd = $i
    while ($prefixEnd -lt $n -and $prefixEnd -lt ($i + 2) -and $s[$prefixEnd] -match '[fFrRbBuU]') { $prefixEnd++ }
    if ($prefixEnd -ge $n -or ($s[$prefixEnd] -ne "'" -and $s[$prefixEnd] -ne '"')) { return $null }
    $isF = $s.Substring($i, $prefixEnd - $i) -match '[fF]'
    $q = $s[$prefixEnd]
    $j = $prefixEnd + 1
    $sb = New-Object System.Text.StringBuilder
    $hasPlaceholder = $false
    while ($j -lt $n -and $s[$j] -ne $q) {
        if ($s[$j] -eq '\' -and ($j + 1) -lt $n) { [void]$sb.Append($s[$j + 1]); $j += 2; continue }
        if ($isF -and $s[$j] -eq '{') {
            if (($j + 1) -lt $n -and $s[$j + 1] -eq '{') { [void]$sb.Append('{'); $j += 2; continue }
            $hasPlaceholder = $true
        } elseif ($isF -and $s[$j] -eq '}' -and ($j + 1) -lt $n -and $s[$j + 1] -eq '}') {
            [void]$sb.Append('}'); $j += 2; continue
        }
        [void]$sb.Append($s[$j]); $j++
    }
    if ($j -lt $n) { $j++ }
    return [pscustomobject]@{ Value = $sb.ToString(); End = $j; HasPlaceholder = $hasPlaceholder }
}

function Read-PyDestExpr {
    param([string]$s, [int]$start)
    $n = $s.Length
    $i = [Math]::Max(0, $start)
    while ($i -lt $n -and $s[$i] -match '[ \t]') { $i++ }
    if ($i -ge $n) { return $null }
    $beginIdx = $i
    $sb = New-Object System.Text.StringBuilder
    $resolvable = $true
    $startI = $i
    while ($true) {
        while ($i -lt $n -and $s[$i] -match '[ \t]') { $i++ }
        $atom = Read-PyLiteralAtom $s $i
        if ($atom) {
            if ($atom.HasPlaceholder) { $resolvable = $false }
            [void]$sb.Append($atom.Value)
            $i = $atom.End
        } else {
            # Non-literal operand (bare name, attribute, call, number, ...):
            # mark unresolvable and skip the whole balanced sub-expression so
            # $i still lands on the true top-level terminator afterward.
            $resolvable = $false
            $depth = 0
            while ($i -lt $n) {
                $c = $s[$i]
                if ($c -eq "'" -or $c -eq '"') {
                    $q = $c; $i++
                    while ($i -lt $n -and $s[$i] -ne $q) {
                        if ($s[$i] -eq '\' -and ($i + 1) -lt $n) { $i += 2; continue }
                        $i++
                    }
                    if ($i -lt $n) { $i++ }
                    continue
                }
                if ($c -eq '(' -or $c -eq '[' -or $c -eq '{') { $depth++; $i++; continue }
                if ($c -eq ')' -or $c -eq ']' -or $c -eq '}') {
                    if ($depth -eq 0) { break }
                    $depth--; $i++; continue
                }
                if ($depth -eq 0 -and ($c -eq ',' -or $c -eq '+')) { break }
                if ($depth -eq 0 -and $c -match '[ \t]') {
                    $peek = $i
                    while ($peek -lt $n -and $s[$peek] -match '[ \t]') { $peek++ }
                    if ($peek -ge $n -or $s[$peek] -eq ',' -or $s[$peek] -eq ')' -or $s[$peek] -eq '+') { $i = $peek; break }
                    $i = $peek
                    continue
                }
                $i++
            }
        }
        # Continue the chain on an explicit top-level '+', or on implicit
        # adjacency (another literal directly follows); otherwise stop here.
        $save = $i
        while ($i -lt $n -and $s[$i] -match '[ \t]') { $i++ }
        if ($i -lt $n -and $s[$i] -eq '+') { $i++; continue }
        $peekAtom = Read-PyLiteralAtom $s $i
        if ($peekAtom) { continue }
        $i = $save
        break
    }
    if ($i -eq $startI) { return $null }   # nothing here at all (e.g. an empty argument)
    return [pscustomobject]@{ Value = $sb.ToString(); Start = $beginIdx; End = $i; Resolvable = $resolvable }
}

# Set by Test-BlockedDest on its most recent call so the final reporting block
# can tell an "unresolvable destination" block apart from an ordinary
# "blocked extension" block and print a distinguishable operator message.
$script:bwUnresolvedReason = $null

function Test-BlockedDest {
    param([string]$dest, [int]$atIndex = [int]::MaxValue, [bool]$hasExpansion = $false)
    $script:bwUnresolvedReason = $null
    # FAIL CLOSED on an unresolvable destination: a shell expansion in the
    # reconstructed argument means this hook cannot statically know the real
    # destination, so it cannot be waved through even when everything else
    # about it (worktree membership, allow-listed dir, extension) looks fine.
    # See the FAIL-CLOSED header comment at the top of this file. Checked
    # BEFORE the empty-$dest guard below: a Read-PyDestExpr fallback over a
    # non-literal operand (a bare name, os.path.join(...), ...) legitimately
    # folds to an EMPTY Value (it never appends non-literal content) while
    # still being a real, unresolvable destination — `if (-not $dest)` alone
    # would have waved that through as "no destination at all" instead of
    # failing closed.
    if ($hasExpansion) {
        $script:bwUnresolvedReason = 'expansion'
        return $true
    }
    if (-not $dest) { return $false }
    # $dest arrives already fully unquoted/reconstructed from Read-ShellArgument
    # or Read-PyStringLiteral; Trim() here is a harmless no-op safety net for
    # any caller that still passes a raw single-quote-wrapped literal.
    $raw = $dest.Trim('"''').Replace('/', '\')
    # FAIL CLOSED on an xargs replacement-string placeholder for the same
    # reason as an unresolvable expansion above: the real destination arrives
    # from stdin at runtime and this hook cannot know it statically. Reuses
    # the identical mechanism/reason so the reporting block below prints the
    # same distinguishable "UNRESOLVABLE destination" message.
    if ($_bwPlaceholders -contains $raw) {
        $script:bwUnresolvedReason = 'expansion'
        return $true
    }
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
#    `>|` (the noclobber-override clobber operator, e.g. `echo x >|file.py`
#    or `echo x >| file.py`) previously matched the bare `>` with zero
#    whitespace, leaving the reader positioned on `|`, which reads as a
#    statement separator and yields no argument at all — the destination was
#    never examined (measured 0). The operator pattern now accepts an
#    optional `|` immediately after the `>`/`>>` and before the optional
#    whitespace, so both the spaced and unspaced forms resolve to the same
#    reconstructed destination as plain `>`. A genuine pipe after a normal
#    redirect (`echo x > a.txt | grep y`) is unaffected: the required
#    whitespace before `|` already ends the destination read there, and the
#    `\|?` here only ever consumes a `|` glued directly onto the `>`.
if (-not $matched) {
    $hits = [regex]::Matches($cmd, '>{1,2}\|?\s*')
    foreach ($hit in $hits) {
        $tok = Read-ShellArgument $cmd ($hit.Index + $hit.Length)
        if ($tok.Value -and (Test-BlockedDest $tok.Value $tok.Start $tok.HasExpansion)) {
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
        if ($tok.Value -and (Test-BlockedDest $tok.Value $tok.Start $tok.HasExpansion)) {
            $matched = $true
            $reason = "tee to '$($tok.Value)'"
            break
        }
    }
}

# 3. cp / mv / copy / move — the LAST reconstructed argument of the invocation
#    (up to the next statement separator) is the destination, UNLESS `-t DIR`
#    / `--target-directory=DIR` is present. `-t` is only recognised as
#    target-directory when `cp`/`mv`/`copy`/`move` is the COMMAND WORD itself
#    (see Test-IsCommandWord) — `-t` means something else entirely for tar
#    (list), sort (field separator), docker/ssh (tty), systemctl (unit type),
#    timeout (duration), etc., and those commands never even reach this
#    branch. With `-t`/`--target-directory=` present, EVERY other operand is a
#    SOURCE (none is a destination) and the real write target is DIR joined
#    with each source's basename — `cp a.py -t dir` writes `dir/a.py`, never
#    `dir` itself.
#    e.g.  cp template.py src/newfile.py    mv old.js new.ts
#          cp supervisor.py -t hydra_core   (writes hydra_core/supervisor.py)
if (-not $matched) {
    $hits = [regex]::Matches($cmd, '\b(?:cp|mv|copy|move)\b')
    foreach ($hit in $hits) {
        $argsList = Get-ShellArgsInRange $cmd ($hit.Index + $hit.Length) $cmd.Length
        $targetDirTok = $null
        $operands = New-Object System.Collections.Generic.List[object]
        $awaitingTargetDir = $false
        if (Test-IsCommandWord $cmd $hit.Index) {
            foreach ($t in $argsList) {
                if ($awaitingTargetDir) {
                    $targetDirTok = [pscustomobject]@{ Value = $t.Value; Start = $t.Start; HasExpansion = $t.HasExpansion }
                    $awaitingTargetDir = $false
                    continue
                }
                if ($t.Value -match '^-t(.*)$') {
                    if ($Matches[1]) {
                        $targetDirTok = [pscustomobject]@{ Value = $Matches[1]; Start = $t.Start; HasExpansion = $t.HasExpansion }
                    } else { $awaitingTargetDir = $true }
                    continue
                }
                if ($t.Value -match '^--target-directory=(.*)$') {
                    $targetDirTok = [pscustomobject]@{ Value = $Matches[1]; Start = $t.Start; HasExpansion = $t.HasExpansion }
                    continue
                }
                if ($t.Value -match '^-') { continue }
                [void]$operands.Add($t)
            }
        }
        if ($targetDirTok) {
            foreach ($src in $operands) {
                $joined = "$($targetDirTok.Value)/$(Get-ShellBasename $src.Value)"
                $expFlag = $targetDirTok.HasExpansion -or $src.HasExpansion
                if (Test-BlockedDest $joined $targetDirTok.Start $expFlag) {
                    $matched = $true
                    $reason = "cp/mv/copy/move to '$joined'"
                    break
                }
            }
            if ($matched) { break }
            continue
        }
        if ($argsList.Count -ge 2) {
            $destTok = $argsList[$argsList.Count - 1]
            if (Test-BlockedDest $destTok.Value $destTok.Start $destTok.HasExpansion) {
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
    # Locate `open(` then reconstruct arg1 (destination EXPRESSION — may be
    # fragmented across adjacent literals and/or joined with `+`) via
    # Read-PyDestExpr, and arg2 (mode) via Read-PyStringLiteral. Arg1 used to
    # be read with Read-PyStringLiteral (adjacency-only) and its destination
    # was NEVER passed through Test-BlockedDest — ANY write-mode open() was
    # treated as a hit regardless of destination, which is why
    # open('notes.md','w') and open('docs/plans/p.html','w') (a destination
    # the docs/plans carve-out exists specifically to allow) both refused.
    # Read-PyDestExpr also fails closed on an unresolvable destination (a
    # bare name, os.path.join(...), an f-string with a `{...}` placeholder)
    # by setting Resolvable=$false, which is passed through as
    # Test-BlockedDest's $hasExpansion — the open() branch was the one place
    # still failing OPEN on that shape while every other branch fails closed.
    #        `io.open(...)` and `codecs.open(...)` need no alternation of
    #        their own: the char immediately before `open` in both is `.`, a
    #        non-word character, so the bare `\bopen\s*\(` word-boundary
    #        match already fires on `io.open(` and `codecs.open(` exactly as
    #        it does on unqualified `open(`. An earlier revision added an
    #        explicit `io\s*\.\s*open|codecs\s*\.\s*open` alternation here;
    #        it was dead code (removed 2026-09) — TestIoCodecsOpen below
    #        still proves the behavior holds without it.
    $openHits = [regex]::Matches($cmd, '\bopen\s*\(\s*')
    foreach ($oh in $openHits) {
        $arg1 = Read-PyDestExpr $cmd ($oh.Index + $oh.Length)
        if (-not $arg1) { continue }
        $j = $arg1.End
        while ($j -lt $cmd.Length -and $cmd[$j] -match '[ \t]') { $j++ }
        if ($j -ge $cmd.Length -or $cmd[$j] -ne ',') { continue }
        $j++
        while ($j -lt $cmd.Length -and $cmd[$j] -match '[ \t]') { $j++ }
        $arg2 = Read-PyStringLiteral $cmd $j
        if ($arg2 -and ($arg2.Value -match '[wax]') -and
            (Test-BlockedDest $arg1.Value $arg1.Start (-not $arg1.Resolvable))) {
            $matched = $true
            $reason  = "python -c with open() in write/append/exclusive mode ('$($arg2.Value)') targeting '$($arg1.Value)'"
            break
        }
    }
}
#    4b. pathlib.Path(...).write_text / write_bytes / open(mode) — scan
#        directly for the method call pattern and test the captured filename.
#        Does not rely on the -c prefix so it works even when the Python code
#        contains semicolons (which would stop a [^;|&\n]* lookahead before
#        reaching the call).
#        e.g.  python -c "from pathlib import Path; Path('x.py').write_text('...')"
#              python -c "from pathlib import Path; Path('x.py').open('w')"
#              python -c "from pathlib import Path; Path('x.py').open(mode='w')"
#        `.open()` with no args (or a bare 'r') defaults to read mode and is
#        NOT a write — matches the same write-mode convention as every other
#        branch (a mode containing w/a/x is a write; bare 'r' is not).
if (-not $matched) {
    $plHits = [regex]::Matches($cmd, '\bPath\s*\(\s*')
    foreach ($ph in $plHits) {
        $arg = Read-PyDestExpr $cmd ($ph.Index + $ph.Length)
        if (-not $arg) { continue }
        $j = $arg.End
        while ($j -lt $cmd.Length -and $cmd[$j] -match '[ \t]') { $j++ }
        if ($j -ge $cmd.Length -or $cmd[$j] -ne ')') { continue }
        $j++
        $rest = $cmd.Substring($j)
        if ($rest -match '^\s*\.\s*write_(?:text|bytes)\b') {
            if (Test-BlockedDest $arg.Value $arg.Start (-not $arg.Resolvable)) {
                $matched = $true
                $reason = "pathlib.Path.write_text/write_bytes to '$($arg.Value)'"
                break
            }
            continue
        }
        $openM = [regex]::Match($rest, '^\s*\.\s*open\s*\(')
        if ($openM.Success) {
            $openParenIdx = $j + $openM.Length - 1
            $bal = Get-BalancedParenText $cmd $openParenIdx
            $modeVal = $null
            if ($bal -and $bal.Text.Trim()) {
                $posLit = Read-PyStringLiteral $bal.Text 0
                if ($posLit) {
                    $modeVal = $posLit.Value
                } else {
                    $kwm = [regex]::Match($bal.Text, "mode\s*=\s*")
                    if ($kwm.Success) {
                        $kwLit = Read-PyStringLiteral $bal.Text ($kwm.Index + $kwm.Length)
                        if ($kwLit) { $modeVal = $kwLit.Value }
                    }
                }
            }
            if ($modeVal -and ($modeVal -match '[wax]')) {
                if (Test-BlockedDest $arg.Value $arg.Start (-not $arg.Resolvable)) {
                    $matched = $true
                    $reason = "pathlib.Path.open('$modeVal') targets '$($arg.Value)'"
                    break
                }
            }
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
        $a1 = Read-PyDestExpr $cmd ($sh.Index + $sh.Length)
        if (-not $a1) { continue }
        $j = $a1.End
        while ($j -lt $cmd.Length -and $cmd[$j] -match '[ \t]') { $j++ }
        if ($j -ge $cmd.Length -or $cmd[$j] -ne ',') { continue }
        $j++
        while ($j -lt $cmd.Length -and $cmd[$j] -match '[ \t]') { $j++ }
        $a2 = Read-PyDestExpr $cmd $j
        if ($a2 -and (Test-BlockedDest $a2.Value $a2.Start (-not $a2.Resolvable))) {
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
            # The FAIL-CLOSED expansion check applies only to the LAST
            # non-flag token — sed's actual file destination — not to every
            # non-flag token. sed's own SCRIPT argument (e.g. "s/$OLD/new/")
            # is also a non-flag token and legitimately contains `$VAR`
            # without naming a write destination at all; treating it as an
            # unresolvable destination was a false positive this revision
            # must not introduce. Every non-flag token still gets the plain
            # extension check (unchanged, catches a fragmented filename in
            # any position), only the expansion fail-close is last-token-only.
            $nonFlagToks = New-Object System.Collections.Generic.List[object]
            foreach ($t in $argsList) { if ($t.Value -notmatch '^-') { [void]$nonFlagToks.Add($t) } }
            for ($ti = 0; $ti -lt $nonFlagToks.Count; $ti++) {
                $t = $nonFlagToks[$ti]
                $isLast = ($ti -eq ($nonFlagToks.Count - 1))
                $expFlag = $isLast -and $t.HasExpansion
                if (Test-BlockedDest $t.Value $t.Start $expFlag) {
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
        # FAIL-CLOSED scope guard: -Value's payload is ALSO a bare positional
        # token once -Path is given by name, and Set-Content/Out-File's own
        # positional binding order is Path THEN Value — so only the ONE
        # destination slot (the named -Path/-FilePath/-LiteralPath value, its
        # inline form, or else the first positional token when no such named
        # flag is used) is a plausible destination. Without this, a
        # completely ordinary `Set-Content -Path notes.md -Value "$USER"`
        # was flagged as an unresolvable destination because of `-Value`'s
        # own payload, not the actual (literal, fine) destination. The plain
        # extension check still runs against every non-flag token as before
        # (unchanged, catches a fragmented filename in any position); only
        # the expansion fail-close is scoped to the genuine destination slot,
        # tracked here as $pathResolved once that slot has been filled.
        $pathResolved = $false
        foreach ($tok in $argsList) {
            if ($nextIsPathValue) {
                if (Test-BlockedDest $tok.Value $tok.Start $tok.HasExpansion) {
                    $matched = $true
                    $reason = "Set-Content/Out-File to '$($tok.Value)'"
                    break
                }
                $nextIsPathValue = $false
                $pathResolved = $true
            } elseif ($tok.Value -match '^-(?:Path|FilePath|LiteralPath)(?::(.*))?$') {
                # Flag with inline value (-Path:foo.py) or flag expecting next token
                $inline = $Matches[1]
                if ($inline) {
                    if (Test-BlockedDest $inline $tok.Start) {
                        $matched = $true
                        $reason = "Set-Content/Out-File to '$inline'"
                        break
                    }
                    $pathResolved = $true
                } else {
                    $nextIsPathValue = $true
                }
            } elseif ($tok.Value -notmatch '^-') {
                # Positional argument (not a flag name or flag value)
                $expFlag = (-not $pathResolved) -and $tok.HasExpansion
                if (Test-BlockedDest $tok.Value $tok.Start $expFlag) {
                    $matched = $true
                    $reason = "Set-Content/Out-File to '$($tok.Value)'"
                    break
                }
                $pathResolved = $true
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
        if ($tok.Value -and (Test-BlockedDest $tok.Value $tok.Start $tok.HasExpansion)) {
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

# --- INVENTORY EXTENSION (2026-09): write idioms the parser never even
# looked at, so their destination was never examined at all. The destination
# PARSER (Read-ShellArgument / Get-ShellArgsInRange / Read-PyDestExpr /
# Test-BlockedDest) is unchanged and reused as-is by every branch below —
# this closes coverage GAPS in which write idioms are recognised, not
# resolution gaps. See Test-IsCommandWord above: `dd`/`truncate`/`ln`/
# `install` are treated as THIS hook's write idiom only in command-word
# position, never as a subcommand argument of another program (`npm
# install`, `pip install`, `apt-get install`, `cargo install`, ...) — a
# package name is not a path.

# 8. dd if=... of=DEST — the destination is the `of=` operand. `dd` without
#    `of=` writes stdout and is not a file write, so no `of=` -> no match.
#    e.g.  dd if=/dev/zero of=hydra_core/supervisor.py
if (-not $matched) {
    $ddHits = [regex]::Matches($cmd, '\bdd\b')
    foreach ($dh in $ddHits) {
        if (-not (Test-IsCommandWord $cmd $dh.Index)) { continue }
        $argsList = Get-ShellArgsInRange $cmd ($dh.Index + $dh.Length) $cmd.Length
        foreach ($t in $argsList) {
            if ($t.Value -match '^of=(.*)$') {
                if (Test-BlockedDest $Matches[1] $t.Start $t.HasExpansion) {
                    $matched = $true
                    $reason = "dd of= write to '$($Matches[1])'"
                }
                break
            }
        }
        if ($matched) { break }
    }
}

# 9. truncate [opts] FILE... — every non-option operand is a file it WRITES
#    (truncates to a given size). `-s`/`--size` takes a value that is NOT a
#    path; `-r`/`--reference` takes a reference file it READS, not writes —
#    both flags' values (attached or as the next token) are consumed and
#    skipped, never tested as a destination.
#    e.g.  truncate -s 0 hydra_core/supervisor.py
if (-not $matched) {
    $truncHits = [regex]::Matches($cmd, '\btruncate\b')
    foreach ($th in $truncHits) {
        if (-not (Test-IsCommandWord $cmd $th.Index)) { continue }
        $argsList = Get-ShellArgsInRange $cmd ($th.Index + $th.Length) $cmd.Length
        $skipNext = $false
        foreach ($t in $argsList) {
            if ($skipNext) { $skipNext = $false; continue }
            if ($t.Value -match '^-s(.*)$') { if (-not $Matches[1]) { $skipNext = $true }; continue }
            if ($t.Value -match '^--size(?:=.*)?$') { if ($t.Value -eq '--size') { $skipNext = $true }; continue }
            if ($t.Value -match '^-r(.*)$') { if (-not $Matches[1]) { $skipNext = $true }; continue }
            if ($t.Value -match '^--reference(?:=.*)?$') { if ($t.Value -eq '--reference') { $skipNext = $true }; continue }
            if ($t.Value -match '^-') { continue }
            if (Test-BlockedDest $t.Value $t.Start $t.HasExpansion) {
                $matched = $true
                $reason = "truncate write to '$($t.Value)'"
                break
            }
        }
        if ($matched) { break }
    }
}

# 10. ln [opts] TARGET LINKNAME  /  ln [opts] TARGET... DIRECTORY — the
#     destination is the LINK (or the directory the links land in), never the
#     TARGET being linked to: reading a protected path is not a write. The
#     last operand is the destination unless `-t DIR` / `--target-directory=
#     DIR` names it explicitly. Every other leading-dash token (-s, -f, -n,
#     -v, -b, ...) is a value-less flag and is skipped.
#     e.g.  ln -sf a hydra_core/supervisor.py        (creates the link there)
#           ln -s hydra_core/supervisor.py mylink    (only READS the target — allowed)
if (-not $matched) {
    $lnHits = [regex]::Matches($cmd, '\bln\b')
    foreach ($lh in $lnHits) {
        if (-not (Test-IsCommandWord $cmd $lh.Index)) { continue }
        $argsList = Get-ShellArgsInRange $cmd ($lh.Index + $lh.Length) $cmd.Length
        $targetDirTok = $null
        $operands = New-Object System.Collections.Generic.List[object]
        $awaitingTargetDir = $false
        foreach ($t in $argsList) {
            if ($awaitingTargetDir) {
                $targetDirTok = [pscustomobject]@{ Value = $t.Value; Start = $t.Start; HasExpansion = $t.HasExpansion }
                $awaitingTargetDir = $false
                continue
            }
            if ($t.Value -match '^-t(.*)$') {
                if ($Matches[1]) {
                    $targetDirTok = [pscustomobject]@{ Value = $Matches[1]; Start = $t.Start; HasExpansion = $t.HasExpansion }
                } else { $awaitingTargetDir = $true }
                continue
            }
            if ($t.Value -match '^--target-directory=(.*)$') {
                $targetDirTok = [pscustomobject]@{ Value = $Matches[1]; Start = $t.Start; HasExpansion = $t.HasExpansion }
                continue
            }
            if ($t.Value -match '^-') { continue }
            [void]$operands.Add($t)
        }
        if ($targetDirTok) {
            # Every operand is a TARGET being linked TO (read, not written);
            # the real write is DIR joined with each operand's basename —
            # `ln -t hydra_core supervisor.py` writes `hydra_core/supervisor.py`.
            foreach ($src in $operands) {
                $joined = "$($targetDirTok.Value)/$(Get-ShellBasename $src.Value)"
                $expFlag = $targetDirTok.HasExpansion -or $src.HasExpansion
                if (Test-BlockedDest $joined $targetDirTok.Start $expFlag) {
                    $matched = $true
                    $reason = "ln creates link at '$joined'"
                    break
                }
            }
        } elseif ($operands.Count -ge 2) {
            $destTok = $operands[$operands.Count - 1]
            if (Test-BlockedDest $destTok.Value $destTok.Start $destTok.HasExpansion) {
                $matched = $true
                $reason = "ln creates link at '$($destTok.Value)'"
            }
        }
        if ($matched) { break }
    }
}

# 11. install [opts] SOURCE... DEST — the coreutils file-copying form writes
#     its last operand, or the `-t DIR` / `--target-directory=DIR` directory.
#     `-m`/`-o`/`-g` (mode/owner/group) take a value that is NOT a path and is
#     consumed and skipped, attached or as the next token, short or long form.
#     `install` is OVERWHELMINGLY a package-manager verb (npm/pip/apt/cargo/
#     go/gem/composer/choco/winget/brew install ...) — Test-IsCommandWord is
#     the load-bearing guard here: it fires only when `install` is the
#     command word itself, never a subcommand argument of another program, so
#     `npm install express`, `pip install ruamel.yaml`, `go install
#     example.com/cmd/tool@latest`, etc. are never even considered.
#     e.g.  install /dev/null hydra_core/supervisor.py
if (-not $matched) {
    $instHits = [regex]::Matches($cmd, '\binstall\b')
    foreach ($ih in $instHits) {
        if (-not (Test-IsCommandWord $cmd $ih.Index)) { continue }
        $argsList = Get-ShellArgsInRange $cmd ($ih.Index + $ih.Length) $cmd.Length
        $targetDirTok = $null
        $operands = New-Object System.Collections.Generic.List[object]
        $skipNext = $false
        $awaitingTargetDir = $false
        foreach ($t in $argsList) {
            if ($awaitingTargetDir) {
                $targetDirTok = [pscustomobject]@{ Value = $t.Value; Start = $t.Start; HasExpansion = $t.HasExpansion }
                $awaitingTargetDir = $false
                continue
            }
            if ($skipNext) { $skipNext = $false; continue }
            if ($t.Value -match '^-t(.*)$') {
                if ($Matches[1]) {
                    $targetDirTok = [pscustomobject]@{ Value = $Matches[1]; Start = $t.Start; HasExpansion = $t.HasExpansion }
                } else { $awaitingTargetDir = $true }
                continue
            }
            if ($t.Value -match '^--target-directory=(.*)$') {
                $targetDirTok = [pscustomobject]@{ Value = $Matches[1]; Start = $t.Start; HasExpansion = $t.HasExpansion }
                continue
            }
            if ($t.Value -match '^-[mog](.*)$') { if (-not $Matches[1]) { $skipNext = $true }; continue }
            if ($t.Value -match '^--(?:mode|owner|group)(?:=.*)?$') {
                if ($t.Value -notmatch '=') { $skipNext = $true }
                continue
            }
            if ($t.Value -match '^-') { continue }
            [void]$operands.Add($t)
        }
        if ($targetDirTok) {
            # Every operand is a SOURCE being installed (read); the real write
            # is DIR joined with each source's basename — `install a.py -t
            # hydra_core` writes `hydra_core/a.py`, never `hydra_core` itself.
            foreach ($src in $operands) {
                $joined = "$($targetDirTok.Value)/$(Get-ShellBasename $src.Value)"
                $expFlag = $targetDirTok.HasExpansion -or $src.HasExpansion
                if (Test-BlockedDest $joined $targetDirTok.Start $expFlag) {
                    $matched = $true
                    $reason = "install write to '$joined'"
                    break
                }
            }
        } elseif ($operands.Count -ge 2) {
            $destTok = $operands[$operands.Count - 1]
            if (Test-BlockedDest $destTok.Value $destTok.Start $destTok.HasExpansion) {
                $matched = $true
                $reason = "install write to '$($destTok.Value)'"
            }
        }
        if ($matched) { break }
    }
}

# 12. python -c os.replace(src, dst) / os.rename(src, dst) — destination is
#     the SECOND argument; the first (src) is only read.
if (-not $matched) {
    $osrHits = [regex]::Matches($cmd, '\bos\s*\.\s*(?:replace|rename)\s*\(\s*')
    foreach ($oh in $osrHits) {
        $a1 = Read-PyDestExpr $cmd ($oh.Index + $oh.Length)
        if (-not $a1) { continue }
        $j = $a1.End
        while ($j -lt $cmd.Length -and $cmd[$j] -match '[ \t]') { $j++ }
        if ($j -ge $cmd.Length -or $cmd[$j] -ne ',') { continue }
        $j++
        while ($j -lt $cmd.Length -and $cmd[$j] -match '[ \t]') { $j++ }
        $a2 = Read-PyDestExpr $cmd $j
        if ($a2 -and (Test-BlockedDest $a2.Value $a2.Start (-not $a2.Resolvable))) {
            $matched = $true
            $reason = "python os.replace/os.rename to '$($a2.Value)'"
            break
        }
    }
}

# 13. python -c os.symlink(src, dst) / os.link(src, dst) — destination is the
#     SECOND argument (the new link); the first (src/target) is only read.
if (-not $matched) {
    $oslHits = [regex]::Matches($cmd, '\bos\s*\.\s*(?:symlink|link)\s*\(\s*')
    foreach ($oh in $oslHits) {
        $a1 = Read-PyDestExpr $cmd ($oh.Index + $oh.Length)
        if (-not $a1) { continue }
        $j = $a1.End
        while ($j -lt $cmd.Length -and $cmd[$j] -match '[ \t]') { $j++ }
        if ($j -ge $cmd.Length -or $cmd[$j] -ne ',') { continue }
        $j++
        while ($j -lt $cmd.Length -and $cmd[$j] -match '[ \t]') { $j++ }
        $a2 = Read-PyDestExpr $cmd $j
        if ($a2 -and (Test-BlockedDest $a2.Value $a2.Start (-not $a2.Resolvable))) {
            $matched = $true
            $reason = "python os.symlink/os.link to '$($a2.Value)'"
            break
        }
    }
}

# 14. python -c os.truncate(path, size) — destination is the FIRST argument.
if (-not $matched) {
    $otHits = [regex]::Matches($cmd, '\bos\s*\.\s*truncate\s*\(\s*')
    foreach ($oh in $otHits) {
        $a1 = Read-PyDestExpr $cmd ($oh.Index + $oh.Length)
        if (-not $a1) { continue }
        if (Test-BlockedDest $a1.Value $a1.Start (-not $a1.Resolvable)) {
            $matched = $true
            $reason = "python os.truncate targets '$($a1.Value)'"
            break
        }
    }
}

if ($matched) {
    if ($script:bwUnresolvedReason -eq 'expansion') {
        # Distinct from the "targets engine source" refusal below so an
        # operator can tell the two apart in a transcript: this path never
        # learned the extension, because the destination itself could not be
        # resolved statically (it names a variable or a command
        # substitution rather than a literal path).
        [Console]::Error.WriteLine("[hydra] BLOCKED: Bash write idiom ($reason) has an UNRESOLVABLE destination.")
        [Console]::Error.WriteLine("[hydra] The destination contains a shell expansion (`$(...), a backtick command, `$VAR, or `${VAR}) that this guard cannot statically resolve, so it cannot verify the write is safe.")
        [Console]::Error.WriteLine("[hydra] Write a literal path instead. Engineering code must still go through the pair-programmer harness, not a Bash write.")
        [Console]::Error.WriteLine("[hydra] Route it: /hydra:run `"<goal>`" (or submit a DEV_TASK via the ingest bridge). Kill-switch: set HYDRA_ENFORCE_ROUTING != 1.")
    } else {
        [Console]::Error.WriteLine("[hydra] BLOCKED: Bash write idiom ($reason) targets engine source.")
        [Console]::Error.WriteLine("[hydra] Engineering code MUST go through the pair-programmer harness, not a Bash write.")
        [Console]::Error.WriteLine("[hydra] Route it: /hydra:run `"<goal>`" (or submit a DEV_TASK via the ingest bridge).")
        [Console]::Error.WriteLine("[hydra] Design docs (.md) are allowed. Kill-switch: set HYDRA_ENFORCE_ROUTING != 1.")
    }
    exit 2
}

exit 0
