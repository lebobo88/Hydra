"""Unit tests for cli._read_agent_frontmatter and its thin wrapper.

The reader reads the agent file exactly once and returns ``(name, error)``;
``_agent_frontmatter_name`` must equal ``reader(p)[0]`` for every input.
"""
from __future__ import annotations

import errno
from pathlib import Path
from unittest import mock

import pytest

from hydra_core.cli import _agent_frontmatter_name, _read_agent_frontmatter


def _check(p: Path, expected: tuple[str | None, str | None]) -> None:
    assert _read_agent_frontmatter(p) == expected
    assert _agent_frontmatter_name(p) == _read_agent_frontmatter(p)[0]


def test_name_present(tmp_path: Path) -> None:
    p = tmp_path / "a.md"
    p.write_text("---\nname: x\n---\nbody\n", encoding="utf-8")
    _check(p, ("x", None))


def test_no_frontmatter(tmp_path: Path) -> None:
    p = tmp_path / "a.md"
    p.write_text("just a body\n", encoding="utf-8")
    _check(p, (None, None))


def test_frontmatter_without_name_key(tmp_path: Path) -> None:
    p = tmp_path / "a.md"
    p.write_text("---\ndescription: d\n  name: nested\n---\nbody\n", encoding="utf-8")
    _check(p, (None, None))


def test_unreadable_file_returns_error(tmp_path: Path) -> None:
    p = tmp_path / "a.md"
    p.write_text("---\nname: x\n---\n", encoding="utf-8")
    with mock.patch.object(Path, "read_text",
                           side_effect=PermissionError(errno.EACCES, "denied")):
        name, err = _read_agent_frontmatter(p)
        assert name is None
        assert err is not None and err.startswith("PermissionError: ")
        assert _agent_frontmatter_name(p) == name


def test_non_oserror_propagates(tmp_path: Path) -> None:
    with mock.patch.object(Path, "read_text", side_effect=ValueError("embedded null byte")):
        with pytest.raises(ValueError):
            _read_agent_frontmatter(tmp_path / "a.md")
        with pytest.raises(ValueError):
            _agent_frontmatter_name(tmp_path / "a.md")
