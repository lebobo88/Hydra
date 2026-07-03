"""Tests for the attended (host-bridged) engineering driver.

The driver re-sequences the pair-programmer stage protocol as an explicit,
resumable step-state-machine: begin_stage pauses for the visible `engineer`
subagent, the host submits its result, the driver records the attempt and pauses
for the judge subagent, the host submits the verdict, and the driver finalizes.

These tests pin: (1) the pause/resume round-trip + host_action shape, (2) pp
ledger calls happen exactly once and under squad_id="engineering" (RBAC), (3)
judge routing follows gate_eligible_judges, (4) a duplicate/stale submit never
re-applies (exactly-once), and (5) the complete vs surfaced finalize.
"""
from __future__ import annotations

import subprocess

import pytest

from hydra_core import host_bridge


def _git(args, cwd):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                          text=True, check=False)


def _init_repo(path):
    _git(["init"], path)
    _git(["config", "user.email", "t@t.test"], path)
    _git(["config", "user.name", "Test"], path)
    _git(["config", "commit.gpgsign", "false"], path)
    (path / "README.md").write_text("base\n", encoding="utf-8")
    _git(["add", "-A"], path)
    _git(["commit", "-m", "base", "--no-verify"], path)


class FakeDispatcher:
    """Records every call_mcp invocation and returns canned pp envelopes."""

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
        # archive_artifact / record_verdict / record_smoke_status / finalize_stage
        return {"status": "done", "result": {}}

    def count(self, tool: str) -> int:
        return sum(1 for _s, t, _a, _q in self.calls if t == tool)

    def tools(self) -> list[str]:
        return [t for _s, t, _a, _q in self.calls]


@pytest.fixture(autouse=True)
def _smoke_passes(monkeypatch):
    """Force a passing smoke so the happy path can finalize 'complete' without a
    real build/test command in the temp dir."""
    monkeypatch.setattr(host_bridge, "_run_smoke",
                        lambda *a, **k: ("pass", "fake smoke pass"))


def _begin(disp, tmp_path):
    return host_bridge.begin_stage(
        disp, workflow_id="wf-1", run_id="run-1",
        project_path=str(tmp_path), request_text="implement the thing",
        project_root=str(tmp_path))


def test_begin_pauses_for_engineer(tmp_path):
    disp = FakeDispatcher()
    res = _begin(disp, tmp_path)
    assert res["status"] == "awaiting_host"
    assert res["state"] == "await_generate"
    assert res["host_action"]["agent_type"] == "engineer"
    assert res["host_action"]["call_key"] == "generate-0"
    assert res["host_action"]["cwd"] == str(tmp_path)
    # start_stage ran under the engineering RBAC scope; nothing else yet.
    assert disp.count("start_stage") == 1
    assert ("pp_harness", "start_stage", {"run_id": "run-1", "kind": "code",
            "gate_type": "code"}, "engineering") in disp.calls
    assert disp.count("record_attempt") == 0


def test_generate_then_judge_then_finalize_complete(tmp_path):
    disp = FakeDispatcher(required_cross_vendor=True)
    res = _begin(disp, tmp_path)
    cfile = res["cursor_path"]

    # Submit the engineer result → driver records the attempt and pauses for the
    # cross-vendor judge.
    res = host_bridge.submit_host_result(
        disp, cursor_file=cfile, call_key="generate-0",
        result={"text": "edited foo.py", "cost_usd": 0.10,
                "tokens_in": 100, "tokens_out": 50, "model": "claude-opus-4-8"})
    assert res["status"] == "awaiting_host"
    assert res["state"] == "await_judge"
    assert res["host_action"]["agent_type"] == "judge-cross-vendor"
    assert res["host_action"]["call_key"] == "judge-0"
    assert disp.count("record_attempt") == 1
    assert disp.count("gate_eligible_judges") == 1

    # Submit the judge verdict → driver records verdict, runs smoke, finalizes.
    res = host_bridge.submit_host_result(
        disp, cursor_file=cfile, call_key="judge-0",
        result={"outcome": "pass", "critique_md": "looks good",
                "judge_producer": "codex", "cost_usd": 0.05})
    assert res["status"] == "complete"
    assert res["final_status"] == "complete"
    assert disp.count("record_verdict") == 1
    assert disp.count("record_smoke_status") == 1
    assert disp.count("finalize_stage") == 1
    assert disp.count("finalize_run") == 1
    # Cost accrued across generate + judge for the caller to charge on HydraState.
    assert res["cost_usd"] == pytest.approx(0.15)
    # Every pp call ran under the engineering RBAC scope.
    assert all(q == "engineering" for _s, _t, _a, q in disp.calls)


