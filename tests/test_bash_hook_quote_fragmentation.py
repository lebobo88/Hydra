r"""Security hardening (2026-09) — `hydra-block-bash-writes.ps1` quote-fragment
evasion.

PROVEN vulnerability: every write-detecting branch captured its destination
with a regex character class that EXCLUDES quote characters (e.g.
`[^\s'";|&<>]+`), so the capture stopped dead at the first quote boundary. A
destination written as adjacent quoted fragments — e.g.
`'hydra_core/sup'"ervisor.py"` — is ONE shell argument (the shell concatenates
adjacent quoted/unquoted runs with no separating whitespace) but the old regex
only ever captured `hydra_core/sup`, silently losing the `ervisor.py` suffix.
`Test-BlockedDest` then saw a value with no blocked extension and returned
"not blocked" — a false negative, proven against the hook as it existed at
`29dbe89` (see `test_fragment_payload_blocked_by_original_hook_prevented_here`
disabled by default; the manual before/after is documented in the PR).

The fix replaces every ad-hoc capture with `Read-ShellArgument` /
`Get-ShellArgsInRange` (shell-aware, quote-state-tracking argument
reconstruction) and `Read-PyStringLiteral` (the analogous fix one layer down,
for adjacent Python string literals inside a `python -c "..."` one-liner).

Every write-detecting branch is driven here through six destination shapes:
  1. unquoted
  2. single-quoted
  3. double-quoted
  4. fragment-concatenated (the evasion)
  5. escaped quote inside
  6. quoted with an embedded space
each of which must BLOCK (exit 2) a protected engine-source target and ALLOW
(exit 0) a docs/plans target. False-positive shapes from the security review
are asserted separately.

Pwsh tests are gated on `pwsh`/`powershell` presence (skipif absent).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

HYDRA_ROOT = Path(__file__).resolve().parents[1]
HOOKS_DIR = HYDRA_ROOT / "plugins" / "hydra" / "hooks"
BASH_HOOK = "hydra-block-bash-writes.ps1"
DIRECT_HOOK = "hydra-block-direct-write.ps1"

_PWSH = shutil.which("pwsh") or shutil.which("powershell")

pytestmark = pytest.mark.skipif(_PWSH is None, reason="pwsh/powershell not on PATH")


def _run_bash_hook(command: str, *, cwd: Path, project_dir: Path) -> subprocess.CompletedProcess:
    payload = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(cwd)}
    env = {**os.environ}
    env["HYDRA_ENFORCE_ROUTING"] = "1"
    env["CLAUDE_PROJECT_DIR"] = str(project_dir)
    env.pop("HYDRA_PP_STAGE_ACTIVE", None)
    env.pop("HYDRA_WORKTREE_ROOT", None)
    return subprocess.run(
        [_PWSH, "-NoProfile", "-File", str(HOOKS_DIR / BASH_HOOK)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


def _run_direct_hook(file_path: Path, *, project_dir: Path) -> subprocess.CompletedProcess:
    payload = {"tool_name": "Write", "tool_input": {"file_path": str(file_path), "content": "x"}}
    env = {**os.environ}
    env["HYDRA_ENFORCE_ROUTING"] = "1"
    env["CLAUDE_PROJECT_DIR"] = str(project_dir)
    env.pop("HYDRA_PP_STAGE_ACTIVE", None)
    env.pop("HYDRA_WORKTREE_ROOT", None)
    return subprocess.run(
        [_PWSH, "-NoProfile", "-File", str(HOOKS_DIR / DIRECT_HOOK)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


@pytest.fixture()
def project_dir(tmp_path: Path) -> Path:
    proj = tmp_path / "Hydra"
    (proj / "hydra_core").mkdir(parents=True)
    (proj / "docs" / "plans").mkdir(parents=True)
    (proj / "docs" / "plansomething").mkdir(parents=True)
    return proj


# ---------------------------------------------------------------------------
# The six destination shapes, applied per write-detecting branch.
# Each entry is a template with {dest} substituted by a *relative* path.
# ---------------------------------------------------------------------------

SHAPES = {
    "unquoted": "{dest}",
    "single_quoted": "'{dest}'",
    "double_quoted": '"{dest}"',
    "fragment_concatenated": None,  # built per-branch (split point differs)
    "escaped_quote_inside": None,   # built per-branch
    "quoted_with_space": None,      # built per-branch (needs a space-bearing path)
}


def _frag(dest: str, split: int) -> str:
    """Split `dest` at `split` and rejoin as adjacent quoted fragments."""
    return f"'{dest[:split]}'\"{dest[split:]}\""


def _redirect_cmd(dest_expr: str) -> str:
    return f"echo a >> {dest_expr}"


BLOCKED_REL = "hydra_core/supervisor.py"
PLANS_REL = "docs/plans/report.html"


def _shapes_for(rel_dest: str, split: int, spaced_rel: str | None = None) -> dict[str, str]:
    out = {
        "unquoted": rel_dest,
        "single_quoted": f"'{rel_dest}'",
        "double_quoted": f'"{rel_dest}"',
        "fragment_concatenated": _frag(rel_dest, split),
        "escaped_quote_inside": f'"{rel_dest[:split]}\\"{rel_dest[split:]}"',
    }
    if spaced_rel is not None:
        out["quoted_with_space"] = f'"{spaced_rel}"'
    return out


class TestOutputRedirection:
    def test_blocked_shapes(self, project_dir: Path):
        for name, expr in _shapes_for(BLOCKED_REL, 10, "hydra core/supervisor.py").items():
            if name == "quoted_with_space":
                (project_dir / "hydra core").mkdir(exist_ok=True)
            result = _run_bash_hook(_redirect_cmd(expr), cwd=project_dir, project_dir=project_dir)
            assert result.returncode == 2, f"{name}: expected BLOCK, got rc={result.returncode} stderr={result.stderr}"

    def test_docs_plans_shapes_allowed(self, project_dir: Path):
        for name, expr in _shapes_for(PLANS_REL, 11, "docs/plans/my report.html").items():
            result = _run_bash_hook(_redirect_cmd(expr), cwd=project_dir, project_dir=project_dir)
            assert result.returncode == 0, f"{name}: expected ALLOW, got rc={result.returncode} stderr={result.stderr}"

    def test_fragment_concatenation_evasion_is_closed(self, project_dir: Path):
        """The exact evasion shape called out by the security review."""
        cmd = "echo a >> 'hydra_core/sup'\"ervisor.py\""
        result = _run_bash_hook(cmd, cwd=project_dir, project_dir=project_dir)
        assert result.returncode == 2, (
            f"fragment-concatenated destination must BLOCK; rc={result.returncode} "
            f"stdout={result.stdout} stderr={result.stderr}"
        )
        assert "BLOCKED" in result.stderr


class TestTee:
    def _cmd(self, expr: str) -> str:
        return f"cat x | tee {expr}"

    def test_blocked_shapes(self, project_dir: Path):
        for name, expr in _shapes_for(BLOCKED_REL, 10, "hydra core/supervisor.py").items():
            if name == "quoted_with_space":
                (project_dir / "hydra core").mkdir(exist_ok=True)
            result = _run_bash_hook(self._cmd(expr), cwd=project_dir, project_dir=project_dir)
            assert result.returncode == 2, f"{name}: expected BLOCK, got rc={result.returncode} stderr={result.stderr}"

    def test_docs_plans_shapes_allowed(self, project_dir: Path):
        for name, expr in _shapes_for(PLANS_REL, 11, "docs/plans/my report.html").items():
            result = _run_bash_hook(self._cmd(expr), cwd=project_dir, project_dir=project_dir)
            assert result.returncode == 0, f"{name}: expected ALLOW, got rc={result.returncode} stderr={result.stderr}"


class TestCpMv:
    def _cmd(self, expr: str) -> str:
        return f"cp template.md {expr}"

    def test_blocked_shapes(self, project_dir: Path):
        for name, expr in _shapes_for(BLOCKED_REL, 10, "hydra core/supervisor.py").items():
            if name == "quoted_with_space":
                (project_dir / "hydra core").mkdir(exist_ok=True)
            result = _run_bash_hook(self._cmd(expr), cwd=project_dir, project_dir=project_dir)
            assert result.returncode == 2, f"{name}: expected BLOCK, got rc={result.returncode} stderr={result.stderr}"

    def test_docs_plans_shapes_allowed(self, project_dir: Path):
        for name, expr in _shapes_for(PLANS_REL, 11, "docs/plans/my report.html").items():
            result = _run_bash_hook(self._cmd(expr), cwd=project_dir, project_dir=project_dir)
            assert result.returncode == 0, f"{name}: expected ALLOW, got rc={result.returncode} stderr={result.stderr}"


class TestSetContentOutFile:
    def _cmd(self, expr: str) -> str:
        return f"Set-Content {expr} -Value 'x'"

    def test_blocked_shapes(self, project_dir: Path):
        for name, expr in _shapes_for(BLOCKED_REL, 10, "hydra core/supervisor.py").items():
            if name == "quoted_with_space":
                (project_dir / "hydra core").mkdir(exist_ok=True)
            result = _run_bash_hook(self._cmd(expr), cwd=project_dir, project_dir=project_dir)
            assert result.returncode == 2, f"{name}: expected BLOCK, got rc={result.returncode} stderr={result.stderr}"

    def test_docs_plans_shapes_allowed(self, project_dir: Path):
        for name, expr in _shapes_for(PLANS_REL, 11, "docs/plans/my report.html").items():
            result = _run_bash_hook(self._cmd(expr), cwd=project_dir, project_dir=project_dir)
            assert result.returncode == 0, f"{name}: expected ALLOW, got rc={result.returncode} stderr={result.stderr}"

    def test_flag_form_blocked(self, project_dir: Path):
        result = _run_bash_hook(
            "Set-Content -Path 'hydra_core/sup'\"ervisor.py\" -Value 'x'",
            cwd=project_dir, project_dir=project_dir,
        )
        assert result.returncode == 2
        assert "BLOCKED" in result.stderr


class TestHeredoc:
    def _cmd(self, expr: str) -> str:
        return f"cat <<'EOF' > {expr}\nx\nEOF"

    def test_blocked_shapes(self, project_dir: Path):
        for name, expr in _shapes_for(BLOCKED_REL, 10, "hydra core/supervisor.py").items():
            if name == "quoted_with_space":
                (project_dir / "hydra core").mkdir(exist_ok=True)
            result = _run_bash_hook(self._cmd(expr), cwd=project_dir, project_dir=project_dir)
            assert result.returncode == 2, f"{name}: expected BLOCK, got rc={result.returncode} stderr={result.stderr}"

    def test_docs_plans_shapes_allowed(self, project_dir: Path):
        for name, expr in _shapes_for(PLANS_REL, 11, "docs/plans/my report.html").items():
            result = _run_bash_hook(self._cmd(expr), cwd=project_dir, project_dir=project_dir)
            assert result.returncode == 0, f"{name}: expected ALLOW, got rc={result.returncode} stderr={result.stderr}"


class TestPythonOpen:
    def test_fragmented_path_still_detected(self, project_dir: Path):
        """Splitting the FILENAME across adjacent Python literals used to make
        the whole open()-detection regex fail to match (it required arg1 to be
        exactly one quoted literal), un-detecting the write regardless of
        mode. Detection here is mode-driven and must still fire."""
        cmd = "python -c \"open('hydra_core/sup' 'ervisor.py', 'w').write('x')\""
        result = _run_bash_hook(cmd, cwd=project_dir, project_dir=project_dir)
        assert result.returncode == 2, f"rc={result.returncode} stderr={result.stderr}"

    def test_read_mode_allowed(self, project_dir: Path):
        cmd = "python -c \"open('hydra_core/supervisor.py', 'r').read()\""
        result = _run_bash_hook(cmd, cwd=project_dir, project_dir=project_dir)
        assert result.returncode == 0, f"rc={result.returncode} stderr={result.stderr}"


class TestPathlibWriteText:
    def test_fragmented_dest_blocked(self, project_dir: Path):
        cmd = "python -c \"from pathlib import Path; Path('hydra_core/sup' 'ervisor.py').write_text('x')\""
        result = _run_bash_hook(cmd, cwd=project_dir, project_dir=project_dir)
        assert result.returncode == 2, f"rc={result.returncode} stderr={result.stderr}"

    def test_docs_plans_allowed(self, project_dir: Path):
        cmd = "python -c \"from pathlib import Path; Path('docs/plans/report.html').write_text('x')\""
        result = _run_bash_hook(cmd, cwd=project_dir, project_dir=project_dir)
        assert result.returncode == 0, f"rc={result.returncode} stderr={result.stderr}"


class TestShutil:
    def test_fragmented_dest_blocked(self, project_dir: Path):
        cmd = "python -c \"import shutil; shutil.copy('t.md', 'hydra_core/sup' 'ervisor.py')\""
        result = _run_bash_hook(cmd, cwd=project_dir, project_dir=project_dir)
        assert result.returncode == 2, f"rc={result.returncode} stderr={result.stderr}"


class TestSedInPlace:
    def test_fragmented_target_blocked(self, project_dir: Path):
        cmd = "sed -i 's/a/b/' 'hydra_core/sup'\"ervisor.py\""
        result = _run_bash_hook(cmd, cwd=project_dir, project_dir=project_dir)
        assert result.returncode == 2, f"rc={result.returncode} stderr={result.stderr}"

    def test_unfragmented_target_still_blocked(self, project_dir: Path):
        cmd = "sed -i 's/a/b/' hydra_core/supervisor.py"
        result = _run_bash_hook(cmd, cwd=project_dir, project_dir=project_dir)
        assert result.returncode == 2, f"rc={result.returncode} stderr={result.stderr}"


# ---------------------------------------------------------------------------
# False positives (§3 of the security review) — must ALLOW.
# ---------------------------------------------------------------------------


class TestFalsePositives:
    def test_commit_message_quoting_blocked_idiom(self, project_dir: Path):
        result = _run_bash_hook(
            'git commit -m "fix bug in hydra_core/supervisor.py"',
            cwd=project_dir, project_dir=project_dir,
        )
        assert result.returncode == 0, f"rc={result.returncode} stderr={result.stderr}"

    def test_grep_pattern_mentioning_py_file(self, project_dir: Path):
        result = _run_bash_hook(
            "grep -rn 'supervisor.py' hydra_core/",
            cwd=project_dir, project_dir=project_dir,
        )
        assert result.returncode == 0, f"rc={result.returncode} stderr={result.stderr}"

    def test_echoed_string_quoting_a_path(self, project_dir: Path):
        result = _run_bash_hook(
            'echo "see hydra_core/supervisor.py for details"',
            cwd=project_dir, project_dir=project_dir,
        )
        assert result.returncode == 0, f"rc={result.returncode} stderr={result.stderr}"

    def test_sed_in_place_program_argument_mentioning_py_but_target_is_md(self, project_dir: Path):
        """sed -i editing a docs file whose SCRIPT happens to mention a .py
        path in the substitution text must not block the actual (allowed)
        target."""
        (project_dir / "README.md").write_text("x", encoding="utf-8")
        result = _run_bash_hook(
            "sed -i 's/supervisor.py/renamed/' README.md",
            cwd=project_dir, project_dir=project_dir,
        )
        assert result.returncode == 0, f"rc={result.returncode} stderr={result.stderr}"


# ---------------------------------------------------------------------------
# Direct-write hook (Write/Edit) — docs/plans .html carve-out, lockstep check.
# ---------------------------------------------------------------------------


class TestDirectWriteDocsPlansCarveOut:
    def test_html_under_docs_plans_allowed(self, project_dir: Path):
        result = _run_direct_hook(project_dir / "docs" / "plans" / "report.html", project_dir=project_dir)
        assert result.returncode == 0, f"rc={result.returncode} stderr={result.stderr}"

    def test_html_elsewhere_still_blocked(self, project_dir: Path):
        result = _run_direct_hook(project_dir / "index.html", project_dir=project_dir)
        assert result.returncode == 2, f"rc={result.returncode} stderr={result.stderr}"

    def test_sibling_dir_beginning_with_plans_not_carved_out(self, project_dir: Path):
        """Segment-bounded: 'docs\\plansomething\\' must NOT match the
        'docs\\plans\\' carve-out."""
        result = _run_direct_hook(project_dir / "docs" / "plansomething" / "report.html", project_dir=project_dir)
        assert result.returncode == 2, f"rc={result.returncode} stderr={result.stderr}"

    def test_other_blocked_extension_under_docs_plans_still_blocked(self, project_dir: Path):
        """The carve-out is scoped to .html only — a .py under docs/plans must
        still block."""
        result = _run_direct_hook(project_dir / "docs" / "plans" / "script.py", project_dir=project_dir)
        assert result.returncode == 2, f"rc={result.returncode} stderr={result.stderr}"

    def test_md_under_docs_plans_still_allowed(self, project_dir: Path):
        """.md was already globally allowed; docs/plans changes nothing here."""
        result = _run_direct_hook(project_dir / "docs" / "plans" / "report.md", project_dir=project_dir)
        assert result.returncode == 0, f"rc={result.returncode} stderr={result.stderr}"


class TestBashHookDocsPlansSiblingNotCarvedOut:
    def test_sibling_dir_beginning_with_plans_not_carved_out(self, project_dir: Path):
        result = _run_bash_hook(
            "echo a >> docs/plansomething/report.html",
            cwd=project_dir, project_dir=project_dir,
        )
        assert result.returncode == 2, f"rc={result.returncode} stderr={result.stderr}"

    def test_other_blocked_extension_under_docs_plans_still_blocked(self, project_dir: Path):
        result = _run_bash_hook(
            "echo a >> docs/plans/script.py",
            cwd=project_dir, project_dir=project_dir,
        )
        assert result.returncode == 2, f"rc={result.returncode} stderr={result.stderr}"


# ---------------------------------------------------------------------------
# Before/after documentation pin (requires the archived original at 29dbe89
# to be checked out by the operator; not run automatically since it needs a
# second file on disk — see the PR body for the measured transcript).
# ---------------------------------------------------------------------------


def test_hardened_hook_documents_the_shared_helper():
    text = (HOOKS_DIR / BASH_HOOK).read_text(encoding="utf-8")
    assert "Read-ShellArgument" in text
    assert "Get-ShellArgsInRange" in text
    assert "Read-PyStringLiteral" in text


def test_both_hooks_document_the_docs_plans_carveout():
    bash_hook = (HOOKS_DIR / BASH_HOOK).read_text(encoding="utf-8")
    direct_hook = (HOOKS_DIR / DIRECT_HOOK).read_text(encoding="utf-8")
    assert "docs\\plans\\" in bash_hook or "docs/plans" in bash_hook.lower() or "docs\\plans" in bash_hook
    assert "docs\\plans\\" in direct_hook or "docs/plans" in direct_hook.lower() or "docs\\plans" in direct_hook


# ---------------------------------------------------------------------------
# Revision (2026-09): five residual bypasses confirmed by cross-vendor judge,
# all PRE-EXISTING (the plain hook at 29dbe89 also scored 0 on every one of
# them) and NOT regressed by the quote-fragmentation fix above:
#   - `$(...)` command substitution forming the blocked extension
#   - a backtick command substitution forming the blocked extension
#   - `"$VAR"` / `"${VAR}"` naming a literal engine-source path
#   - a backslash-newline line continuation splitting the extension
# A shell expansion cannot be resolved without running a shell, which this
# hook must never do, so it FAILS CLOSED: any reconstructed write destination
# containing an unresolvable `$(...)`, backtick, `$VAR`, or `${VAR}` in
# unquoted/double-quoted context now blocks with its own distinct message,
# regardless of extension or worktree membership. Line-continuation, unlike
# expansion, IS statically resolvable, so it is normalised (resolved) up
# front instead of being treated as unresolvable.
# ---------------------------------------------------------------------------

UNRESOLVABLE_MARKER = "UNRESOLVABLE"


class TestExpansionFailsClosed:
    """Requirement (b): a shell expansion in the reconstructed destination
    blocks unconditionally, with a message distinguishable from the ordinary
    'targets engine source' refusal."""

    def test_command_substitution_forming_extension(self, project_dir: Path):
        result = _run_bash_hook(
            'echo x > "hydra_core/supervisor.$(printf py)"',
            cwd=project_dir, project_dir=project_dir,
        )
        assert result.returncode == 2, f"rc={result.returncode} stderr={result.stderr}"
        assert UNRESOLVABLE_MARKER in result.stderr

    def test_backtick_substitution_forming_extension(self, project_dir: Path):
        result = _run_bash_hook(
            'echo x > "hydra_core/supervisor.`printf py`"',
            cwd=project_dir, project_dir=project_dir,
        )
        assert result.returncode == 2, f"rc={result.returncode} stderr={result.stderr}"
        assert UNRESOLVABLE_MARKER in result.stderr

    def test_double_quoted_variable_destination(self, project_dir: Path):
        result = _run_bash_hook(
            f'D={BLOCKED_REL}; echo x > "$D"',
            cwd=project_dir, project_dir=project_dir,
        )
        assert result.returncode == 2, f"rc={result.returncode} stderr={result.stderr}"
        assert UNRESOLVABLE_MARKER in result.stderr

    def test_double_quoted_braced_variable_destination(self, project_dir: Path):
        result = _run_bash_hook(
            f'D={BLOCKED_REL}; echo x > "${{D}}"',
            cwd=project_dir, project_dir=project_dir,
        )
        assert result.returncode == 2, f"rc={result.returncode} stderr={result.stderr}"
        assert UNRESOLVABLE_MARKER in result.stderr

    def test_message_distinguishable_from_extension_block_message(self, project_dir: Path):
        """The unresolvable-destination refusal must not read identically to
        the ordinary 'targets engine source' refusal, so an operator can tell
        the two apart in a transcript."""
        expansion_result = _run_bash_hook(
            f'D={BLOCKED_REL}; echo x > "$D"',
            cwd=project_dir, project_dir=project_dir,
        )
        extension_result = _run_bash_hook(
            f"echo a >> {BLOCKED_REL}",
            cwd=project_dir, project_dir=project_dir,
        )
        assert expansion_result.returncode == 2
        assert extension_result.returncode == 2
        assert expansion_result.stderr != extension_result.stderr
        assert "targets engine source" in extension_result.stderr
        assert "targets engine source" not in expansion_result.stderr

    def test_expansion_in_cp_destination_blocks(self, project_dir: Path):
        result = _run_bash_hook(
            f'D={BLOCKED_REL}; cp template.txt "$D"',
            cwd=project_dir, project_dir=project_dir,
        )
        assert result.returncode == 2, f"rc={result.returncode} stderr={result.stderr}"

    def test_expansion_in_sed_destination_blocks(self, project_dir: Path):
        result = _run_bash_hook(
            f'D={BLOCKED_REL}; sed -i "s/a/b/" "$D"',
            cwd=project_dir, project_dir=project_dir,
        )
        assert result.returncode == 2, f"rc={result.returncode} stderr={result.stderr}"

    def test_expansion_in_set_content_destination_blocks(self, project_dir: Path):
        result = _run_bash_hook(
            f'D={BLOCKED_REL}; Set-Content -Path "$D" -Value x',
            cwd=project_dir, project_dir=project_dir,
        )
        assert result.returncode == 2, f"rc={result.returncode} stderr={result.stderr}"

    def test_expansion_in_heredoc_destination_blocks(self, project_dir: Path):
        result = _run_bash_hook(
            f'D={BLOCKED_REL}; cat <<EOF > "$D"\nbody\nEOF',
            cwd=project_dir, project_dir=project_dir,
        )
        assert result.returncode == 2, f"rc={result.returncode} stderr={result.stderr}"

    def test_expansion_blocks_even_inside_worktree_root(self, project_dir: Path):
        """An agent has no legitimate need to name a write destination
        through a variable, even inside its own allow-listed worktree — it
        can always write a literal path. Placing the (fake) worktree root at
        project_dir itself and using HYDRA_WORKTREE_ROOT to point at it must
        NOT exempt an expansion-bearing destination."""
        env_extra = {"HYDRA_WORKTREE_ROOT": str(project_dir)}
        payload = {
            "tool_name": "Bash",
            "tool_input": {"command": f'D={BLOCKED_REL}; echo x > "$D"'},
            "cwd": str(project_dir),
        }
        env = {**os.environ}
        env["HYDRA_ENFORCE_ROUTING"] = "1"
        env["CLAUDE_PROJECT_DIR"] = str(project_dir)
        env.pop("HYDRA_PP_STAGE_ACTIVE", None)
        env.update(env_extra)
        result = subprocess.run(
            [_PWSH, "-NoProfile", "-File", str(HOOKS_DIR / BASH_HOOK)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )
        assert result.returncode == 2, f"rc={result.returncode} stderr={result.stderr}"


class TestLineContinuationResolved:
    """Requirement (a): a backslash-newline continuation is statically
    resolvable, so it is joined up front and the resulting write is
    evaluated normally (blocked when the joined destination is blocked)."""

    def test_backslash_newline_splits_extension(self, project_dir: Path):
        # Built from explicit character codes, not a "\\\n" string literal:
        # a transport step (shell quoting, an editor, a copy/paste) can
        # silently flatten "\n" into the two characters backslash+'n', which
        # would make this test pass for the WRONG reason (some other branch
        # blocking a payload that no longer contains a real newline at all —
        # exactly the "proves the property, not the instance" trap). Assert
        # the payload itself carries a real LF before trusting the result.
        cmd = "echo x > hydra_core/supervisor." + chr(92) + chr(10) + "py"
        assert chr(10) in cmd, "payload lost its real newline before reaching the hook"
        result = _run_bash_hook(cmd, cwd=project_dir, project_dir=project_dir)
        assert result.returncode == 2, f"rc={result.returncode} stderr={result.stderr}"

    def test_backslash_crlf_newline_splits_extension(self, project_dir: Path):
        cmd = "echo x > hydra_core/supervisor." + chr(92) + chr(13) + chr(10) + "py"
        assert chr(13) in cmd and chr(10) in cmd, "payload lost its real CRLF before reaching the hook"
        result = _run_bash_hook(cmd, cwd=project_dir, project_dir=project_dir)
        assert result.returncode == 2, f"rc={result.returncode} stderr={result.stderr}"


class TestAnsiCQuotingRegression:
    """ANSI-C `$'...'` quoting was already blocked by 373f0cc (the leading
    `$` before the quoted run is itself an unquoted expansion marker under
    Read-ShellArgument, so it now also trips the fail-closed expansion rule
    above). Locked in here as a regression guard."""

    def test_ansi_c_hex_escape_regression(self, project_dir: Path):
        result = _run_bash_hook(
            r"echo x > $'hydra_core/supervisor\x2epy'",
            cwd=project_dir, project_dir=project_dir,
        )
        assert result.returncode == 2, f"rc={result.returncode} stderr={result.stderr}"

    def test_ansi_c_octal_escape_regression(self, project_dir: Path):
        result = _run_bash_hook(
            r"echo x > $'hydra_core/supervisor\056py'",
            cwd=project_dir, project_dir=project_dir,
        )
        assert result.returncode == 2, f"rc={result.returncode} stderr={result.stderr}"

    def test_adjacent_fragment_split_still_blocked(self, project_dir: Path):
        """373f0cc's own fix, re-asserted here so this revision cannot
        silently regress it."""
        result = _run_bash_hook(
            "echo x > 'hydra_core/superviso'\"r.py\"",
            cwd=project_dir, project_dir=project_dir,
        )
        assert result.returncode == 2, f"rc={result.returncode} stderr={result.stderr}"


class TestExpansionFalsePositives:
    """A literal `$` has no shell meaning inside single quotes, and a
    backslash-escaped `\\$` inside double quotes is also literal — neither
    may be treated as an expansion."""

    def test_single_quoted_dollar_is_literal(self, project_dir: Path):
        result = _run_bash_hook(
            "echo x > '$HOME/notes.md'",
            cwd=project_dir, project_dir=project_dir,
        )
        assert result.returncode == 0, f"rc={result.returncode} stderr={result.stderr}"

    def test_escaped_dollar_in_double_quotes_is_literal(self, project_dir: Path):
        result = _run_bash_hook(
            'echo x > "notes\\$HOME.md"',
            cwd=project_dir, project_dir=project_dir,
        )
        assert result.returncode == 0, f"rc={result.returncode} stderr={result.stderr}"

    def test_expansion_elsewhere_in_command_does_not_block(self, project_dir: Path):
        """The rule applies to the reconstructed DESTINATION only — an
        expansion in, say, a grep pattern must not block anything."""
        result = _run_bash_hook(
            'grep -n "$pattern" README.md',
            cwd=project_dir, project_dir=project_dir,
        )
        assert result.returncode == 0, f"rc={result.returncode} stderr={result.stderr}"

    def test_expansion_in_commit_message_does_not_block(self, project_dir: Path):
        """An expansion in a commit message (not a write destination) must
        not block."""
        result = _run_bash_hook(
            'git commit -m "built from $BRANCH"',
            cwd=project_dir, project_dir=project_dir,
        )
        assert result.returncode == 0, f"rc={result.returncode} stderr={result.stderr}"

    def test_expansion_in_inplace_edit_program_argument_does_not_block(self, project_dir: Path):
        """An expansion in the *program* argument of an in-place edit (the
        sed script, not the destination file) must not block."""
        (project_dir / "README.md").write_text("x", encoding="utf-8")
        result = _run_bash_hook(
            'sed -i "s/$OLD/new/" README.md',
            cwd=project_dir, project_dir=project_dir,
        )
        assert result.returncode == 0, f"rc={result.returncode} stderr={result.stderr}"


class TestSetContentValuePayloadNotTreatedAsDestination:
    """Found while implementing the fail-closed expansion rule above:
    Set-Content/Out-File's own positional-argument scan (added earlier to
    catch a filename in `Set-Content foo.py 'content'`) was also applying to
    -Value's PAYLOAD once -Path had already been given by name, so an
    entirely ordinary `Set-Content -Path notes.md -Value "$USER"` was
    misread as an unresolvable destination. The destination slot is now
    resolved once (named flag, inline flag, or first positional) and the
    expansion fail-close only applies to that one slot."""

    def test_named_path_with_variable_value_not_blocked(self, project_dir: Path):
        result = _run_bash_hook(
            'Set-Content -Path notes.md -Value "$USER"',
            cwd=project_dir, project_dir=project_dir,
        )
        assert result.returncode == 0, f"rc={result.returncode} stderr={result.stderr}"

    def test_positional_path_with_variable_content_not_blocked(self, project_dir: Path):
        result = _run_bash_hook(
            'Set-Content notes.md "$USER"',
            cwd=project_dir, project_dir=project_dir,
        )
        assert result.returncode == 0, f"rc={result.returncode} stderr={result.stderr}"

    def test_named_path_expansion_destination_still_blocks(self, project_dir: Path):
        result = _run_bash_hook(
            f'D={BLOCKED_REL}; Set-Content -Path "$D" -Value x',
            cwd=project_dir, project_dir=project_dir,
        )
        assert result.returncode == 2, f"rc={result.returncode} stderr={result.stderr}"

    def test_positional_expansion_destination_still_blocks(self, project_dir: Path):
        result = _run_bash_hook(
            f'D={BLOCKED_REL}; Set-Content "$D" "content"',
            cwd=project_dir, project_dir=project_dir,
        )
        assert result.returncode == 2, f"rc={result.returncode} stderr={result.stderr}"


class TestPropertyNotInstance:
    """Prove the fixes are load-bearing: patch them out and confirm the
    corresponding test regresses to allow (rc=0)."""

    def test_removing_expansion_check_allows_the_bypass(self, project_dir: Path, tmp_path: Path):
        hook_text = (HOOKS_DIR / BASH_HOOK).read_text(encoding="utf-8")
        needle = (
            "    if ($hasExpansion) {\n"
            "        $script:bwUnresolvedReason = 'expansion'\n"
            "        return $true\n"
            "    }\n"
        )
        assert needle in hook_text, "expansion-check block not found; test is stale"
        patched = hook_text.replace(needle, "    if ($false -and $hasExpansion) {\n        return $true\n    }\n")
        patched_hook = tmp_path / BASH_HOOK
        patched_hook.write_text(patched, encoding="utf-8")

        payload = {
            "tool_name": "Bash",
            "tool_input": {"command": f'D={BLOCKED_REL}; echo x > "$D"'},
            "cwd": str(project_dir),
        }
        env = {**os.environ}
        env["HYDRA_ENFORCE_ROUTING"] = "1"
        env["CLAUDE_PROJECT_DIR"] = str(project_dir)
        env.pop("HYDRA_PP_STAGE_ACTIVE", None)
        env.pop("HYDRA_WORKTREE_ROOT", None)
        result = subprocess.run(
            [_PWSH, "-NoProfile", "-File", str(patched_hook)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )
        # WITHOUT the fix, the variable destination silently resolves to a
        # relative path with no blocked extension text and is allowed.
        assert result.returncode == 0, (
            f"property check failed: removing the expansion guard should have "
            f"allowed this write, but rc={result.returncode} stderr={result.stderr}"
        )

    def test_removing_continuation_normalisation_allows_the_bypass(self, project_dir: Path, tmp_path: Path):
        hook_text = (HOOKS_DIR / BASH_HOOK).read_text(encoding="utf-8")
        needle = "$cmd = Remove-LineContinuations $cmd"
        assert needle in hook_text, "continuation-normalisation call not found; test is stale"
        patched = hook_text.replace(needle, "# disabled for property test")
        patched_hook = tmp_path / BASH_HOOK
        patched_hook.write_text(patched, encoding="utf-8")

        cmd = "echo x > hydra_core/supervisor.\\\npy"
        payload = {"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(project_dir)}
        env = {**os.environ}
        env["HYDRA_ENFORCE_ROUTING"] = "1"
        env["CLAUDE_PROJECT_DIR"] = str(project_dir)
        env.pop("HYDRA_PP_STAGE_ACTIVE", None)
        env.pop("HYDRA_WORKTREE_ROOT", None)
        result = subprocess.run(
            [_PWSH, "-NoProfile", "-File", str(patched_hook)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )
        assert result.returncode == 0, (
            f"property check failed: removing continuation normalisation should "
            f"have allowed this write, but rc={result.returncode} stderr={result.stderr}"
        )


# ---------------------------------------------------------------------------
# Third revision (2026-09): three further bypasses confirmed by cross-vendor
# judge and reproduced by execution against 830a090, none a regression of the
# preceding revision (all measured 0 there too):
#   - the noclobber-override clobber operator, spaced and unspaced
#   - a placeholder destination whose real value only exists at runtime, on
#     stdin (xargs -I<replstr>)
#   - the line-continuation splice added in 830a090 spliced a backslash-
#     newline even INSIDE single quotes, where a shell never would, forging
#     the docs/plans carve-out for a path a shell would not actually write
#     there (a FALSE ALLOW, not a missed block)
# ---------------------------------------------------------------------------

_CLOBBER_OP = ">" + "|"


class TestClobberOperator:
    """The clobber-override redirect operator overrides `noclobber`; the
    guard's redirect-operator pattern matched only the bare `>` (zero
    whitespace before the following `|`), leaving the argument reader
    positioned on the `|` separator, which reads as end-of-argument — the
    destination was never examined."""

    def test_spaced_protected_blocked(self, project_dir: Path):
        result = _run_bash_hook(f"echo x {_CLOBBER_OP} {BLOCKED_REL}", cwd=project_dir, project_dir=project_dir)
        assert result.returncode == 2, f"rc={result.returncode} stderr={result.stderr}"

    def test_unspaced_protected_blocked(self, project_dir: Path):
        result = _run_bash_hook(f"echo x {_CLOBBER_OP}{BLOCKED_REL}", cwd=project_dir, project_dir=project_dir)
        assert result.returncode == 2, f"rc={result.returncode} stderr={result.stderr}"

    def test_spaced_docs_plans_allowed(self, project_dir: Path):
        result = _run_bash_hook(f"echo x {_CLOBBER_OP} {PLANS_REL}", cwd=project_dir, project_dir=project_dir)
        assert result.returncode == 0, f"rc={result.returncode} stderr={result.stderr}"

    def test_unspaced_docs_plans_allowed(self, project_dir: Path):
        result = _run_bash_hook(f"echo x {_CLOBBER_OP}{PLANS_REL}", cwd=project_dir, project_dir=project_dir)
        assert result.returncode == 0, f"rc={result.returncode} stderr={result.stderr}"

    def test_genuine_pipe_after_plain_redirect_unaffected(self, project_dir: Path):
        """A plain redirect followed by a real pipe (space-separated) — the
        file must still be the destination and the piped command must never
        be mistaken for one."""
        result = _run_bash_hook("echo x > a.txt | grep y", cwd=project_dir, project_dir=project_dir)
        assert result.returncode == 0, f"rc={result.returncode} stderr={result.stderr}"


class TestXargsPlaceholderDestination:
    """`xargs -I<replstr> ... <replstr> ...` substitutes the literal replstr
    with a line read from stdin at RUNTIME; the guard only ever sees the
    literal placeholder text in the static command string, never the real
    destination — the identical situation as an unresolvable variable or
    command-substitution expansion, and it fails closed through that same
    mechanism."""

    def test_default_placeholder_blocked(self, project_dir: Path):
        cmd = "printf " + BLOCKED_REL + " | xargs -I{} sh -c 'echo x > {}'"
        result = _run_bash_hook(cmd, cwd=project_dir, project_dir=project_dir)
        assert result.returncode == 2, f"rc={result.returncode} stderr={result.stderr}"
        assert UNRESOLVABLE_MARKER in result.stderr

    def test_named_replstr_placeholder_blocked(self, project_dir: Path):
        """A custom `-I` replacement string (not the default placeholder)."""
        cmd = "printf " + BLOCKED_REL + " | xargs -IFILE sh -c 'echo x > FILE'"
        result = _run_bash_hook(cmd, cwd=project_dir, project_dir=project_dir)
        assert result.returncode == 2, f"rc={result.returncode} stderr={result.stderr}"
        assert UNRESOLVABLE_MARKER in result.stderr

    def test_spaced_named_replstr_placeholder_blocked(self, project_dir: Path):
        """`-I <replstr>` with a space before the replstr token."""
        cmd = "printf " + BLOCKED_REL + " | xargs -I FILE sh -c 'echo x > FILE'"
        result = _run_bash_hook(cmd, cwd=project_dir, project_dir=project_dir)
        assert result.returncode == 2, f"rc={result.returncode} stderr={result.stderr}"
        assert UNRESOLVABLE_MARKER in result.stderr


class TestQuoteAwareLineContinuationSplice:
    """830a090's line-continuation normalisation spliced a backslash-newline
    unconditionally, INCLUDING inside single quotes, where a shell never
    splices — both the backslash and the newline are literal characters
    there. A single-quoted path split across a backslash-newline is, to a
    real shell, a path literally CONTAINING a backslash and a newline (not
    the clean joined path), but the old blind splice rewrote it into the
    clean carve-out path and ALLOWED it: a FALSE ALLOW, not a missed block.
    Every payload here is built from explicit character codes and asserted
    on before being sent, per the project's own documented gotcha about
    flattened continuation payloads."""

    def test_single_quoted_continuation_does_not_forge_the_carveout(self, project_dir: Path):
        cmd = "echo x > 'docs/plan" + chr(92) + chr(10) + "s/x.html'"
        assert chr(10) in cmd, "payload lost its real newline before reaching the hook"
        result = _run_bash_hook(cmd, cwd=project_dir, project_dir=project_dir)
        assert result.returncode == 2, (
            f"single-quoted backslash-newline must NOT splice into the docs/plans "
            f"carve-out; rc={result.returncode} stderr={result.stderr}"
        )

    def test_single_quoted_continuation_crlf_does_not_forge_the_carveout(self, project_dir: Path):
        cmd = "echo x > 'docs/plan" + chr(92) + chr(13) + chr(10) + "s/x.html'"
        assert chr(13) in cmd and chr(10) in cmd, "payload lost its real CRLF before reaching the hook"
        result = _run_bash_hook(cmd, cwd=project_dir, project_dir=project_dir)
        assert result.returncode == 2, f"rc={result.returncode} stderr={result.stderr}"

    def test_double_quoted_continuation_still_blocks_protected(self, project_dir: Path):
        cmd = 'echo x > "hydra_core/supervi' + chr(92) + chr(10) + 'sor.py"'
        assert chr(10) in cmd, "payload lost its real newline before reaching the hook"
        result = _run_bash_hook(cmd, cwd=project_dir, project_dir=project_dir)
        assert result.returncode == 2, f"rc={result.returncode} stderr={result.stderr}"

    def test_unquoted_continuation_still_blocks_protected(self, project_dir: Path):
        cmd = "echo x > hydra_core/supervi" + chr(92) + chr(10) + "sor.py"
        assert chr(10) in cmd, "payload lost its real newline before reaching the hook"
        result = _run_bash_hook(cmd, cwd=project_dir, project_dir=project_dir)
        assert result.returncode == 2, f"rc={result.returncode} stderr={result.stderr}"

    def test_double_quoted_continuation_docs_plans_still_allowed(self, project_dir: Path):
        """A GENUINE double-quoted continuation (splice IS correct here)
        must still resolve to the clean carve-out path and be allowed."""
        cmd = 'echo x > "docs/plan' + chr(92) + chr(10) + 's/x.html"'
        assert chr(10) in cmd, "payload lost its real newline before reaching the hook"
        result = _run_bash_hook(cmd, cwd=project_dir, project_dir=project_dir)
        assert result.returncode == 0, f"rc={result.returncode} stderr={result.stderr}"

    def test_unquoted_continuation_docs_plans_still_allowed(self, project_dir: Path):
        cmd = "echo x > docs/plan" + chr(92) + chr(10) + "s/x.html"
        assert chr(10) in cmd, "payload lost its real newline before reaching the hook"
        result = _run_bash_hook(cmd, cwd=project_dir, project_dir=project_dir)
        assert result.returncode == 0, f"rc={result.returncode} stderr={result.stderr}"

    def test_docs_plans_x_html_plain_still_allowed(self, project_dir: Path):
        result = _run_bash_hook(f"echo x > {PLANS_REL}", cwd=project_dir, project_dir=project_dir)
        assert result.returncode == 0, f"rc={result.returncode} stderr={result.stderr}"

    def test_single_quoted_continuation_inside_otherwise_allowed_path_stays_blocked(self, project_dir: Path):
        """A single-quoted backslash-newline splitting the `docs` SEGMENT
        itself (distinct from the `plan[s]` split above — a different point
        in the path) must not become allowed by splicing: without the
        splice, the literal path's second component is `<newline>cs`, not
        `docs`, so it never lands under the docs/plans prefix and the
        (otherwise carve-out-eligible) `.html` extension is generically
        blocked."""
        cmd = "echo x > 'do" + chr(92) + chr(10) + "cs/plans/x.html'"
        assert chr(10) in cmd, "payload lost its real newline before reaching the hook"
        result = _run_bash_hook(cmd, cwd=project_dir, project_dir=project_dir)
        assert result.returncode == 2, f"rc={result.returncode} stderr={result.stderr}"


class TestThirdRevisionPropertyNotInstance:
    """Prove each of the three fixes above is load-bearing: patch out exactly
    that one change in a temporary copy of the hook and confirm the
    corresponding bypass returns (rc=0)."""

    def _patched_hook(self, tmp_path: Path, old: str, new: str) -> Path:
        hook_text = (HOOKS_DIR / BASH_HOOK).read_text(encoding="utf-8")
        assert old in hook_text, "expected hook text not found; test is stale"
        patched_hook = tmp_path / BASH_HOOK
        patched_hook.write_text(hook_text.replace(old, new), encoding="utf-8")
        return patched_hook

    def _run(self, hook_path: Path, cmd: str, *, cwd: Path, project_dir: Path) -> subprocess.CompletedProcess:
        payload = {"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(cwd)}
        env = {**os.environ}
        env["HYDRA_ENFORCE_ROUTING"] = "1"
        env["CLAUDE_PROJECT_DIR"] = str(project_dir)
        env.pop("HYDRA_PP_STAGE_ACTIVE", None)
        env.pop("HYDRA_WORKTREE_ROOT", None)
        return subprocess.run(
            [_PWSH, "-NoProfile", "-File", str(hook_path)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )

    def test_removing_clobber_operator_support_allows_the_bypass(self, project_dir: Path, tmp_path: Path):
        patched_hook = self._patched_hook(
            tmp_path,
            "$hits = [regex]::Matches($cmd, '>{1,2}\\|?\\s*')",
            "$hits = [regex]::Matches($cmd, '>{1,2}\\s*')",
        )
        result = self._run(
            patched_hook, f"echo x {_CLOBBER_OP} {BLOCKED_REL}", cwd=project_dir, project_dir=project_dir
        )
        assert result.returncode == 0, (
            f"property check failed: removing clobber-operator support should "
            f"have allowed this write, but rc={result.returncode} stderr={result.stderr}"
        )

    def test_removing_placeholder_check_allows_the_bypass(self, project_dir: Path, tmp_path: Path):
        needle = (
            "    if ($_bwPlaceholders -contains $raw) {\n"
            "        $script:bwUnresolvedReason = 'expansion'\n"
            "        return $true\n"
            "    }\n"
        )
        patched_hook = self._patched_hook(tmp_path, needle, "")
        cmd = "printf " + BLOCKED_REL + " | xargs -I{} sh -c 'echo x > {}'"
        result = self._run(patched_hook, cmd, cwd=project_dir, project_dir=project_dir)
        assert result.returncode == 0, (
            f"property check failed: removing the placeholder check should have "
            f"allowed this write, but rc={result.returncode} stderr={result.stderr}"
        )

    def test_reverting_to_blind_splice_forges_the_carveout(self, project_dir: Path, tmp_path: Path):
        """Reverting ONLY the quote-awareness (call the old blind
        whole-command regex instead of the new quote-tracking function,
        leaving everything else — including the function definition —
        untouched) must bring back the FALSE ALLOW."""
        patched_hook = self._patched_hook(
            tmp_path,
            "$cmd = Remove-LineContinuations $cmd",
            "$cmd = $cmd -replace '\\\\\\r?\\n', ''",
        )
        cmd = "echo x > 'docs/plan" + chr(92) + chr(10) + "s/x.html'"
        assert chr(10) in cmd, "payload lost its real newline before reaching the hook"
        result = self._run(patched_hook, cmd, cwd=project_dir, project_dir=project_dir)
        assert result.returncode == 0, (
            f"property check failed: reverting to the blind splice should have "
            f"forged the docs/plans carve-out (rc=0), but rc={result.returncode} "
            f"stderr={result.stderr}"
        )


# ---------------------------------------------------------------------------
# Fourth revision (2026-09): two further pre-existing defects in the
# `python -c` branches, both measured identically on 29dbe89 (the bypass) and
# on every revision through c634849 (both defects) — neither a regression of
# the preceding revisions:
#
#   Fix A (bypass, want BLOCK): Read-PyStringLiteral only folds ADJACENT
#   literals; its callers then require a `,`/`)` immediately after, so
#   `'hydra_core/' + 'supervisor.py'` (Python `+` concatenation) made the
#   whole destination-detection branch stop parsing and the write went
#   unexamined, for all three python -c branches (open/Path/shutil).
#   Read-PyDestExpr now reads a whole destination EXPRESSION — literals
#   joined by any mix of adjacency and `+` — and fails closed (through the
#   SAME unresolvable-destination mechanism as a shell $VAR/$(...) expansion)
#   the moment any operand isn't a plain literal.
#
#   Fix B (false positive, want ALLOW): the open() write-mode branch treated
#   ANY write-mode open() as a hit without ever testing its destination
#   against Test-BlockedDest, so open('notes.md','w') and
#   open('docs/plans/p.html','w') — the latter directly contradicting the
#   docs/plans carve-out — both refused. The destination is now routed
#   through the same Test-BlockedDest check the Path/shutil branches use.
# ---------------------------------------------------------------------------

PLANS_ASSETS_REL = "docs/plans/assets/a.txt"


class TestPyDestExprConcatenation:
    """`+`-joined (and mixed adjacent/`+`) Python string-literal destinations
    must resolve exactly as a single literal would, across all three
    python -c branches."""

    def test_open_plus_concatenation_blocked(self, project_dir: Path):
        cmd = "python -c \"open('hydra_core/' + 'supervisor.py', 'w').write('x')\""
        result = _run_bash_hook(cmd, cwd=project_dir, project_dir=project_dir)
        assert result.returncode == 2, f"rc={result.returncode} stderr={result.stderr}"

    def test_pathlib_plus_concatenation_blocked(self, project_dir: Path):
        cmd = "python -c \"from pathlib import Path; Path('hydra_core/' + 'supervisor.py').write_text('x')\""
        result = _run_bash_hook(cmd, cwd=project_dir, project_dir=project_dir)
        assert result.returncode == 2, f"rc={result.returncode} stderr={result.stderr}"

    def test_shutil_plus_concatenation_blocked(self, project_dir: Path):
        cmd = "python -c \"import shutil; shutil.copy('a.txt', 'hydra_core/' + 'supervisor.py')\""
        result = _run_bash_hook(cmd, cwd=project_dir, project_dir=project_dir)
        assert result.returncode == 2, f"rc={result.returncode} stderr={result.stderr}"

    def test_three_operand_plus_chain_blocked(self, project_dir: Path):
        cmd = "python -c \"open('hydra_core/' + 'supervisor' + '.py', 'w').write('x')\""
        result = _run_bash_hook(cmd, cwd=project_dir, project_dir=project_dir)
        assert result.returncode == 2, f"rc={result.returncode} stderr={result.stderr}"

    def test_mixed_adjacent_and_plus_chain_blocked(self, project_dir: Path):
        """Adjacency AND `+` combined in one destination expression:
        `'hydra_core/''supervisor' + '.py'` — the first two literals are
        adjacent (implicit concatenation), then `+` joins the third."""
        cmd = "python -c \"open('hydra_core/''supervisor' + '.py', 'w').write('x')\""
        result = _run_bash_hook(cmd, cwd=project_dir, project_dir=project_dir)
        assert result.returncode == 2, f"rc={result.returncode} stderr={result.stderr}"

    def test_plus_whitespace_variations_blocked(self, project_dir: Path):
        cmd = "python -c \"open('hydra_core/'   +   'supervisor.py', 'w').write('x')\""
        result = _run_bash_hook(cmd, cwd=project_dir, project_dir=project_dir)
        assert result.returncode == 2, f"rc={result.returncode} stderr={result.stderr}"


class TestPyDestExprUnresolvable:
    """An operand of the destination expression that isn't a plain string
    literal (a bare name, a call such as os.path.join(...), or an f-string
    with a `{...}` placeholder) must fail closed as UNRESOLVABLE, the same
    mechanism as a shell $VAR/$(...) expansion — not fall through un-detected
    the way the `+`-bypass did."""

    def test_open_bare_name_destination_blocked(self, project_dir: Path):
        cmd = "python -c \"D='hydra_core/supervisor.py'; open(D, 'w').write('x')\""
        result = _run_bash_hook(cmd, cwd=project_dir, project_dir=project_dir)
        assert result.returncode == 2, f"rc={result.returncode} stderr={result.stderr}"
        assert UNRESOLVABLE_MARKER in result.stderr

    def test_pathlib_os_path_join_destination_blocked(self, project_dir: Path):
        cmd = (
            "python -c \"from pathlib import Path; import os; "
            "Path(os.path.join('hydra_core', 'supervisor.py')).write_text('x')\""
        )
        result = _run_bash_hook(cmd, cwd=project_dir, project_dir=project_dir)
        assert result.returncode == 2, f"rc={result.returncode} stderr={result.stderr}"
        assert UNRESOLVABLE_MARKER in result.stderr

    def test_shutil_os_path_join_destination_blocked(self, project_dir: Path):
        cmd = (
            "python -c \"import shutil, os; "
            "shutil.copy('t.md', os.path.join('hydra_core', 'supervisor.py'))\""
        )
        result = _run_bash_hook(cmd, cwd=project_dir, project_dir=project_dir)
        assert result.returncode == 2, f"rc={result.returncode} stderr={result.stderr}"
        assert UNRESOLVABLE_MARKER in result.stderr

    def test_open_fstring_placeholder_destination_blocked(self, project_dir: Path):
        cmd = "python -c \"x='supervisor'; open(f'hydra_core/{x}.py', 'w').write('y')\""
        result = _run_bash_hook(cmd, cwd=project_dir, project_dir=project_dir)
        assert result.returncode == 2, f"rc={result.returncode} stderr={result.stderr}"
        assert UNRESOLVABLE_MARKER in result.stderr

    def test_pathlib_fstring_placeholder_destination_blocked(self, project_dir: Path):
        cmd = (
            "python -c \"from pathlib import Path; x='supervisor'; "
            "Path(f'hydra_core/{x}.py').write_text('y')\""
        )
        result = _run_bash_hook(cmd, cwd=project_dir, project_dir=project_dir)
        assert result.returncode == 2, f"rc={result.returncode} stderr={result.stderr}"
        assert UNRESOLVABLE_MARKER in result.stderr


class TestPyOpenDestinationChecked:
    """Fix B: open()'s destination must be routed through the same
    Test-BlockedDest check Path/shutil already use, not treated as a hit on
    write-mode alone."""

    def test_open_notes_md_allowed(self, project_dir: Path):
        cmd = "python -c \"open('notes.md', 'w').write('x')\""
        result = _run_bash_hook(cmd, cwd=project_dir, project_dir=project_dir)
        assert result.returncode == 0, f"rc={result.returncode} stderr={result.stderr}"

    def test_open_docs_plans_html_allowed(self, project_dir: Path):
        cmd = "python -c \"open('docs/plans/p.html', 'w').write('x')\""
        result = _run_bash_hook(cmd, cwd=project_dir, project_dir=project_dir)
        assert result.returncode == 0, f"rc={result.returncode} stderr={result.stderr}"

    def test_open_docs_plans_assets_allowed(self, project_dir: Path):
        cmd = f"python -c \"open('{PLANS_ASSETS_REL}', 'w').write('x')\""
        result = _run_bash_hook(cmd, cwd=project_dir, project_dir=project_dir)
        assert result.returncode == 0, f"rc={result.returncode} stderr={result.stderr}"

    def test_open_read_mode_still_ignored(self, project_dir: Path):
        cmd = f"python -c \"open('{BLOCKED_REL}', 'r').read()\""
        result = _run_bash_hook(cmd, cwd=project_dir, project_dir=project_dir)
        assert result.returncode == 0, f"rc={result.returncode} stderr={result.stderr}"

    def test_fstring_with_no_placeholder_allowed_path(self, project_dir: Path):
        """An f-string with NO `{...}` placeholder is just a literal and must
        resolve (not block) like any other plain string."""
        cmd = "python -c \"open(f'notes.md', 'w').write('x')\""
        result = _run_bash_hook(cmd, cwd=project_dir, project_dir=project_dir)
        assert result.returncode == 0, f"rc={result.returncode} stderr={result.stderr}"

    def test_py_path_in_non_destination_position_not_blocked(self, project_dir: Path):
        """A `.py` path appearing somewhere in the one-liner OTHER than a
        destination argument (here: printed, not written) must not block."""
        cmd = f"python -c \"print('{BLOCKED_REL}')\""
        result = _run_bash_hook(cmd, cwd=project_dir, project_dir=project_dir)
        assert result.returncode == 0, f"rc={result.returncode} stderr={result.stderr}"


class TestFourthRevisionPropertyNotInstance:
    """Prove Fix A and Fix B are each load-bearing: patch out exactly that
    one change in a temporary copy of the hook and confirm the corresponding
    defect returns."""

    def _patched_hook(self, tmp_path: Path, old: str, new: str) -> Path:
        hook_text = (HOOKS_DIR / BASH_HOOK).read_text(encoding="utf-8")
        assert old in hook_text, "expected hook text not found; test is stale"
        patched_hook = tmp_path / BASH_HOOK
        patched_hook.write_text(hook_text.replace(old, new), encoding="utf-8")
        return patched_hook

    def _run(self, hook_path: Path, cmd: str, *, cwd: Path, project_dir: Path) -> subprocess.CompletedProcess:
        payload = {"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(cwd)}
        env = {**os.environ}
        env["HYDRA_ENFORCE_ROUTING"] = "1"
        env["CLAUDE_PROJECT_DIR"] = str(project_dir)
        env.pop("HYDRA_PP_STAGE_ACTIVE", None)
        env.pop("HYDRA_WORKTREE_ROOT", None)
        return subprocess.run(
            [_PWSH, "-NoProfile", "-File", str(hook_path)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )

    def test_removing_dest_expr_allows_the_plus_bypass(self, project_dir: Path, tmp_path: Path):
        """Fix A: swap Read-PyDestExpr back for the old adjacency-only
        Read-PyStringLiteral in the open() branch and confirm the `+`
        bypass is allowed again."""
        patched_hook = self._patched_hook(
            tmp_path,
            "        $arg1 = Read-PyDestExpr $cmd ($oh.Index + $oh.Length)\n"
            "        if (-not $arg1) { continue }\n"
            "        $j = $arg1.End\n"
            "        while ($j -lt $cmd.Length -and $cmd[$j] -match '[ \\t]') { $j++ }\n"
            "        if ($j -ge $cmd.Length -or $cmd[$j] -ne ',') { continue }\n"
            "        $j++\n"
            "        while ($j -lt $cmd.Length -and $cmd[$j] -match '[ \\t]') { $j++ }\n"
            "        $arg2 = Read-PyStringLiteral $cmd $j\n"
            "        if ($arg2 -and ($arg2.Value -match '[wax]') -and\n"
            "            (Test-BlockedDest $arg1.Value $arg1.Start (-not $arg1.Resolvable))) {\n",
            "        $arg1 = Read-PyStringLiteral $cmd ($oh.Index + $oh.Length)\n"
            "        if (-not $arg1) { continue }\n"
            "        $j = $arg1.End\n"
            "        while ($j -lt $cmd.Length -and $cmd[$j] -match '[ \\t]') { $j++ }\n"
            "        if ($j -ge $cmd.Length -or $cmd[$j] -ne ',') { continue }\n"
            "        $j++\n"
            "        while ($j -lt $cmd.Length -and $cmd[$j] -match '[ \\t]') { $j++ }\n"
            "        $arg2 = Read-PyStringLiteral $cmd $j\n"
            "        if ($arg2 -and ($arg2.Value -match '[wax]')) {\n",
        )
        cmd = "python -c \"open('hydra_core/' + 'supervisor.py', 'w').write('x')\""
        result = self._run(patched_hook, cmd, cwd=project_dir, project_dir=project_dir)
        assert result.returncode == 0, (
            f"property check failed: reverting to Read-PyStringLiteral should have "
            f"allowed the '+' bypass again, but rc={result.returncode} stderr={result.stderr}"
        )

    def test_removing_open_destination_check_refuses_notes_md(self, project_dir: Path, tmp_path: Path):
        """Fix B: make the open() branch match on mode alone again (as it
        did before Test-BlockedDest was added) and confirm open('notes.md',
        'w') goes back to being refused."""
        patched_hook = self._patched_hook(
            tmp_path,
            "        if ($arg2 -and ($arg2.Value -match '[wax]') -and\n"
            "            (Test-BlockedDest $arg1.Value $arg1.Start (-not $arg1.Resolvable))) {\n",
            "        if ($arg2 -and ($arg2.Value -match '[wax]')) {\n",
        )
        cmd = "python -c \"open('notes.md', 'w').write('x')\""
        result = self._run(patched_hook, cmd, cwd=project_dir, project_dir=project_dir)
        assert result.returncode == 2, (
            f"property check failed: removing the destination check should have "
            f"refused open('notes.md','w') again, but rc={result.returncode} stderr={result.stderr}"
        )


# ---------------------------------------------------------------------------
# Fifth revision (2026-09): INVENTORY EXTENSION. The destination parser
# (Read-ShellArgument / Get-ShellArgsInRange / Read-PyDestExpr /
# Test-BlockedDest) is unchanged; these idioms were simply never recognised
# as write idioms at all, so their destination was never examined. Measured
# 0 (undetected) both on this branch before this revision and on main
# (29dbe89) — pre-existing gaps, not regressions.
#
#   dd if=/dev/zero of=hydra_core/supervisor.py
#   install /dev/null hydra_core/supervisor.py
#   truncate -s 0 hydra_core/supervisor.py
#   ln -sf a hydra_core/supervisor.py
#   python -c "import os; os.replace('a','hydra_core/supervisor.py')"
#   python -c "import os; os.rename('a','hydra_core/supervisor.py')"
#   python -c "from pathlib import Path; Path('hydra_core/supervisor.py').open('w')"
#
# `install` is the CRITICAL false-positive trap: it is overwhelmingly a
# package-manager verb, not coreutils. Test-IsCommandWord is the load-bearing
# discriminator — `install` (and `dd`/`truncate`/`ln`) counts as this hook's
# own write idiom ONLY in command-word position (start of command, or
# immediately after `;`/`&`/`|`/newline/`sudo`/`env`/`nice`), never as a
# subcommand argument of another program.
# ---------------------------------------------------------------------------


def _dd_cmd(dest_expr: str) -> str:
    return f"dd if=/dev/zero of={dest_expr}"


def _truncate_cmd(dest_expr: str) -> str:
    return f"truncate -s 0 {dest_expr}"


def _ln_cmd(dest_expr: str) -> str:
    return f"ln -sf a {dest_expr}"


def _install_cmd(dest_expr: str) -> str:
    return f"install /dev/null {dest_expr}"


class TestDdOfWrite:
    def test_blocked_shapes(self, project_dir: Path):
        for name, expr in _shapes_for(BLOCKED_REL, 10, "hydra core/supervisor.py").items():
            if name == "quoted_with_space":
                (project_dir / "hydra core").mkdir(exist_ok=True)
            result = _run_bash_hook(_dd_cmd(expr), cwd=project_dir, project_dir=project_dir)
            assert result.returncode == 2, f"{name}: expected BLOCK, got rc={result.returncode} stderr={result.stderr}"

    def test_unresolvable_destination_blocked(self, project_dir: Path):
        result = _run_bash_hook(_dd_cmd("$D"), cwd=project_dir, project_dir=project_dir)
        assert result.returncode == 2, f"rc={result.returncode} stderr={result.stderr}"
        assert UNRESOLVABLE_MARKER in result.stderr

    def test_docs_plans_allowed(self, project_dir: Path):
        result = _run_bash_hook(_dd_cmd(PLANS_REL), cwd=project_dir, project_dir=project_dir)
        assert result.returncode == 0, f"rc={result.returncode} stderr={result.stderr}"

    def test_docs_plansomething_blocked(self, project_dir: Path):
        result = _run_bash_hook(
            _dd_cmd("docs/plansomething/x.html"), cwd=project_dir, project_dir=project_dir
        )
        assert result.returncode == 2, f"rc={result.returncode} stderr={result.stderr}"

    def test_no_of_argument_allowed(self, project_dir: Path):
        """`dd` with no `of=` writes stdout, not a file — not a write idiom."""
        result = _run_bash_hook("dd if=/dev/zero bs=1 count=1", cwd=project_dir, project_dir=project_dir)
        assert result.returncode == 0, f"rc={result.returncode} stderr={result.stderr}"


class TestTruncateWrite:
    def test_blocked_shapes(self, project_dir: Path):
        for name, expr in _shapes_for(BLOCKED_REL, 10, "hydra core/supervisor.py").items():
            if name == "quoted_with_space":
                (project_dir / "hydra core").mkdir(exist_ok=True)
            result = _run_bash_hook(_truncate_cmd(expr), cwd=project_dir, project_dir=project_dir)
            assert result.returncode == 2, f"{name}: expected BLOCK, got rc={result.returncode} stderr={result.stderr}"

    def test_unresolvable_destination_blocked(self, project_dir: Path):
        result = _run_bash_hook(_truncate_cmd("$D"), cwd=project_dir, project_dir=project_dir)
        assert result.returncode == 2, f"rc={result.returncode} stderr={result.stderr}"
        assert UNRESOLVABLE_MARKER in result.stderr

    def test_docs_plans_allowed(self, project_dir: Path):
        result = _run_bash_hook(_truncate_cmd(PLANS_REL), cwd=project_dir, project_dir=project_dir)
        assert result.returncode == 0, f"rc={result.returncode} stderr={result.stderr}"

    def test_docs_plansomething_blocked(self, project_dir: Path):
        result = _run_bash_hook(
            _truncate_cmd("docs/plansomething/x.html"), cwd=project_dir, project_dir=project_dir
        )
        assert result.returncode == 2, f"rc={result.returncode} stderr={result.stderr}"

    def test_size_flag_value_not_mistaken_for_path(self, project_dir: Path):
        """`-s 10`: the size value is NOT a path and must not be examined
        (and there is no other operand here at all, so nothing to block)."""
        result = _run_bash_hook("truncate -s 10", cwd=project_dir, project_dir=project_dir)
        assert result.returncode == 0, f"rc={result.returncode} stderr={result.stderr}"

    def test_reference_file_is_read_not_write(self, project_dir: Path):
        """`-r RFILE` is READ (to copy its size), never written — even when
        RFILE is itself a protected path — and the real write target (the
        trailing operand) is what gets examined."""
        result = _run_bash_hook(
            f"truncate -r {BLOCKED_REL} notes.txt", cwd=project_dir, project_dir=project_dir
        )
        assert result.returncode == 0, f"rc={result.returncode} stderr={result.stderr}"


class TestLnWrite:
    def test_blocked_shapes(self, project_dir: Path):
        for name, expr in _shapes_for(BLOCKED_REL, 10, "hydra core/supervisor.py").items():
            if name == "quoted_with_space":
                (project_dir / "hydra core").mkdir(exist_ok=True)
            result = _run_bash_hook(_ln_cmd(expr), cwd=project_dir, project_dir=project_dir)
            assert result.returncode == 2, f"{name}: expected BLOCK, got rc={result.returncode} stderr={result.stderr}"

    def test_unresolvable_destination_blocked(self, project_dir: Path):
        result = _run_bash_hook(_ln_cmd("$D"), cwd=project_dir, project_dir=project_dir)
        assert result.returncode == 2, f"rc={result.returncode} stderr={result.stderr}"
        assert UNRESOLVABLE_MARKER in result.stderr

    def test_docs_plans_allowed(self, project_dir: Path):
        result = _run_bash_hook(_ln_cmd(PLANS_REL), cwd=project_dir, project_dir=project_dir)
        assert result.returncode == 0, f"rc={result.returncode} stderr={result.stderr}"

    def test_docs_plansomething_blocked(self, project_dir: Path):
        result = _run_bash_hook(
            _ln_cmd("docs/plansomething/x.html"), cwd=project_dir, project_dir=project_dir
        )
        assert result.returncode == 2, f"rc={result.returncode} stderr={result.stderr}"

    def test_target_protected_but_linkname_not_allowed(self, project_dir: Path):
        """Reading a protected path (as the link TARGET) is not a write —
        only the link NAME (the destination) is examined. Confirms the
        implementation reads the LAST operand as the destination, not the
        first."""
        result = _run_bash_hook(f"ln -s {BLOCKED_REL} mylink", cwd=project_dir, project_dir=project_dir)
        assert result.returncode == 0, f"rc={result.returncode} stderr={result.stderr}"


class TestInstallWrite:
    def test_blocked_shapes(self, project_dir: Path):
        for name, expr in _shapes_for(BLOCKED_REL, 10, "hydra core/supervisor.py").items():
            if name == "quoted_with_space":
                (project_dir / "hydra core").mkdir(exist_ok=True)
            result = _run_bash_hook(_install_cmd(expr), cwd=project_dir, project_dir=project_dir)
            assert result.returncode == 2, f"{name}: expected BLOCK, got rc={result.returncode} stderr={result.stderr}"

    def test_unresolvable_destination_blocked(self, project_dir: Path):
        result = _run_bash_hook(_install_cmd("$D"), cwd=project_dir, project_dir=project_dir)
        assert result.returncode == 2, f"rc={result.returncode} stderr={result.stderr}"
        assert UNRESOLVABLE_MARKER in result.stderr

    def test_docs_plans_allowed(self, project_dir: Path):
        result = _run_bash_hook(_install_cmd(PLANS_REL), cwd=project_dir, project_dir=project_dir)
        assert result.returncode == 0, f"rc={result.returncode} stderr={result.stderr}"

    def test_docs_plansomething_blocked(self, project_dir: Path):
        result = _run_bash_hook(
            _install_cmd("docs/plansomething/x.html"), cwd=project_dir, project_dir=project_dir
        )
        assert result.returncode == 2, f"rc={result.returncode} stderr={result.stderr}"

    @pytest.mark.parametrize(
        "cmd",
        [
            "npm install",
            "npm install express",
            "npm i",
            "pnpm install",
            "yarn install",
            "bun install",
            "pip install requests",
            "pip3 install -r requirements.txt",
            "python -m pip install -e .",
            "apt install foo",
            "apt-get install foo",
            "dnf install foo",
            "yum install foo",
            "brew install foo",
            "cargo install ripgrep",
            "go install ./...",
            "gem install bundler",
            "composer install",
            "choco install foo",
            "winget install foo",
            # slash/dot/scope/at package names must not be mistaken for paths
            "pip install ruamel.yaml",
            "npm install @scope/pkg",
            "go install example.com/cmd/tool@latest",
        ],
    )
    def test_package_manager_install_allowed(self, project_dir: Path, cmd: str):
        result = _run_bash_hook(cmd, cwd=project_dir, project_dir=project_dir)
        assert result.returncode == 0, f"{cmd!r}: expected ALLOW, got rc={result.returncode} stderr={result.stderr}"


class TestPyOsReplaceRename:
    @pytest.mark.parametrize("fn", ["replace", "rename"])
    def test_blocked(self, project_dir: Path, fn: str):
        cmd = f"python -c \"import os; os.{fn}('a','{BLOCKED_REL}')\""
        result = _run_bash_hook(cmd, cwd=project_dir, project_dir=project_dir)
        assert result.returncode == 2, f"rc={result.returncode} stderr={result.stderr}"

    @pytest.mark.parametrize("fn", ["replace", "rename"])
    def test_unresolvable_blocked(self, project_dir: Path, fn: str):
        cmd = f"python -c \"import os; D='{BLOCKED_REL}'; os.{fn}('a', D)\""
        result = _run_bash_hook(cmd, cwd=project_dir, project_dir=project_dir)
        assert result.returncode == 2, f"rc={result.returncode} stderr={result.stderr}"
        assert UNRESOLVABLE_MARKER in result.stderr

    @pytest.mark.parametrize("fn", ["replace", "rename"])
    def test_docs_plans_allowed(self, project_dir: Path, fn: str):
        cmd = f"python -c \"import os; os.{fn}('a','{PLANS_REL}')\""
        result = _run_bash_hook(cmd, cwd=project_dir, project_dir=project_dir)
        assert result.returncode == 0, f"rc={result.returncode} stderr={result.stderr}"


class TestPyOsSymlinkLink:
    @pytest.mark.parametrize("fn", ["symlink", "link"])
    def test_blocked(self, project_dir: Path, fn: str):
        cmd = f"python -c \"import os; os.{fn}('a','{BLOCKED_REL}')\""
        result = _run_bash_hook(cmd, cwd=project_dir, project_dir=project_dir)
        assert result.returncode == 2, f"rc={result.returncode} stderr={result.stderr}"

    @pytest.mark.parametrize("fn", ["symlink", "link"])
    def test_target_protected_but_dst_not_allowed(self, project_dir: Path, fn: str):
        """The first argument (the existing file being linked TO) is only
        read; only the second argument (the new link) is a write."""
        cmd = f"python -c \"import os; os.{fn}('{BLOCKED_REL}','newlink')\""
        result = _run_bash_hook(cmd, cwd=project_dir, project_dir=project_dir)
        assert result.returncode == 0, f"rc={result.returncode} stderr={result.stderr}"

    @pytest.mark.parametrize("fn", ["symlink", "link"])
    def test_docs_plans_allowed(self, project_dir: Path, fn: str):
        cmd = f"python -c \"import os; os.{fn}('a','{PLANS_REL}')\""
        result = _run_bash_hook(cmd, cwd=project_dir, project_dir=project_dir)
        assert result.returncode == 0, f"rc={result.returncode} stderr={result.stderr}"


class TestPyOsTruncate:
    def test_blocked(self, project_dir: Path):
        cmd = f"python -c \"import os; os.truncate('{BLOCKED_REL}', 0)\""
        result = _run_bash_hook(cmd, cwd=project_dir, project_dir=project_dir)
        assert result.returncode == 2, f"rc={result.returncode} stderr={result.stderr}"

    def test_unresolvable_blocked(self, project_dir: Path):
        cmd = f"python -c \"import os; D='{BLOCKED_REL}'; os.truncate(D, 0)\""
        result = _run_bash_hook(cmd, cwd=project_dir, project_dir=project_dir)
        assert result.returncode == 2, f"rc={result.returncode} stderr={result.stderr}"
        assert UNRESOLVABLE_MARKER in result.stderr

    def test_docs_plans_allowed(self, project_dir: Path):
        cmd = f"python -c \"import os; os.truncate('{PLANS_REL}', 0)\""
        result = _run_bash_hook(cmd, cwd=project_dir, project_dir=project_dir)
        assert result.returncode == 0, f"rc={result.returncode} stderr={result.stderr}"


class TestPathOpenWriteMode:
    def test_positional_write_mode_blocked(self, project_dir: Path):
        cmd = "python -c \"from pathlib import Path; Path('" + BLOCKED_REL + "').open('w')\""
        result = _run_bash_hook(cmd, cwd=project_dir, project_dir=project_dir)
        assert result.returncode == 2, f"rc={result.returncode} stderr={result.stderr}"

    def test_keyword_mode_blocked(self, project_dir: Path):
        cmd = "python -c \"from pathlib import Path; Path('" + BLOCKED_REL + "').open(mode='w')\""
        result = _run_bash_hook(cmd, cwd=project_dir, project_dir=project_dir)
        assert result.returncode == 2, f"rc={result.returncode} stderr={result.stderr}"

    def test_read_mode_allowed(self, project_dir: Path):
        cmd = "python -c \"from pathlib import Path; Path('" + BLOCKED_REL + "').open('r')\""
        result = _run_bash_hook(cmd, cwd=project_dir, project_dir=project_dir)
        assert result.returncode == 0, f"rc={result.returncode} stderr={result.stderr}"

    def test_no_args_defaults_to_read_allowed(self, project_dir: Path):
        cmd = "python -c \"from pathlib import Path; Path('" + BLOCKED_REL + "').open()\""
        result = _run_bash_hook(cmd, cwd=project_dir, project_dir=project_dir)
        assert result.returncode == 0, f"rc={result.returncode} stderr={result.stderr}"

    def test_docs_plans_allowed(self, project_dir: Path):
        cmd = "python -c \"from pathlib import Path; Path('" + PLANS_REL + "').open('w')\""
        result = _run_bash_hook(cmd, cwd=project_dir, project_dir=project_dir)
        assert result.returncode == 0, f"rc={result.returncode} stderr={result.stderr}"


class TestIoCodecsOpen:
    @pytest.mark.parametrize("prefix", ["io", "codecs"])
    def test_write_mode_blocked(self, project_dir: Path, prefix: str):
        cmd = f"python -c \"import {prefix}; {prefix}.open('{BLOCKED_REL}','w')\""
        result = _run_bash_hook(cmd, cwd=project_dir, project_dir=project_dir)
        assert result.returncode == 2, f"rc={result.returncode} stderr={result.stderr}"

    @pytest.mark.parametrize("prefix", ["io", "codecs"])
    def test_read_mode_allowed(self, project_dir: Path, prefix: str):
        cmd = f"python -c \"import {prefix}; {prefix}.open('{BLOCKED_REL}','r')\""
        result = _run_bash_hook(cmd, cwd=project_dir, project_dir=project_dir)
        assert result.returncode == 0, f"rc={result.returncode} stderr={result.stderr}"

    @pytest.mark.parametrize("prefix", ["io", "codecs"])
    def test_docs_plans_allowed(self, project_dir: Path, prefix: str):
        cmd = f"python -c \"import {prefix}; {prefix}.open('{PLANS_REL}','w')\""
        result = _run_bash_hook(cmd, cwd=project_dir, project_dir=project_dir)
        assert result.returncode == 0, f"rc={result.returncode} stderr={result.stderr}"


class TestFifthRevisionPropertyNotInstance:
    """Prove each new idiom's detection is load-bearing: patch out exactly
    that one branch in a temporary copy of the hook and confirm the
    corresponding bypass returns (rc=0), following the same pattern as the
    third/fourth revisions' test_removing_* tests."""

    def _patched_hook(self, tmp_path: Path, old: str, new: str) -> Path:
        hook_text = (HOOKS_DIR / BASH_HOOK).read_text(encoding="utf-8")
        assert old in hook_text, "expected hook text not found; test is stale"
        patched_hook = tmp_path / BASH_HOOK
        patched_hook.write_text(hook_text.replace(old, new), encoding="utf-8")
        return patched_hook

    def _run(self, hook_path: Path, cmd: str, *, cwd: Path, project_dir: Path) -> subprocess.CompletedProcess:
        payload = {"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(cwd)}
        env = {**os.environ}
        env["HYDRA_ENFORCE_ROUTING"] = "1"
        env["CLAUDE_PROJECT_DIR"] = str(project_dir)
        env.pop("HYDRA_PP_STAGE_ACTIVE", None)
        env.pop("HYDRA_WORKTREE_ROOT", None)
        return subprocess.run(
            [_PWSH, "-NoProfile", "-File", str(hook_path)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )

    def test_removing_dd_detection_allows_the_bypass(self, project_dir: Path, tmp_path: Path):
        patched_hook = self._patched_hook(
            tmp_path,
            "if (-not $matched) {\n    $ddHits = [regex]::Matches($cmd, '\\bdd\\b')",
            "if ($false) {\n    $ddHits = [regex]::Matches($cmd, '\\bdd\\b')",
        )
        result = self._run(patched_hook, _dd_cmd(BLOCKED_REL), cwd=project_dir, project_dir=project_dir)
        assert result.returncode == 0, (
            f"property check failed: removing dd detection should have allowed "
            f"the bypass, but rc={result.returncode} stderr={result.stderr}"
        )

    def test_removing_truncate_detection_allows_the_bypass(self, project_dir: Path, tmp_path: Path):
        patched_hook = self._patched_hook(
            tmp_path,
            "if (-not $matched) {\n    $truncHits = [regex]::Matches($cmd, '\\btruncate\\b')",
            "if ($false) {\n    $truncHits = [regex]::Matches($cmd, '\\btruncate\\b')",
        )
        result = self._run(patched_hook, _truncate_cmd(BLOCKED_REL), cwd=project_dir, project_dir=project_dir)
        assert result.returncode == 0, (
            f"property check failed: removing truncate detection should have "
            f"allowed the bypass, but rc={result.returncode} stderr={result.stderr}"
        )

    def test_removing_ln_detection_allows_the_bypass(self, project_dir: Path, tmp_path: Path):
        patched_hook = self._patched_hook(
            tmp_path,
            "if (-not $matched) {\n    $lnHits = [regex]::Matches($cmd, '\\bln\\b')",
            "if ($false) {\n    $lnHits = [regex]::Matches($cmd, '\\bln\\b')",
        )
        result = self._run(patched_hook, _ln_cmd(BLOCKED_REL), cwd=project_dir, project_dir=project_dir)
        assert result.returncode == 0, (
            f"property check failed: removing ln detection should have allowed "
            f"the bypass, but rc={result.returncode} stderr={result.stderr}"
        )

    def test_removing_install_detection_allows_the_bypass(self, project_dir: Path, tmp_path: Path):
        patched_hook = self._patched_hook(
            tmp_path,
            "if (-not $matched) {\n    $instHits = [regex]::Matches($cmd, '\\binstall\\b')",
            "if ($false) {\n    $instHits = [regex]::Matches($cmd, '\\binstall\\b')",
        )
        result = self._run(patched_hook, _install_cmd(BLOCKED_REL), cwd=project_dir, project_dir=project_dir)
        assert result.returncode == 0, (
            f"property check failed: removing install detection should have "
            f"allowed the bypass, but rc={result.returncode} stderr={result.stderr}"
        )

    def test_removing_install_command_word_guard_breaks_npm_install(self, project_dir: Path, tmp_path: Path):
        """The false-positive trap: without Test-IsCommandWord gating the
        `install` branch, an npm invocation shaped like `npm install <pkg>
        <path>` would also be examined by the coreutils `install SOURCE...
        DEST` logic (its LAST operand tested as a destination) — prove the
        guard is why it currently isn't.

        Needs >=2 operands after the word `install`: the coreutils branch's
        last-operand-is-destination check only fires when
        `operands.Count -ge 2` (single-operand installs like plain `npm
        install <path>` never reach that check regardless of the guard, so
        they would not distinguish this property)."""
        patched_hook = self._patched_hook(
            tmp_path,
            "    foreach ($ih in $instHits) {\n"
            "        if (-not (Test-IsCommandWord $cmd $ih.Index)) { continue }\n",
            "    foreach ($ih in $instHits) {\n",
        )
        cmd = f"npm install /dev/null {BLOCKED_REL}"
        result = self._run(patched_hook, cmd, cwd=project_dir, project_dir=project_dir)
        assert result.returncode == 2, (
            f"property check failed: removing Test-IsCommandWord gating should "
            f"have made `{cmd}` examine its last operand as a destination and "
            f"block it, but rc={result.returncode} stderr={result.stderr}"
        )

    def test_removing_os_replace_rename_detection_allows_the_bypass(self, project_dir: Path, tmp_path: Path):
        patched_hook = self._patched_hook(
            tmp_path,
            "if (-not $matched) {\n    $osrHits = [regex]::Matches($cmd, '\\bos\\s*\\.\\s*(?:replace|rename)\\s*\\(\\s*')",
            "if ($false) {\n    $osrHits = [regex]::Matches($cmd, '\\bos\\s*\\.\\s*(?:replace|rename)\\s*\\(\\s*')",
        )
        cmd = f"python -c \"import os; os.replace('a','{BLOCKED_REL}')\""
        result = self._run(patched_hook, cmd, cwd=project_dir, project_dir=project_dir)
        assert result.returncode == 0, (
            f"property check failed: removing os.replace/os.rename detection "
            f"should have allowed the bypass, but rc={result.returncode} stderr={result.stderr}"
        )

    def test_removing_os_symlink_link_detection_allows_the_bypass(self, project_dir: Path, tmp_path: Path):
        patched_hook = self._patched_hook(
            tmp_path,
            "if (-not $matched) {\n    $oslHits = [regex]::Matches($cmd, '\\bos\\s*\\.\\s*(?:symlink|link)\\s*\\(\\s*')",
            "if ($false) {\n    $oslHits = [regex]::Matches($cmd, '\\bos\\s*\\.\\s*(?:symlink|link)\\s*\\(\\s*')",
        )
        cmd = f"python -c \"import os; os.symlink('a','{BLOCKED_REL}')\""
        result = self._run(patched_hook, cmd, cwd=project_dir, project_dir=project_dir)
        assert result.returncode == 0, (
            f"property check failed: removing os.symlink/os.link detection "
            f"should have allowed the bypass, but rc={result.returncode} stderr={result.stderr}"
        )

    def test_removing_os_truncate_detection_allows_the_bypass(self, project_dir: Path, tmp_path: Path):
        patched_hook = self._patched_hook(
            tmp_path,
            "if (-not $matched) {\n    $otHits = [regex]::Matches($cmd, '\\bos\\s*\\.\\s*truncate\\s*\\(\\s*')",
            "if ($false) {\n    $otHits = [regex]::Matches($cmd, '\\bos\\s*\\.\\s*truncate\\s*\\(\\s*')",
        )
        cmd = f"python -c \"import os; os.truncate('{BLOCKED_REL}', 0)\""
        result = self._run(patched_hook, cmd, cwd=project_dir, project_dir=project_dir)
        assert result.returncode == 0, (
            f"property check failed: removing os.truncate detection should "
            f"have allowed the bypass, but rc={result.returncode} stderr={result.stderr}"
        )

    def test_removing_path_open_mode_check_allows_the_bypass(self, project_dir: Path, tmp_path: Path):
        patched_hook = self._patched_hook(
            tmp_path,
            "            if ($modeVal -and ($modeVal -match '[wax]')) {\n"
            "                if (Test-BlockedDest $arg.Value $arg.Start (-not $arg.Resolvable)) {\n"
            "                    $matched = $true\n"
            "                    $reason = \"pathlib.Path.open('$modeVal') targets '$($arg.Value)'\"\n"
            "                    break\n"
            "                }\n"
            "            }",
            "            if ($false) {\n"
            "                if (Test-BlockedDest $arg.Value $arg.Start (-not $arg.Resolvable)) {\n"
            "                    $matched = $true\n"
            "                    $reason = \"pathlib.Path.open('$modeVal') targets '$($arg.Value)'\"\n"
            "                    break\n"
            "                }\n"
            "            }",
        )
        cmd = "python -c \"from pathlib import Path; Path('" + BLOCKED_REL + "').open('w')\""
        result = self._run(patched_hook, cmd, cwd=project_dir, project_dir=project_dir)
        assert result.returncode == 0, (
            f"property check failed: removing the Path.open mode check should "
            f"have allowed the bypass, but rc={result.returncode} stderr={result.stderr}"
        )

    # NOTE: there is deliberately no test_removing_io_codecs_open_extension_*
    # property test here. An earlier revision matched io.open(/codecs.open(
    # via an explicit `io\s*\.\s*open|codecs\s*\.\s*open` alternation; a
    # property test proved that alternation redundant, not load-bearing:
    # the char before `open` in `io.open(` / `codecs.open(` is `.`, a
    # non-word character, so the bare `\bopen\s*\(` word-boundary match
    # already matches it — reverting to the bare regex does NOT reopen the
    # bypass. The alternation was removed as dead code; TestIoCodecsOpen
    # above is the real regression guard for io.open()/codecs.open() write
    # detection.

# ---------------------------------------------------------------------------
# Sixth revision (2026-09): `-t DIR` / `--target-directory=DIR` destinations.
#
# PROVEN gap: `cp`/`mv` had no `-t` handling at all, and `ln`/`install` parsed
# `-t DIR` / `--target-directory=DIR` but checked the DIRECTORY path itself -
# a directory like `hydra_core` carries no blocked extension, so the write
# sailed through. The actual file written is DIR joined with the BASENAME of
# each source (`cp supervisor.py -t hydra_core` writes
# `hydra_core/supervisor.py`), which IS protected. Fixed by synthesising that
# joined path and running it through the same `Test-BlockedDest` every other
# branch already uses - no new parsing, no new refusal reason.
#
# THE TRAP: `-t` means something else entirely for tar (list), sort (field
# separator), docker/ssh (tty), systemctl (unit type), timeout (duration),
# etc. Only `cp`/`mv`/`install`/`ln`, and only in COMMAND-WORD position
# (Test-IsCommandWord), may interpret `-t` as target-directory.
# ---------------------------------------------------------------------------


class TestCpMvTargetDirectory:
    def test_cp_dash_t_separated_blocked(self, project_dir: Path):
        result = _run_bash_hook(
            "cp supervisor.py -t hydra_core", cwd=project_dir, project_dir=project_dir
        )
        assert result.returncode == 2, f"rc={result.returncode} stderr={result.stderr}"

    def test_cp_target_directory_equals_blocked(self, project_dir: Path):
        result = _run_bash_hook(
            "cp supervisor.py --target-directory=hydra_core", cwd=project_dir, project_dir=project_dir
        )
        assert result.returncode == 2, f"rc={result.returncode} stderr={result.stderr}"

    def test_mv_dash_t_blocked(self, project_dir: Path):
        result = _run_bash_hook(
            "mv supervisor.py -t hydra_core", cwd=project_dir, project_dir=project_dir
        )
        assert result.returncode == 2, f"rc={result.returncode} stderr={result.stderr}"

    def test_cp_dash_t_docs_allowed(self, project_dir: Path):
        # cp notes.md -t docs resolves to docs/notes.md, an allowed
        # extension, so allowed.
        result = _run_bash_hook("cp notes.md -t docs", cwd=project_dir, project_dir=project_dir)
        assert result.returncode == 0, f"rc={result.returncode} stderr={result.stderr}"

    def test_cp_dash_t_docs_plans_html_allowed(self, project_dir: Path):
        # The docs/plans carve-out is scoped to .html, not to cp/mv
        # generally - cp report.html -t docs/plans resolves to the
        # carved-out docs/plans/report.html and is allowed.
        result = _run_bash_hook(
            "cp report.html -t docs/plans", cwd=project_dir, project_dir=project_dir
        )
        assert result.returncode == 0, f"rc={result.returncode} stderr={result.stderr}"

    def test_cp_dash_t_docs_plansomething_blocked(self, project_dir: Path):
        # Segment-bounded: docs/plansomething must NOT match the docs/plans
        # carve-out, even reached via -t.
        result = _run_bash_hook(
            "cp supervisor.py -t docs/plansomething", cwd=project_dir, project_dir=project_dir
        )
        assert result.returncode == 2, f"rc={result.returncode} stderr={result.stderr}"

    def test_mv_dash_t_docs_plansomething_blocked(self, project_dir: Path):
        result = _run_bash_hook(
            "mv supervisor.py -t docs/plansomething", cwd=project_dir, project_dir=project_dir
        )
        assert result.returncode == 2, f"rc={result.returncode} stderr={result.stderr}"

    def test_positional_control_still_blocked(self, project_dir: Path):
        # Control: the already-correct positional form must stay blocked.
        result = _run_bash_hook(
            "cp a.txt hydra_core/supervisor.py", cwd=project_dir, project_dir=project_dir
        )
        assert result.returncode == 2, f"rc={result.returncode} stderr={result.stderr}"


class TestInstallTargetDirectory:
    def test_install_dash_t_separated_blocked(self, project_dir: Path):
        result = _run_bash_hook(
            "install supervisor.py -t hydra_core", cwd=project_dir, project_dir=project_dir
        )
        assert result.returncode == 2, f"rc={result.returncode} stderr={result.stderr}"

    def test_install_target_directory_equals_blocked(self, project_dir: Path):
        result = _run_bash_hook(
            "install supervisor.py --target-directory=hydra_core", cwd=project_dir, project_dir=project_dir
        )
        assert result.returncode == 2, f"rc={result.returncode} stderr={result.stderr}"

    def test_install_dev_null_dash_t_allowed(self, project_dir: Path):
        # install /dev/null -t hydra_core resolves to hydra_core/null - no
        # blocked extension, so ALLOWED. Deliberate: the guard is
        # extension-based, not path-based.
        result = _run_bash_hook(
            "install /dev/null -t hydra_core", cwd=project_dir, project_dir=project_dir
        )
        assert result.returncode == 0, f"rc={result.returncode} stderr={result.stderr}"

    def test_install_dash_t_docs_plansomething_blocked(self, project_dir: Path):
        result = _run_bash_hook(
            "install supervisor.py -t docs/plansomething", cwd=project_dir, project_dir=project_dir
        )
        assert result.returncode == 2, f"rc={result.returncode} stderr={result.stderr}"


class TestLnTargetDirectory:
    def test_ln_dash_t_blocked(self, project_dir: Path):
        result = _run_bash_hook(
            "ln -t hydra_core supervisor.py", cwd=project_dir, project_dir=project_dir
        )
        assert result.returncode == 2, f"rc={result.returncode} stderr={result.stderr}"

    def test_ln_dash_t_docs_plansomething_blocked(self, project_dir: Path):
        result = _run_bash_hook(
            "ln -t docs/plansomething supervisor.py", cwd=project_dir, project_dir=project_dir
        )
        assert result.returncode == 2, f"rc={result.returncode} stderr={result.stderr}"


class TestDashTFalsePositiveTrap:
    # -t is a common flag meaning something entirely different for these
    # commands - none of them is cp/mv/install/ln, so this hook must never
    # touch them regardless of the -t target-directory handling added above.

    @pytest.mark.parametrize(
        "cmd",
        [
            "tar -tf archive.tar",
            "sort -t, -k2 data.csv",
            "docker run -t image",
            "ssh -t host cmd",
            "systemctl list-units -t service",
            "timeout -t 5 cmd",
        ],
    )
    def test_non_copy_commands_dash_t_allowed(self, project_dir: Path, cmd: str):
        result = _run_bash_hook(cmd, cwd=project_dir, project_dir=project_dir)
        assert result.returncode == 0, f"{cmd}: rc={result.returncode} stderr={result.stderr}"


class TestSixthRevisionPropertyNotInstance:
    # Prove the -t/--target-directory= handling is load-bearing: patch it
    # out in a temporary copy of the hook and confirm the bypass returns
    # (rc=0), following the same pattern as the fourth/fifth revisions.

    def _patched_hook(self, tmp_path: Path, old: str, new: str) -> Path:
        hook_text = (HOOKS_DIR / BASH_HOOK).read_text(encoding="utf-8")
        assert old in hook_text, "expected hook text not found; test is stale"
        patched_hook = tmp_path / BASH_HOOK
        patched_hook.write_text(hook_text.replace(old, new), encoding="utf-8")
        return patched_hook

    def _run(self, hook_path: Path, cmd: str, *, cwd: Path, project_dir: Path) -> subprocess.CompletedProcess:
        payload = {"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(cwd)}
        env = {**os.environ}
        env["HYDRA_ENFORCE_ROUTING"] = "1"
        env["CLAUDE_PROJECT_DIR"] = str(project_dir)
        env.pop("HYDRA_PP_STAGE_ACTIVE", None)
        env.pop("HYDRA_WORKTREE_ROOT", None)
        return subprocess.run(
            [_PWSH, "-NoProfile", "-File", str(hook_path)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )

    def test_removing_cp_mv_target_directory_handling_allows_the_bypass(
        self, project_dir: Path, tmp_path: Path
    ):
        patched_hook = self._patched_hook(
            tmp_path,
            "        if (Test-IsCommandWord $cmd $hit.Index) {\n",
            "        if ($false) {\n",
        )
        result = self._run(
            patched_hook, "cp supervisor.py -t hydra_core", cwd=project_dir, project_dir=project_dir
        )
        assert result.returncode == 0, (
            f"property check failed: removing the cp/mv -t target-directory "
            f"handling should have allowed the bypass, but "
            f"rc={result.returncode} stderr={result.stderr}"
        )
