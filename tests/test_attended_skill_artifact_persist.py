"""E2-35: attended claude-skill squad artifacts are always persisted.

A squad with no ``NATIVE_PACKS`` entry (customer-support via the xenia shim)
used to come back ``complete`` with ``artifact_persist_error`` and no
``artifact_ref``, so its output never reached memory or synthesis. These tests
pin the generic attended store, the best-effort shim leg, and the
``complete_unpersisted`` downgrade when every persist path fails.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from hydra_core import artifact_store, host_bridge


class _StubDispatcher:
    """Records `<prefix>.output.write` calls; optionally fails them."""

    def __init__(self, *, fail: bool = False, relative: str | None = None) -> None:
        self.fail = fail
        self.relative = relative
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def call_mcp(self, server: str, tool: str, args: dict[str, Any],
                 *, squad_id: str | None = None) -> dict[str, Any]:
        self.calls.append((server, tool, args))
        if self.fail:
            return {"status": "failed", "error": "xenia shim unreachable"}
        out: dict[str, Any] = {"status": "done"}
        if self.relative:
            out["relative"] = self.relative
        return out


def _begin(tmp_path: Path, wf: str, *, slug: str = "customer-support") -> dict[str, Any]:
    return host_bridge.begin_squad_stage(
        workflow_id=wf, task_id="task-cs1", squad_slug=slug,
        entrypoint="claude-skill", lead_agent="xenia:support-lead",
        pack_cwd=str(tmp_path / "pack"), request_text="triage the ticket",
        project_root=tmp_path,
    )


def _trace_kinds(tmp_path: Path, wf: str) -> list[str]:
    """Trace lands under the cursor's project_path (the pack cwd for squads)."""
    path = tmp_path / "pack" / ".hydra" / wf / "trace.jsonl"
    if not path.exists():
        return []
    return [json.loads(line)["kind"] for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_skill_squad_artifact_persisted_generically_and_via_shim(tmp_path: Path) -> None:
    wf = str(uuid4())
    started = _begin(tmp_path, wf)
    disp = _StubDispatcher(relative="triage/attended.md")

    res = host_bridge.submit_host_result(
        disp, cursor_file=started["cursor_path"], call_key="squad-task-cs1-0",
        result={"text": "# Ticket triage"},
    )

    assert res["status"] == "complete"
    assert res["artifact_persisted_via"] == "generic+shim"
    assert res["artifact_ref"]["tier"] == "episodic"
    assert res["artifact_ref"]["key"] == f"attended:artifacts:{wf}/task-cs1.md"
    assert "artifact_persist_error" not in res

    written = tmp_path / ".hydra" / wf / "attended" / "artifacts" / "task-cs1.md"
    assert written.read_text(encoding="utf-8") == "# Ticket triage"

    # The shim leg went to xenia's output writer with the squad's RBAC id.
    assert [(s, t) for s, t, _ in disp.calls] == [("xenia", "xenia.output.write")]
    assert disp.calls[0][2]["content"] == "# Ticket triage"
    assert "attended.skill_artifact_persisted" in _trace_kinds(tmp_path, wf)


def test_shim_failure_still_persists_generically(tmp_path: Path) -> None:
    wf = str(uuid4())
    started = _begin(tmp_path, wf)
    disp = _StubDispatcher(fail=True)

    res = host_bridge.submit_host_result(
        disp, cursor_file=started["cursor_path"], call_key="squad-task-cs1-0",
        result={"text": "# Ticket triage"},
    )

    assert res["status"] == "complete"
    assert res["artifact_persisted_via"] == "generic"
    assert res["artifact_ref"]["key"] == f"attended:artifacts:{wf}/task-cs1.md"
    assert "xenia shim unreachable" in res["artifact_persist_warning"]
    assert (tmp_path / ".hydra" / wf / "attended" / "artifacts" / "task-cs1.md").exists()


def test_all_persist_paths_failing_yields_complete_unpersisted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    wf = str(uuid4())
    started = _begin(tmp_path, wf)

    def _boom(*_a: Any, **_k: Any) -> Any:
        raise OSError("disk is read-only")

    monkeypatch.setattr(artifact_store, "write_attended_artifact", _boom)
    disp = _StubDispatcher(fail=True)

    res = host_bridge.submit_host_result(
        disp, cursor_file=started["cursor_path"], call_key="squad-task-cs1-0",
        result={"text": "# Ticket triage"},
    )

    assert res["status"] == "complete_unpersisted"
    assert res["final_status"] == "complete_unpersisted"
    assert "artifact_ref" not in res
    assert "disk is read-only" in res["artifact_persist_error"]
    assert "xenia shim unreachable" in res["artifact_persist_error"]
    assert "attended.skill_artifact_persist_failed" in _trace_kinds(tmp_path, wf)


def test_squad_without_shim_still_persists_generically(tmp_path: Path) -> None:
    """A claude-skill squad missing a _SKILL_PACK_SHIMS entry must not lose work."""
    wf = str(uuid4())
    started = _begin(tmp_path, wf, slug="healthcare")
    disp = _StubDispatcher()

    res = host_bridge.submit_host_result(
        disp, cursor_file=started["cursor_path"], call_key="squad-task-cs1-0",
        result={"text": "# Notes"},
    )

    assert res["status"] == "complete"
    assert res["artifact_persisted_via"] == "generic"
    assert disp.calls == []


def test_attended_store_rejects_escape_and_binary(tmp_path: Path) -> None:
    ref = artifact_store.write_attended_artifact(tmp_path, "wf1", "a/b.md", "hi")
    assert ref.key == "attended:artifacts:wf1/a/b.md"
    with pytest.raises(artifact_store.ArtifactStoreError):
        artifact_store.write_attended_artifact(tmp_path, "wf1", "../../escape.md", "no")
    with pytest.raises(artifact_store.ArtifactStoreError):
        artifact_store.write_attended_artifact(tmp_path, "wf1", "asset.png", "no")
    with pytest.raises(artifact_store.ArtifactStoreError):
        artifact_store.write_attended_artifact(tmp_path, "../evil", "a.md", "no")
