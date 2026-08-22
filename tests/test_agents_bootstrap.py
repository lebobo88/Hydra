"""Unit tests for AGENTS.md and CLAUDE.md bootstrap in ``_via_mcp``."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from hydra_core.schemas import DevTask
from hydra_core.squad_loader import discover_squads
from hydra_core.squad_node import _via_mcp
from hydra_core.state import HydraState

HYDRA_ROOT = Path(__file__).resolve().parents[1]


class _RecordingDispatcher:
    """Records MCP calls and returns canned responses."""

    def __init__(
        self,
        responses: dict[tuple[str, str], dict[str, Any]] | None = None,
        *,
        raise_on: set[tuple[str, str]] | None = None,
    ) -> None:
        self.responses = responses or {
            ("pp_harness", "start_run"): {
                "status": "done",
                "result": {"run_id": "run_BOOT"},
            },
        }
        self.raise_on = raise_on or set()
        self.calls: list[tuple[str, str, dict[str, Any], str | None]] = []

    def call_mcp(
        self,
        server: str,
        tool: str,
        args: dict[str, Any],
        *,
        squad_id: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append((server, tool, args, squad_id))
        if (server, tool) in self.raise_on:
            raise RuntimeError("boom on ensure_agents_md")
        return self.responses.get((server, tool), {"status": "done", "result": {}})

    def tool_seq(self) -> list[str]:
        return [tool for (_server, tool, _args, _squad_id) in self.calls]

    def emit_claude_prompt(self, *_a, **_k): raise NotImplementedError  # pragma: no cover
    def invoke_claude_skill(self, *_a, **_k): raise NotImplementedError  # pragma: no cover
    def spawn_subprocess(self, *_a, **_k): raise NotImplementedError  # pragma: no cover


def _eng_pack():
    return discover_squads(HYDRA_ROOT)["engineering"]


def _inbound(state: HydraState) -> DevTask:
    return DevTask(
        workflow_id=state.workflow_id,
        origin_squad="hydra",
        owner="backend",
        repo="hydra",
        branch="wf",
        instructions="author bootstrap coverage",
        # WS1-E: engineering dispatch requires an explicit, resolved target
        # repo -- this file exercises the AGENTS.md bootstrap side effect,
        # not repo-targeting, so point at "hydra" (this checkout) rather
        # than relying on the removed cwd fallback.
        target_repo_id="hydra",
    )


def _patch_side_effects(monkeypatch) -> None:
    monkeypatch.setattr(
        "hydra_core.squad_node.harvest_pp_run_artifacts",
        lambda **_k: None,
    )
    monkeypatch.setattr(
        "hydra_core.squad_node._maybe_write_claude_shim",
        lambda _project_path: None,
    )
    # These tests dispatch _via_mcp against the real HYDRA_ROOT checkout with
    # run_id + status="done" -- stub the target-repo scaffolding helpers so
    # they don't write .gitignore / test-runner exclude entries into it. They
    # are exercised hermetically against tmp_path in
    # tests/test_target_repo_scaffolding.py.
    monkeypatch.setattr(
        "hydra_core.squad_node.ensure_target_repo_ignores",
        lambda _project_path: None,
    )
    monkeypatch.setattr(
        "hydra_core.squad_node.ensure_target_repo_test_excludes",
        lambda _project_path: None,
    )


def test_ensure_agents_md_called_after_start_run(monkeypatch) -> None:
    _patch_side_effects(monkeypatch)
    state = HydraState(root_goal="t")
    disp = _RecordingDispatcher()

    result = _via_mcp(state, _eng_pack(), _inbound(state), disp)

    assert result.status == "running"
    start_call = next(call for call in disp.calls if call[1] == "start_run")
    ensure_call = next(call for call in disp.calls if call[1] == "ensure_agents_md")
    assert ensure_call[0] == "pp_harness"
    assert ensure_call[2]["project_path"] == start_call[2]["project_path"]
    assert ensure_call[3] == "engineering"
    assert disp.tool_seq()[0:2] == ["start_run", "ensure_agents_md"]


def test_ensure_agents_md_not_called_without_run_id(monkeypatch) -> None:
    _patch_side_effects(monkeypatch)
    cases = [
        {
            "status": "done",
            "result": {},
        },
        {
            "status": "failed",
            "result": {"run_id": "run_BOOT"},
        },
    ]

    for response in cases:
        state = HydraState(root_goal="t")
        disp = _RecordingDispatcher({("pp_harness", "start_run"): response})

        result = _via_mcp(state, _eng_pack(), _inbound(state), disp)

        assert result.status == response["status"]
        assert "ensure_agents_md" not in disp.tool_seq()


def test_ensure_agents_md_failsoft(monkeypatch) -> None:
    _patch_side_effects(monkeypatch)
    state = HydraState(root_goal="t")
    disp = _RecordingDispatcher(raise_on={("pp_harness", "ensure_agents_md")})

    result = _via_mcp(state, _eng_pack(), _inbound(state), disp)

    assert result.status == "running"
    assert disp.tool_seq()[0:2] == ["start_run", "ensure_agents_md"]
    assert any(entry.get("run_id") == "run_BOOT" for entry in state.open_pp_runs)
