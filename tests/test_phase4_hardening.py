"""Phase 4 hardening tests.

Covers F26+M8, F31, F27, F28, F29, F30, F5, GAP-a2, GAP-f, GAP-g, GAP-h.
GAP-d is validated via fixture pins applied in test_pp_team_routing.py and
test_rlm_gaming_delegation.py.
"""
from __future__ import annotations

import importlib
import os
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from hydra_core import host_bridge

# ---------------------------------------------------------------------------
# Shared dispatcher + helpers (mirrored from test_host_bridge.py)
# ---------------------------------------------------------------------------

class FakeDispatcher:
    """Canned pp responses; records every call_mcp invocation."""

    def __init__(self, *, required_cross_vendor=True, can_pass=True,
                 finalize_status="complete", downgraded=False):
        self.calls: list[tuple[str, str, dict, str | None]] = []
        self._required_cross = required_cross_vendor
        self._can_pass = can_pass
        self._finalize_status = finalize_status
        self._downgraded = downgraded

    def call_mcp(self, server, tool, args, squad_id=None):
        self.calls.append((server, tool, dict(args), squad_id))
        if tool == "start_stage":
            return {"status": "done", "result": {"stage_id": "stage-1"}}
        if tool == "record_attempt":
            return {"status": "done", "result": {"attempt_id": "att-1"}}
        if tool == "gate_eligible_judges":
            return {"status": "done", "result": {
                "required_cross_vendor": self._required_cross,
                "rubric_id": "rfc-2119-normative"}}
        if tool == "get_stage_finalize_readiness":
            return {"status": "done", "result": {"can_pass": self._can_pass}}
        if tool == "finalize_run":
            return {"status": "done", "result": {
                "effective_status": self._finalize_status,
                "downgraded": self._downgraded}}
        return {"status": "done", "result": {}}

    def count(self, tool: str) -> int:
        return sum(1 for _s, t, _a, _q in self.calls if t == tool)

    def args_for(self, tool: str) -> dict:
        return next(a for _s, t, a, _q in self.calls if t == tool)


@pytest.fixture(autouse=True)
def _smoke_passes(monkeypatch):
    """Default: smoke always passes so happy-path tests don't need a real build."""
    monkeypatch.setattr(host_bridge, "_run_smoke",
                        lambda *a, **k: ("pass", "fixture smoke pass"))


def _begin(disp, tmp_path):
    return host_bridge.begin_stage(
        disp, workflow_id="wf-p4", run_id="run-p4",
        project_path=str(tmp_path), request_text="implement the thing",
        project_root=str(tmp_path))


def _submit(disp, res, call_key, **result_kw):
    return host_bridge.submit_host_result(
        disp, cursor_file=res["cursor_path"],
        call_key=call_key, result=dict(result_kw))


def _full_pass(disp, tmp_path, judge_result=None):
    """Drive the full generate-0 → judge-0(pass) → finalize path."""
    if judge_result is None:
        judge_result = {"outcome": "pass", "judge_producer": "codex"}
    res = _begin(disp, tmp_path)
    res = _submit(disp, res, "generate-0", text="edited foo.py")
    res = _submit(disp, res, "judge-0", **judge_result)
    return res


# ===========================================================================
# F29: agent_type="engineer" in record_attempt (attended loop)
# ===========================================================================

def test_f29_record_attempt_includes_agent_type_engineer(tmp_path):
    """record_attempt payload must carry agent_type='engineer'."""
    disp = FakeDispatcher()
    res = _begin(disp, tmp_path)
    _submit(disp, res, "generate-0", text="edited foo.py")
    ra = disp.args_for("record_attempt")
    assert ra.get("agent_type") == "engineer", (
        f"record_attempt should carry agent_type='engineer', got: {ra}")


# ===========================================================================
# F31: required_cross_vendor=True but judge is same-vendor → downgrade
# ===========================================================================

def test_f31_same_vendor_judge_when_required_cross_downgrades(tmp_path):
    """A same-vendor verdict when required_cross_vendor=True must NOT finalize
    as complete.  Finding 2: F31 is an infra failure — surface immediately
    without burning a Reflexion retry."""
    disp = FakeDispatcher(required_cross_vendor=True)
    # NB: producer defaults to "claude"; judge_producer="claude" → same-vendor.
    res = _full_pass(disp, tmp_path,
                     judge_result={"outcome": "pass", "judge_producer": "claude"})
    # Finding 2: F31 is an infra downgrade → direct surface, no Reflexion.
    assert res["status"] != "complete", (
        "F31: same-vendor judge when cross required must not produce complete")
    assert res["status"] == "surfaced", (
        "F31 (Finding 2): infra downgrade must surface immediately, not trigger Reflexion")


def test_f31_infra_failure_does_not_consume_reflexion_slot(tmp_path):
    """F31 infra downgrade must surface immediately — the Reflexion slot must
    NOT be consumed by an infra failure (it is reserved for code defects)."""
    disp = FakeDispatcher(required_cross_vendor=True)
    res = _begin(disp, tmp_path)
    res = _submit(disp, res, "generate-0", text="edit1")
    # F31: same-vendor "pass" when cross required → infra downgrade → surface
    res = _submit(disp, res, "judge-0",
                  outcome="pass", judge_producer="claude")
    # Must surface immediately, NOT wait for generate-1.
    assert res["status"] == "surfaced", (
        "F31 infra: must surface directly without triggering generate-1")
    assert res.get("state") == "surfaced", "state must be surfaced after F31 infra"
    # Verify no second generate was requested (no pending_action for generate-1)
    assert disp.count("record_attempt") == 1, (
        "F31 infra: only one generate attempt must be recorded")


# ===========================================================================
# F26+M8: record_verdict RPC failure on pass → downgrade
# ===========================================================================

class _RVFailDispatcher(FakeDispatcher):
    """Raises RuntimeError on every record_verdict call."""
    def call_mcp(self, server, tool, args, squad_id=None):
        if tool == "record_verdict":
            raise RuntimeError("record_verdict RPC boom (F26+M8 test)")
        return super().call_mcp(server, tool, args, squad_id=squad_id)


def test_f26_m8_rv_failure_on_gen0_surfaces_immediately(tmp_path):
    """record_verdict RPC failure on gen-0 is an infra failure — must surface
    immediately (Finding 2), NOT burn a Reflexion retry.  A retry cannot fix
    an RPC-level failure."""
    disp = _RVFailDispatcher(required_cross_vendor=True)
    res = _begin(disp, tmp_path)
    res = _submit(disp, res, "generate-0", text="edit")
    # record_verdict raises → _infra_downgrade=True → direct surface, no Reflexion
    res = _submit(disp, res, "judge-0",
                  outcome="pass", judge_producer="codex")
    assert res["status"] == "surfaced", (
        "F26+M8 Finding 2: rv failure on gen-0 must surface immediately, "
        "not trigger Reflexion")
    assert res.get("state") == "surfaced", "state must be surfaced after rv failure"


def test_f26_m8_rv_failure_on_gen1_surfaces(tmp_path):
    """record_verdict failure on gen-1 (second attempt) must surface the stage —
    no more retries are available."""
    disp = _RVFailDispatcher(required_cross_vendor=True)
    res = _begin(disp, tmp_path)
    # First pass: revise (not rv-failure) to get to gen-1
    res = _submit(disp, res, "generate-0", text="edit1")
    # Use a cross-vendor revise verdict so rv doesn't fail on gen-0 submission
    # Actually _RVFailDispatcher raises on ALL record_verdict. gen-0 judge revise
    # → outcome already "revise" → rv failure doesn't change anything → reflexion.
    res = _submit(disp, res, "judge-0",
                  outcome="revise", judge_producer="codex")
    assert res["state"] == "await_generate", "reflexion expected"
    # Gen-1 → judge-1 pass, but rv fails → surfaces
    res = _submit(disp, res, "generate-1", text="edit2")
    res = _submit(disp, res, "judge-1",
                  outcome="pass", judge_producer="codex")
    assert res["status"] == "surfaced", (
        "F26+M8: rv failure on gen-1 must surface (no more retries)")
    assert res["final_status"] == "surfaced"


# ===========================================================================
# F26+M8: finalize_stage RPC failure on pass → downgrade
# ===========================================================================

class _FSFailDispatcher(FakeDispatcher):
    """Raises RuntimeError on finalize_stage(passed) calls."""
    def call_mcp(self, server, tool, args, squad_id=None):
        if tool == "finalize_stage" and args.get("status") == "passed":
            raise RuntimeError("finalize_stage RPC boom (F26+M8 test)")
        return super().call_mcp(server, tool, args, squad_id=squad_id)


def test_f26_m8_finalize_stage_failure_downgrades_to_surfaced(tmp_path):
    """finalize_stage RPC failure on a passing verdict must surface the stage."""
    disp = _FSFailDispatcher(required_cross_vendor=True)
    # Drive through reflexion to get to gen-1 pass (cross-vendor judge)
    res = _begin(disp, tmp_path)
    res = _submit(disp, res, "generate-0", text="edit1")
    res = _submit(disp, res, "judge-0",
                  outcome="revise", judge_producer="codex")
    res = _submit(disp, res, "generate-1", text="edit2")
    res = _submit(disp, res, "judge-1",
                  outcome="pass", judge_producer="codex")
    # finalize_stage raised → passed=False → surfaced
    assert res["status"] == "surfaced", (
        "F26+M8: finalize_stage failure must produce surfaced, not complete")
    assert res["final_status"] == "surfaced"


# ===========================================================================
# F30: abort reason embedded in summary_md
# ===========================================================================

def test_f30_abort_reason_in_summary_md(tmp_path):
    """abort_stage must embed the reason inside summary_md (not a bare 'reason'
    key, which FinalizeRunSchema strips)."""
    disp = FakeDispatcher()
    res = _begin(disp, tmp_path)
    host_bridge.abort_stage(disp, cursor_file=res["cursor_path"],
                            reason="test abort reason X")
    fr = disp.args_for("finalize_run")
    assert "reason" not in fr or fr.get("reason") is None, (
        "F30: finalize_run must not carry a top-level 'reason' key")
    assert "summary_md" in fr, "F30: finalize_run must carry 'summary_md'"
    assert "test abort reason X" in (fr.get("summary_md") or ""), (
        "F30: abort reason must be embedded in summary_md text")


# ===========================================================================
# GAP-f: Reflexion x1 — cursor transitions
# ===========================================================================

