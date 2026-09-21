"""Reject non-finite input at every boundary that accepts an
operator-supplied budget number.

Ground truth: the checkpoint-deserialization choke point
(`state.make_checkpoint_serde`) only ever inspects a value coming BACK OUT
of an already-persisted checkpoint. `hydra run --budget nan` (or `inf`) used
to be accepted because argparse's own `type=float` parses both, and the
value landed in a FRESH `HydraState`/checkpoint mutation the choke point
never sees on write -- poisoning it for every future reader. This module
proves the shared validator (`strict_json.reject_non_finite` /
`finite_float_arg`) is applied identically at every entry: the CLI argparse
`type=` for every command that accepts `--budget` (and the audited
`--older-than-hours` / `--max-age-hours` float flags), the `hydra budget
--set` / `hydra resume --action modify-budget` mutation paths, and the MCP
`hydra.workflow.launch` / `.plan` / `.budget` tool handlers.
"""
from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import json
from pathlib import Path
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from hydra_core import cli
from hydra_core.state import HydraState, TaskState
from hydra_core.strict_json import finite_float_arg, reject_non_finite

REPO_ROOT = Path(__file__).resolve().parents[1]


class _PoisonStubDispatcher:
    live_execution = False

    def call_mcp(self, server, tool, args, *, squad_id=None):
        return {"status": "done", "result": {"ok": True}}

    def spawn_subprocess(self, *_a, **_kw):
        return {"status": "done"}

    def emit_claude_prompt(self, prompt, agent=None):
        return {"status": "host_pickup_required"}

    def invoke_claude_skill(self, skill, args):
        return {"status": "host_pickup_required"}

    def set_squad_packs(self, packs):
        pass


@pytest.fixture()
def poisoned_checkpoint_hermetic(tmp_path, monkeypatch):
    monkeypatch.setenv("HYDRA_CHECKPOINT_DB", str(tmp_path / "checkpoints.db"))
    monkeypatch.setenv("HYDRA_EIGHTS_SPOOL", str(tmp_path / "spool"))
    monkeypatch.setenv("HYDRA_EIGHTS_DEAD_LETTER", str(tmp_path / "dead"))
    monkeypatch.setattr(cli, "_attended_live_dispatcher",
                        lambda *_a, **_kw: _PoisonStubDispatcher())
    return tmp_path


def _seed_poisoned_workflow():
    from hydra_core.supervisor import build_supervisor, _PurePythonRunner

    wf = uuid4()
    tasks = [TaskState(owner_squad="engineering", description="ship the change")]
    state = HydraState(
        workflow_id=wf,
        root_goal="finalize poison regression",
        selected_squads=["engineering"],
        phase="synthesis",
        tasks=tasks,
    )
    sup = build_supervisor(project_root=REPO_ROOT, dispatcher=_PoisonStubDispatcher())
    assert not isinstance(sup, _PurePythonRunner), "langgraph required for this test"
    config = {"configurable": {"thread_id": str(wf)}}
    sup.update_state(config, state.model_dump(mode="json"), as_node="judge_per_squad")
    # Poison AFTER the initial (clean) write so the checkpoint the choke
    # point later refuses to read is the poisoned one.
    sup.update_state(config, {"verdicts": [
        {"outcome": "pass", "target_envelope_id": "x",
         "score_json": {"quality": float("nan")}},
    ]}, as_node="judge_per_squad")
    return str(wf)


def test_finalize_names_the_sanitize_recovery_flag(poisoned_checkpoint_hermetic):
    wf = _seed_poisoned_workflow()
    args = argparse.Namespace(project=str(REPO_ROOT), workflow_id=wf, verbose=False)

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli._cmd_finalize(args)

    payload = json.loads(buf.getvalue())
    assert rc == 0
    assert payload["ok"] is False
    assert payload["status"] == "unjudgeable"
    assert "sanitize-non-finite" in payload["detail"]
    assert wf in payload["detail"]


