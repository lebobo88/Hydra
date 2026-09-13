# Write-guard: tracked open limits

**Status:** open backlog, deliberately not closed.
**Raised:** 2026-09-13, during the planning-phase work (`feat/planning-phase`).
**Guard:** `plugins/hydra/hooks/hydra-block-bash-writes.ps1` and its lockstep
twin `plugins/hydra/hooks/hydra-block-direct-write.ps1`.

## Why this file exists

The Bash write-guard was hardened across six review rounds. Those rounds closed
roughly thirty-five evasion shapes on two different axes, and every defect they
closed measured identically against `main@29dbe89` — all pre-existing, none
introduced.

The two axes are worth keeping distinct, because they have different endings:

- **The destination parser** — how the guard resolves the destination it is
  looking at. This is now sound and was made so by a single principle: *resolve
  what is statically resolvable, fail closed on what is not, with one mechanism
  and one distinguishable refusal for every unresolvable case.* A parser can be
  finished, and this one is.
- **The inventory of write idioms** — which programs and APIs the guard knows can
  write a file at all. This cannot be finished. Every round produced a fresh
  batch, and the list has no natural end.

This file tracks the second. It is not a defect report against the hardening
work; it is the acknowledgement that an enumeration is never complete, recorded
so a future reader does not mistake silence for coverage.

## Known unguarded write surfaces

Each of these writes a statically resolvable destination and is **not** detected.
Every one also measures unguarded on `main@29dbe89`, so none is a regression.

| surface | measured |
|---|---|
| `rsync <src> <protected>` | exit 0 |
| `patch -o <protected> <patchfile>` | exit 0 |
| `node -e "require('fs').writeFileSync('<protected>', …)"` | exit 0 |
| `7z` / `unzip -o` extracting over a protected path | not probed |
| `perl -i` in-place edit | not probed |
| `split` / `csplit` output prefixes | not probed |
| `tar -x` / `--extract` to a protected path | not probed |
| PowerShell `Set-Content` / `Out-File` / `Add-Content` | partially covered; audit needed |
| `git apply` | not probed |

Anything closed here should follow the established pattern: route the destination
through the existing shell-argument / Python-destination readers rather than new
parsing, so it inherits fragment welding, ANSI-C escapes, quote-aware
continuation splicing and the fail-closed rule; and gate the command word with
`Test-IsCommandWord` so the word is only treated as this guard's idiom in
command-word position.

## Deliberate non-goal: `git checkout -- <path>`

`git checkout -- <protected>` overwrites a tracked file and measures exit 0. It
is **intentionally left unguarded.**

It restores a file from git rather than writing arbitrary content, and
`git checkout -- .` is a routine developer command. Blocking it would be a
material false positive, and false positives in this area have proven as
damaging as bypasses — see below. If this is ever revisited, it needs a policy
decision, not a pattern.

## Deliberate non-goal: runtime indirection

A destination can be constructed inside `sh -c` from a variable, decoded from
base64, read from a file, or assembled by a `python -c` one-liner. No static
path-scanning PreToolUse hook can resolve any of those without executing the
shell, which a guard must never do. One reachable shape — an `xargs`
substitution placeholder — is closed by failing closed on it; the general class
is not closed and cannot be.

## What this guard is, and is not

It is **defence-in-depth** against an agent hand-writing engine source. It is
**not a sandbox.** The primary controls remain the routing contract in
`AGENTS.md` / `CLAUDE.md` and the companion Write/Edit guard, which receives an
already-resolved `file_path` and therefore cannot be outwitted by shell quoting
at all. Treating this hook as a security boundary would overstate it.

## False positives are defects too

Stated because the hardening work found two that mattered more than some of the
bypasses:

- The `open()` branch refused **every** write-mode open without testing its
  destination, so it blocked writes into the tracked plan directory — the exact
  carve-out the work existed to create.
- A blind whole-command line-continuation splice let a single-quoted path forge
  that carve-out, because joining is how an allowed path gets manufactured.