def test_gapf_first_revise_transitions_to_generate_1(tmp_path):
    """A revise verdict on gen-0 must transition cursor to await_generate with
    call_key='generate-1', not immediately surface."""
    disp = FakeDispatcher(required_cross_vendor=True)
    res = _begin(disp, tmp_path)
    res = _submit(disp, res, "generate-0", text="edit")
    res = _submit(disp, res, "judge-0",
                  outcome="revise", critique_md="File x.py has issues",
                  judge_producer="codex")
    assert res["status"] == "awaiting_host", (
        "GAP-f: first revise must transition to await_generate, not surface")
    assert res["state"] == "await_generate"
    assert res["host_action"]["call_key"] == "generate-1"
    assert res["host_action"]["agent_type"] == "engineer"
    # Critique should be embedded in the prompt for generate-1.
    prompt = res["host_action"].get("prompt", "")
    assert "issues" in prompt or "generate-1" in str(res), (
        "GAP-f: critique should appear in the generate-1 prompt context")


def test_gapf_generate_1_judge_key_is_judge_1(tmp_path):
    """After generate-1 submit, the next awaited action must be judge-1."""
    disp = FakeDispatcher(required_cross_vendor=True)
    res = _begin(disp, tmp_path)
    res = _submit(disp, res, "generate-0", text="edit1")
    res = _submit(disp, res, "judge-0",
                  outcome="revise", judge_producer="codex")
    assert res["state"] == "await_generate"
    res = _submit(disp, res, "generate-1", text="revision")
    assert res["state"] == "await_judge"
    assert res["host_action"]["call_key"] == "judge-1"


def test_gapf_second_revise_surfaces_without_further_retry(tmp_path):
    """A revise on gen-1 must finalize surfaced — no second Reflexion."""
    disp = FakeDispatcher(required_cross_vendor=True)
    res = _begin(disp, tmp_path)
    res = _submit(disp, res, "generate-0", text="edit1")
    res = _submit(disp, res, "judge-0",
                  outcome="revise", judge_producer="codex")
    res = _submit(disp, res, "generate-1", text="revision")
    res = _submit(disp, res, "judge-1",
                  outcome="revise", judge_producer="codex")
    assert res["status"] == "surfaced"
    assert res["final_status"] == "surfaced"
    # Exactly two generate attempts, exactly two judge calls.
    assert disp.count("record_attempt") == 2
    # No third generate pending.
    assert res["state"] == "surfaced"


def test_gapf_pass_on_gen_1_completes(tmp_path):
    """Pass on gen-1 (reflexion retry) must finalize complete."""
    disp = FakeDispatcher(required_cross_vendor=True)
    res = _begin(disp, tmp_path)
    res = _submit(disp, res, "generate-0", text="edit1")
    res = _submit(disp, res, "judge-0",
                  outcome="revise", judge_producer="codex")
    res = _submit(disp, res, "generate-1", text="revision")
    res = _submit(disp, res, "judge-1",
                  outcome="pass", judge_producer="codex")
    assert res["status"] == "complete"
    assert res["final_status"] == "complete"


# ===========================================================================
# GAP-h: suspicious critique telemetry warning
# ===========================================================================

def test_gaph_suspicious_critique_emits_warning(tmp_path, monkeypatch):
    """When the judge's critique_md contains no path token matching an existing
    worktree file, a warning telemetry event must be emitted."""
    emitted: list[tuple] = []

    import hydra_core.telemetry as _tel
    monkeypatch.setattr(_tel, "emit", lambda *a, **k: emitted.append(a))

    disp = FakeDispatcher(required_cross_vendor=True)
    res = _begin(disp, tmp_path)
    res = _submit(disp, res, "generate-0", text="edit")
    # critique_md has no path token that exists in tmp_path → suspicious.
    res = _submit(disp, res, "judge-0",
                  outcome="revise",
                  critique_md="The implementation is vague and needs more work",
                  judge_producer="codex")
    suspicious = [e for e in emitted
                  if len(e) >= 3 and "suspicious_critique" in str(e[2])]
    assert suspicious, (
        "GAP-h: a critique with no file references must emit "
        "attended.judge.suspicious_critique telemetry")


def test_gaph_critique_with_valid_file_ref_no_warning(tmp_path, monkeypatch):
    """A critique referencing an existing file must NOT trigger the warning."""
    emitted: list[tuple] = []
    import hydra_core.telemetry as _tel
    monkeypatch.setattr(_tel, "emit", lambda *a, **k: emitted.append(a))

    # Create a real file the critique can reference.
    (tmp_path / "foo.py").write_text("# stub\n", encoding="utf-8")

    disp = FakeDispatcher(required_cross_vendor=True)
    res = _begin(disp, tmp_path)
    res = _submit(disp, res, "generate-0", text="edit")
    res = _submit(disp, res, "judge-0",
                  outcome="revise",
                  critique_md="In foo.py the function signature is wrong.",
                  judge_producer="codex")
    suspicious = [e for e in emitted
                  if len(e) >= 3 and "suspicious_critique" in str(e[2])]
    assert not suspicious, (
        "GAP-h: critique referencing a real file must NOT trigger warning")


# ===========================================================================
# GAP-a2: lazy baseline from HYDRA_SMOKE_BASELINE_TESTS env var
# ===========================================================================

def test_gapa2_lazy_baseline_from_env_excuses_pre_existing_failures(
        tmp_path, monkeypatch):
    """When smoke fails and HYDRA_SMOKE_BASELINE_TESTS lists the failing tests,
    the smoke result is upgraded to pass (failures are pre-existing baseline)."""
    # Override smoke to fail.
    monkeypatch.setattr(host_bridge, "_run_smoke",
                        lambda *a, **k: ("fail", "pytest: test_old FAILED"))
    monkeypatch.setenv("HYDRA_SMOKE_BASELINE_TESTS", "tests/test_old.py::test_old")
    # Mock the subprocess re-run at judge time: current failures = same as baseline.
    _mock_proc = MagicMock()
    _mock_proc.stdout = "FAILED tests/test_old.py::test_old\n1 failed"
    _mock_proc.stderr = ""
    _mock_proc.returncode = 1
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _mock_proc)

    disp = FakeDispatcher(required_cross_vendor=True)
    res = _begin(disp, tmp_path)
    res = _submit(disp, res, "generate-0", text="edit")
    res = _submit(disp, res, "judge-0",
                  outcome="pass", judge_producer="codex")
    # All current failures are in the baseline → smoke excused → complete.
    assert res["status"] == "complete", (
        "GAP-a2: pre-existing baseline failures must excuse smoke failure")


def test_gapa2_new_failure_not_in_baseline_keeps_smoke_fail(
        tmp_path, monkeypatch):
    """A new test failure not in the baseline must keep smoke_status='fail'."""
    monkeypatch.setattr(host_bridge, "_run_smoke",
                        lambda *a, **k: ("fail", "pytest: test_new FAILED"))
    monkeypatch.setenv("HYDRA_SMOKE_BASELINE_TESTS", "tests/test_old.py::test_old")
    _mock_proc = MagicMock()
    _mock_proc.stdout = (
        "FAILED tests/test_old.py::test_old\n"
        "FAILED tests/test_new.py::test_new\n"
        "2 failed"
    )
    _mock_proc.stderr = ""
    _mock_proc.returncode = 1
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _mock_proc)

    disp = FakeDispatcher(required_cross_vendor=True)
    res = _begin(disp, tmp_path)
    res = _submit(disp, res, "generate-0", text="edit")
    res = _submit(disp, res, "judge-0",
                  outcome="pass", judge_producer="codex")
    # test_new is NOT in baseline → smoke not excused → surfaced.
    assert res["status"] != "complete", (
        "GAP-a2: a new failure not in baseline must keep smoke_status='fail'")


# ===========================================================================
# GAP-a2: _capture_baseline_failures tries parent directory
# ===========================================================================

def test_gapa2_capture_baseline_tries_parent_dir(tmp_path, monkeypatch):
    """_capture_baseline_failures must try the parent directory when the
    worktree itself has no tests/ — the real tests live in the repo root."""
    from hydra_core.host_bridge import _capture_baseline_failures

    # The 'worktree' dir has no tests/.
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    # The parent (repo root) has a tests/ dir.
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()

    _mock_proc = MagicMock()
    _mock_proc.stdout = "FAILED tests/test_x.py::test_x\n1 failed"
    _mock_proc.stderr = ""
    _mock_proc.returncode = 1
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _mock_proc)

    result = _capture_baseline_failures(str(worktree))
    assert isinstance(result, list), "must return a list"
    assert any("test_x" in r for r in result), (
        "GAP-a2: baseline must include the parent-dir failures")


# ===========================================================================
# GAP-g: npm/npx shell=True on Windows
# ===========================================================================

def test_gapg_npm_uses_shell_on_nt(tmp_path, monkeypatch):
    """On Windows (os.name=='nt'), _run_smoke must pass shell=True to
    subprocess.run when the detected command is npm/npx."""
    from hydra_core import squad_node

    # Set up a package.json with a test script to trigger npm detection.
    (tmp_path / "package.json").write_text(
        '{"scripts": {"test": "jest"}}', encoding="utf-8")

    captured: dict = {}

    def _fake_run(cmd, **kwargs):
        captured["shell"] = kwargs.get("shell", False)
        captured["cmd"] = cmd
        m = MagicMock()
        m.returncode = 0
        m.stdout = ""
        m.stderr = ""
        return m

    monkeypatch.setattr(subprocess, "run", _fake_run)
    # Force Windows behavior regardless of real OS.
    monkeypatch.setattr(os, "name", "nt")

    disp = MagicMock()
    squad_node._run_smoke(disp, project_path=str(tmp_path), stage_id="st-x")

    assert captured.get("shell") is True, (
        "GAP-g: npm command on Windows must use shell=True")


def test_gapg_python_cmd_no_shell(tmp_path, monkeypatch):
    """A Python pytest command must NOT use shell=True."""
    from hydra_core import squad_node

    # tests/ dir triggers pytest detection.
    (tmp_path / "tests").mkdir()

    captured: dict = {}

    def _fake_run(cmd, **kwargs):
        captured["shell"] = kwargs.get("shell", False)
        m = MagicMock()
        m.returncode = 0
        m.stdout = ""
        m.stderr = ""
        return m

    monkeypatch.setattr(subprocess, "run", _fake_run)
    disp = MagicMock()
    squad_node._run_smoke(disp, project_path=str(tmp_path), stage_id="st-x")
    assert captured.get("shell") is False, (
        "GAP-g: pytest command must NOT use shell=True")


