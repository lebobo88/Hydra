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
