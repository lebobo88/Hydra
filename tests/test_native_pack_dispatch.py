"""Regression coverage for the Claude Code-native pack handoff."""
from __future__ import annotations

import re
from pathlib import Path
from uuid import uuid4

import pytest

from hydra_core import artifact_store, host_bridge
from hydra_core.artifact_store import ArtifactStoreError
from hydra_core.native_packs import NATIVE_PACKS, NativePack, native_pack_root
from hydra_core.squad_loader import discover_squads


ROOT = Path(__file__).resolve().parents[1]

_FRONTMATTER_NAME = re.compile(r"^name:\s*(.+?)\s*$", re.MULTILINE)


def _frontmatter_name(path: Path) -> str | None:
    """Return the ``name:`` value from *path*'s YAML frontmatter block."""
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    block = text[3:end] if end != -1 else text[3:]
    match = _FRONTMATTER_NAME.search(block)
    return match.group(1).strip().strip("'\"") if match else None


def _agent_file(repo_root: Path, pack: NativePack) -> Path | None:
    """First existing agent markdown file for *pack*'s lead agent, if any."""
    candidates = (
        repo_root / "plugins" / pack.plugin / "agents" / f"{pack.lead_agent}.md",
        repo_root / ".claude" / "agents" / f"{pack.lead_agent}.md",
    )
    return next((c for c in candidates if c.is_file()), None)


@pytest.mark.parametrize("slug", sorted(NATIVE_PACKS))
def test_native_pack_lead_agent_exists_in_sibling_pack(slug: str) -> None:
    """Every NATIVE_PACKS lead_agent must name a real agent in its pack repo.

    Regression guard for E2-29: garland declared ``brand-strategist`` while
    RLM-Creative's crew lead is ``calliope``, so attended dispatch emitted an
    ``agent_type`` no host could spawn. Skips when the sibling checkout is
    absent so the suite still runs on a partial workspace.
    """
    pack = NATIVE_PACKS[slug]
    try:
        repo_root = native_pack_root(slug)
    except ValueError as exc:  # unregistered / missing / not a git checkout
        pytest.skip(f"sibling checkout for {slug!r} unavailable: {exc}")

    agents_dir = repo_root / "plugins" / pack.plugin / "agents"
    if not agents_dir.is_dir() and not (repo_root / ".claude" / "agents").is_dir():
        pytest.skip(f"{slug!r} pack checkout has no agents directory")

    agent_file = _agent_file(repo_root, pack)
    assert agent_file is not None, (
        f"squad {slug!r} declares lead_agent {pack.qualified_lead_agent!r} "
        f"but no agent markdown exists under {agents_dir}"
    )
    assert _frontmatter_name(agent_file) == pack.lead_agent, (
        f"{agent_file} frontmatter name does not match lead_agent "
        f"{pack.lead_agent!r}"
    )


class _NoopDispatcher:
    pass


def test_native_squad_result_preserves_emitted_envelopes(tmp_path: Path) -> None:
    started = host_bridge.begin_squad_stage(
        workflow_id=str(uuid4()), task_id="task-1",
        squad_slug="garland", entrypoint="claude-native", lead_agent="rlm-creative:calliope",
        pack_cwd=str(tmp_path), request_text="make a brief", project_root=tmp_path,
    )
    result = host_bridge.submit_host_result(
        _NoopDispatcher(), cursor_file=started["cursor_path"], call_key="squad-task-1-0",
        result={"text": "# Creative brief", "emitted_envelopes": [{"kind": "DEV_TASK"}]},
    )

    assert result["status"] == "complete"
    assert result["artifact_text"] == "# Creative brief"
    assert result["emitted_envelopes"] == [{"kind": "DEV_TASK"}]


def test_native_artifact_store_is_confined_to_declared_output_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setattr(artifact_store, "native_pack_root", lambda _slug: tmp_path)

    ref = artifact_store.write_native_artifact("executive", "attended/decision.md", "# Decision")

    assert (tmp_path / "output/attended/decision.md").read_text(encoding="utf-8") == "# Decision"
    assert ref.key == "executive-suite:output:attended/decision.md"
    with pytest.raises(ArtifactStoreError):
        artifact_store.write_native_artifact("executive", "../../escape.md", "no")
    with pytest.raises(ArtifactStoreError):
        artifact_store.write_native_artifact("executive", "attended/asset.png", "no")


def test_active_source_packs_are_host_attended_native_plugins() -> None:
    packs = discover_squads(ROOT)
    expected = {
        "executive", "garland", "legal-compliance", "rlm-gaming",
        "marketing-strategy", "marketing-creative", "marketing-research",
        "marketing-production", "marketing-ops",
    }
    assert {slug for slug, pack in packs.items() if pack.entrypoint == "claude-native"} == expected