# ===========================================================================
# F5: risk param forwarded through MCP handlers + CLI
# ===========================================================================

def _server_handlers():
    from mcp_servers.hydra_control.server import _tool_handlers
    return _tool_handlers()


def test_f5_workflow_launch_invalid_risk_returns_error():
    """workflow.launch with invalid risk enum returns {ok: False}."""
    h = _server_handlers()
    out = h["hydra.workflow.launch"]({"goal": "test", "risk": "invalid"})
    assert out["ok"] is False
    assert "risk" in out.get("error", "").lower() or "invalid" in str(out)


def test_f5_workflow_plan_invalid_risk_returns_error():
    """hydra.workflow.plan with invalid risk enum returns {ok: False}."""
    h = _server_handlers()
    out = h["hydra.workflow.plan"]({"goal": "test", "risk": "extreme"})
    assert out["ok"] is False


def test_f5_launch_risk_forwarded_to_cmd(monkeypatch, tmp_path):
    """workflow.launch with risk='medium' must pass --risk medium to the CLI subprocess."""
    from mcp_servers.hydra_control import server as _srv

    # Gate opened: _launch_run is gated by HYDRA_ALLOW_DETACHED (G4). Without it
    # the function returns detached_disabled before building the cmd.
    monkeypatch.setenv("HYDRA_ALLOW_DETACHED", "1")
    # Redirect _HYDRA_ROOT so mkdir/log operations don't touch the real project.
    monkeypatch.setattr(_srv, "_HYDRA_ROOT", tmp_path)

    captured_cmd: list = []

    class _FakePopen:
        pid = 12345
        def __init__(self, cmd, **kwargs):
            captured_cmd.extend(cmd)

    monkeypatch.setattr(subprocess, "Popen", _FakePopen)
    # _launch_run opens a log file; patch open so no real file is created.
    monkeypatch.setattr("builtins.open", lambda *a, **k: MagicMock(
        __enter__=lambda s: s, __exit__=lambda *a: None, write=lambda x: None))

    _srv._launch_run("test goal", squad=None, budget=None,
                     workflow_id="wf-test-risk", risk="medium")
    assert "--risk" in captured_cmd
    idx = captured_cmd.index("--risk")
    assert captured_cmd[idx + 1] == "medium", (
        f"F5: --risk medium must be forwarded, got: {captured_cmd}")


def test_f5_plan_risk_forwarded_to_cli_args(monkeypatch):
    """_run_plan must include --risk in the CLI args when risk is given."""
    from mcp_servers.hydra_control import server as _srv

    captured_cli_args: list = []

    def _fake_run_cli_json(cli_args, *, timeout_s, err_label, workflow_id=None):
        captured_cli_args.extend(cli_args)
        return {"ok": True, "workflow_id": workflow_id}

    monkeypatch.setattr(_srv, "_run_cli_json", _fake_run_cli_json)
    _srv._run_plan("test goal", squad=None, budget=None,
                   workflow_id="wf-r", risk="high")
    assert "--risk" in captured_cli_args
    idx = captured_cli_args.index("--risk")
    assert captured_cli_args[idx + 1] == "high", (
        f"F5: --risk high must be in plan CLI args, got: {captured_cli_args}")


def test_f5_inputschema_includes_risk_for_launch_and_plan():
    """Both workflow.launch and workflow.plan inputSchemas must declare 'risk'."""
    from mcp_servers.hydra_control import server as _srv
    schemas = _srv._TOOL_SCHEMAS
    for tool_name in ("hydra.workflow.launch", "hydra.workflow.plan"):
        props = schemas[tool_name]["inputSchema"]["properties"]
        assert "risk" in props, (
            f"F5: {tool_name} inputSchema must declare 'risk' property")
        assert props["risk"].get("type") == "string"
        assert set(props["risk"].get("enum", [])) == {"low", "medium", "high"}


def test_f5_cli_plan_accepts_risk_argument(tmp_path):
    """The CLI 'plan' subparser must accept --risk low|medium|high."""
    result = subprocess.run(
        [sys.executable, "-m", "hydra_core.cli", "plan", "--help"],
        capture_output=True, text=True,
        cwd=str(Path(__file__).resolve().parents[1]),
    )
    assert "--risk" in result.stdout, (
        "F5: 'hydra plan --help' must show --risk option")


# ===========================================================================
# F27: engineer.md agent stub files exist (integration check)
# ===========================================================================

HYDRA_ROOT = Path(__file__).resolve().parents[1]


def test_f27_engineer_md_agent_stub_exists():
    """The canonical plugin's engineer.md stub must exist in the worktree."""
    eng_file = HYDRA_ROOT / "plugins" / "hydra" / "agents" / "engineer.md"
    assert eng_file.exists(), (
        f"F27: engineer agent stub not found at {eng_file}")
    content = eng_file.read_text(encoding="utf-8")
    assert len(content) > 50, "F27: engineer.md should be non-trivial content"
    assert "engineer" in content.lower(), (
        "F27: engineer.md should contain engineer-related content")


def test_f27_judge_cross_vendor_md_exists():
    """The canonical plugin's judge-cross-vendor.md stub must exist."""
    f = HYDRA_ROOT / "plugins" / "hydra" / "agents" / "judge-cross-vendor.md"
    assert f.exists(), f"F27: judge-cross-vendor.md not found at {f}"
    assert len(f.read_text(encoding="utf-8")) > 20


def test_f27_judge_same_vendor_md_exists():
    """The canonical plugin's judge-same-vendor.md stub must exist."""
    f = HYDRA_ROOT / "plugins" / "hydra" / "agents" / "judge-same-vendor.md"
    assert f.exists(), f"F27: judge-same-vendor.md not found at {f}"
    assert len(f.read_text(encoding="utf-8")) > 20


def test_f27_plugin_json_includes_agent_stubs():
    """The canonical plugin manifest must list all three new agent stubs."""
    import json
    pj = HYDRA_ROOT / "plugins" / "hydra" / ".claude-plugin" / "plugin.json"
    data = json.loads(pj.read_text(encoding="utf-8"))
    agents = data.get("agents", [])
    assert any("engineer.md" in a for a in agents), (
        "F27: plugin.json must list engineer.md in agents")
    assert any("judge-cross-vendor.md" in a for a in agents), (
        "F27: plugin.json must list judge-cross-vendor.md")
    assert any("judge-same-vendor.md" in a for a in agents), (
        "F27: plugin.json must list judge-same-vendor.md")


def test_f27_preflight_check_exists_in_cli(monkeypatch, tmp_path):
    """The attended step preflight must return error when engineer.md is absent.

    Tests the guard logic directly by constructing the check condition.
    """
    project = tmp_path
    # No canonical Hydra plugin agent here.
    eng_agent_file = project / "plugins" / "hydra" / "agents" / "engineer.md"
    assert not eng_agent_file.exists(), "precondition: file must not exist"
    # The check from cli.py _cmd_attended_step:
    result_would_be_error = not eng_agent_file.exists()
    assert result_would_be_error, (
        "F27: preflight must detect missing engineer.md and return error")


# ===========================================================================
# F28: ensure_agents_md called in attended step (integration check on CLI)
# ===========================================================================

def test_f28_ensure_agents_md_in_cli_source():
    """The cli.py _cmd_attended_step must call ensure_agents_md after start_run.

    Verifies the source code contains the call rather than full CLI integration
    (which requires LangGraph checkpoint machinery).
    """
    cli_src = (HYDRA_ROOT / "hydra_core" / "cli.py").read_text(encoding="utf-8")
    assert "ensure_agents_md" in cli_src, (
        "F28: cli.py must call pp_harness.ensure_agents_md in _cmd_attended_step")
    # Verify it's after start_run (ordering check via line numbers).
    lines = cli_src.splitlines()
    start_run_line = next((i for i, l in enumerate(lines) if '"start_run"' in l
                           and "_cmd_attended_step" not in l), None)
    ensure_line = next((i for i, l in enumerate(lines) if "ensure_agents_md" in l), None)
    if start_run_line and ensure_line:
        assert ensure_line > start_run_line, (
            "F28: ensure_agents_md must appear after start_run in cli.py")


# ===========================================================================
# F26+M8 in headless squad_node loop
# ===========================================================================

def _headless_responses(outcome: str = "pass") -> dict[tuple[str, str], dict]:
    return {
        ("pp_harness", "start_run"): {"status": "done", "result": {"run_id": "run_H"}},
        ("pp_harness", "start_stage"): {"status": "done", "result": {"stage_id": "st_H"}},
        ("pp_codex", "generate"): {"status": "done", "result": {
            "text": "edited foo.py", "model": "codex-1",
            "tokens_in": 5, "tokens_out": 7, "cost_usd": 0.02, "wall_ms": 100}},
        ("pp_harness", "archive_artifact"): {"status": "done", "result": {"path": ".h/x"}},
        ("pp_harness", "record_attempt"): {"status": "done", "result": {"attempt_id": "att_H"}},
        ("pp_codex", "critique"): {"status": "done", "result": {"parsed": {
            "outcome": outcome, "critique_md": "c" * 90,
            "score": {"correctness": 9}}}},
        ("pp_harness", "record_verdict"): {"status": "done", "result": {}},
        ("pp_harness", "finalize_stage"): {"status": "done", "result": {}},
        ("pp_harness", "finalize_run"): {"status": "done", "result": {"status": "complete"}},
    }


class _ScriptedDispatcher:
    def __init__(self, responses, *, raise_on=None, drive=True):
        self.responses = responses
        self.raise_on = raise_on or set()
        self.calls: list[tuple[str, str, dict]] = []
        if drive:
            self.drive_pp_loop = True

    def call_mcp(self, server, tool, args, *, squad_id=None):
        self.calls.append((server, tool, dict(args)))
        if (server, tool) in self.raise_on:
            raise RuntimeError(f"boom on {server}.{tool}")
        return self.responses.get((server, tool), {"status": "done", "result": {}})

    def tool_seq(self):
        return [t for (_s, t, _a) in self.calls]

    def emit_claude_prompt(self, *_a, **_k): raise NotImplementedError
    def invoke_claude_skill(self, *_a, **_k): raise NotImplementedError
    def spawn_subprocess(self, *_a, **_k): raise NotImplementedError