def test_record_attempt_notes_only_allowed_keys(tmp_path):
    """Regression: the pp daemon's record_attempt rejects unrecognized `notes`
    keys (zod strict). The attended driver must send only the allowed shape —
    `{candidate_index}` — never a custom marker like `attended`. (A live smoke
    caught this: an `attended` key made record_attempt fail → no attempt_id →
    no verdict/smoke → surfaced.)"""
    disp = FakeDispatcher()
    res = _begin(disp, tmp_path)
    host_bridge.submit_host_result(
        disp, cursor_file=res["cursor_path"], call_key="generate-0",
        result={"text": "edited foo.py"})
    notes = [a.get("notes") for _s, t, a, _q in disp.calls if t == "record_attempt"]
    assert notes, "expected a record_attempt call"
    for n in notes:
        assert set(n.keys()) <= {"candidate_index"}, (
            f"record_attempt notes must not carry unrecognized keys: {n}")


def test_same_vendor_judge_routing(tmp_path):
    """When gate_eligible_judges does NOT require cross-vendor, the host is told
    to spawn the same-vendor judge."""
    disp = FakeDispatcher(required_cross_vendor=False)
    res = _begin(disp, tmp_path)
    res = host_bridge.submit_host_result(
        disp, cursor_file=res["cursor_path"], call_key="generate-0",
        result={"text": "edited foo.py"})
    assert res["host_action"]["agent_type"] == "judge-same-vendor"


def test_duplicate_submit_is_exactly_once(tmp_path):
    disp = FakeDispatcher()
    res = _begin(disp, tmp_path)
    cfile = res["cursor_path"]
    host_bridge.submit_host_result(
        disp, cursor_file=cfile, call_key="generate-0",
        result={"text": "edited foo.py"})
    res = host_bridge.submit_host_result(
        disp, cursor_file=cfile, call_key="judge-0",
        result={"outcome": "pass", "judge_producer": "codex"})
    assert res["status"] == "complete"
    finalize_runs = disp.count("finalize_run")

    # A retried judge submit after the run is terminal must NOT re-finalize.
    res2 = host_bridge.submit_host_result(
        disp, cursor_file=cfile, call_key="judge-0",
        result={"outcome": "pass", "judge_producer": "codex"})
    assert res2["status"] == "complete"
    assert disp.count("finalize_run") == finalize_runs  # no extra call
    assert disp.count("record_verdict") == 1


def test_stale_call_key_is_ignored(tmp_path):
    """A submit whose call_key doesn't match the awaited action is ignored — it
    never re-applies, so the pp ledger isn't double-written."""
    disp = FakeDispatcher()
    res = _begin(disp, tmp_path)
    cfile = res["cursor_path"]
    # Awaiting generate-0, but submit judge-0 (wrong) → ignored.
    res = host_bridge.submit_host_result(
        disp, cursor_file=cfile, call_key="judge-0", result={"outcome": "pass"})
    assert res["status"] == "awaiting_host"
    assert res["state"] == "await_generate"
    assert "ignored" in res
    assert disp.count("record_attempt") == 0


