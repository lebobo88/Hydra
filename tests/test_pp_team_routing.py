"""Unit tests for pair-programmer game-team routing in ``_via_mcp`` (RC2).

The engineering squad historically dispatched every task in ``mode=single`` (one
generic ``engineer`` agent) because the inbound envelope had no way to pick a pp
team. These tests cover ``_resolve_pp_team`` + its integration:

  - an explicit ``pp_team`` on the inbound envelope wins and forces team-mode
  - work originating from ``rlm-gaming`` auto-defaults to ``game-feature-team``
  - ``constraints.industries`` intersecting the game set auto-defaults too
  - explicit ``pp_team`` beats the industry auto-default (precedence)
  - a plain engineering task (no game signal) is unchanged: single-mode, no team
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from hydra_core.schemas import Constraints, CSuiteDecisionPacket, DevTask
from hydra_core.squad_loader import discover_squads
from hydra_core.squad_node import _resolve_pp_team, _via_mcp
from hydra_core.state import HydraState

HYDRA_ROOT = Path(__file__).resolve().parents[1]


class _RecordingDispatcher:
    """Records call_mcp args; never drives the loop (drive_pp_loop unset)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []

    def call_mcp(self, server: str, tool: str, args: dict[str, Any],
                 *, squad_id: str | None = None) -> dict[str, Any]:
        self.calls.append((server, tool, args))
        if tool == "start_run":
            return {"status": "done", "result": {"run_id": "run_X"}}
        return {"status": "done", "result": {}}

    def start_run_args(self) -> dict:
        return next(a for (s, t, a) in self.calls if t == "start_run")

    def emit_claude_prompt(self, *_a, **_k): raise NotImplementedError  # pragma: no cover
    def invoke_claude_skill(self, *_a, **_k): raise NotImplementedError  # pragma: no cover
    def spawn_subprocess(self, *_a, **_k): raise NotImplementedError  # pragma: no cover


def _eng_pack():
    return discover_squads(HYDRA_ROOT)["engineering"]


def _run(monkeypatch, inbound) -> dict:
    monkeypatch.setattr("hydra_core.squad_node.harvest_pp_run_artifacts",
                        lambda **_k: None)
    disp = _RecordingDispatcher()
    state = HydraState(root_goal="t")
    _via_mcp(state, _eng_pack(), inbound, disp)
    return disp.start_run_args()


# --------------------------------------------------------------------------- #
# _resolve_pp_team (pure)
# --------------------------------------------------------------------------- #

def test_resolve_explicit_pp_team_wins() -> None:
    env = DevTask(workflow_id=HydraState().workflow_id, origin_squad="rlm-gaming",
                  owner="backend", repo="candc", branch="wf",
                  instructions="x", pp_team="game-netcode-team",
                  constraints=Constraints(industries=["games"]))
    team, reason = _resolve_pp_team(env)
    assert team == "game-netcode-team"
    assert "explicit" in reason


def test_resolve_origin_rlm_gaming_autodefaults() -> None:
    env = DevTask(workflow_id=HydraState().workflow_id, origin_squad="rlm-gaming",
                  owner="backend", repo="candc", branch="wf", instructions="x")
    team, reason = _resolve_pp_team(env)
    assert team == "game-feature-team"
    assert "auto-default" in reason


def test_resolve_game_industries_autodefaults() -> None:
    env = CSuiteDecisionPacket(workflow_id=HydraState().workflow_id,
                               origin_squad="hydra", origin="BOARDROOM",
                               objective="ship a level",
                               constraints=Constraints(industries=["AAA-Games"]))
    team, _ = _resolve_pp_team(env)
    assert team == "game-feature-team"


def test_resolve_no_game_signal_is_none() -> None:
    env = CSuiteDecisionPacket(workflow_id=HydraState().workflow_id,
                               origin_squad="hydra", origin="BOARDROOM",
                               objective="fix the billing API")
    team, _ = _resolve_pp_team(env)
    assert team is None


# --------------------------------------------------------------------------- #
# _via_mcp integration — inspect the start_run payload
# --------------------------------------------------------------------------- #

def test_via_mcp_explicit_pp_team(monkeypatch) -> None:
    env = DevTask(workflow_id=HydraState().workflow_id, origin_squad="rlm-gaming",
                  owner="backend", repo="candc", branch="wf",
                  instructions="build the fog-of-war system",
                  pp_team="game-netcode-team")
    args = _run(monkeypatch, env)
    assert args["mode"] == "team"
    assert args["team"] == "game-netcode-team"


def test_via_mcp_rlm_gaming_autodefaults_game_team(monkeypatch) -> None:
    env = DevTask(workflow_id=HydraState().workflow_id, origin_squad="rlm-gaming",
                  owner="backend", repo="candc", branch="wf",
                  instructions="implement the tech tree")
    args = _run(monkeypatch, env)
    assert args["mode"] == "team"
    assert args["team"] == "game-feature-team"


def test_via_mcp_plain_engineering_unchanged(monkeypatch) -> None:
    # A normal hydra-origin packet with no game signal: single mode, no team key.
    env = CSuiteDecisionPacket(workflow_id=HydraState().workflow_id,
                               origin_squad="hydra", origin="BOARDROOM",
                               objective="add idempotency keys to the payments API")
    args = _run(monkeypatch, env)
    assert args["mode"] == "single"
    assert "team" not in args