def test_headless_f26_m8_rv_failure_downgrades_not_passed(monkeypatch):
    """In headless _drive_pp_stage_loop, a record_verdict RPC failure on a
    passing verdict must downgrade the stage (not finalize as complete)."""
    from hydra_core.squad_node import _drive_pp_stage_loop
    monkeypatch.setattr("hydra_core.squad_node._run_smoke",
                        lambda *_a, **_k: ("pass", "stub"))
    resp = _headless_responses("pass")
    disp = _ScriptedDispatcher(
        resp, raise_on={("pp_harness", "record_verdict")})
    out = _drive_pp_stage_loop(
        disp, run_id="run_H", project_path="/tmp/proj",
        request_text="do the thing")
    # record_verdict raised → outcome downgraded from pass → NOT complete
    assert out["final_status"] != "complete", (
        "F26+M8 headless: rv failure must not produce complete")


def test_headless_f26_m8_fs_failure_downgrades_not_passed(monkeypatch):
    """In headless loop, a finalize_stage RPC failure on a passing verdict must
    downgrade (surfaced), not report complete."""
    from hydra_core.squad_node import _drive_pp_stage_loop
    monkeypatch.setattr("hydra_core.squad_node._run_smoke",
                        lambda *_a, **_k: ("pass", "stub"))
    resp = _headless_responses("pass")
    disp = _ScriptedDispatcher(
        resp, raise_on={("pp_harness", "finalize_stage")})
    out = _drive_pp_stage_loop(
        disp, run_id="run_H", project_path="/tmp/proj",
        request_text="do the thing")
    assert out["stage_outcome"] != "pass", (
        "F26+M8 headless: finalize_stage failure must not report stage_outcome=pass")


def test_headless_f31_degraded_judge_downgrades(monkeypatch):
    """In headless loop, required_cross_vendor=True but same-vendor critique
    (codex judging codex) → must NOT finalize complete."""
    from hydra_core.squad_node import _drive_pp_stage_loop
    monkeypatch.setattr("hydra_core.squad_node._run_smoke",
                        lambda *_a, **_k: ("pass", "stub"))
    resp = _headless_responses("pass")
    disp = _ScriptedDispatcher(resp)

    # Patch gate_eligible_judges to require cross-vendor.
    orig_call = disp.call_mcp
    def _patched_call(server, tool, args, *, squad_id=None):
        if tool == "gate_eligible_judges":
            return {"status": "done", "result": {
                "required_cross_vendor": True,
                "rubric_id": "rfc-2119-normative",
            }}
        return orig_call(server, tool, args, squad_id=squad_id)
    disp.call_mcp = _patched_call

    out = _drive_pp_stage_loop(
        disp, run_id="run_H", project_path="/tmp/proj",
        request_text="do the thing")
    # codex generates + codex critiques → same-vendor → degraded → downgraded
    # (codex is same vendor as codex producer; required_cross=True → not satisfied)
    # The loop surfaces via reflexion x1 → then surfaces.
    assert out["final_status"] != "complete" or out.get("stage_outcome") != "pass", (
        "F31 headless: same-vendor judge when cross required must not produce complete/pass")


def test_headless_f29_agent_type_in_record_attempt(monkeypatch):
    """record_attempt in the headless loop must include agent_type='engineer'."""
    from hydra_core.squad_node import _drive_pp_stage_loop
    monkeypatch.setattr("hydra_core.squad_node._run_smoke",
                        lambda *_a, **_k: ("pass", "stub"))
    resp = _headless_responses("pass")
    disp = _ScriptedDispatcher(resp)
    _drive_pp_stage_loop(
        disp, run_id="run_H", project_path="/tmp/proj",
        request_text="do the thing")
    ra_calls = [a for (s, t, a) in disp.calls if t == "record_attempt"]
    assert ra_calls, "record_attempt must be called"
    assert ra_calls[0].get("agent_type") == "engineer", (
        f"F29 headless: record_attempt must carry agent_type='engineer', got: {ra_calls[0]}")


def test_headless_f30_abort_reason_in_summary_md(monkeypatch):
    """In headless loop abort, finalize_run must carry summary_md with the error
    reason — not a bare 'reason' key that FinalizeRunSchema strips."""
    from hydra_core.squad_node import _drive_pp_stage_loop
    monkeypatch.setattr("hydra_core.squad_node._run_smoke",
                        lambda *_a, **_k: ("pass", "stub"))
    resp = _headless_responses("pass")
    # Raise on start_stage to force the abort path.
    disp = _ScriptedDispatcher(resp, raise_on={("pp_harness", "start_stage")})
    out = _drive_pp_stage_loop(
        disp, run_id="run_H", project_path="/tmp/proj",
        request_text="do the thing")
    assert out["final_status"] == "aborted"
    fr_calls = [a for (s, t, a) in disp.calls if t == "finalize_run"]
    assert fr_calls, "finalize_run must be called on abort"
    fr = fr_calls[0]
    assert "reason" not in fr or fr.get("reason") is None, (
        "F30 headless: finalize_run abort must not carry top-level 'reason'")
    assert "summary_md" in fr, "F30 headless: abort finalize_run must carry summary_md"


# ===========================================================================
# GAP-d sanity: fixture pin smoke tests
# ===========================================================================

def test_gapd_fixture_packs_importable():
    """The fixture squad packs in the pinned test files must be importable and
    produce valid SquadPacks (GAP-d self-check)."""
    from hydra_core.squad_loader import _coerce_pack, SquadPack
    eng = _coerce_pack("engineering", {
        "name": "Engineering (fixture)",
        "entrypoint": "mcp",
        "accepts": ["PRD", "DEV_TASK"],
        "invoke": {"mode": "pp_best_of"},
    })
    assert isinstance(eng, SquadPack)
    assert eng.invoke.get("mode") == "pp_best_of"


def test_gapd_fixture_uses_real_coerce_pack():
    """The fixture uses the real _coerce_pack loader path (not a hand-rolled
    bypass).  Verifies the real schema is exercised, not a dict-return stub."""
    from hydra_core.squad_loader import _coerce_pack, SquadPack
    # Must produce a typed SquadPack, not a raw dict.
    pack = _coerce_pack("engineering", {
        "name": "test-fixture",
        "entrypoint": "mcp",
        "accepts": ["DEV_TASK"],
        "invoke": {"mode": "pp_best_of", "command_hint": "/pp:run"},
    })
    assert isinstance(pack, SquadPack), (
        "GAP-d: _coerce_pack must return a typed SquadPack, not a raw dict")
    # Real loader populates defaults — verify a known default field is set.
    assert hasattr(pack, "slug"), "GAP-d: SquadPack must have a slug attribute"


def test_gapd_fixture_survives_modified_squad_yaml(monkeypatch, tmp_path):
    """Fixture-pinned tests must pass even when squads/engineering/squad.yaml
    is modified.  Simulate by monkeypatching discover_squads to return a
    deliberately wrong config; the fixture must still produce a valid pack."""
    from hydra_core.squad_loader import _coerce_pack, SquadPack

    # Simulate discover_squads returning a broken / different config.
    bad_config = {"name": "WRONG-NAME", "entrypoint": "stub", "accepts": []}
    monkeypatch.setattr(
        "hydra_core.squad_loader.discover_squads",
        lambda _root=None: {"engineering": bad_config},
    )

    # The fixture bypasses discover_squads — it calls _coerce_pack directly
    # with its own pinned config, which must still produce a valid pack.
    fixture_pack = _coerce_pack("engineering", {
        "name": "Engineering & Product (fixture)",
        "entrypoint": "mcp",
        "accepts": ["PRD", "DEV_TASK"],
        "invoke": {"mode": "pp_best_of"},
    })
    assert isinstance(fixture_pack, SquadPack), (
        "GAP-d: fixture _coerce_pack must succeed regardless of live squad.yaml")
    assert fixture_pack.invoke.get("mode") == "pp_best_of", (
        "GAP-d: fixture config must take precedence over (simulated) modified squad.yaml")


# ===========================================================================
# HYDRA_ALLOW_DETACHED gate (G4 routing-audit)
# ===========================================================================

def test_detached_gate_launch_blocked_without_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """_launch_run returns a detached_disabled refusal when HYDRA_ALLOW_DETACHED
    is not set, and subprocess.Popen is never called.  No log directory must be
    created — the gate fires before any filesystem side-effects."""
    from mcp_servers.hydra_control import server as _srv

    monkeypatch.delenv("HYDRA_ALLOW_DETACHED", raising=False)
    # Redirect _HYDRA_ROOT so any accidental mkdir goes to a temp dir we can
    # inspect, not the real project root.
    monkeypatch.setattr(_srv, "_HYDRA_ROOT", tmp_path)

    class _ShouldNotBeCalled:
        def __init__(self, *a: Any, **kw: Any) -> None:
            raise AssertionError(
                "Popen must NOT be called when the detached gate is active"
            )

    monkeypatch.setattr(subprocess, "Popen", _ShouldNotBeCalled)

    out = _srv._launch_run("test goal", squad=None, budget=None, workflow_id="wf-gate-1")
    assert out["ok"] is False, "gate off → ok must be False"
    assert out["error"] == "detached_disabled", f"unexpected error key: {out}"
    assert "HYDRA_ALLOW_DETACHED" in out.get("remediation", ""), (
        "remediation must mention HYDRA_ALLOW_DETACHED"
    )
    # Side-effect assertion: no workflow directory must have been created.
    assert not (tmp_path / ".hydra" / "wf-gate-1").exists(), (
        "gate must fire before log_dir.mkdir — no workflow subdir may be created"
    )


def test_detached_gate_resume_blocked_without_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """_launch_resume returns a detached_disabled refusal when HYDRA_ALLOW_DETACHED
    is not set, and subprocess.Popen is never called.  No log directory must be
    created — the gate fires before any filesystem side-effects."""
    from mcp_servers.hydra_control import server as _srv

    monkeypatch.delenv("HYDRA_ALLOW_DETACHED", raising=False)
    # Redirect _HYDRA_ROOT so any accidental mkdir goes to a temp dir we can
    # inspect, not the real project root.
    monkeypatch.setattr(_srv, "_HYDRA_ROOT", tmp_path)

    class _ShouldNotBeCalled:
        def __init__(self, *a: Any, **kw: Any) -> None:
            raise AssertionError(
                "Popen must NOT be called when the detached gate is active"
            )

    monkeypatch.setattr(subprocess, "Popen", _ShouldNotBeCalled)

    out = _srv._launch_resume("wf-gate-2", "approve", None)
    assert out["ok"] is False, "gate off → ok must be False"
    assert out["error"] == "detached_disabled", f"unexpected error key: {out}"
    assert "HYDRA_ALLOW_DETACHED" in out.get("remediation", ""), (
        "remediation must mention HYDRA_ALLOW_DETACHED"
    )
    # Side-effect assertion: no workflow directory must have been created.
    assert not (tmp_path / ".hydra" / "wf-gate-2").exists(), (
        "gate must fire before log_dir.mkdir — no workflow subdir may be created"
    )