def _load_hydra_control_server():
    spec = importlib.util.spec_from_file_location(
        "hydra_control_server_under_test_reject_non_finite",
        REPO_ROOT / "mcp_servers" / "hydra_control" / "server.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# The shared validator itself.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_reject_non_finite_names_flag_and_value(bad):
    with pytest.raises(ValueError) as exc_info:
        reject_non_finite(bad, flag="--budget")
    msg = str(exc_info.value)
    assert "--budget" in msg
    assert repr(bad) in msg


def test_reject_non_finite_passes_through_ordinary_value():
    assert reject_non_finite(250.0, flag="--budget") == 250.0


@pytest.mark.parametrize("raw", ["nan", "inf", "-inf", "Infinity", "NaN"])
def test_finite_float_arg_rejects_every_non_finite_spelling(raw):
    convert = finite_float_arg("--budget")
    with pytest.raises(argparse.ArgumentTypeError) as exc_info:
        convert(raw)
    assert repr(raw) in str(exc_info.value)


def test_finite_float_arg_accepts_ordinary_value():
    convert = finite_float_arg("--budget")
    assert convert("42.5") == 42.5


# ---------------------------------------------------------------------------
# CLI argparse entries: --budget on `run`/`plan`, plus the audited
# --older-than-hours / --max-age-hours float flags.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("argv", [
    ["run", "some goal", "--budget", "nan"],
    ["run", "some goal", "--budget", "inf"],
    ["plan", "some goal", "--budget", "nan"],
    ["plan", "some goal", "--budget", "inf"],
    ["reap", "--older-than-hours", "nan"],
    ["eights-drain", "--max-age-hours", "inf"],
])
def test_cli_argparse_rejects_non_finite_budget(argv, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    buf = io.StringIO()
    with pytest.raises(SystemExit) as exc_info, contextlib.redirect_stderr(buf):
        cli.main(argv)
    assert exc_info.value.code == 2
    err = buf.getvalue()
    flag = argv[argv.index([a for a in argv if a.startswith("--")][0])]
    assert flag in err
    assert ("nan" in err or "inf" in err)


def test_cli_run_ordinary_budget_still_parses(monkeypatch, tmp_path):
    """Control: an ordinary numeric --budget must not be rejected by the
    new argparse `type=` (only NaN/Infinity are refused)."""
    ap = argparse.ArgumentParser()
    ap.add_argument("--budget", type=finite_float_arg("--budget"))
    ns = ap.parse_args(["--budget", "100"])
    assert ns.budget == 100.0


# ---------------------------------------------------------------------------
# `hydra budget --set` mutation path (via `_cmd_budget`).
# ---------------------------------------------------------------------------

def _make_mock_sup_for_budget(workflow_id: str):
    mock_sup = MagicMock()
    mock_sup.get_state.return_value = MagicMock(
        values={
            "workflow_id": workflow_id,
            "phase": "executing",
            "budget": {
                "budget_usd": 50.0, "spent_usd": 0.0,
                "token_limit": 200000, "spent_tokens": 0,
            },
        }
    )
    return mock_sup


@pytest.mark.parametrize("bad", ["nan", "inf", "-inf"])
def test_cmd_budget_set_rejects_non_finite(bad, tmp_path):
    wf_id = str(uuid4())
    mock_sup = _make_mock_sup_for_budget(wf_id)

    with patch("hydra_core.supervisor.build_supervisor", return_value=mock_sup), \
         patch("hydra_core.supervisor._PurePythonRunner", type(None)):
        args = argparse.Namespace(
            project=str(tmp_path), workflow_id=wf_id, set_usd=bad, operator=None,
        )
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli._cmd_budget(args)

    assert rc == 1
    mock_sup.update_state.assert_not_called()


def test_cmd_budget_set_ordinary_value_still_works(tmp_path):
    wf_id = str(uuid4())
    mock_sup = _make_mock_sup_for_budget(wf_id)
    mock_sup.update_state.return_value = None

    with patch("hydra_core.supervisor.build_supervisor", return_value=mock_sup), \
         patch("hydra_core.supervisor._PurePythonRunner", type(None)):
        args = argparse.Namespace(
            project=str(tmp_path), workflow_id=wf_id, set_usd="250", operator=None,
        )
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli._cmd_budget(args)

    assert rc == 0
    assert mock_sup.update_state.called


# ---------------------------------------------------------------------------
# `hydra resume --action modify-budget` mutation path.
# ---------------------------------------------------------------------------

def _make_mock_sup_for_resume(workflow_id: str, pending_hitl: dict):
    mock_sup = MagicMock()
    mock_sup.get_state.return_value = MagicMock(
        values={
            "pending_hitl": pending_hitl,
            "phase": "approval",
            "budget": {
                "budget_usd": 50.0, "spent_usd": 0.0,
                "token_limit": 200000, "spent_tokens": 0,
            },
        }
    )
    mock_sup.update_state.return_value = None
    mock_sup.invoke.return_value = {"phase": "done"}
    return mock_sup


@pytest.mark.parametrize("bad", ["nan", "inf"])
def test_cmd_resume_modify_budget_rejects_non_finite(monkeypatch, tmp_path, bad):
    monkeypatch.delenv("HYDRA_OPERATOR_KEY", raising=False)
    wf_id = "wf-resume-modify-budget-nonfinite"
    pending = {
        "workflow_id": wf_id, "gate_node": "approval", "reason": "high_risk",
        "options": ["approve", "reject", "modify-budget"],
    }
    mock_sup = _make_mock_sup_for_resume(wf_id, pending)

    with patch("hydra_core.supervisor.build_supervisor", return_value=mock_sup), \
         patch("hydra_core.supervisor._PurePythonRunner", type(None)):
        args = argparse.Namespace(project=str(tmp_path), live=False, verbose=False)
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            rc = cli._cmd_resume_locked(args, tmp_path, wf_id, "modify-budget", bad)

    assert rc == 1
    err = json.loads(buf.getvalue())
    assert "finite number" in err["error"]
    mock_sup.invoke.assert_not_called()
    # No patch containing a poisoned budget was ever written to the checkpoint.
    for call in mock_sup.update_state.call_args_list:
        call_patch = call[0][1] if len(call[0]) > 1 else {}
        assert "budget" not in call_patch


def test_cmd_resume_modify_budget_ordinary_value_still_works(monkeypatch, tmp_path):
    monkeypatch.setenv("HYDRA_OPERATOR_KEY", "0" * 64)
    wf_id = "wf-resume-modify-budget-ordinary"
    pending = {
        "workflow_id": wf_id, "gate_node": "approval", "reason": "high_risk",
        "options": ["approve", "reject", "modify-budget"],
    }
    mock_sup = _make_mock_sup_for_resume(wf_id, pending)

    with patch("hydra_core.supervisor.build_supervisor", return_value=mock_sup), \
         patch("hydra_core.supervisor._PurePythonRunner", type(None)):
        args = argparse.Namespace(project=str(tmp_path), live=False, verbose=False)
        rc = cli._cmd_resume_locked(args, tmp_path, wf_id, "modify-budget", "99")

    assert rc == 0
    patch_dict = mock_sup.update_state.call_args_list[0][0][1]
    assert patch_dict["budget"]["budget_usd"] == 99.0


# ---------------------------------------------------------------------------
# MCP tool handlers: `hydra.workflow.launch` / `.plan` / `.budget`.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("tool_name", ["hydra.workflow.launch", "hydra.workflow.plan"])
@pytest.mark.parametrize("bad", ["nan", "inf", "-inf"])
def test_mcp_launch_and_plan_reject_non_finite_budget(tool_name, bad):
    server = _load_hydra_control_server()
    handlers = server._tool_handlers()
    handler = handlers[tool_name]

    result = handler({"goal": "ship it", "budget": bad})

    assert result["ok"] is False
    assert "finite number" in result["error"]
    assert "budget" in result["error"]


def test_mcp_workflow_budget_rejects_non_finite_set_budget():
    server = _load_hydra_control_server()
    handlers = server._tool_handlers()
    handler = handlers["hydra.workflow.budget"]

    result = handler({"set_budget": "nan"})

    assert result["ok"] is False
    assert "finite number" in result["error"]
    assert "set_budget" in result["error"]


# ---------------------------------------------------------------------------
# The sanctioned raw-checkpoint-read bypass is reachable ONLY through the
# `--sanitize-non-finite` recovery path.
# ---------------------------------------------------------------------------

def test_raw_checkpoint_reader_is_not_a_module_level_name():
    """`_raw_checkpoint_channel_values` (the one sanctioned bypass of the
    scanning choke point, `state.make_checkpoint_serde`) used to be a
    module-level function in `hydra_core.cli` -- importable, and callable,
    from anywhere. It is now a closure defined ONLY inside `_cmd_replay`'s
    `--sanitize-non-finite` branch: there is no module attribute for a
    future caller to import at all, accidentally or otherwise."""
    assert not hasattr(cli, "_raw_checkpoint_channel_values")
    # No OTHER module-level name in `hydra_core.cli` bypasses the choke
    # point either -- the only remaining reference is inside `_cmd_replay`'s
    # source (a local closure), not a public/importable symbol.
    assert not any(
        "raw_checkpoint" in name for name in vars(cli)
        if not name.startswith("_cmd_replay")
    )