def test_generate_failure_surfaces_without_judge(tmp_path):
    """An empty generate (no text, no file changes) is a generate failure: the
    driver records a failed attempt and finalizes surfaced WITHOUT a judge."""
    disp = FakeDispatcher()
    res = _begin(disp, tmp_path)
    res = host_bridge.submit_host_result(
        disp, cursor_file=res["cursor_path"], call_key="generate-0",
        result={"text": ""})  # no output, tmp_path is not a git repo → no changes
    assert res["status"] == "surfaced"
    assert res["final_status"] == "surfaced"
    assert disp.count("gate_eligible_judges") == 0
    assert disp.count("record_verdict") == 0
    assert disp.count("finalize_run") == 1


def test_finalize_downgrade_is_honoured(tmp_path):
    """A pp finalize_run downgrade (downgraded=True) is never laundered into
    complete."""
    disp = FakeDispatcher(finalize_status="surfaced", downgraded=True)
    res = _begin(disp, tmp_path)
    cfile = res["cursor_path"]
    host_bridge.submit_host_result(
        disp, cursor_file=cfile, call_key="generate-0",
        result={"text": "edited foo.py"})
    res = host_bridge.submit_host_result(
        disp, cursor_file=cfile, call_key="judge-0",
        result={"outcome": "pass", "judge_producer": "codex"})
    assert res["status"] == "surfaced"
    assert res["final_status"] == "surfaced"


def test_worktree_isolation_and_merge_back(tmp_path):
    """In a git repo, the engineer is isolated into a `.harness/worktrees/`
    worktree (hook-allowed) and a passing finalize merges the change back into
    the repo. The worktree is removed afterward."""
    _init_repo(tmp_path)
    disp = FakeDispatcher()
    res = host_bridge.begin_stage(
        disp, workflow_id="wf-wt", run_id="run-wt",
        project_path=str(tmp_path), request_text="add a feature file",
        project_root=str(tmp_path), isolate=True)
    action = res["host_action"]
    assert action["isolated_worktree"] is True
    wt = action["cwd"]
    assert "worktrees" in wt.replace("\\", "/")
    assert wt != str(tmp_path)

    # Simulate the engineer writing a NEW file inside the worktree.
    from pathlib import Path
    Path(wt, "feature.py").write_text("print('hello')\n", encoding="utf-8")

    res = host_bridge.submit_host_result(
        disp, cursor_file=res["cursor_path"], call_key="generate-0",
        result={"text": "added feature.py"})
    assert res["state"] == "await_judge"

    res = host_bridge.submit_host_result(
        disp, cursor_file=res["cursor_path"], call_key="judge-0",
        result={"outcome": "pass", "judge_producer": "codex"})
    assert res["status"] == "complete"
    assert res["merge"]["merged"] is True
    # The change landed in the repo working tree...
    assert (tmp_path / "feature.py").exists()
    # ...and the worktree was cleaned up.
    assert not Path(wt).exists()


def test_worktree_discarded_on_surface(tmp_path):
    """When the stage surfaces (judge revise), the worktree is discarded and the
    repo is left untouched."""
    _init_repo(tmp_path)
    disp = FakeDispatcher()
    res = host_bridge.begin_stage(
        disp, workflow_id="wf-wt2", run_id="run-wt2",
        project_path=str(tmp_path), request_text="add a file",
        project_root=str(tmp_path), isolate=True)
    wt = res["host_action"]["cwd"]
    from pathlib import Path
    Path(wt, "feature.py").write_text("x\n", encoding="utf-8")
    res = host_bridge.submit_host_result(
        disp, cursor_file=res["cursor_path"], call_key="generate-0",
        result={"text": "added feature.py"})
    res = host_bridge.submit_host_result(
        disp, cursor_file=res["cursor_path"], call_key="judge-0",
        result={"outcome": "revise", "judge_producer": "codex"})
    assert res["status"] == "surfaced"
    # Nothing merged; repo working tree clean of the change.
    assert not (tmp_path / "feature.py").exists()
    assert not Path(wt).exists()