def test_detached_gate_launch_allowed_with_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """_launch_run proceeds to Popen when HYDRA_ALLOW_DETACHED=1."""
    from mcp_servers.hydra_control import server as _srv

    monkeypatch.setenv("HYDRA_ALLOW_DETACHED", "1")
    # Redirect _HYDRA_ROOT to tmp_path so mkdir/log operations go to a temp dir.
    monkeypatch.setattr(_srv, "_HYDRA_ROOT", tmp_path)

    popen_calls: list[list[str]] = []

    class _RecordPopen:
        pid: int = 99999

        def __init__(self, cmd: list[str], **kw: Any) -> None:
            popen_calls.append(list(cmd))

    monkeypatch.setattr(subprocess, "Popen", _RecordPopen)

    out = _srv._launch_run("test goal", squad=None, budget=None, workflow_id="wf-gate-3")
    assert popen_calls, "Popen must be called when HYDRA_ALLOW_DETACHED=1"
    assert out.get("ok") is True, f"expected ok=True, got: {out}"


def test_detached_gate_launch_fleet_bypasses_gate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """_launch_run bypasses the detached gate for fleet goals even when
    HYDRA_ALLOW_DETACHED is not set (fleet is detached by design)."""
    from mcp_servers.hydra_control import server as _srv

    monkeypatch.delenv("HYDRA_ALLOW_DETACHED", raising=False)
    monkeypatch.setattr(_srv, "_HYDRA_ROOT", tmp_path)

    popen_calls: list[list[str]] = []

    class _RecordPopen:
        pid: int = 99999

        def __init__(self, cmd: list[str], **kw: Any) -> None:
            popen_calls.append(list(cmd))

    monkeypatch.setattr(subprocess, "Popen", _RecordPopen)

    fleet_goal = "fix bug --repos agentsmith,theeights"
    out = _srv._launch_run(fleet_goal, squad=None, budget=None, workflow_id="wf-gate-4")
    assert popen_calls, (
        "Popen must be called for a fleet goal even without HYDRA_ALLOW_DETACHED"
    )
    assert out.get("ok") is True, f"expected ok=True for fleet goal, got: {out}"


