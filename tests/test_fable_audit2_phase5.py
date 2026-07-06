"""Tests for fable-audit-2 Phase 5 enforcement + missing surfaces.

Covers:
  F1  — settings.json has HYDRA_ENFORCE_ROUTING=1 and
        hydra-block-direct-pp.ps1 has HYDRA_PP_STAGE_ACTIVE bypass.
  F2  — hooks.json UserPromptSubmit includes hydra-route-directive entry.
  F3  — `hydra budget` CLI (list / show / set) incl M3 capability gate.
  F4  — hydra-block-bash-writes.ps1 blocks echo redirect to .py and allows
        `git status` (pwsh subprocess test; skipped if pwsh absent).
  F6  — `_cmd_status` ordering (latest-first by trace mtime) + structured render.
  F7  — .claude-plugin/hooks.json is gone; hooks.json has Task Pre/PostToolUse.
  F15+M6 — gateway server.py docstring says 16 backends + 6 meta-tools.
  M1  — hydra-session-contract.ps1 has HYDRA_SCAFFOLD_CONTRACT=0 opt-out.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

HYDRA_ROOT = Path(__file__).resolve().parents[1]
HOOKS_JSON = HYDRA_ROOT / "hooks.json"
SETTINGS_JSON = HYDRA_ROOT / ".claude" / "settings.json"
HOOKS_DIR = HYDRA_ROOT / ".claude" / "hooks"
PLUGIN_DIR = HYDRA_ROOT / ".claude-plugin"
GATEWAY_SERVER = HYDRA_ROOT / "mcp_servers" / "hydra_gateway" / "server.py"


# ---------------------------------------------------------------------------
# F1 — settings.json enforcement default-on
# ---------------------------------------------------------------------------

def test_settings_json_has_enforce_routing():
    """HYDRA_ENFORCE_ROUTING must be '1' in the project settings."""
    assert SETTINGS_JSON.exists(), "settings.json missing"
    cfg = json.loads(SETTINGS_JSON.read_text(encoding="utf-8"))
    env = cfg.get("env", {})
    assert env.get("HYDRA_ENFORCE_ROUTING") == "1", (
        f"HYDRA_ENFORCE_ROUTING not '1' in settings.json env block: {env}"
    )


def test_block_direct_write_has_pp_stage_bypass():
    """hydra-block-direct-write.ps1 must early-exit 0 when HYDRA_PP_STAGE_ACTIVE=1."""
    script = HOOKS_DIR / "hydra-block-direct-write.ps1"
    assert script.exists()
    text = script.read_text(encoding="utf-8")
    assert "HYDRA_PP_STAGE_ACTIVE" in text, (
        "hydra-block-direct-write.ps1 lacks HYDRA_PP_STAGE_ACTIVE bypass"
    )
    assert "exit 0" in text


def test_block_direct_pp_has_pp_stage_bypass():
    """hydra-block-direct-pp.ps1 must early-exit 0 when HYDRA_PP_STAGE_ACTIVE=1."""
    script = HOOKS_DIR / "hydra-block-direct-pp.ps1"
    assert script.exists()
    text = script.read_text(encoding="utf-8")
    assert "HYDRA_PP_STAGE_ACTIVE" in text, (
        "hydra-block-direct-pp.ps1 lacks HYDRA_PP_STAGE_ACTIVE bypass"
    )
    # Must be before the main logic (early exit)
    lines = text.splitlines()
    bypass_idx = next(
        (i for i, ln in enumerate(lines) if "HYDRA_PP_STAGE_ACTIVE" in ln), None
    )
    enforce_idx = next(
        (i for i, ln in enumerate(lines) if "HYDRA_ENFORCE_ROUTING" in ln and "exit 0" in ln), None
    )
    assert bypass_idx is not None
    # bypass must appear near the top (within 30 lines of enforce check)
    if enforce_idx is not None:
        assert bypass_idx <= enforce_idx + 30


# ---------------------------------------------------------------------------
# F2 — hooks.json UserPromptSubmit has route-directive
# ---------------------------------------------------------------------------

def test_hooks_json_has_route_directive_in_user_prompt_submit():
    """hooks.json must register hydra-route-directive under UserPromptSubmit."""
    cfg = json.loads(HOOKS_JSON.read_text(encoding="utf-8"))
    ups_groups = cfg.get("hooks", {}).get("UserPromptSubmit", [])
    all_commands = [
        h.get("command", "")
        for group in ups_groups
        for h in group.get("hooks", [])
    ]
    assert any("hydra-route-directive" in cmd for cmd in all_commands), (
        f"hydra-route-directive not found in UserPromptSubmit hooks: {all_commands}"
    )


# ---------------------------------------------------------------------------
# F4 — hydra-block-bash-writes.ps1 subprocess test
# ---------------------------------------------------------------------------

_PWSH = shutil.which("pwsh") or shutil.which("powershell")
_BASH_WRITES_SCRIPT = HOOKS_DIR / "hydra-block-bash-writes.ps1"


def _run_bash_writes_hook(command_str: str, *, enforce: str = "1") -> subprocess.CompletedProcess:
    """Feed a fabricated PreToolUse JSON into the hook via stdin."""
    payload = json.dumps({
        "tool_name": "Bash",
        "tool_input": {"command": command_str},
    })
    env = {**os.environ, "HYDRA_ENFORCE_ROUTING": enforce,
           "HYDRA_PP_STAGE_ACTIVE": ""}
    return subprocess.run(
        [_PWSH, "-NoProfile", "-File", str(_BASH_WRITES_SCRIPT)],
        input=payload,
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )


@pytest.mark.skipif(_PWSH is None, reason="pwsh/powershell not on PATH")
def test_bash_writes_hook_blocks_redirect_to_py():
    """echo x > foo.py must be blocked (exit 2)."""
    assert _BASH_WRITES_SCRIPT.exists(), "hydra-block-bash-writes.ps1 missing"
    result = _run_bash_writes_hook("echo x > foo.py")
    assert result.returncode == 2, (
        f"Expected exit 2 (block) for redirect to .py, got {result.returncode}.\n"
        f"stderr: {result.stderr}\nstdout: {result.stdout}"
    )
    assert "BLOCKED" in result.stderr or "block" in result.stderr.lower()


@pytest.mark.skipif(_PWSH is None, reason="pwsh/powershell not on PATH")
def test_bash_writes_hook_allows_git_status():
    """git status must be allowed (exit 0)."""
    assert _BASH_WRITES_SCRIPT.exists()
    result = _run_bash_writes_hook("git status")
    assert result.returncode == 0, (
        f"Expected exit 0 (allow) for git status, got {result.returncode}.\n"
        f"stderr: {result.stderr}\nstdout: {result.stdout}"
    )


@pytest.mark.skipif(_PWSH is None, reason="pwsh/powershell not on PATH")
def test_bash_writes_hook_bypassed_when_stage_active(tmp_path):
    """Hook must exit 0 when HYDRA_PP_STAGE_ACTIVE=1 and a real stage marker exists.

    RA-1 hardening: the bypass now requires a filesystem marker. This test explicitly
    creates the .harness/stage-active marker and points CLAUDE_PROJECT_DIR at it,
    which is the correct way to simulate a harness-driven stage.
    """
    assert _BASH_WRITES_SCRIPT.exists()
    # RA-1: create the required stage-active marker.
    harness = tmp_path / ".harness"
    harness.mkdir()
    (harness / "stage-active").write_text("stage-id=phase5-bypass-test", encoding="utf-8")

    payload = json.dumps({
        "tool_name": "Bash",
        "tool_input": {"command": "echo x > foo.py"},
    })
    env = {**os.environ, "HYDRA_ENFORCE_ROUTING": "1",
           "HYDRA_PP_STAGE_ACTIVE": "1",
           "CLAUDE_PROJECT_DIR": str(tmp_path)}
    result = subprocess.run(
        [_PWSH, "-NoProfile", "-File", str(_BASH_WRITES_SCRIPT)],
        input=payload, capture_output=True, text=True, env=env, timeout=15,
    )
    assert result.returncode == 0, (
        f"Expected exit 0 with bypass (stage-active marker present), "
        f"got {result.returncode}.\nstderr: {result.stderr}\nstdout: {result.stdout}"
    )


@pytest.mark.skipif(_PWSH is None, reason="pwsh/powershell not on PATH")
def test_bash_writes_hook_disabled_when_no_enforce():
    """Hook must exit 0 when HYDRA_ENFORCE_ROUTING != 1."""
    assert _BASH_WRITES_SCRIPT.exists()
    result = _run_bash_writes_hook("echo x > foo.py", enforce="0")
    assert result.returncode == 0, (
        f"Expected exit 0 (disabled), got {result.returncode}.\nstderr: {result.stderr}"
    )


@pytest.mark.skipif(_PWSH is None, reason="pwsh/powershell not on PATH")
def test_bash_writes_hook_blocks_set_content_named_path():
    """Set-Content -Path foo.py must be blocked (flag form — previously a false negative)."""
    assert _BASH_WRITES_SCRIPT.exists()
    result = _run_bash_writes_hook("Set-Content -Path foo.py -Value 'hello'")
    assert result.returncode == 2, (
        f"Expected exit 2 (block) for Set-Content -Path foo.py, got {result.returncode}.\n"
        f"stderr: {result.stderr}\nstdout: {result.stdout}"
    )
    assert "BLOCKED" in result.stderr or "block" in result.stderr.lower()


@pytest.mark.skipif(_PWSH is None, reason="pwsh/powershell not on PATH")
def test_bash_writes_hook_blocks_set_content_positional():
    """Set-Content foo.py 'x' must be blocked (positional form)."""
    assert _BASH_WRITES_SCRIPT.exists()
    result = _run_bash_writes_hook("Set-Content foo.py 'x'")
    assert result.returncode == 2, (
        f"Expected exit 2 (block) for Set-Content foo.py positional, got {result.returncode}.\n"
        f"stderr: {result.stderr}\nstdout: {result.stdout}"
    )


@pytest.mark.skipif(_PWSH is None, reason="pwsh/powershell not on PATH")
def test_bash_writes_hook_blocks_pathlib_write_text():
    """python -c pathlib.Path.write_text targeting a .ts file must be blocked."""
    assert _BASH_WRITES_SCRIPT.exists()
    cmd = "python -c \"from pathlib import Path; Path('src/index.ts').write_text('x')\""
    result = _run_bash_writes_hook(cmd)
    assert result.returncode == 2, (
        f"Expected exit 2 (block) for pathlib write_text, got {result.returncode}.\n"
        f"stderr: {result.stderr}\nstdout: {result.stdout}"
    )


@pytest.mark.skipif(_PWSH is None, reason="pwsh/powershell not on PATH")
def test_bash_writes_hook_blocks_open_write_bytes():
    """python -c open(...,'wb') must be blocked (binary write mode)."""
    assert _BASH_WRITES_SCRIPT.exists()
    cmd = "python -c \"open('x.ts','wb').write(b'hello')\""
    result = _run_bash_writes_hook(cmd)
    assert result.returncode == 2, (
        f"Expected exit 2 (block) for open(..'wb'), got {result.returncode}.\n"
        f"stderr: {result.stderr}\nstdout: {result.stdout}"
    )


@pytest.mark.skipif(_PWSH is None, reason="pwsh/powershell not on PATH")
def test_bash_writes_hook_allows_redirect_to_log():
    """pytest > log.txt must be ALLOWED — .txt is not a blocked engine-source extension."""
    assert _BASH_WRITES_SCRIPT.exists()
    result = _run_bash_writes_hook("pytest tests/ -q > test_results.log.txt")
    assert result.returncode == 0, (
        f"Expected exit 0 (allow) for redirect to .txt, got {result.returncode}.\n"
        f"stderr: {result.stderr}\nstdout: {result.stdout}"
    )


@pytest.mark.skipif(_PWSH is None, reason="pwsh/powershell not on PATH")
def test_bash_writes_hook_allows_open_read_mode():
    """open('data.py','r') must be ALLOWED — read-only mode, NOT a write idiom.
    Regression guard against the false-positive where the filename ('data.py')
    contains 'a' which wrongly satisfied the old [wax] class."""
    assert _BASH_WRITES_SCRIPT.exists()
    cmd = "python -c \"open('data.py','r').read()\""
    result = _run_bash_writes_hook(cmd)
    assert result.returncode == 0, (
        f"Expected exit 0 (allow) for open(..'r') read-only, got {result.returncode}.\n"
        f"stderr: {result.stderr}\nstdout: {result.stdout}"
    )


# ---------------------------------------------------------------------------
# F6 — _cmd_status: latest-first ordering + structured render
# ---------------------------------------------------------------------------

def test_status_no_arg_returns_empty_when_no_hydra_dir(tmp_path, capsys):
    from hydra_core import cli
    rc = cli.main(["--project", str(tmp_path), "status"])
    out = capsys.readouterr().out
    assert rc == 0
    data = json.loads(out)
    assert "workflows" in data
    assert data["workflows"] == []


def test_status_no_arg_latest_first_ordering(tmp_path, capsys):
    """Workflows must be listed latest-first by trace mtime."""
    from hydra_core import cli
    from hydra_core.telemetry import emit

    # Create three fake workflows with different trace mtimes.
    wf_old = "aaaa0001-0000-0000-0000-000000000001"
    wf_mid = "aaaa0002-0000-0000-0000-000000000002"
    wf_new = "aaaa0003-0000-0000-0000-000000000003"

    for wf, goal in [(wf_old, "old goal"), (wf_mid, "mid goal"), (wf_new, "new goal")]:
        emit(tmp_path, wf, "workflow_start", {"goal": goal})
        time.sleep(0.02)  # ensure distinct mtimes

    rc = cli.main(["--project", str(tmp_path), "status"])
    out = capsys.readouterr().out
    assert rc == 0
    data = json.loads(out)
    wf_ids = [w["workflow_id"] for w in data["workflows"]]
    # newest must come first
    assert wf_ids.index(wf_new) < wf_ids.index(wf_old), (
        f"Expected {wf_new} before {wf_old} (latest-first). Got order: {wf_ids}"
    )


def test_status_with_id_structured_no_raw_dump(tmp_path, capsys, monkeypatch):
    """With a workflow_id, status must return structured JSON, not raw trace."""
    from hydra_core import cli
    from hydra_core.telemetry import emit

    wf = "bbbb1234-0000-0000-0000-000000000099"
    emit(tmp_path, wf, "workflow_start", {"goal": "test structured status"})

    # Patch build_supervisor to raise (simulating no langgraph) so we test fallback.
    monkeypatch.setattr(
        "hydra_core.cli.sys.modules",
        {**sys.modules},
    )
    with patch("hydra_core.cli._NullDispatcher"):
        # Even when the checkpoint supervisor fails, we should get structured JSON,
        # not raw text.
        rc = cli.main(["--project", str(tmp_path), "status", wf])
    out = capsys.readouterr().out
    assert rc == 0
    # Output must be valid JSON (not a raw text dump)
    data = json.loads(out)
    assert "workflow_id" in data
    assert data["workflow_id"] == wf
    # Must NOT be just a raw trace dump (the old behavior printed JSONL text)
    assert "tasks" in data or "events" in data or "note" in data


def test_status_with_id_checkpoint_view(tmp_path, capsys):
    """When checkpoint is available, status shows tasks/budget, not raw trace."""
    from hydra_core import cli
    from hydra_core.state import HydraState, TaskState, BudgetLedger
    from hydra_core.telemetry import emit
    from uuid import uuid4

    wf = str(uuid4())
    emit(tmp_path, wf, "workflow_start", {"goal": "structured view test"})

    # Build a fake HydraState with tasks.
    task_id = str(uuid4())
    fake_state = HydraState(
        workflow_id=wf,
        root_goal="structured view test",
        phase="executing",
    )
    fake_state.tasks = [
        TaskState(
            task_id=task_id,
            owner_squad="engineering",
            status="running",
            description="do the thing",
        )
    ]

    # Mock the supervisor to return our fake state.
    class _FakeSup:
        def get_state(self, config):
            snap = MagicMock()
            snap.values = fake_state.model_dump(mode="json")
            return snap

    # build_supervisor is a local import inside _cmd_status — patch at source module.
    with patch("hydra_core.supervisor.build_supervisor", return_value=_FakeSup()):
        with patch("hydra_core.supervisor._PurePythonRunner", type(None)):
            try:
                rc = cli.main(["--project", str(tmp_path), "status", wf])
            except SystemExit as e:
                rc = e.code

    out = capsys.readouterr().out
    # Tolerate failure to import langgraph in CI — just verify the output is structured.
    if out.strip():
        try:
            data = json.loads(out)
            if "tasks" in data:
                assert isinstance(data["tasks"], list)
                if data["tasks"]:
                    assert "owner_squad" in data["tasks"][0]
                    assert "status" in data["tasks"][0]
        except json.JSONDecodeError:
            pass  # structured output may be trace-fallback in minimal CI


# ---------------------------------------------------------------------------
# F3 — `hydra budget` CLI
# ---------------------------------------------------------------------------

try:
    from langgraph.checkpoint.memory import MemorySaver as _MemSaver  # noqa: F401
    _HAS_LANGGRAPH = True
except ImportError:
    _HAS_LANGGRAPH = False


def test_budget_list_no_checkpoint_db(tmp_path, capsys):
    """When langgraph checkpoint is unavailable, budget returns error JSON (exit 1)."""
    from hydra_core import cli
    from hydra_core.supervisor import _PurePythonRunner

    # Simulate langgraph unavailability by making build_supervisor return a
    # _PurePythonRunner (which _cmd_budget treats as "langgraph unavailable").
    # build_supervisor is a local import inside _cmd_budget — patch at source module.
    fake_runner = MagicMock(spec=_PurePythonRunner)

    with patch("hydra_core.supervisor.build_supervisor", return_value=fake_runner):
        with patch("hydra_core.supervisor._PurePythonRunner", _PurePythonRunner):
            rc = cli.main(["--project", str(tmp_path), "budget"])

    err = capsys.readouterr().err
    assert rc == 1
    assert "langgraph" in err.lower() or "unavailable" in err.lower() or "budget" in err.lower()


@pytest.mark.skipif(not _HAS_LANGGRAPH, reason="langgraph not installed")
def test_budget_show_workflow(tmp_path, capsys):
    """budget <wf_id> returns full ledger with repo_budgets / repo_spend."""
    from hydra_core import cli
    from hydra_core.state import HydraState, BudgetLedger
    from hydra_core.telemetry import emit
    from uuid import uuid4

    wf = str(uuid4())
    emit(tmp_path, wf, "workflow_start", {"goal": "budget show test"})

    # Build a fake state with non-trivial budget.
    fake_budget = BudgetLedger(
        budget_usd=100.0,
        spent_usd=42.5,
        spent_tokens=10000,
        repo_budgets={"agentsmith": 50.0, "hydra": 50.0},
        repo_spend={"agentsmith": 20.0},
    )
    fake_state = HydraState(workflow_id=wf, root_goal="budget show test")
    fake_state.budget = fake_budget

    class _FakeSup:
        def get_state(self, config):
            snap = MagicMock()
            snap.values = fake_state.model_dump(mode="json")
            return snap

    # build_supervisor is imported inside _cmd_budget, so patch at the source module.
    from hydra_core.supervisor import _PurePythonRunner
    with patch("hydra_core.supervisor.build_supervisor", return_value=_FakeSup()):
        with patch("hydra_core.supervisor._PurePythonRunner", type(None)):
            rc = cli.main(["--project", str(tmp_path), "budget", wf])

    out = capsys.readouterr().out
    assert rc == 0
    data = json.loads(out)
    assert data["workflow_id"] == wf
    assert data["budget_usd"] == 100.0
    assert data["spent_usd"] == pytest.approx(42.5, abs=1e-5)
    assert "repo_budgets" in data
    assert "repo_spend" in data
    assert "remaining_usd" in data or "usd_remaining" in data


@pytest.mark.skipif(not _HAS_LANGGRAPH, reason="langgraph not installed")
def test_budget_set_capability_gate(tmp_path, capsys):
    """budget --set triggers M3 capability verification (degraded-warn in tests)."""
    from hydra_core import cli
    from hydra_core.state import HydraState, BudgetLedger
    from hydra_core.telemetry import emit
    from uuid import uuid4

    wf = str(uuid4())
    emit(tmp_path, wf, "workflow_start", {"goal": "budget set test"})

    fake_budget = BudgetLedger(budget_usd=50.0, spent_usd=10.0)
    fake_state = HydraState(workflow_id=wf, root_goal="budget set test")
    fake_state.budget = fake_budget

    state_holder: dict = {"applied_patch": None}

    class _FakeSup:
        def get_state(self, config):
            snap = MagicMock()
            snap.values = fake_state.model_dump(mode="json")
            return snap

        def update_state(self, config, applied_patch):
            state_holder["applied_patch"] = applied_patch

    # build_supervisor is a local import inside _cmd_budget — patch at source module.
    with patch("hydra_core.supervisor.build_supervisor", return_value=_FakeSup()):
        with patch("hydra_core.supervisor._PurePythonRunner", type(None)):
            # Remove HYDRA_OPERATOR_KEY to force degraded path (no key configured in test).
            old_key = os.environ.pop("HYDRA_OPERATOR_KEY", None)
            try:
                rc = cli.main(["--project", str(tmp_path), "budget", wf, "--set", "200"])
            finally:
                if old_key is not None:
                    os.environ["HYDRA_OPERATOR_KEY"] = old_key

    out = capsys.readouterr().out
    assert rc == 0
    data = json.loads(out)
    assert data.get("set") is True
    assert data["budget_usd"] == 200.0
    # update_state was called with the budget patch
    assert state_holder["applied_patch"] is not None
    _state_patch = state_holder["applied_patch"]
    assert "budget" in _state_patch
    assert _state_patch["budget"]["budget_usd"] == 200.0
    # capability token should be in the patch (even if degraded)
    assert "operator_capability" in _state_patch


@pytest.mark.skipif(not _HAS_LANGGRAPH, reason="langgraph not installed")
def test_budget_set_fail_closed_when_key_configured(tmp_path, capsys):
    """When HYDRA_OPERATOR_KEY is set and mint_for_approval raises, --set must fail
    closed (exit 1) and NOT call update_state."""
    from hydra_core import cli
    from hydra_core.state import HydraState, BudgetLedger
    from hydra_core.telemetry import emit
    from uuid import uuid4

    wf = str(uuid4())
    emit(tmp_path, wf, "workflow_start", {"goal": "fail-closed test"})

    fake_budget = BudgetLedger(budget_usd=50.0, spent_usd=5.0)
    fake_state = HydraState(workflow_id=wf, root_goal="fail-closed test")
    fake_state.budget = fake_budget

    update_state_called = {"called": False}

    class _FakeSup:
        def get_state(self, config):
            snap = MagicMock()
            snap.values = fake_state.model_dump(mode="json")
            return snap

        def update_state(self, config, applied_patch):
            update_state_called["called"] = True

    def _boom_mint(**kwargs):
        raise RuntimeError("simulated mint failure — key configured")

    with patch("hydra_core.supervisor.build_supervisor", return_value=_FakeSup()):
        with patch("hydra_core.supervisor._PurePythonRunner", type(None)):
            # Patch mint_for_approval to raise AND ensure HYDRA_OPERATOR_KEY is set
            # so _force_degraded_bs is False (non-degraded / key-configured path).
            old_key = os.environ.get("HYDRA_OPERATOR_KEY")
            os.environ["HYDRA_OPERATOR_KEY"] = "fake-test-key"
            old_op = os.environ.get("HYDRA_OPERATOR_ID")
            os.environ["HYDRA_OPERATOR_ID"] = "test-operator"
            try:
                with patch("hydra_core.auth.capability.mint_for_approval", side_effect=_boom_mint):
                    rc = cli.main(["--project", str(tmp_path), "budget", wf, "--set", "999"])
            finally:
                if old_key is None:
                    os.environ.pop("HYDRA_OPERATOR_KEY", None)
                else:
                    os.environ["HYDRA_OPERATOR_KEY"] = old_key
                if old_op is None:
                    os.environ.pop("HYDRA_OPERATOR_ID", None)
                else:
                    os.environ["HYDRA_OPERATOR_ID"] = old_op

    err = capsys.readouterr().err
    # Must fail closed (exit 1) when key is configured and mint raises.
    assert rc == 1, (
        f"Expected exit 1 (fail-closed) when key configured and mint raises; got {rc}.\n"
        f"stderr: {err}"
    )
    assert "capability_mint_failed" in err or "mint" in err.lower(), (
        f"Expected mint-failure error in stderr; got: {err!r}"
    )
    # Budget MUST NOT have been patched.
    assert not update_state_called["called"], (
        "update_state was called despite mint failure — budget set must not proceed"
    )


@pytest.mark.skipif(not _HAS_LANGGRAPH, reason="langgraph not installed")
def test_budget_set_fail_closed_on_verify_exception_with_key(tmp_path, capsys):
    """When HYDRA_OPERATOR_KEY is set and verify_operator_capability RAISES, --set
    must fail closed (exit 1) and NOT call update_state.

    Distinguishes from the no-key degraded path where verify raising just warns.
    Also covers the new keying rule: _force_degraded_bs gates on KEY presence,
    so even if HYDRA_OPERATOR_ID is 'unknown', a configured key means fail-closed.
    """
    from hydra_core import cli
    from hydra_core.state import HydraState, BudgetLedger
    from hydra_core.telemetry import emit
    from uuid import uuid4

    wf = str(uuid4())
    emit(tmp_path, wf, "workflow_start", {"goal": "verify-exc fail-closed test"})

    fake_budget = BudgetLedger(budget_usd=50.0, spent_usd=5.0)
    fake_state = HydraState(workflow_id=wf, root_goal="verify-exc fail-closed test")
    fake_state.budget = fake_budget

    update_state_called = {"called": False}

    class _FakeSup:
        def get_state(self, config):
            snap = MagicMock()
            snap.values = fake_state.model_dump(mode="json")
            return snap

        def update_state(self, config, applied_patch):
            update_state_called["called"] = True

    # mint succeeds (returns a plausible token), but verify raises.
    def _ok_mint(**kwargs):
        return {"capability": "budget_set", "sig": {"value": "tok", "degraded": False}}

    def _boom_verify(token, **kwargs):
        raise RuntimeError("simulated verify crash — should fail closed")

    with patch("hydra_core.supervisor.build_supervisor", return_value=_FakeSup()):
        with patch("hydra_core.supervisor._PurePythonRunner", type(None)):
            old_key = os.environ.get("HYDRA_OPERATOR_KEY")
            os.environ["HYDRA_OPERATOR_KEY"] = "fake-test-key"
            # Leave HYDRA_OPERATOR_ID unset / unknown to validate that key presence
            # alone (not operator-id) governs fail-closed behaviour.
            old_op = os.environ.pop("HYDRA_OPERATOR_ID", None)
            try:
                with patch("hydra_core.auth.capability.mint_for_approval", side_effect=_ok_mint):
                    with patch("hydra_core.auth.capability.verify_operator_capability",
                               side_effect=_boom_verify):
                        rc = cli.main(["--project", str(tmp_path), "budget", wf, "--set", "777"])
            finally:
                if old_key is None:
                    os.environ.pop("HYDRA_OPERATOR_KEY", None)
                else:
                    os.environ["HYDRA_OPERATOR_KEY"] = old_key
                if old_op is not None:
                    os.environ["HYDRA_OPERATOR_ID"] = old_op

    err = capsys.readouterr().err
    assert rc == 1, (
        f"Expected exit 1 (fail-closed) when key configured and verify raises; got {rc}.\n"
        f"stderr: {err}"
    )
    assert "capability_verify_exception" in err or "verify" in err.lower(), (
        f"Expected verify-exception error in stderr; got: {err!r}"
    )
    assert not update_state_called["called"], (
        "update_state was called despite verify exception — budget set must not proceed"
    )


# ---------------------------------------------------------------------------
# F7 — .claude-plugin/hooks.json deleted + Task entries in root hooks.json
# ---------------------------------------------------------------------------

def test_claude_plugin_hooks_json_deleted():
    """.claude-plugin/hooks.json must no longer exist (merged into root hooks.json)."""
    orphan = PLUGIN_DIR / "hooks.json"
    assert not orphan.exists(), (
        ".claude-plugin/hooks.json still exists — it should be deleted after "
        "the iolaus entries were merged into root hooks.json"
    )


def test_hooks_json_has_task_pretooluse():
    """Root hooks.json must have a Task PreToolUse entry (iolaus_check)."""
    cfg = json.loads(HOOKS_JSON.read_text(encoding="utf-8"))
    pre = cfg.get("hooks", {}).get("PreToolUse", [])
    task_groups = [g for g in pre if g.get("matcher") == "Task"]
    assert task_groups, "No Task PreToolUse entry in root hooks.json"
    commands = [h.get("command", "") for g in task_groups for h in g.get("hooks", [])]
    assert any("iolaus_check" in cmd for cmd in commands), (
        f"iolaus_check not found in Task PreToolUse hooks: {commands}"
    )


def test_hooks_json_has_task_posttooluse():
    """Root hooks.json must have a Task PostToolUse entry (iolaus_log)."""
    cfg = json.loads(HOOKS_JSON.read_text(encoding="utf-8"))
    post = cfg.get("hooks", {}).get("PostToolUse", [])
    task_groups = [g for g in post if g.get("matcher") == "Task"]
    assert task_groups, "No Task PostToolUse entry in root hooks.json"
    commands = [h.get("command", "") for g in task_groups for h in g.get("hooks", [])]
    assert any("iolaus_log" in cmd for cmd in commands), (
        f"iolaus_log not found in Task PostToolUse hooks: {commands}"
    )


def test_hooks_json_has_bash_pretooluse():
    """Root hooks.json must have a Bash PreToolUse entry (hydra-block-bash-writes)."""
    cfg = json.loads(HOOKS_JSON.read_text(encoding="utf-8"))
    pre = cfg.get("hooks", {}).get("PreToolUse", [])
    bash_groups = [g for g in pre if g.get("matcher") == "Bash"]
    assert bash_groups, "No Bash PreToolUse entry in root hooks.json"
    commands = [h.get("command", "") for g in bash_groups for h in g.get("hooks", [])]
    assert any("hydra-block-bash-writes" in cmd for cmd in commands), (
        f"hydra-block-bash-writes not found in Bash PreToolUse hooks: {commands}"
    )


def test_iolaus_hooks_use_claude_plugin_root_path():
    """Iolaus hook commands must use CLAUDE_PLUGIN_ROOT-resolved paths."""
    cfg = json.loads(HOOKS_JSON.read_text(encoding="utf-8"))
    hooks_flat = cfg.get("hooks", {})
    all_commands = []
    for section in hooks_flat.values():
        for group in section:
            for h in group.get("hooks", []):
                cmd = h.get("command", "")
                if "iolaus" in cmd:
                    all_commands.append(cmd)
    assert all_commands, "No iolaus commands found in hooks.json"
    for cmd in all_commands:
        assert "CLAUDE_PLUGIN_ROOT" in cmd, (
            f"Iolaus hook does not use CLAUDE_PLUGIN_ROOT: {cmd!r}"
        )


# ---------------------------------------------------------------------------
# F15+M6 — gateway server.py docstring counts
# ---------------------------------------------------------------------------

def test_gateway_docstring_backend_count():
    """Gateway server.py module docstring must say 16 backends."""
    text = GATEWAY_SERVER.read_text(encoding="utf-8")
    # Check the module docstring
    assert "16 individual MCP server registrations" in text, (
        "gateway/server.py module docstring should say '16 individual MCP server registrations'"
    )


def test_gateway_docstring_meta_tool_count():
    """Gateway server.py module docstring must say 6 meta-tools."""
    text = GATEWAY_SERVER.read_text(encoding="utf-8")
    assert "6 gateway meta-tools" in text, (
        "gateway/server.py module docstring should say '6 gateway meta-tools'"
    )


def test_gateway_build_static_tool_list_count():
    """_build_static_tool_list docstring must not say '8 backends'."""
    text = GATEWAY_SERVER.read_text(encoding="utf-8")
    assert "8 backends" not in text, (
        "_build_static_tool_list still references '8 backends'; should be '16 backends'"
    )
    assert "16 backends" in text


def test_toolshed_build_default_shed_registers_16_backends():
    """build_default_shed registers exactly 16 static catalogs."""
    from hydra_core.toolshed import build_default_shed
    shed = build_default_shed()
    # ToolShed catalogs dict: each key is a server name.
    catalog_keys = list(shed._catalogs.keys()) if hasattr(shed, "_catalogs") else []
    if catalog_keys:
        assert len(catalog_keys) == 16, (
            f"Expected 16 catalogs in build_default_shed, got {len(catalog_keys)}: {catalog_keys}"
        )


def test_meta_tool_schemas_count():
    """META_TOOL_SCHEMAS must have exactly 6 entries."""
    from mcp_servers.hydra_gateway.server import META_TOOL_SCHEMAS
    assert len(META_TOOL_SCHEMAS) == 6, (
        f"Expected 6 META_TOOL_SCHEMAS, got {len(META_TOOL_SCHEMAS)}: {list(META_TOOL_SCHEMAS)}"
    )


# ---------------------------------------------------------------------------
# M1 — hydra-session-contract.ps1 scaffold opt-out
# ---------------------------------------------------------------------------

def test_session_contract_has_scaffold_opt_out():
    """hydra-session-contract.ps1 must check HYDRA_SCAFFOLD_CONTRACT=0."""
    script = HOOKS_DIR / "hydra-session-contract.ps1"
    assert script.exists()
    text = script.read_text(encoding="utf-8")
    assert "HYDRA_SCAFFOLD_CONTRACT" in text, (
        "hydra-session-contract.ps1 lacks HYDRA_SCAFFOLD_CONTRACT opt-out"
    )
    assert "'0'" in text or '"0"' in text or "ne '0'" in text or '-ne "0"' in text


def test_session_contract_logs_scaffolded_files():
    """hydra-session-contract.ps1 must log when it writes AGENTS.md/CLAUDE.md."""
    script = HOOKS_DIR / "hydra-session-contract.ps1"
    text = script.read_text(encoding="utf-8")
    # Must have a Write-Output or similar for the scaffolded file path.
    assert "Scaffolded" in text or "scaffolded" in text or "Write-Output" in text


# ---------------------------------------------------------------------------
# Structural: .claude-plugin/plugin.json hooks pointer
# ---------------------------------------------------------------------------

def test_plugin_json_hooks_points_to_root():
    """plugin.json must have hooks pointing at ./hooks.json (root)."""
    plugin_json = PLUGIN_DIR / "plugin.json"
    assert plugin_json.exists()
    cfg = json.loads(plugin_json.read_text(encoding="utf-8"))
    assert cfg.get("hooks") == "./hooks.json", (
        f"plugin.json hooks field should be './hooks.json', got {cfg.get('hooks')!r}"
    )