def test_abort_releases_lock(tmp_path):
    disp = FakeDispatcher()
    res = _begin(disp, tmp_path)
    res = host_bridge.abort_stage(disp, cursor_file=res["cursor_path"],
                                  reason="operator_cancel")
    assert res["status"] == "aborted"
    # finalize_run(aborted) was issued to release the pp lock.
    aborts = [a for _s, t, a, _q in disp.calls
              if t == "finalize_run" and a.get("status") == "aborted"]
    assert len(aborts) == 1


# --------------------------------------------------------------------------- #
# Non-engineering squad attended path (claude-skill / agent-impersonation)     #
# --------------------------------------------------------------------------- #
# These squads produce documents, not engine code, so the attended cursor is a
# lightweight one-hop machine: begin_squad_stage pauses for the pack lead agent,
# submit drives straight to `complete`. No pp ledger calls, no worktree.

def test_squad_stage_pauses_for_pack_lead(tmp_path):
    disp = FakeDispatcher()
    res = host_bridge.begin_squad_stage(
        workflow_id="wf-1", task_id="task-7", squad_slug="marketing-strategy",
        entrypoint="claude-skill", lead_agent="brand-strategist",
        pack_cwd=str(tmp_path), request_text="draft the brand brief",
        project_root=str(tmp_path))
    assert res["status"] == "awaiting_host"
    assert res["state"] == "await_squad_agent"
    assert res["task_id"] == "task-7"
    # run_id mirrors task_id so `submit-host-result --run-id <task_id>` resolves
    # the cursor for non-engineering tasks.
    assert res["run_id"] == "task-7"
    assert res["host_action"]["agent_type"] == "brand-strategist"
    assert res["host_action"]["cwd"] == str(tmp_path)
    assert res["host_action"]["call_key"] == "squad-task-7-0"
    # The squad path never touches the engineering pp ledger.
    assert disp.calls == []


def test_squad_stage_submit_completes_and_accrues_spend(tmp_path):
    disp = FakeDispatcher()
    res = host_bridge.begin_squad_stage(
        workflow_id="wf-1", task_id="task-7", squad_slug="marketing-strategy",
        entrypoint="claude-skill", lead_agent="brand-strategist",
        pack_cwd=str(tmp_path), request_text="draft the brand brief",
        project_root=str(tmp_path))
    cfile = res["cursor_path"]

    res = host_bridge.submit_host_result(
        disp, cursor_file=cfile, call_key="squad-task-7-0",
        result={"text": "# Brand Brief\n...", "cost_usd": 0.08,
                "tokens_in": 200, "tokens_out": 400})
    assert res["status"] == "complete"
    assert res["final_status"] == "complete"
    # task_id flows back so the CLI charges budget and marks the task complete.
    assert res["task_id"] == "task-7"
    assert res["cost_usd"] == pytest.approx(0.08)
    assert res["tokens_in"] == 200
    assert res["tokens_out"] == 400
    # Still no pp calls — no double-judging, no engineering-scope RBAC use.
    assert disp.calls == []


def test_squad_stage_duplicate_submit_is_exactly_once(tmp_path):
    """A retried submit on a terminal squad cursor returns the terminal result
    without re-accruing spend."""
    disp = FakeDispatcher()
    res = host_bridge.begin_squad_stage(
        workflow_id="wf-1", task_id="task-9", squad_slug="executive",
        entrypoint="agent-impersonation", lead_agent="ceo",
        pack_cwd=str(tmp_path), request_text="frame the decision",
        project_root=str(tmp_path))
    cfile = res["cursor_path"]
    r1 = host_bridge.submit_host_result(
        disp, cursor_file=cfile, call_key="squad-task-9-0",
        result={"text": "minutes", "cost_usd": 0.05})
    assert r1["status"] == "complete"
    assert r1["cost_usd"] == pytest.approx(0.05)
    # Duplicate submit (same call_key, already terminal) → no re-charge.
    r2 = host_bridge.submit_host_result(
        disp, cursor_file=cfile, call_key="squad-task-9-0",
        result={"text": "minutes", "cost_usd": 0.05})
    assert r2["status"] == "complete"
    assert r2["cost_usd"] == pytest.approx(0.05)
