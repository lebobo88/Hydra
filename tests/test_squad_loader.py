"""Unit tests for hydra_core/squad_loader.py.

Verifies squad discovery, duplicate agent slug rejection, and dual-path fallback resolution.
"""
from __future__ import annotations

from pathlib import Path
import pytest
import yaml

from hydra_core.squad_loader import (
    _coerce_pack,
    discover_squads,
    resolve_agent_file_path,
    SquadPack,
)


def test_squad_loader_rejects_duplicate_slugs(tmp_path):
    duplicate_data = {
        "squad": "test-squad",
        "description": "testing duplicate rejection",
        "agents": [
            {"slug": "agent-a", "role": "Worker 1"},
            {"slug": "agent-a", "role": "Worker 2"},
        ],
    }
    with pytest.raises(ValueError, match="Duplicate agent slug 'agent-a'"):
        _coerce_pack("test-squad", duplicate_data)


def test_resolve_agent_file_path_dual_fallback(tmp_path):
    pack_root = tmp_path / "test_pack"
    pack_root.mkdir(parents=True)

    # 1. Primary path exists directly
    primary_dir = pack_root / "plugins/rlm-creative/agents"
    primary_dir.mkdir(parents=True)
    primary_file = primary_dir / "calliope.md"
    primary_file.write_text("# Calliope", encoding="utf-8")

    res1 = resolve_agent_file_path(pack_root, "plugins/rlm-creative/agents/calliope.md")
    assert res1 == primary_file

    # 2. Modern path requested, but only legacy exists -> fallback to legacy
    legacy_dir = pack_root / ".claude/agents"
    legacy_dir.mkdir(parents=True)
    legacy_file = legacy_dir / "erato.md"
    legacy_file.write_text("# Erato Legacy", encoding="utf-8")

    res2 = resolve_agent_file_path(pack_root, "plugins/rlm-creative/agents/erato.md")
    assert res2 == legacy_file

    # 3. Legacy path requested, but only modern exists -> fallback to modern
    modern_file = primary_dir / "polyhymnia.md"
    modern_file.write_text("# Polyhymnia Modern", encoding="utf-8")

    res3 = resolve_agent_file_path(pack_root, ".claude/agents/polyhymnia.md")
    assert res3 == modern_file

    # 4. Neither exists -> FileNotFoundError
    with pytest.raises(FileNotFoundError, match="not found in pack"):
        resolve_agent_file_path(pack_root, "plugins/rlm-creative/agents/missing.md")
