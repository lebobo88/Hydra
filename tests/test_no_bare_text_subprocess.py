"""E2-36 — no bare ``text=True`` subprocess call may survive in the codebase.

``subprocess.run(..., text=True)`` without an explicit ``encoding`` decodes the
child's streams with the platform's preferred encoding.  On Windows that is the
ANSI codepage (cp1252), and a single unmappable byte raises inside the reader
thread, killing it and losing that stream entirely.  Live evidence: the
detached ingest log for workflow ``166fc7ee`` shows
``UnicodeDecodeError: 'charmap' codec can't decode byte 0x90``.

The guard test walks the AST of every source file under ``hydra_core/`` and
``mcp_servers/`` and fails on any call that passes ``text=True`` (or
``universal_newlines=True``) without also passing ``encoding=``.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

from hydra_core.proc import run_text

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCANNED_DIRS = ("hydra_core", "mcp_servers")

# This file documents the anti-pattern in prose and would otherwise flag itself.
_EXEMPT = {Path(__file__).resolve()}


def _source_files() -> list[Path]:
    files: list[Path] = []
    for rel in _SCANNED_DIRS:
        root = _REPO_ROOT / rel
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.py")):
            if path.resolve() in _EXEMPT:
                continue
            files.append(path)
    return files


def _offenders_in(path: Path) -> list[str]:
    """Return ``<file>:<line>`` for every call with text=True but no encoding."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError as exc:  # pragma: no cover — a broken file is its own bug
        pytest.fail(f"{path}: could not parse: {exc}")

    hits: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        kwargs = {kw.arg for kw in node.keywords if kw.arg}
        text_mode = any(
            kw.arg in ("text", "universal_newlines")
            and isinstance(kw.value, ast.Constant)
            and kw.value.value is True
            for kw in node.keywords
        )
        if text_mode and "encoding" not in kwargs:
            hits.append(f"{path.relative_to(_REPO_ROOT)}:{node.lineno}")
    return hits


def test_no_bare_text_true_subprocess_calls() -> None:
    offenders: list[str] = []
    for path in _source_files():
        offenders.extend(_offenders_in(path))
    assert not offenders, (
        "text=True without encoding= decodes with the Windows ANSI codepage and "
        "kills the reader thread on the first unmappable byte (E2-36). Use "
        "hydra_core.proc.run_text, or pass encoding='utf-8', errors='replace'. "
        "Offenders:\n  " + "\n  ".join(offenders)
    )


def test_scanner_detects_a_planted_offender(tmp_path: Path) -> None:
    """The guard must actually fail on the pattern it claims to catch."""
    planted = tmp_path / "planted.py"
    planted.write_text(
        "import subprocess\n"
        "subprocess.run(['x'], capture_output=True, text=True)\n",
        encoding="utf-8",
    )
    tree = ast.parse(planted.read_text(encoding="utf-8"))
    bare = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and any(
            kw.arg == "text" and getattr(kw.value, "value", None) is True
            for kw in n.keywords
        )
        and "encoding" not in {kw.arg for kw in n.keywords}
    ]
    assert bare, "scanner logic would not have caught a bare text=True call"


def test_run_text_survives_non_cp1252_bytes() -> None:
    """A child emitting bytes unmappable in cp1252 must not kill the reader."""
    res = run_text(
        [sys.executable, "-c",
         r"import sys; sys.stdout.buffer.write(b'\xe2\x94\x80\x90ok')"],
        capture_output=True,
        timeout=60,
    )
    assert res.returncode == 0
    # \xe2\x94\x80 is U+2500 BOX DRAWINGS LIGHT HORIZONTAL; \x90 is not valid
    # UTF-8 on its own and must degrade to U+FFFD rather than raise.
    assert res.stdout.endswith("ok")
    assert "─" in res.stdout
    assert "�" in res.stdout


def test_run_text_forces_utf8_child_io_encoding() -> None:
    res = run_text(
        [sys.executable, "-c", "import os; print(os.environ['PYTHONIOENCODING'])"],
        capture_output=True,
        timeout=60,
    )
    assert res.returncode == 0
    assert res.stdout.strip().lower().startswith("utf-8")


def test_run_text_preserves_caller_env() -> None:
    res = run_text(
        [sys.executable, "-c", "import os; print(os.environ['E2_36_MARKER'])"],
        capture_output=True,
        timeout=60,
        env={**{"E2_36_MARKER": "kept"}, "SYSTEMROOT": _systemroot()},
    )
    assert res.returncode == 0
    assert res.stdout.strip() == "kept"


def _systemroot() -> str:
    import os
    # Windows python refuses to start without SystemRoot in a scrubbed env.
    return os.environ.get("SYSTEMROOT", os.environ.get("SystemRoot", ""))


def test_run_text_returns_completed_process() -> None:
    res = run_text([sys.executable, "-c", "pass"], capture_output=True, timeout=60)
    assert isinstance(res, subprocess.CompletedProcess)
