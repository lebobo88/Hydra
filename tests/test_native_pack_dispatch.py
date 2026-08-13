"""Regression coverage for the Claude Code-native pack handoff."""
from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from hydra_core import artifact_store, host_bridge
from hydra_core.artifact_store import ArtifactStoreError
from hydra_core.squad_loader import discover_squads


ROOT = Path(__file__).resolve().parents[1]


class _NoopDispatcher:
    pass


def test_native_squad_result_preserves_emitted_envelopes(tmp_path: Path) -> None:
    started = host_bridge.begin_squad_stage(
        workflow_id=str(uuid4()), task_id="task-1",
        squad_slug="garland", entrypoint="claude-native", lead_agent="rlm-creative:brand-strategist",
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
