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
    """When the stage surfaces (judge revise × Reflexion x1), the worktree is
    discarded and the repo is left untouched.

    GAP-f: the first revise now triggers Reflexion x1 (back to await_generate
    for generate-1). The worktree must persist through generate-1 and only be
    discarded once the stage actually finalizes surfaced after the second revise.
    """
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
    # First revise → GAP-f Reflexion x1 transition (back to await_generate).
    res = host_bridge.submit_host_result(
        disp, cursor_file=res["cursor_path"], call_key="judge-0",
        result={"outcome": "revise", "judge_producer": "codex"})
    assert res["state"] == "await_generate", "GAP-f: first revise should trigger Reflexion"
    assert Path(wt).exists(), "worktree must survive through Reflexion"
    # Reflexion generate-1 and second revise → finalize surfaced.
    res = host_bridge.submit_host_result(
        disp, cursor_file=res["cursor_path"], call_key="generate-1",
        result={"text": "revision attempt"})
    res = host_bridge.submit_host_result(
        disp, cursor_file=res["cursor_path"], call_key="judge-1",
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


# --------------------------------------------------------------------------- #
# Fix-1b: idempotency markers (verdict_recorded_for / smoke_result_for)       #
# --------------------------------------------------------------------------- #

def test_verdict_idempotency_skips_record_verdict_on_retry(tmp_path, monkeypatch):
    """If verdict_recorded_for is set on the cursor for the current call_key, a
    retried submit with the same call_key must NOT call record_verdict again.

    Scenario: submit timeout kills mid-_run_smoke after record_verdict succeeded
    but before the outer save_cursor.  The cursor still has state=await_judge with
    the verdict_recorded_for marker persisted by the mid-function save.  A retry
    with call_key='judge-0' must honour the marker and skip record_verdict.
    """
    disp = FakeDispatcher(required_cross_vendor=True)
    res = _begin(disp, tmp_path)
    cfile = res["cursor_path"]

    # Advance to await_judge state.
    host_bridge.submit_host_result(
        disp, cursor_file=cfile, call_key="generate-0",
        result={"text": "edited foo.py"})
    assert disp.count("record_attempt") == 1

    # Simulate a mid-function save: set verdict_recorded_for on the cursor manually
    # (as if the first judge submit wrote it before timing out).
    cursor = host_bridge.load_cursor(cfile)
    cursor["verdict_recorded_for"] = "judge-0"
    host_bridge.save_cursor(cfile, cursor)

    verdict_calls_before = disp.count("record_verdict")

    # Retry judge submit — must NOT re-invoke record_verdict.
    res2 = host_bridge.submit_host_result(
        disp, cursor_file=cfile, call_key="judge-0",
        result={"outcome": "pass", "critique_md": "looks good",
                "judge_producer": "codex", "cost_usd": 0.05})
    assert res2["status"] == "complete"
    assert disp.count("record_verdict") == verdict_calls_before, (
        "record_verdict must not be called again when verdict_recorded_for matches"
    )
    # The stage must still finalize correctly.
    assert disp.count("finalize_run") == 1


# --------------------------------------------------------------------------- #
# Phase 7b: hydra_context_block injection into engineer prompt                 #
# --------------------------------------------------------------------------- #

def test_begin_stage_hydra_context_block_prepended(tmp_path):
    """When hydra_context_block is supplied, the engineer prompt STARTS with
    that block (followed by a blank line) — the request body comes after."""
    disp = FakeDispatcher()
    block = "## Hydra context\nworkflow_id: wf-1"
    res = host_bridge.begin_stage(
        disp, workflow_id="wf-1", run_id="run-1",
        project_path=str(tmp_path), request_text="implement the thing",
        project_root=str(tmp_path),
        hydra_context_block=block)
    prompt = res["host_action"]["prompt"]
    assert prompt.startswith(block), (
        f"prompt must start with hydra_context_block; got: {prompt[:120]!r}"
    )
    # The context block must be followed by a blank separator line.
    assert f"{block}\n\n" in prompt, (
        "hydra_context_block must be separated from the engineer body by a blank line"
    )
    # The original request body is still present.
    assert "implement the thing" in prompt


def test_begin_stage_without_hydra_context_block_prompt_unchanged(tmp_path):
    """Without hydra_context_block (default None), the prompt must NOT contain
    any Hydra context header — identical to the pre-7b behavior."""
    disp = FakeDispatcher()
    res = host_bridge.begin_stage(
        disp, workflow_id="wf-1", run_id="run-1",
        project_path=str(tmp_path), request_text="implement the thing",
        project_root=str(tmp_path))
    prompt = res["host_action"]["prompt"]
    assert "## Hydra context" not in prompt, (
        "prompt must not contain a Hydra context header when hydra_context_block is absent"
    )


def test_reflexion_retry_prompt_contains_block_exactly_once(tmp_path):
    """7b fix: the Reflexion generate-1 retry prompt must contain the
    hydra_context_block heading exactly once AND the critique augmentation.

    Drive: begin_stage(hydra_context_block=block) → submit generate-0 →
    submit judge-0 revise (same-vendor ok, required_cross=False) →
    assert the pending_action prompt for generate-1 contains the block
    exactly once and contains the critique text.
    """
    HEADING = "## Hydra context"
    block = f"{HEADING}\nworkflow_id: wf-1"
    CRITIQUE = "critique: the thing needs fixing"
    # required_cross_vendor=False so a same-vendor revise is a genuine code
    # defect (not an infra downgrade) and Reflexion fires cleanly.
    disp = FakeDispatcher(required_cross_vendor=False)
    res = host_bridge.begin_stage(
        disp, workflow_id="wf-1", run_id="run-1",
        project_path=str(tmp_path), request_text="implement the thing",
        project_root=str(tmp_path),
        hydra_context_block=block)
    cfile = res["cursor_path"]

    # Submit generate result.
    res = host_bridge.submit_host_result(
        disp, cursor_file=cfile, call_key="generate-0",
        result={"text": "edited foo.py", "cost_usd": 0.05,
                "tokens_in": 50, "tokens_out": 25,
                "model": "claude-sonnet-4-6"})
    assert res["state"] == "await_judge"

    # Submit revise verdict → Reflexion fires, cursor back to await_generate.
    res = host_bridge.submit_host_result(
        disp, cursor_file=cfile, call_key="judge-0",
        result={"outcome": "revise", "critique_md": CRITIQUE,
                "judge_producer": "claude", "cost_usd": 0.02})
    assert res["state"] == "await_generate", (
        "Reflexion must transition cursor back to await_generate"
    )
    assert res["host_action"]["call_key"] == "generate-1"

    prompt = res["host_action"]["prompt"]

    # Block must appear exactly once — not zero (dropped), not two (duplicated).
    count = prompt.count(HEADING)
    assert count == 1, (
        f"hydra_context_block heading must appear exactly once in the generate-1 "
        f"retry prompt; found {count} time(s). prompt[:400]={prompt[:400]!r}"
    )
    # Block must precede the critique (same structure as generate-0).
    block_pos = prompt.index(HEADING)
    critique_pos = prompt.index(CRITIQUE)
    assert block_pos < critique_pos, (
        "hydra_context_block must precede the critique in the retry prompt"
    )
    # Critique augmentation must be present.
    assert CRITIQUE in prompt, (
        f"critique must be embedded in the retry prompt; "
        f"prompt[:400]={prompt[:400]!r}"
    )


def test_smoke_idempotency_skips_run_smoke_on_retry(tmp_path, monkeypatch):
    """If smoke_result_for is set on the cursor for the current call_key, a retried
    submit with the same call_key must NOT call _run_smoke again and must reuse
    the persisted status.

    Scenario: submit timeout kills between record_smoke_status and the outer
    save_cursor.  smoke_result_for is persisted; a retry finds it and skips smoke.
    """
    # Track whether _run_smoke is invoked.
    smoke_calls: list[tuple[str, str]] = []

    def _fake_smoke_pass(*_a, **_k) -> tuple[str, str]:
        smoke_calls.append(("called", ""))
        return ("pass", "fake smoke pass")

    monkeypatch.setattr(host_bridge, "_run_smoke", _fake_smoke_pass)

    disp = FakeDispatcher(required_cross_vendor=True)
    res = _begin(disp, tmp_path)
    cfile = res["cursor_path"]

    host_bridge.submit_host_result(
        disp, cursor_file=cfile, call_key="generate-0",
        result={"text": "edited foo.py"})

    # Simulate that smoke already ran and its result was persisted.
    cursor = host_bridge.load_cursor(cfile)
    cursor["verdict_recorded_for"] = "judge-0"
    cursor["smoke_result_for"] = {
        "call_key": "judge-0",
        "status": "pass",
        "reason": "pre-persisted smoke pass",
    }
    host_bridge.save_cursor(cfile, cursor)
    smoke_calls.clear()  # reset; the fixture-level monkeypatch already suppresses real smoke

    # Retry judge submit — _run_smoke must not be invoked.
    res2 = host_bridge.submit_host_result(
        disp, cursor_file=cfile, call_key="judge-0",
        result={"outcome": "pass", "critique_md": "LGTM",
                "judge_producer": "codex", "cost_usd": 0.03})
    assert res2["status"] == "complete"
    assert smoke_calls == [], (
        "_run_smoke must not be invoked when smoke_result_for matches the call_key"
    )
    assert disp.count("finalize_run") == 1


def test_reflexion_clears_idempotency_markers(tmp_path, monkeypatch):
    """After a Reflexion×1 transition (revise on gen_idx=0), the idempotency
    markers must be cleared so the next judge cycle (judge-1) records its own
    verdict independently.
    """
    # Monkeypatch _run_smoke at module level (the autouse fixture already does
    # this, but we want to confirm markers are gone after Reflexion regardless).
    disp = FakeDispatcher(required_cross_vendor=True)
    res = _begin(disp, tmp_path)
    cfile = res["cursor_path"]

    # Advance to await_judge.
    host_bridge.submit_host_result(
        disp, cursor_file=cfile, call_key="generate-0",
        result={"text": "edited foo.py"})

    # Simulate verdict_recorded_for being set before the first revise verdict.
    cursor = host_bridge.load_cursor(cfile)
    cursor["verdict_recorded_for"] = "judge-0"
    host_bridge.save_cursor(cfile, cursor)

    # Submit revise verdict → Reflexion fires, cursor transitions to await_generate.
    res2 = host_bridge.submit_host_result(
        disp, cursor_file=cfile, call_key="judge-0",
        result={"outcome": "revise", "critique_md": "needs work",
                "judge_producer": "codex", "cost_usd": 0.02})
    assert res2["state"] == "await_generate", "Reflexion must transition to await_generate"

    # After the transition, markers must be cleared so judge-1 can record freshly.
    cursor_after = host_bridge.load_cursor(cfile)
    assert "verdict_recorded_for" not in cursor_after, (
        "verdict_recorded_for must be cleared after Reflexion transition"
    )
    assert "smoke_result_for" not in cursor_after, (
        "smoke_result_for must be cleared after Reflexion transition"
    )


# --------------------------------------------------------------------------- #
# LV-1: error-payload detection                                               #
# --------------------------------------------------------------------------- #
# MCPStdioDispatcher.call_mcp returns error DICTS instead of raising, so the
# existing try/except downgrade paths would silently pass through failures.
# _raise_on_error_payload converts those dicts into RuntimeError so the
# existing except clauses fire for payload-level errors too.


class _FakeDispatcherVerdictRejected(FakeDispatcher):
    """Variant where record_verdict returns a rejection payload (no raise)."""

    def call_mcp(self, server: str, tool: str, args: dict,
                 squad_id: str | None = None) -> dict:
        if tool == "record_verdict":
            # Record locally only for the intercepted tool, then return rejection.
            # (super() must NOT be called here — it would append a second time.)
            self.calls.append((server, tool, dict(args), squad_id))
            return {"status": "rejected", "error": "vendor pinning"}
        # For all other tools, delegate to base (which records the call once).
        return super().call_mcp(server, tool, args, squad_id=squad_id)


class _FakeDispatcherAttemptRejected(FakeDispatcher):
    """Variant where the success-path record_attempt returns a rejection payload."""

    def call_mcp(self, server: str, tool: str, args: dict,
                 squad_id: str | None = None) -> dict:
        if tool == "record_attempt" and args.get("status") == "ok":
            # Record locally only for the intercepted case, then return rejection.
            # (super() must NOT be called here — it would append a second time.)
            self.calls.append((server, tool, dict(args), squad_id))
            return {"status": "rejected", "error": "attempt rejected by ledger"}
        # For all other tools (and non-ok record_attempt), delegate to base.
        return super().call_mcp(server, tool, args, squad_id=squad_id)


def test_record_verdict_error_payload_surfaces_stage(tmp_path):
    """LV-1: a record_verdict that returns an error dict (no raise) must
    trigger the existing F26+M8 downgrade — the stage surfaces rather than
    completing.  Without _raise_on_error_payload the error dict was silently
    swallowed and the stage incorrectly finalized 'complete'."""
    disp = _FakeDispatcherVerdictRejected(required_cross_vendor=True)
    res = _begin(disp, tmp_path)
    cfile = res["cursor_path"]
    host_bridge.submit_host_result(
        disp, cursor_file=cfile, call_key="generate-0",
        result={"text": "edited foo.py", "cost_usd": 0.10,
                "tokens_in": 100, "tokens_out": 50, "model": "claude-opus-4"})
    res = host_bridge.submit_host_result(
        disp, cursor_file=cfile, call_key="judge-0",
        result={"outcome": "pass", "judge_producer": "codex", "cost_usd": 0.05})
    assert res.get("final_status") != "complete", (
        "record_verdict error payload must surface the stage, not complete it"
    )
    assert res.get("status") in ("surfaced", "aborted"), (
        f"expected surfaced/aborted after record_verdict rejection, got {res.get('status')!r}"
    )
    # finalize_run must still have been called (stage exits cleanly)
    assert disp.count("finalize_run") == 1


def test_record_attempt_error_payload_surfaces_without_judge(tmp_path):
    """LV-1: a record_attempt success-path that returns an error dict surfaces
    the stage immediately — no judge routing, no record_verdict.  Without
    _raise_on_error_payload the rejection dict silently left attempt_id=None
    and allowed gate_eligible_judges to run."""
    disp = _FakeDispatcherAttemptRejected(required_cross_vendor=True)
    res = _begin(disp, tmp_path)
    cfile = res["cursor_path"]
    res = host_bridge.submit_host_result(
        disp, cursor_file=cfile, call_key="generate-0",
        result={"text": "edited foo.py", "cost_usd": 0.05,
                "tokens_in": 80, "tokens_out": 40, "model": "claude-sonnet-4"})
    assert res.get("status") in ("surfaced", "aborted"), (
        f"expected surfaced/aborted after record_attempt error payload, "
        f"got {res.get('status')!r}"
    )
    assert disp.count("gate_eligible_judges") == 0, (
        "gate_eligible_judges must NOT be called when record_attempt errors"
    )
    assert disp.count("record_verdict") == 0, (
        "record_verdict must NOT be called when record_attempt errors"
    )
    assert disp.count("finalize_run") == 1, (
        "finalize_run must still be called to release the pp run lock"
    )


# --------------------------------------------------------------------------- #
# LV-3: same-vendor producer relabeling                                       #
# --------------------------------------------------------------------------- #

def test_same_vendor_judge_producer_relabeled_for_ledger(tmp_path):
    """LV-3: when required_cross=False and the judge_producer equals the
    generator producer ('claude'), record_verdict must receive
    judge_producer='claude-same-vendor-host' so the pp ledger does not reject
    the generator-identical pair.  score_json._judge_tier stays 'same_vendor'."""
    disp = FakeDispatcher(required_cross_vendor=False)
    res = _begin(disp, tmp_path)
    cfile = res["cursor_path"]
    host_bridge.submit_host_result(
        disp, cursor_file=cfile, call_key="generate-0",
        result={"text": "edited foo.py", "cost_usd": 0.08,
                "tokens_in": 90, "tokens_out": 45, "model": "claude-sonnet-4"})
    # Submit same-vendor judge result (judge_producer == producer == "claude")
    host_bridge.submit_host_result(
        disp, cursor_file=cfile, call_key="judge-0",
        result={"outcome": "pass", "judge_producer": "claude",
                "judge_model_id": "claude-haiku-4", "cost_usd": 0.02})
    verdict_calls = [
        (a.get("judge_producer"), a.get("score_json", {}).get("_judge_tier"))
        for _s, t, a, _q in disp.calls
        if t == "record_verdict"
    ]
    assert verdict_calls, "record_verdict was not called"
    actual_producer, actual_tier = verdict_calls[0]
    assert actual_producer == "claude-same-vendor-host", (
        f"expected judge_producer='claude-same-vendor-host', got {actual_producer!r}"
    )
    # _judge_tier must remain 'same_vendor' — the score_json reflects the truth
    # (same-vendor judging), not the relabeled name.
    assert actual_tier == "same_vendor", (
        f"expected _judge_tier='same_vendor', got {actual_tier!r}"
    )


# --------------------------------------------------------------------------- #
# G6: squad-kind mark_charged parity                                          #
# --------------------------------------------------------------------------- #

def test_squad_stage_mark_charged_parity(tmp_path):
    """mark_charged on a completed squad-kind cursor must flip already_charged to
    True — mirroring the engineering-kind charged flag (rider b idempotency guard).

    Flow: begin_squad_stage → submit_host_result (completes) → verify
    already_charged=False → mark_charged → re-read via submit (terminal
    short-circuit) → verify already_charged=True.
    """
    disp = FakeDispatcher()
    res = host_bridge.begin_squad_stage(
        workflow_id="wf-mc", task_id="task-mc", squad_slug="executive",
        entrypoint="agent-impersonation", lead_agent="ceo",
        pack_cwd=str(tmp_path), request_text="frame decision",
        project_root=str(tmp_path))
    cfile = res["cursor_path"]

    # First submit: completes the squad stage.
    r1 = host_bridge.submit_host_result(
        disp, cursor_file=cfile, call_key="squad-task-mc-0",
        result={"text": "decision doc", "cost_usd": 0.06})
    assert r1["status"] == "complete"
    assert r1.get("already_charged") is False, (
        "before mark_charged, already_charged must be False (charge not yet recorded)")

    # Mark as charged (the CLI does this after charging HydraState).
    host_bridge.mark_charged(cfile)

    # Re-read via a duplicate submit (terminal cursor → short-circuits to step result).
    r2 = host_bridge.submit_host_result(
        disp, cursor_file=cfile, call_key="squad-task-mc-0",
        result={"text": "decision doc", "cost_usd": 0.06})
    assert r2["status"] == "complete"
    assert r2.get("already_charged") is True, (
        "after mark_charged, already_charged must be True (prevents double-charge)")


# --------------------------------------------------------------------------- #
# G6: baseline timeout marker                                                  #
# --------------------------------------------------------------------------- #

def test_capture_baseline_timeout_marker(tmp_path, monkeypatch):
    """On TimeoutExpired _capture_baseline_failures must write a <sha>.timeout.json
    marker and on the second call return [] WITHOUT re-running the suite.

    The marker prevents re-paying an already-too-slow suite on every stage at the
    same HEAD commit (which would double the damage and blow step budget).
    """
    import json
    from hydra_core.host_bridge import _capture_baseline_failures

    # Create a tests/ dir so the candidate loop finds something to attempt.
    (tmp_path / "tests").mkdir()

    fixed_sha = "cafebabe1234abcd"

    # Bypass the real git subprocess: return a known fixed SHA so the cache paths
    # are deterministic and don't accidentally pick up real HEAD state.
    class _FakeGitResult:
        stdout: str = fixed_sha
        returncode: int = 0

    monkeypatch.setattr(host_bridge, "_git", lambda *a, **k: _FakeGitResult())

    call_count: list[int] = [0]

    def _fake_run(cmd, **kwargs):  # noqa: ANN001
        call_count[0] += 1
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 600))

    monkeypatch.setattr(subprocess, "run", _fake_run)

    # First call: suite runs once, times out, marker written, returns [].
    result1 = _capture_baseline_failures(str(tmp_path))
    assert result1 == [], "TimeoutExpired must return []"
    assert call_count[0] == 1, "suite must run exactly once on the first timed-out call"

    # Marker file must exist in .harness/baseline/.
    baseline_dir = tmp_path / ".harness" / "baseline"
    timeout_markers = list(baseline_dir.glob("*.timeout.json"))
    assert timeout_markers, "a <sha>.timeout.json marker file must be created"
    marker_data = json.loads(timeout_markers[0].read_text(encoding="utf-8"))
    assert "timeout_s" in marker_data, "marker must contain the timeout_s value"

    # Second call: marker detected → suite must NOT re-run.
    result2 = _capture_baseline_failures(str(tmp_path))
    assert result2 == [], "marker-hit call must return []"
    assert call_count[0] == 1, (
        "suite must NOT re-run when the timeout marker already exists for this sha"
    )