- Treating every command containing the word `install` as coreutils `install`
  would have broken `npm install`, `pip install`, `cargo install` and every
  other package manager. The command-word discriminator is what prevents that,
  and it is load-bearing.

Any future addition to the inventory must be tested against false positives as
carefully as against evasion.

## Method notes for whoever picks this up

- **Execute the guard; do not read it.** This defect class was invisible to
  static review and to unit tests that did not run the hook — four separate
  times. The `-t` gap is the clearest example: the parsing looked present and
  correct but checked the directory rather than the directory joined with each
  source's basename, and only a probe where those two differ in protectedness
  exposes it.
- **Prove the property, not the instance.** Every mechanism has a test that
  deletes exactly that mechanism in a temporary copy and asserts the bypass
  returns. The helper asserts its patch target is present first — a property
  test whose patch silently no-ops looks like a guard while proving nothing.
- **Build payloads from character codes and assert on them.** An escape sequence
  can be flattened in transport, and a test built on a flattened payload passes
  for the wrong reason. This cost one nearly-dispatched fix for a non-bug.
- **The guard will block your own tooling.** Writing a `.py` probe into a
  scratchpad is refused; so is a command whose *text* quotes a protected path,
  and so was an earlier draft of a memory note that quoted a payload. Build
  payloads from concatenated pieces, pipe harnesses through stdin, and describe
  payloads in prose rather than quoting them.
- **Reproduce a judge's finding before acting on it.** Cross-vendor review drove
  most of this work and was worth the cost, but it was also wrong about at least
  three exit codes it claimed to have measured.

## Heredoc bodies: FIXED, with one residual shape

**Fixed 2026-09-13.** Heredoc bodies were parsed as shell syntax, so a body
containing one of the discriminated write idioms plus a path-shaped token was
refused. A body with a brace-grouped install measured 0 on `main@29dbe89` and 2
after the inventory work; so did a body with a **plain** install and no grouping
at all, which is what located the cause in the write-idiom inventory rather than
in the grouping predicates.

Fixed by extending the existing quote/escape mask with a heredoc-body state
(opening at `<<WORD` / `<<-WORD` / `<<'WORD'` / `<<"WORD"`, closing at the
delimiter line) and excluding those spans from the command-word and boundary
scans, exactly as quoted spans already were. No fourth notion of position was
added. The heredoc **destination** check is untouched and still refuses a
protected target; only the body is data.

Verified after the fix: quoted and unquoted delimiters, the `<<-` tab-indented
form, a path-only body, and the real case — a heredoc writing documentation to an
allowed extension whose body names a protected path — all pass; a heredoc whose
destination is protected still refuses; a delimiter appearing as a substring of a
body line does not close the body.

### Residual: two heredocs on the SAME command line

**Measured 2026-09-13, tracked, not fixed.**

    cat <<A <<B      (both openers on one line, write idiom in the SECOND body)
      ORIG 0  ->  now 2     FALSE POSITIVE

Two heredocs in *separate* commands are handled correctly (0 both). The mask
finds the first `<<`, masks its body, then advances its cursor past that body's
terminator, so a second opener that sat on the original opener line is already
behind the cursor and never registers. The fix is to collect all openers on a
line first and resolve their bodies in opener order.

Not fixed because it was found on the eleventh review pass, the shape is exotic
(a single command opening two heredocs with a write idiom in the second body), and
it is a false positive with no security exposure. Three of the four fixes in this
area regressed something — one reopened a closed bypass — so further widening of
this parser carries more risk than this shape justifies.

**Test-truthfulness note, worth correcting when someone is next in this file:**
`test_two_heredocs_in_one_command_both_bodies_excluded` uses two *separate* `cat`
commands, not two openers on one line. It passes and it does cover something real
— sequential heredocs — but its name and docstring claim more than it tests.
Rename it to say what it covers, and add a genuine same-line case alongside the
fix above. A test whose name overclaims is a small lie that a future reader will
believe.