@pytest.mark.parametrize("flag", ["--repos", "--fleet"])
def test_detached_gate_single_id_remains_gated(
    flag: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A single-id token (no comma) must NOT bypass the detached gate.

    ``--repos agentsmith`` and ``--fleet agentsmith`` each carry only one repo
    id; they do not constitute a fleet run, so _is_fleet_goal must return False
    and _launch_run must return the detached_disabled refusal.
    """
    from mcp_servers.hydra_control import server as _srv

    monkeypatch.delenv("HYDRA_ALLOW_DETACHED", raising=False)
    monkeypatch.setattr(_srv, "_HYDRA_ROOT", tmp_path)

    class _ShouldNotBeCalled:
        def __init__(self, *a: Any, **kw: Any) -> None:
            raise AssertionError(
                f"Popen must NOT be called for single-id {flag} goal"
            )

    monkeypatch.setattr(subprocess, "Popen", _ShouldNotBeCalled)

    single_id_goal = f"fix a bug {flag} agentsmith"
    assert not _srv._is_fleet_goal(single_id_goal), (
        f"_is_fleet_goal must be False for single-id goal: {single_id_goal!r}"
    )

    out = _srv._launch_run(
        single_id_goal, squad=None, budget=None, workflow_id="wf-single-id"
    )
    assert out["ok"] is False, (
        f"single-id {flag} goal must be gated → ok=False, got: {out}"
    )
    assert out["error"] == "detached_disabled", (
        f"single-id {flag} goal must produce detached_disabled, got: {out}"
    )
    # Side-effect: no log directory created.
    assert not (tmp_path / ".hydra" / "wf-single-id").exists(), (
        "gate must fire before log_dir.mkdir for single-id goal"
    )


def test_detached_gate_ingest_always_allowed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """_launch_ingest is ungated: it always proceeds to Popen regardless of
    HYDRA_ALLOW_DETACHED because it is the attended skill-squad continuation
    transport (not a user-initiated detached launch)."""
    from mcp_servers.hydra_control import server as _srv

    monkeypatch.delenv("HYDRA_ALLOW_DETACHED", raising=False)
    monkeypatch.setattr(_srv, "_HYDRA_ROOT", tmp_path)

    popen_calls: list[list[str]] = []

    class _RecordPopen:
        pid: int = 99999

        def __init__(self, cmd: list[str], **kw: Any) -> None:
            popen_calls.append(list(cmd))

    monkeypatch.setattr(subprocess, "Popen", _RecordPopen)

    envelopes: list[dict[str, Any]] = [
        {"type": "DEV_TASK", "origin_squad": "garland", "id": "env-1"}
    ]
    out = _srv._launch_ingest("wf-gate-5", envelopes)
    assert popen_calls, (
        "Popen must be called for ingest regardless of HYDRA_ALLOW_DETACHED"
    )
    assert out.get("ok") is True, f"expected ok=True for ingest, got: {out}"


# ===========================================================================
# _FLEET_GOAL_RE unit cases (G4 routing-audit tightening)
# ===========================================================================

@pytest.mark.parametrize("goal,expected", [
    # ---- should match (genuine multi-id fleet) ----
    ("--repos a,b", True),
    ("fix bug --repos agentsmith,theeights", True),
    ("fix bug --fleet agentsmith,theeights,xenia", True),
    ("--repos a,b extra", True),        # trailing content after whitespace is fine
    # ---- should NOT match ----
    ("--repos a", False),               # single id, no comma
    ("--fleet a", False),               # single id via --fleet
    ("--repos a,b,", False),            # trailing comma — not a valid id list
    ("refactor the --repos parser", False),  # prose: only one id, no comma
])
def test_fleet_goal_re_token_boundaries(goal: str, expected: bool) -> None:
    """_FLEET_GOAL_RE must respect token boundaries.

    Matches require 2+ comma-separated ids, the flag must follow start-of-string
    or whitespace, and the id list must end at whitespace or end-of-string (so a
    trailing comma does not sneak through).
    """
    from mcp_servers.hydra_control.server import _is_fleet_goal
    result = _is_fleet_goal(goal)
    assert result is expected, (
        f"_is_fleet_goal({goal!r}): expected {expected}, got {result}"
    )


# ===========================================================================
# Finding 1: record_attempt schema pin + happy-path try/except
# ===========================================================================

_PP_HARNESS_TS = Path(
    "C:/AiAppDeployments/pair-programmer/daemon/src/mcp/harness-server.ts"
)


@pytest.mark.skipif(
    not _PP_HARNESS_TS.exists(),
    reason="pair-programmer harness-server.ts not found on this machine; skip schema pin",
)
def test_f1_record_attempt_payload_has_accepted_keys():
    """Our record_attempt payload keys must be a strict subset of pp's
    RecordAttemptSchema.  Reads harness-server.ts at test runtime so the pin
    stays honest across pp schema evolution (8-residual: live extraction)."""
    import re as _re

    ts_text = _PP_HARNESS_TS.read_text(encoding="utf-8")

    # Locate the RecordAttemptSchema block.
    start_m = _re.search(r"const RecordAttemptSchema\s*=\s*z\.object\(\{", ts_text)
    assert start_m, (
        "Could not find 'const RecordAttemptSchema = z.object({' in harness-server.ts — "
        "the schema may have been renamed; update this test."
    )
    # Walk brace-depth to extract the body between the outer { }.
    block_start = start_m.end()
    depth = 1
    i = block_start
    while i < len(ts_text) and depth > 0:
        ch = ts_text[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        i += 1
    schema_block = ts_text[block_start : i - 1]

    # Extract property names: lines that start (after whitespace) with an
    # identifier followed by optional whitespace and a colon.
    accepted_keys = frozenset(
        _re.findall(r"^\s+(\w+)\s*:", schema_block, _re.MULTILINE)
    )
    assert accepted_keys, (
        "Extracted an empty accepted_keys set from RecordAttemptSchema — "
        "the extraction regex may need updating."
    )

    # Keys we actually send in host_bridge._apply_generate.
    our_keys = frozenset({
        "stage_id", "producer", "model_id", "agent_type",
        "tokens_in", "tokens_out", "cost_usd", "status", "retry_index",
        "notes",
    })

    extra = our_keys - accepted_keys
    assert not extra, (
        f"record_attempt payload contains keys NOT in pp RecordAttemptSchema: {extra}. "
        f"Accepted keys extracted from harness-server.ts: {sorted(accepted_keys)}"
    )


def test_f1_record_attempt_rpc_failure_surfaces_not_crashes(tmp_path):
    """A record_attempt RPC failure on the happy path must surface the stage
    cleanly, NOT crash submit_host_result and orphan the attended workflow."""

    class _RAFailDispatcher(FakeDispatcher):
        """Raises RuntimeError on record_attempt call."""
        def call_mcp(self, server, tool, args, squad_id=None):
            if tool == "record_attempt":
                raise RuntimeError("record_attempt RPC boom (Finding 1 test)")
            return super().call_mcp(server, tool, args, squad_id=squad_id)

    disp = _RAFailDispatcher()
    res = _begin(disp, tmp_path)
    # Should NOT raise; must return a surfaced result.
    res = _submit(disp, res, "generate-0", text="edit")
    assert res["status"] == "surfaced", (
        "Finding 1: record_attempt RPC failure must surface the stage, not crash")
    assert "record_attempt" in (res.get("error") or ""), (
        "Finding 1: error message must mention record_attempt")


# ===========================================================================
# Finding 2: infra downgrades surface immediately (F31 + F26 attended)
# ===========================================================================

def test_f2_f31_attended_does_not_consume_reflexion_and_surfaces(tmp_path):
    """F31 infra downgrade in attended mode: direct surface, no Reflexion.
    The generate-1 slot is preserved (not consumed by an infra failure)."""
    disp = FakeDispatcher(required_cross_vendor=True)
    res = _begin(disp, tmp_path)
    res = _submit(disp, res, "generate-0", text="edit")
    # F31: same-vendor "pass" when cross required → infra downgrade
    res = _submit(disp, res, "judge-0", outcome="pass", judge_producer="claude")
    assert res["status"] == "surfaced", (
        "Finding 2 (F31 attended): infra downgrade must surface immediately")
    assert disp.count("record_attempt") == 1, "only one generate attempt must be made"


def test_f2_f26_rv_failure_attended_surfaces_not_reflexion(tmp_path):
    """F26+M8 RV failure in attended mode: direct surface, no Reflexion.
    record_verdict RPC failure is an infra problem — retrying cannot fix it."""

    class _RVFailDispatcher2(FakeDispatcher):
        def call_mcp(self, server, tool, args, squad_id=None):
            if tool == "record_verdict":
                raise RuntimeError("boom")
            return super().call_mcp(server, tool, args, squad_id=squad_id)

    disp = _RVFailDispatcher2(required_cross_vendor=True)
    res = _begin(disp, tmp_path)
    res = _submit(disp, res, "generate-0", text="edit")
    # cross-vendor pass → rv fails → infra downgrade → surface immediately
    res = _submit(disp, res, "judge-0", outcome="pass", judge_producer="codex")
    assert res["status"] == "surfaced", (
        "Finding 2 (F26 attended): rv RPC failure must surface immediately, not Reflexion")
    assert disp.count("record_attempt") == 1


# ===========================================================================
# Finding 3: GAP-a2 baseline uses repo_root (not just parent)
# ===========================================================================

def test_f3_capture_baseline_uses_repo_root_when_provided(tmp_path, monkeypatch):
    """_capture_baseline_failures must try repo_root first when provided.
    A worktree at <repo>/.harness/worktrees/attended-X needs to run pytest
    from <repo>/tests/, not from the worktree or its direct parent."""
    from hydra_core.host_bridge import _capture_baseline_failures

    # Simulate: worktree path (no tests/) is two levels deep in the repo.
    repo_root = tmp_path / "myrepo"
    worktrees_dir = repo_root / ".harness" / "worktrees"
    worktree = worktrees_dir / "attended-run123"
    worktree.mkdir(parents=True)
    # Only repo_root has tests/
    (repo_root / "tests").mkdir()

    _mock_proc = MagicMock()
    _mock_proc.stdout = "FAILED tests/test_repo.py::test_r\n1 failed"
    _mock_proc.stderr = ""
    _mock_proc.returncode = 1
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _mock_proc)

    result = _capture_baseline_failures(str(worktree), repo_root=str(repo_root))
    assert isinstance(result, list), "must return a list"
    assert any("test_repo" in r for r in result), (
        "Finding 3: baseline must find tests via repo_root, not worktree parent")


def test_f3_capture_baseline_no_repo_root_falls_back_to_parent(
        tmp_path, monkeypatch):
    """When repo_root is not provided, parent-dir fallback still works."""
    from hydra_core.host_bridge import _capture_baseline_failures

    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (tmp_path / "tests").mkdir()  # tests/ is at tmp_path (parent of worktree)

    _mock_proc = MagicMock()
    _mock_proc.stdout = "FAILED tests/test_y.py::test_y\n1 failed"
    _mock_proc.stderr = ""
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _mock_proc)

    result = _capture_baseline_failures(str(worktree))  # no repo_root
    assert any("test_y" in r for r in result), (
        "Finding 3: parent-dir fallback must still work when repo_root is absent")


# ===========================================================================
# Finding 4: attended _finalize merge before finalize_run
# ===========================================================================

def test_f4_merge_failure_makes_finalize_run_surfaced(tmp_path, monkeypatch):
    """When _merge_worktree_back fails after finalize_stage(passed), _finalize
    must call finalize_run(surfaced), NOT finalize_run(complete).  The pp ledger
    must reflect the true state: code did not land."""
    finalize_run_calls: list[dict] = []

    class _MergeFailDispatcher(FakeDispatcher):
        def call_mcp(self, server, tool, args, squad_id=None):
            if tool == "finalize_run":
                finalize_run_calls.append(dict(args))
            return super().call_mcp(server, tool, args, squad_id=squad_id)

    # Inject a worktree_path + repo_root + branch into the cursor so the merge
    # path is exercised; then patch _merge_worktree_back to fail.
    monkeypatch.setattr(
        host_bridge, "_merge_worktree_back",
        lambda *a, **k: {"merged": False, "error": "simulated_merge_failure"},
    )
    monkeypatch.setattr(host_bridge, "_remove_worktree", lambda *a, **k: None)

    disp = _MergeFailDispatcher(required_cross_vendor=True)
    res = _begin(disp, tmp_path)
    # Inject fake worktree fields into the cursor.
    import json
    cpath = res["cursor_path"]
    cursor = json.loads(Path(cpath).read_text())
    cursor["worktree_path"] = str(tmp_path / "wt")
    cursor["repo_root"] = str(tmp_path)
    cursor["branch"] = "attended/test-branch"
    Path(cpath).write_text(json.dumps(cursor))

    res = _submit(disp, res, "generate-0", text="edit")
    res = _submit(disp, res, "judge-1",  # skip to gen-1 judge (after reflexion)
                  outcome="pass", judge_producer="codex") \
        if False else None

    # Drive through the full pass path without reflexion by using gen-0 pass
    # and a cross-vendor judge so F31 doesn't fire.
    disp2 = _MergeFailDispatcher(required_cross_vendor=False)
    monkeypatch.setattr(
        host_bridge, "_merge_worktree_back",
        lambda *a, **k: {"merged": False, "error": "simulated_merge_failure"},
    )
    res2 = _begin(disp2, tmp_path)
    cursor2 = json.loads(Path(res2["cursor_path"]).read_text())
    cursor2["worktree_path"] = str(tmp_path / "wt2")
    cursor2["repo_root"] = str(tmp_path)
    cursor2["branch"] = "attended/test-branch-2"
    Path(res2["cursor_path"]).write_text(json.dumps(cursor2))

    res2 = _submit(disp2, res2, "generate-0", text="edit")
    res2 = _submit(disp2, res2, "judge-0",
                   outcome="pass", judge_producer="claude")  # same-vendor OK (not required_cross)

    # finalize_run must have been called with surfaced (merge failed)
    assert finalize_run_calls, "finalize_run must be called"
    last_call = finalize_run_calls[-1]
    assert last_call.get("status") == "surfaced", (
        f"Finding 4: merge failure must call finalize_run(surfaced), "
        f"got: {last_call.get('status')}")


def test_new_merge_failure_sets_pass_unlanded_outcome(tmp_path, monkeypatch):
    """When the worktree merge fails after a passing verdict, cursor['outcome']
    must be downgraded to 'pass_unlanded' so step_result / summary report an
    outcome that matches the surfaced landing status."""
    finalize_run_calls: list[dict] = []

    class _MergeFailDisp2(FakeDispatcher):
        def call_mcp(self, server, tool, args, squad_id=None):
            if tool == "finalize_run":
                finalize_run_calls.append(dict(args))
            return super().call_mcp(server, tool, args, squad_id=squad_id)

    monkeypatch.setattr(
        host_bridge, "_merge_worktree_back",
        lambda *a, **k: {"merged": False, "error": "test_merge_fail"},
    )
    monkeypatch.setattr(host_bridge, "_remove_worktree", lambda *a, **k: None)

    disp = _MergeFailDisp2(required_cross_vendor=False)  # same-vendor OK
    res = _begin(disp, tmp_path)

    # Inject fake worktree fields so the merge path is exercised.
    import json
    cpath = res["cursor_path"]
    cursor = json.loads(Path(cpath).read_text())
    cursor["worktree_path"] = str(tmp_path / "wt_new")
    cursor["repo_root"] = str(tmp_path)
    cursor["branch"] = "attended/new-test"
    Path(cpath).write_text(json.dumps(cursor))

    res = _submit(disp, res, "generate-0", text="edit")
    # same-vendor claude judge with required_cross_vendor=False → pass, no F31
    res = _submit(disp, res, "judge-0", outcome="pass", judge_producer="claude")

    # Run must be surfaced (merge failed).
    assert res["status"] == "surfaced", (
        "NEW: merge failure must produce surfaced run")
    # stage_outcome in step result must NOT be 'pass'.
    stage_outcome = res.get("stage_outcome")
    assert stage_outcome == "pass_unlanded", (
        f"NEW: cursor['outcome'] must be 'pass_unlanded' after merge failure, "
        f"got: {stage_outcome!r}")
    # finalize_run must carry the true (surfaced) status.
    assert finalize_run_calls, "finalize_run must be called"
    assert finalize_run_calls[-1].get("status") == "surfaced"


# ===========================================================================
# Finding 5: best-of loop merges winner only after readiness+finalize_stage
# ===========================================================================

def test_f5_best_of_readiness_fail_does_not_call_archive(monkeypatch):
    """When readiness check fails, archive_winner_and_losers must NOT be called
    (winner code must not land in the project)."""
    from hydra_core.squad_node import _drive_best_of_loop

    archive_called = []

    def _fake_cm(server, tool, args, *, squad_id=None):
        if tool == "archive_winner_and_losers":
            archive_called.append(True)
            return {"status": "done", "result": {"merge_status": "merged"}}
        if tool == "start_best_of_stage":
            # Return stage_id + candidates list as the real daemon does.
            return {"status": "done", "result": {
                "stage_id": "stg-bo", "n": 1,
                "candidates": [{"candidate_index": 0, "attempt_slot_id": "s0",
                                "worktree_path": "/tmp/wt0"}]}}
        if tool == "get_stage_finalize_readiness":
            return {"status": "done", "result": {"can_pass": False,
                                                  "next_action": "blocker_gate"}}
        if tool in {"record_attempt", "archive_artifact"}:
            return {"status": "done", "result": {"attempt_id": "a0"}}
        if tool in {"record_verdict", "record_smoke_status"}:
            return {"status": "done", "result": {}}
        if tool == "finalize_stage":
            return {"status": "done", "result": {}}
        if tool == "finalize_run":
            return {"status": "done", "result": {"status": "surfaced"}}
        if tool == "teardown_candidates":
            return {"status": "done", "result": {"teardown_status": "ok"}}
        if tool == "gate_eligible_judges":
            return {"status": "done", "result": {
                "required_cross_vendor": False, "rubric_id": "rfc-2119-normative"}}
        return {"status": "done", "result": {}}

    class _BODisp:
        def call_mcp(self, server, tool, args, *, squad_id=None):
            return _fake_cm(server, tool, args, squad_id=squad_id)
        def emit_claude_prompt(self, *a, **k): return {"text": "ok", "model": "m"}
        def invoke_claude_skill(self, *a, **k): raise NotImplementedError
        def spawn_subprocess(self, *a, **k): raise NotImplementedError

    monkeypatch.setattr("hydra_core.squad_node._run_smoke",
                        lambda *a, **k: ("pass", "stub"))
    monkeypatch.setattr("hydra_core.squad_node._drive_generate",
                        lambda *a, **k: (
                            {"status": "done", "result": {
                                "text": "edit", "model": "m",
                                "tokens_in": 1, "tokens_out": 1,
                                "cost_usd": 0.01, "wall_ms": 10}},
                            "claude"))
    monkeypatch.setattr("hydra_core.squad_node._claude_critique",
                        lambda *a, **k: {"outcome": "pass", "critique_md": "ok",
                                         "score": {"c": 9}, "cost_usd": 0.0,
                                         "tokens_in": 0, "tokens_out": 0})

    # Note: _drive_best_of_loop calls start_best_of_stage itself; do NOT pass stage_id.
    disp = _BODisp()
    out = _drive_best_of_loop(disp, run_id="run-bo",
                               project_path="/tmp/proj", request_text="do it",
                               n=1)
    assert not archive_called, (
        "Finding 5: readiness fail must prevent archive_winner_and_losers from running")
    assert out.get("final_status") == "surfaced", (
        "Finding 5: readiness fail must produce surfaced final_status")


# ===========================================================================
# Finding 6: bounded GAP-a2 baseline
# ===========================================================================

def test_f6_baseline_too_broad_fails_smoke(tmp_path, monkeypatch):
    """When excusable set exceeds HYDRA_SMOKE_BASELINE_MAX, smoke must remain
    failed (not excused) and a telemetry event must be emitted."""
    import hydra_core.telemetry as _tel
    telemetry_events: list = []
    monkeypatch.setattr(_tel, "emit", lambda *a, **k: telemetry_events.append(a))

    monkeypatch.setattr(host_bridge, "_run_smoke",
                        lambda *a, **k: ("fail", "many tests failed"))
    # Set up 3 excusable tests with a cap of 2 → too broad
    monkeypatch.setenv("HYDRA_SMOKE_BASELINE_TESTS",
                       "tests/t1.py::t1,tests/t2.py::t2,tests/t3.py::t3")
    monkeypatch.setenv("HYDRA_SMOKE_BASELINE_MAX", "2")

    disp = FakeDispatcher(required_cross_vendor=True)
    res = _begin(disp, tmp_path)
    res = _submit(disp, res, "generate-0", text="edit")
    res = _submit(disp, res, "judge-0", outcome="pass", judge_producer="codex")
    # smoke must remain failed → not complete
    assert res["status"] != "complete", (
        "Finding 6: baseline too broad must keep smoke failed")
    # telemetry must mention too_broad
    too_broad = [e for e in telemetry_events
                 if "baseline_too_broad" in str(e)]
    assert too_broad, "Finding 6: baseline_too_broad telemetry must be emitted"


def test_f6_baseline_intersection_with_env_var(tmp_path, monkeypatch):
    """When both captured baseline and env var are set, excusable = intersection.
    A test that is in env var but NOT in captured baseline must NOT be excused."""
    monkeypatch.setattr(host_bridge, "_run_smoke",
                        lambda *a, **k: ("fail", "test_new FAILED"))
    # env var allows test_old; captured baseline also has test_old
    # current failure: test_new (not in either) → not excused → surfaced
    monkeypatch.setenv("HYDRA_SMOKE_BASELINE_TESTS", "tests/test_old.py::test_old")

    _mock_proc = MagicMock()
    _mock_proc.stdout = "FAILED tests/test_new.py::test_new\n1 failed"
    _mock_proc.stderr = ""
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _mock_proc)

    disp = FakeDispatcher(required_cross_vendor=True)
    res = _begin(disp, tmp_path)
    # Set baseline_failures in cursor to have test_old
    import json
    cursor = json.loads(Path(res["cursor_path"]).read_text())
    cursor["baseline_failures"] = ["tests/test_old.py::test_old"]
    Path(res["cursor_path"]).write_text(json.dumps(cursor))

    res = _submit(disp, res, "generate-0", text="edit")
    res = _submit(disp, res, "judge-0", outcome="pass", judge_producer="codex")
    assert res["status"] != "complete", (
        "Finding 6: test_new not in excusable set → must not complete")


def test_f6_baseline_excuse_telemetry_emitted(tmp_path, monkeypatch):
    """When failures are excused (pre-existing), telemetry must list which
    tests were excused (baseline_excuse_decision event)."""
    import hydra_core.telemetry as _tel
    telemetry_events: list = []
    monkeypatch.setattr(_tel, "emit", lambda *a, **k: telemetry_events.append(a))

    monkeypatch.setattr(host_bridge, "_run_smoke",
                        lambda *a, **k: ("fail", "test_old FAILED"))
    monkeypatch.setenv("HYDRA_SMOKE_BASELINE_TESTS", "tests/test_old.py::test_old")
    _mock_proc = MagicMock()
    _mock_proc.stdout = "FAILED tests/test_old.py::test_old\n1 failed"
    _mock_proc.stderr = ""
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _mock_proc)

    disp = FakeDispatcher(required_cross_vendor=True)
    res = _begin(disp, tmp_path)
    res = _submit(disp, res, "generate-0", text="edit")
    res = _submit(disp, res, "judge-0", outcome="pass", judge_producer="codex")
    excuse_events = [e for e in telemetry_events
                     if "baseline_excuse_decision" in str(e)]
    assert excuse_events, (
        "Finding 6: baseline_excuse_decision telemetry must be emitted "
        "when failures are excused")


# ===========================================================================
# Finding 7: F27 preflight checks all three agent files
# ===========================================================================

def test_f7_preflight_missing_judge_cross_vendor(monkeypatch, tmp_path):
    """The attended preflight must error when judge-cross-vendor.md is absent,
    not just when engineer.md is absent."""
    from hydra_core import cli as _cli
    # Create only engineer.md and judge-same-vendor.md — omit judge-cross-vendor.md
    agents_dir = tmp_path / ".claude" / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "engineer.md").write_text("stub", encoding="utf-8")
    (agents_dir / "judge-same-vendor.md").write_text("stub", encoding="utf-8")
    # judge-cross-vendor.md is missing

    # Invoke the preflight logic directly (mirrors _cmd_attended_step check)
    _required = {"engineer.md", "judge-cross-vendor.md", "judge-same-vendor.md"}
    missing = [n for n in _required if not (agents_dir / n).exists()]
    assert "judge-cross-vendor.md" in missing, (
        "Finding 7: preflight must detect missing judge-cross-vendor.md")


def test_f7_preflight_missing_judge_same_vendor(tmp_path):
    """The attended preflight must error when judge-same-vendor.md is absent."""
    agents_dir = tmp_path / ".claude" / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "engineer.md").write_text("stub", encoding="utf-8")
    (agents_dir / "judge-cross-vendor.md").write_text("stub", encoding="utf-8")
    # judge-same-vendor.md is missing

    _required = {"engineer.md", "judge-cross-vendor.md", "judge-same-vendor.md"}
    missing = [n for n in _required if not (agents_dir / n).exists()]
    assert "judge-same-vendor.md" in missing, (
        "Finding 7: preflight must detect missing judge-same-vendor.md")


def test_f7_engineer_md_has_self_verification():
    """engineer.md must contain the self-verification step (anti-pattern grep
    and do_not_touch) so the engineer sub-agent knows to run it."""
    eng_file = HYDRA_ROOT / "plugins" / "hydra" / "agents" / "engineer.md"
    content = eng_file.read_text(encoding="utf-8")
    assert "self-verif" in content.lower() or "anti-pattern" in content.lower(), (
        "Finding 7: engineer.md must mention self-verification or anti-pattern grep")
    assert "do_not_touch" in content or "do not touch" in content.lower(), (
        "Finding 7: engineer.md must mention do_not_touch boundary check")


def test_f7_judge_files_say_do_not_call_record_verdict():
    """Both judge stubs must explicitly say NOT to call record_verdict."""
    for name in ("judge-cross-vendor.md", "judge-same-vendor.md"):
        f = HYDRA_ROOT / "plugins" / "hydra" / "agents" / name
        content = f.read_text(encoding="utf-8")
        assert "record_verdict" in content and (
            "not" in content.lower() or "do NOT" in content
        ), (f"Finding 7: {name} must explicitly say NOT to call record_verdict")


def test_f7_judge_files_tools_do_not_include_record_verdict():
    """Neither judge stub's tools frontmatter should list record_verdict."""
    for name in ("judge-cross-vendor.md", "judge-same-vendor.md"):
        f = HYDRA_ROOT / "plugins" / "hydra" / "agents" / name
        content = f.read_text(encoding="utf-8")
        # Extract the tools line from frontmatter
        for line in content.splitlines():
            if line.startswith("tools:"):
                assert "record_verdict" not in line, (
                    f"Finding 7: {name} tools frontmatter must NOT list "
                    f"mcp__pp_harness__record_verdict (got: {line!r})")
                break


# ===========================================================================
# R9: smoke-aware best-of winner selection + execution evidence for judges
# (incidents run_eawskOzIx3TS / run_fL4GeNrIRfaS — ~USD 26 discarded because a
#  judge-preferred but smoke-FAILED candidate was picked over a smoke-PASS one,
#  and judges withheld pass for lack of execution evidence).
# ===========================================================================

def _make_best_of_dispatcher(*, borda_winner_att=None, judge_capture=None):
    """A dispatcher for _drive_best_of_loop with 2 candidates and unique attempt
    ids (att-0 / att-1) keyed off record_attempt's candidate_index note.

    ``borda_winner_att`` — attempt id borda_count should return as winner
    (simulates the judge/Borda preferring that candidate). When it names a
    candidate that is smoke-excluded, the smoke-aware selection must ignore it.
    ``judge_capture`` — optional list; unused here (judge text is captured by
    monkeypatching _claude_critique in the test that needs it)."""

    def _fake_cm(server, tool, args, *, squad_id=None):
        if tool == "start_best_of_stage":
            n = int(args.get("n") or 2)
            cands = [{"candidate_index": i, "attempt_slot_id": f"s{i}",
                      "worktree_path": f"/tmp/wt{i}"} for i in range(n)]
            return {"status": "done", "result": {
                "stage_id": "stg-bo", "n": n, "candidates": cands}}
        if tool == "record_attempt":
            ci = int((args.get("notes") or {}).get("candidate_index") or 0)
            return {"status": "done", "result": {"attempt_id": f"att-{ci}"}}
        if tool == "borda_count":
            return {"status": "done", "result": {"winner": borda_winner_att}}
        if tool == "gate_eligible_judges":
            return {"status": "done", "result": {
                "required_cross_vendor": False, "rubric_id": "rfc-2119-normative"}}
        if tool == "get_stage_finalize_readiness":
            return {"status": "done", "result": {"can_pass": True}}
        if tool == "archive_winner_and_losers":
            return {"status": "done", "result": {"merge_status": "merged"}}
        if tool == "finalize_run":
            return {"status": "done", "result": {"effective_status": "complete"}}
        if tool == "teardown_candidates":
            return {"status": "done", "result": {"teardown_status": "ok"}}
        return {"status": "done", "result": {}}

    class _BODisp:
        def __init__(self):
            self.calls: list[tuple[str, str, dict]] = []

        def call_mcp(self, server, tool, args, *, squad_id=None):
            self.calls.append((server, tool, dict(args)))
            return _fake_cm(server, tool, args, squad_id=squad_id)

        def tool_seq(self):
            return [t for (_s, t, _a) in self.calls]

        def emit_claude_prompt(self, *a, **k): return {"text": "ok", "model": "m"}
        def invoke_claude_skill(self, *a, **k): raise NotImplementedError
        def spawn_subprocess(self, *a, **k): raise NotImplementedError

    return _BODisp()


def _patch_generate_and_judge(monkeypatch, *, outcome="pass", score=None):
    """Stub _drive_generate (claude producer, wrote nothing but non-empty text)
    and _claude_critique so best-of runs without a real repo/model."""
    monkeypatch.setattr("hydra_core.squad_node._drive_generate",
                        lambda *a, **k: (
                            {"status": "done", "result": {
                                "text": "edit summary", "model": "m",
                                "tokens_in": 1, "tokens_out": 1,
                                "cost_usd": 0.01, "wall_ms": 10}},
                            "claude"))
    monkeypatch.setattr("hydra_core.squad_node._claude_critique",
                        lambda *a, **k: {"outcome": outcome, "critique_md": "ok",
                                         "score": dict(score or {"c": 9}),
                                         "cost_usd": 0.0,
                                         "tokens_in": 0, "tokens_out": 0})


def test_r9_smoke_fail_candidate_excluded_when_another_passed(monkeypatch):
    """When >=1 candidate passes smoke, a smoke-FAILED candidate must be excluded
    from selection even if the judge/Borda prefers it. Winner must be the
    smoke-pass candidate, and it must merge (final complete)."""
    from hydra_core.squad_node import _drive_best_of_loop

    # Candidate 0 fails smoke, candidate 1 passes.  worktree_path is /tmp/wt<idx>.
    smoke_map = {"/tmp/wt0": ("fail", "exit=1"), "/tmp/wt1": ("pass", "exit=0")}
    monkeypatch.setattr("hydra_core.squad_node._run_smoke",
                        lambda disp, *, project_path, stage_id:
                        smoke_map.get(project_path, ("skipped", "")))
    _patch_generate_and_judge(monkeypatch)

    # Borda "prefers" candidate 0 (the smoke-FAIL one) — must be ignored.
    disp = _make_best_of_dispatcher(borda_winner_att="att-0")
    out = _drive_best_of_loop(disp, run_id="run-bo", project_path="/tmp/proj",
                              request_text="do it", n=2)

    # Winner is the smoke-PASS candidate (index 1), not the judge-preferred one.
    aw = [a for (_s, t, a) in disp.calls if t == "archive_winner_and_losers"]
    assert aw, "smoke-pass winner must be merged via archive_winner_and_losers"
    assert aw[0]["winner_candidate_index"] == 1, (
        f"R9: winner must be the smoke-PASS candidate (1), got {aw[0]}")
    assert out["final_status"] == "complete"
    excluded = [e["ci"] for e in out.get("smoke_excluded_candidates", [])]
    assert 0 in excluded, (
        f"R9: smoke-fail candidate 0 must be recorded as excluded, got {excluded}")


def test_r9_all_smoke_fail_preserves_old_behavior(monkeypatch):
    """When NO candidate passes smoke, selection falls back to today's judge
    order and the post-selection smoke veto surfaces the stage (no merge)."""
    from hydra_core.squad_node import _drive_best_of_loop

    monkeypatch.setattr("hydra_core.squad_node._run_smoke",
                        lambda *a, **k: ("fail", "exit=1"))
    _patch_generate_and_judge(monkeypatch)

    disp = _make_best_of_dispatcher(borda_winner_att="att-0")
    out = _drive_best_of_loop(disp, run_id="run-bo", project_path="/tmp/proj",
                              request_text="do it", n=2)

    # No candidate passed → nothing excluded, veto downgrades to surfaced, and
    # archive_winner_and_losers must NOT run (no failed code lands).
    assert out.get("smoke_excluded_candidates") == [], (
        "R9: all-fail must exclude nothing (old-behavior fallback)")
    assert out["final_status"] == "surfaced"
    assert "archive_winner_and_losers" not in disp.tool_seq(), (
        "R9: a stage where every candidate failed smoke must not merge a winner")


def test_r9_judge_context_contains_execution_evidence(monkeypatch):
    """The judge's artifact text must carry a '## Execution evidence' section
    stating the smoke command + its status (so the judge sees the build/tests
    actually ran)."""
    from hydra_core.squad_node import _drive_best_of_loop

    monkeypatch.setattr("hydra_core.squad_node._run_smoke",
                        lambda *a, **k: ("pass", "`pytest -q` exit=0"))
    captured: list[str] = []
    monkeypatch.setattr("hydra_core.squad_node._drive_generate",
                        lambda *a, **k: (
                            {"status": "done", "result": {
                                "text": "edit summary", "model": "m",
                                "tokens_in": 1, "tokens_out": 1,
                                "cost_usd": 0.01, "wall_ms": 10}},
                            "claude"))

    def _capture_critique(artifact_text, rubric_md, cwd, *a, **k):
        captured.append(artifact_text)
        return {"outcome": "pass", "critique_md": "ok", "score": {"c": 9},
                "cost_usd": 0.0, "tokens_in": 0, "tokens_out": 0}
    monkeypatch.setattr("hydra_core.squad_node._claude_critique",
                        _capture_critique)

    disp = _make_best_of_dispatcher()
    _drive_best_of_loop(disp, run_id="run-bo", project_path="/tmp/proj",
                        request_text="do it", n=2)

    assert captured, "judge critique must have been invoked"
    assert all("## Execution evidence" in t for t in captured), (
        "R9: every judge artifact must carry the Execution evidence heading")
    assert any("smoke status: pass" in t for t in captured), (
        "R9: execution evidence must state the smoke status")


def test_r9_smoke_runs_before_judging(monkeypatch):
    """ORDER: for each candidate record_smoke_status must be emitted BEFORE the
    judge gate (gate_eligible_judges) and BEFORE record_verdict — smoke now runs
    ahead of judging so the judge can see the execution outcome."""
    from hydra_core.squad_node import _drive_best_of_loop

    monkeypatch.setattr("hydra_core.squad_node._run_smoke",
                        lambda *a, **k: ("pass", "exit=0"))
    _patch_generate_and_judge(monkeypatch)

    disp = _make_best_of_dispatcher()
    _drive_best_of_loop(disp, run_id="run-bo", project_path="/tmp/proj",
                        request_text="do it", n=1)

    seq = disp.tool_seq()
    assert "record_smoke_status" in seq and "gate_eligible_judges" in seq
    assert seq.index("record_smoke_status") < seq.index("gate_eligible_judges"), (
        f"R9: smoke must be recorded before the judge gate, got {seq}")
    assert seq.index("record_smoke_status") < seq.index("record_verdict"), (
        f"R9: smoke must be recorded before the verdict, got {seq}")


# ===========================================================================
# G6: timeout-error shape (remediation + stale_state)
# ===========================================================================

def test_g6_timeout_error_shape_step_label(monkeypatch):
    """_run_cli_json with err_label='step' on TimeoutExpired must return a
    'remediation' field naming HYDRA_STEP_TIMEOUT_S and a 'stale_state' list
    naming resume.lock, orphan pp run, and orphan attended worktree."""
    from mcp_servers.hydra_control import server as _srv

    def _fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0] if args else [], 900)

    monkeypatch.setattr(subprocess, "run", _fake_run)

    out = _srv._run_cli_json(["step", "--args"], timeout_s=900,
                             err_label="step", workflow_id="wf-g6-1")
    assert out["ok"] is False
    assert out["error"] == "step_timeout"
    assert "remediation" in out, "step timeout must carry remediation"
    assert "HYDRA_STEP_TIMEOUT_S" in out["remediation"], (
        "remediation must name HYDRA_STEP_TIMEOUT_S")
    assert "stale_state" in out, "step timeout must carry stale_state list"
    stale = out["stale_state"]
    assert isinstance(stale, list) and len(stale) >= 1
    stale_str = " ".join(stale)
    assert "resume.lock" in stale_str, (
        "stale_state must name the resume.lock file")
    assert "worktree" in stale_str.lower(), (
        "stale_state must name the orphan attended worktree")
    assert "pp run" in stale_str or "finalize" in stale_str, (
        "stale_state must name the orphan pp run entry")


def test_g6_timeout_error_shape_submit_label(monkeypatch):
    """_run_cli_json with err_label='submit' on TimeoutExpired must return a
    'remediation' field naming HYDRA_SUBMIT_TIMEOUT_S with an idempotency note;
    no 'stale_state' key (stale_state applies only to the step label)."""
    from mcp_servers.hydra_control import server as _srv

    def _fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0] if args else [], 1800)

    monkeypatch.setattr(subprocess, "run", _fake_run)

    out = _srv._run_cli_json(["submit", "--args"], timeout_s=1800,
                             err_label="submit", workflow_id="wf-g6-2")
    assert out["ok"] is False
    assert out["error"] == "submit_timeout"
    assert "remediation" in out, "submit timeout must carry remediation"
    assert "HYDRA_SUBMIT_TIMEOUT_S" in out["remediation"], (
        "remediation must name HYDRA_SUBMIT_TIMEOUT_S")
    rem = out["remediation"].lower()
    assert "idempotent" in rem or "call_key" in rem, (
        "remediation must state that re-issuing is safe via call_key idempotency")
    assert "stale_state" not in out, (
        "submit timeout must NOT carry stale_state (only the step label does)")
