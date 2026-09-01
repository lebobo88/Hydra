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
    assert res["host_action"]["call_key"] == "judge-run-1-stage-1-att-1-0"
    assert disp.count("record_attempt") == 1
    assert disp.count("gate_eligible_judges") == 1

    # Submit the judge verdict → driver records verdict, runs smoke, finalizes.
    res = host_bridge.submit_host_result(
        disp, cursor_file=cfile, call_key="judge-run-1-stage-1-att-1-0",
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
        disp, cursor_file=cfile, call_key="judge-run-1-stage-1-att-1-0",
        result={"outcome": "pass", "judge_producer": "codex"})
    assert res["status"] == "complete"
    finalize_runs = disp.count("finalize_run")

    # A retried judge submit after the run is terminal must NOT re-finalize.
    res2 = host_bridge.submit_host_result(
        disp, cursor_file=cfile, call_key="judge-run-1-stage-1-att-1-0",
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
        disp, cursor_file=cfile, call_key="judge-run-1-stage-1-att-1-0",
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
        disp, cursor_file=res["cursor_path"], call_key="judge-run-wt-stage-1-att-1-0",
        result={"outcome": "pass", "judge_producer": "codex"})
    assert res["status"] == "complete"
    assert res["merge"]["merged"] is True
    # The change landed in the repo working tree...
    assert (tmp_path / "feature.py").exists()
    # ...and the worktree was cleaned up.
    assert not Path(wt).exists()


def test_merge_worktree_back_excludes_byproduct_dirs(tmp_path):
    """_merge_worktree_back must not stage .hydra/, .harness/, or *.log
    byproducts left inside the worktree (e.g. by a nested tool run with
    cwd=worktree) — only the engineer's actual code changes should land on
    merge. Regression for the iolaus.log / trace.jsonl merge-conflict class of
    bug where a byproduct written into the worktree's own copy of a
    project-scratch directory collided with the same file at the repo root."""
    _init_repo(tmp_path)
    disp = FakeDispatcher()
    res = host_bridge.begin_stage(
        disp, workflow_id="wf-bp", run_id="run-bp",
        project_path=str(tmp_path), request_text="add a feature file",
        project_root=str(tmp_path), isolate=True)
    wt = res["host_action"]["cwd"]

    from pathlib import Path
    # The engineer's real, mergeable change.
    Path(wt, "feature.py").write_text("print('hi')\n", encoding="utf-8")
    # Byproducts that must NOT be merged back.
    (Path(wt) / ".hydra").mkdir(parents=True, exist_ok=True)
    (Path(wt) / ".hydra" / "trace.jsonl").write_text("{}\n", encoding="utf-8")
    (Path(wt) / ".harness").mkdir(parents=True, exist_ok=True)
    (Path(wt) / ".harness" / "scratch.json").write_text("{}\n", encoding="utf-8")
    (Path(wt) / "run.log").write_text("log line\n", encoding="utf-8")

    res = host_bridge.submit_host_result(
        disp, cursor_file=res["cursor_path"], call_key="generate-0",
        result={"text": "added feature.py"})
    res = host_bridge.submit_host_result(
        disp, cursor_file=res["cursor_path"], call_key="judge-run-bp-stage-1-att-1-0",
        result={"outcome": "pass", "judge_producer": "codex"})
    assert res["status"] == "complete"
    assert res["merge"]["merged"] is True
    # The real change landed...
    assert (tmp_path / "feature.py").exists()
    # ...but none of the byproducts did.
    assert not (tmp_path / ".hydra" / "trace.jsonl").exists()
    assert not (tmp_path / ".harness" / "scratch.json").exists()
    assert not (tmp_path / "run.log").exists()


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
        disp, cursor_file=res["cursor_path"], call_key="judge-run-wt2-stage-1-att-1-0",
        result={"outcome": "revise", "judge_producer": "codex"})
    assert res["state"] == "await_generate", "GAP-f: first revise should trigger Reflexion"
    assert Path(wt).exists(), "worktree must survive through Reflexion"
    # Reflexion generate-1 and second revise → finalize surfaced.
    res = host_bridge.submit_host_result(
        disp, cursor_file=res["cursor_path"], call_key="generate-1",
        result={"text": "revision attempt"})
    res = host_bridge.submit_host_result(
        disp, cursor_file=res["cursor_path"], call_key="judge-run-wt2-stage-1-att-1-1",
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


def test_begin_stage_writes_and_finalize_clears_stage_active_sentinel(tmp_path):
    """begin_stage must WRITE .harness/stage-active — the sentinel the
    PreToolUse write-enforcement hooks (hydra-block-direct-write.ps1 etc.)
    check for their HYDRA_PP_STAGE_ACTIVE=1 bypass. Before this fix nothing
    ever wrote it: it was dead code the hooks referenced but no producer ever
    created, which is why the stale attended-* worktree-directory check was
    load-bearing. A passing finalize must clear it again so a later, unrelated
    session doesn't inherit a stale bypass."""
    disp = FakeDispatcher()
    res = _begin(disp, tmp_path)
    sentinel = tmp_path / ".harness" / "stage-active"
    assert sentinel.exists(), "begin_stage must write the stage-active sentinel"
    assert sentinel.is_file()

    res = host_bridge.submit_host_result(
        disp, cursor_file=res["cursor_path"], call_key="generate-0",
        result={"text": "edited foo.py"})
    res = host_bridge.submit_host_result(
        disp, cursor_file=res["cursor_path"], call_key="judge-run-1-stage-1-att-1-0",
        result={"outcome": "pass", "judge_producer": "codex"})
    assert res["status"] == "complete"
    assert not sentinel.exists(), "a terminal finalize must clear the sentinel"


def test_abort_stage_clears_stage_active_sentinel(tmp_path):
    """abort_stage must clear the sentinel too, not just a clean finalize."""
    disp = FakeDispatcher()
    res = _begin(disp, tmp_path)
    sentinel = tmp_path / ".harness" / "stage-active"
    assert sentinel.exists()
    host_bridge.abort_stage(disp, cursor_file=res["cursor_path"],
                            reason="operator_cancel")
    assert not sentinel.exists(), "abort_stage must clear the sentinel"


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
    cursor["verdict_recorded_for"] = "judge-run-1-stage-1-att-1-0"
    host_bridge.save_cursor(cfile, cursor)

    verdict_calls_before = disp.count("record_verdict")

    # Retry judge submit — must NOT re-invoke record_verdict.
    res2 = host_bridge.submit_host_result(
        disp, cursor_file=cfile, call_key="judge-run-1-stage-1-att-1-0",
        result={"outcome": "pass", "critique_md": "looks good",
                "judge_producer": "codex", "cost_usd": 0.05})
    assert res2["status"] == "complete"
    assert disp.count("record_verdict") == verdict_calls_before, (
        "record_verdict must not be called again when verdict_recorded_for matches"
    )
    # The stage must still finalize correctly.
    assert disp.count("finalize_run") == 1


# --------------------------------------------------------------------------- #
# LV-8: judge call_key / record_verdict idempotency_token collision           #
# --------------------------------------------------------------------------- #
# The judge_call_key built at generate-apply time (f"judge-{gen_idx}" pre-fix)
# round-trips verbatim as pp's record_verdict idempotency_token. pp's lookup
# (findVerdictByIdempotencyToken) is keyed GLOBALLY across every run/stage in
# the one shared daemon DB, so an unscoped "judge-0" from one stage's first
# verdict call collides with every other stage's first verdict call. This
# FakeDispatcher variant reproduces that global, cross-instance collision
# surface for real (a module-level dict shared across dispatcher instances,
# exactly like pp's single sqlite table is shared across every attended
# stage), so it fails the moment two independently-run stages share a token.

_GLOBAL_VERDICT_LEDGER: dict[str, dict] = {}


class _FakeDispatcherGlobalVerdictLedger(FakeDispatcher):
    """record_verdict backed by a ledger SHARED across every instance of this
    class -- mirrors pp's single machine-wide verdicts table + unique index on
    idempotency_token, unscoped by run/stage/attempt."""

    def call_mcp(self, server, tool, args, squad_id=None):
        if tool == "record_verdict":
            self.calls.append((server, tool, dict(args), squad_id))
            token = args.get("idempotency_token")
            if token and token in _GLOBAL_VERDICT_LEDGER:
                # Idempotency short-circuit: pp returns the ORIGINAL row's
                # verdict_id, exactly as findVerdictByIdempotencyToken does.
                return {"status": "done", "result": _GLOBAL_VERDICT_LEDGER[token]}
            verdict_id = f"verdict_{len(_GLOBAL_VERDICT_LEDGER) + 1}"
            row = {"verdict_id": verdict_id, "cross_vendor": True,
                   "attempt_id": args.get("attempt_id")}
            if token:
                _GLOBAL_VERDICT_LEDGER[token] = row
            return {"status": "done", "result": row}
        return super().call_mcp(server, tool, args, squad_id=squad_id)


def test_lv8_distinct_stages_do_not_collide_on_shared_verdict_ledger(tmp_path, tmp_path_factory):
    """Two DIFFERENT stages (different run_id) each recording their FIRST judge
    verdict (gen_idx=0) against a globally-shared idempotency ledger must each
    persist their OWN verdict row -- not silently short-circuit into the other
    stage's row via a token collision.

    Pre-fix, judge_call_key was the bare f"judge-{gen_idx}" ("judge-0" for
    both), so stage B's record_verdict call would hit the ledger entry stage A
    already wrote and receive stage A's verdict_id back as if it were its own
    -- exactly the incident this fix addresses. This test fails against the
    pre-fix f"judge-{gen_idx}" call_key and passes against the scoped
    f"judge-{run_id}-{stage_id}-{gen_idx}" key.
    """
    _GLOBAL_VERDICT_LEDGER.clear()
    tmp_a = tmp_path_factory.mktemp("stage-a")
    tmp_b = tmp_path_factory.mktemp("stage-b")

    disp_a = _FakeDispatcherGlobalVerdictLedger(required_cross_vendor=True)
    disp_b = _FakeDispatcherGlobalVerdictLedger(required_cross_vendor=True)

    res_a = host_bridge.begin_stage(
        disp_a, workflow_id="wf-lv8a", run_id="run-lv8a",
        project_path=str(tmp_a), request_text="implement thing A",
        project_root=str(tmp_a))
    res_b = host_bridge.begin_stage(
        disp_b, workflow_id="wf-lv8b", run_id="run-lv8b",
        project_path=str(tmp_b), request_text="implement thing B",
        project_root=str(tmp_b))

    res_a = host_bridge.submit_host_result(
        disp_a, cursor_file=res_a["cursor_path"], call_key="generate-0",
        result={"text": "edited a.py"})
    res_b = host_bridge.submit_host_result(
        disp_b, cursor_file=res_b["cursor_path"], call_key="generate-0",
        result={"text": "edited b.py"})

    key_a = res_a["host_action"]["call_key"]
    key_b = res_b["host_action"]["call_key"]
    assert key_a != key_b, (
        "LV-8: two distinct stages' first-judge call_key/idempotency_token "
        f"must not collide; got {key_a!r} == {key_b!r}"
    )

    res_a = host_bridge.submit_host_result(
        disp_a, cursor_file=res_a["cursor_path"], call_key=key_a,
        result={"outcome": "pass", "judge_producer": "codex"})
    res_b = host_bridge.submit_host_result(
        disp_b, cursor_file=res_b["cursor_path"], call_key=key_b,
        result={"outcome": "pass", "judge_producer": "codex"})

    assert res_a["status"] == "complete"
    assert res_b["status"] == "complete"

    # Each stage's record_verdict call must have actually reached the ledger
    # (not silently short-circuited into the OTHER stage's pre-existing row) --
    # the ledger must hold two distinct entries, one per token.
    assert len(_GLOBAL_VERDICT_LEDGER) == 2, (
        "LV-8: both stages' first verdict must persist its own ledger row "
        f"(pre-fix, they'd collide into one); ledger={_GLOBAL_VERDICT_LEDGER!r}"
    )
    assert key_a in _GLOBAL_VERDICT_LEDGER
    assert key_b in _GLOBAL_VERDICT_LEDGER
    assert _GLOBAL_VERDICT_LEDGER[key_a] is not _GLOBAL_VERDICT_LEDGER[key_b]
    _GLOBAL_VERDICT_LEDGER.clear()


def test_lv8_retry_same_call_key_stays_idempotent(tmp_path):
    """The retry-safety property MUST survive the scoping fix: a genuine retry
    of the SAME judge call on the SAME attempt (same run_id/stage_id/gen_idx)
    still carries the SAME idempotency_token, so pp's short-circuit still
    dedupes it -- no duplicate ledger row, no double finalize."""
    _GLOBAL_VERDICT_LEDGER.clear()
    disp = _FakeDispatcherGlobalVerdictLedger(required_cross_vendor=True)
    res = _begin(disp, tmp_path)
    cfile = res["cursor_path"]
    res = host_bridge.submit_host_result(
        disp, cursor_file=cfile, call_key="generate-0",
        result={"text": "edited foo.py"})
    key = res["host_action"]["call_key"]

    res1 = host_bridge.submit_host_result(
        disp, cursor_file=cfile, call_key=key,
        result={"outcome": "pass", "judge_producer": "codex"})
    assert res1["status"] == "complete"

    # A second submit_host_result with the SAME call_key on an already-terminal
    # cursor is the exactly-once short-circuit path (never re-applies) --
    # confirm no second record_verdict call happens and no duplicate ledger
    # row is created for the token.
    res2 = host_bridge.submit_host_result(
        disp, cursor_file=cfile, call_key=key,
        result={"outcome": "pass", "judge_producer": "codex"})
    assert res2["status"] == "complete"
    assert disp.count("record_verdict") == 1, (
        "a retried submit on a terminal cursor must not re-invoke record_verdict"
    )
    assert len(_GLOBAL_VERDICT_LEDGER) == 1
    _GLOBAL_VERDICT_LEDGER.clear()


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
        disp, cursor_file=cfile, call_key="judge-run-1-stage-1-att-1-0",
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
    cursor["verdict_recorded_for"] = "judge-run-1-stage-1-att-1-0"
    cursor["smoke_result_for"] = {
        "call_key": "judge-run-1-stage-1-att-1-0",
        "status": "pass",
        "reason": "pre-persisted smoke pass",
    }
    host_bridge.save_cursor(cfile, cursor)
    smoke_calls.clear()  # reset; the fixture-level monkeypatch already suppresses real smoke

    # Retry judge submit — _run_smoke must not be invoked.
    res2 = host_bridge.submit_host_result(
        disp, cursor_file=cfile, call_key="judge-run-1-stage-1-att-1-0",
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
    cursor["verdict_recorded_for"] = "judge-run-1-stage-1-att-1-0"
    host_bridge.save_cursor(cfile, cursor)

    # Submit revise verdict → Reflexion fires, cursor transitions to await_generate.
    res2 = host_bridge.submit_host_result(
        disp, cursor_file=cfile, call_key="judge-run-1-stage-1-att-1-0",
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
        disp, cursor_file=cfile, call_key="judge-run-1-stage-1-att-1-0",
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
        disp, cursor_file=cfile, call_key="judge-run-1-stage-1-att-1-0",
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


# --------------------------------------------------------------------------- #
# W2-2/W2-3/W2-4: transport-shaped record_verdict failure holds the cursor    #
# open instead of downgrading + finalizing, and a sanctioned recovery path    #
# drives a stranded stage to completion.                                     #
# --------------------------------------------------------------------------- #

class _FakeDispatcherVerdictTransportFail(FakeDispatcher):
    """record_verdict fails with a transport-shaped error until `.recover()`
    flips it to succeed — simulating the SQLITE_BUSY/cold-start race that
    caused the incident this fix addresses."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.verdict_should_fail = True

    def call_mcp(self, server: str, tool: str, args: dict,
                 squad_id: str | None = None) -> dict:
        if tool == "record_verdict" and self.verdict_should_fail:
            self.calls.append((server, tool, dict(args), squad_id))
            return {"status": "failed", "timeout": True,
                    "error": "tool 'record_verdict' on 'pp_harness' timed out after 120s",
                    "phase": "call_tool", "timeout_s": 120.0}
        return super().call_mcp(server, tool, args, squad_id=squad_id)

    def recover(self) -> None:
        self.verdict_should_fail = False


def test_classify_infra_failure_transport_vs_deterministic():
    from hydra_core.host_bridge import _classify_infra_failure
    assert _classify_infra_failure(None) == "deterministic"
    assert _classify_infra_failure(RuntimeError(
        "pp ledger call 'record_verdict' returned error (status='failed'): "
        "\"tool 'record_verdict' on 'pp_harness' timed out after 120s\"")) == "transport"
    assert _classify_infra_failure(RuntimeError(
        "call_tool raised after connect: ConnectionResetError: [Errno 104]")) == "transport"
    assert _classify_infra_failure(RuntimeError(
        "pp ledger call 'record_verdict' returned error payload (status='rejected'): "
        "'vendor pinning'")) == "deterministic"
    assert _classify_infra_failure(RuntimeError(
        "validation failed: rubric_id must be a known rubric")) == "deterministic"
    # An unrecognized shape must fail the stage (deterministic), never mask a
    # real rejection by guessing "transport".
    assert _classify_infra_failure(RuntimeError("something odd happened")) == "deterministic"


def test_classify_infra_failure_venom_gate_fail_closed_is_deterministic():
    """A venom-gate fail-CLOSED rejection (dispatcher.py._venom_gate's
    gate-internal-error branch) must classify as "deterministic" even when
    its wrapped inner-exception text contains a transport-sounding phrase
    like "database is locked" -- e.g. a degraded/locked episodic audit
    store. The classification is keyed on the STRUCTURED rejection payload
    (status == "rejected", gate_error, hitl_required), not on message text,
    so this can never collide with the transport marker list no matter what
    the underlying exception says."""
    from hydra_core.host_bridge import PPLedgerError, _classify_infra_failure, _raise_on_error_payload

    # Exactly the shape dispatcher.py._venom_gate's fail-closed branch returns.
    venom_gate_payload = {
        "status": "rejected",
        "error": "venom gate internal error: database is locked",
        "hitl_required": True,
        "gate_error": True,
    }
    try:
        _raise_on_error_payload(venom_gate_payload, "record_verdict")
        assert False, "expected PPLedgerError"
    except PPLedgerError as exc:
        assert exc.payload == venom_gate_payload
        assert _classify_infra_failure(exc) == "deterministic"

    # Direct construction, in case a caller builds the exception by hand.
    exc2 = PPLedgerError(
        "pp ledger call 'record_verdict' returned error payload "
        "(status='rejected'): 'venom gate internal error: database is locked'",
        venom_gate_payload,
    )
    assert _classify_infra_failure(exc2) == "deterministic"

    # A hitl_required/gate_error payload without status=="rejected" also
    # forces deterministic via _DETERMINISTIC_PAYLOAD_KEYS.
    exc3 = PPLedgerError(
        "timeout: database is locked", {"gate_error": True},
    )
    assert _classify_infra_failure(exc3) == "deterministic"

    # A genuine transport-shaped PPLedgerError (status=="failed", no
    # deterministic payload keys) still falls through to the text markers.
    exc4 = PPLedgerError(
        "pp ledger call 'record_verdict' returned error (status='failed'): "
        "\"tool 'record_verdict' on 'pp_harness' timed out after 120s\"",
        {"status": "failed", "error": "timed out"},
    )
    assert _classify_infra_failure(exc4) == "transport"


def test_transport_verdict_failure_holds_cursor_open_not_finalized(tmp_path):
    """A transport-shaped record_verdict failure on a pass outcome must hold
    the cursor open (state='stalled_infra'), NOT downgrade to revise/surfaced
    and finalize. finalize_stage/finalize_run must never be called, and the
    original judge pending_action (call_key) must survive untouched so a
    re-issued submit_host_result can re-drive it."""
    disp = _FakeDispatcherVerdictTransportFail(required_cross_vendor=True)
    res = _begin(disp, tmp_path)
    cfile = res["cursor_path"]
    host_bridge.submit_host_result(
        disp, cursor_file=cfile, call_key="generate-0",
        result={"text": "edited foo.py", "cost_usd": 0.10,
                "tokens_in": 100, "tokens_out": 50, "model": "claude-opus-4"})
    res = host_bridge.submit_host_result(
        disp, cursor_file=cfile, call_key="judge-run-1-stage-1-att-1-0",
        result={"outcome": "pass", "judge_producer": "codex", "cost_usd": 0.05})

    assert res["state"] == "stalled_infra"
    assert res["status"] == "awaiting_host", "must not become a terminal status"
    assert res.get("stalled_infra") is True
    assert res["host_action"]["call_key"] == "judge-run-1-stage-1-att-1-0", (
        "pending_action must still carry the original judge call_key so a "
        "re-drive can match it"
    )
    assert disp.count("finalize_stage") == 0
    assert disp.count("finalize_run") == 0

    cursor = host_bridge.load_cursor(cfile)
    assert cursor.get("verdict_recorded_for") is None
    assert cursor.get("pending_verdict_payload", {}).get("idempotency_token") == "judge-run-1-stage-1-att-1-0"


def test_stalled_infra_redrive_completes_exactly_once(tmp_path):
    """A re-issued submit_host_result carrying the SAME call_key/result after a
    stalled_infra hold re-enters record_verdict (now succeeding), finalizes,
    and does NOT double-count the judge's cost_usd/tokens."""
    disp = _FakeDispatcherVerdictTransportFail(required_cross_vendor=True)
    res = _begin(disp, tmp_path)
    cfile = res["cursor_path"]
    host_bridge.submit_host_result(
        disp, cursor_file=cfile, call_key="generate-0",
        result={"text": "edited foo.py", "cost_usd": 0.10,
                "tokens_in": 100, "tokens_out": 50, "model": "claude-opus-4"})
    judge_result = {"outcome": "pass", "judge_producer": "codex", "cost_usd": 0.05,
                    "tokens_in": 20, "tokens_out": 10}
    res = host_bridge.submit_host_result(
        disp, cursor_file=cfile, call_key="judge-run-1-stage-1-att-1-0", result=judge_result)
    assert res["state"] == "stalled_infra"
    cost_after_stall = res["cost_usd"]

    # The underlying transport issue clears; the SAME call_key/result is
    # resubmitted (a real recovery caller resubmits exactly what it has).
    disp.recover()
    res2 = host_bridge.submit_host_result(
        disp, cursor_file=cfile, call_key="judge-run-1-stage-1-att-1-0", result=judge_result)

    assert res2["status"] == "complete"
    assert res2["cost_usd"] == cost_after_stall, (
        "re-driving the same call_key must not double-count the judge's cost_usd"
    )
    # record_verdict was called twice at the transport layer (once failed, once
    # succeeded) but pp's idempotency_token makes that safe; only ONE
    # finalize_run/finalize_stage happened.
    assert disp.count("finalize_stage") == 1
    assert disp.count("finalize_run") == 1
    # Every record_verdict call carried the SAME idempotency_token.
    tokens = {a.get("idempotency_token") for _s, t, a, _q in disp.calls
              if t == "record_verdict"}
    assert tokens == {"judge-run-1-stage-1-att-1-0"}


class _FakeDispatcherTransportFailThenLedger(FakeDispatcher):
    """Combines the W2-3 transport-fail-until-recovered behaviour with a
    per-instance verdict ledger keyed by idempotency_token (mirrors pp's
    unique index on that column via findVerdictByIdempotencyToken). Used to
    prove -- by test, not by reading call sites -- that a stalled-infra
    re-drive reuses the SAME token and that token maps to EXACTLY ONE
    verdict row even though record_verdict is invoked twice at the
    transport layer (once failed, once succeeded)."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.verdict_should_fail = True
        self.verdict_ledger: dict[str, dict] = {}

    def call_mcp(self, server: str, tool: str, args: dict,
                 squad_id: str | None = None) -> dict:
        if tool == "record_verdict":
            self.calls.append((server, tool, dict(args), squad_id))
            if self.verdict_should_fail:
                return {"status": "failed", "timeout": True,
                        "error": "tool 'record_verdict' on 'pp_harness' timed out after 120s",
                        "phase": "call_tool", "timeout_s": 120.0}
            token = args.get("idempotency_token")
            if token and token in self.verdict_ledger:
                # pp's real short-circuit: same idempotency_token returns the
                # ORIGINAL row instead of inserting a duplicate.
                return {"status": "done", "result": self.verdict_ledger[token]}
            row = {"verdict_id": f"verdict-{len(self.verdict_ledger) + 1}",
                   "cross_vendor": True, "attempt_id": args.get("attempt_id")}
            if token:
                self.verdict_ledger[token] = row
            return {"status": "done", "result": row}
        return super().call_mcp(server, tool, args, squad_id=squad_id)

    def recover(self) -> None:
        self.verdict_should_fail = False


def test_stalled_infra_redrive_reuses_token_and_yields_one_verdict_row(tmp_path):
    """Retry-fix verification (item 3): a judge call that stalls on
    transport, then gets re-driven exactly the way a stalled-infra hold (or
    recover_stalled_stage) would, must (a) carry the SAME idempotency_token
    on every record_verdict attempt and (b) end up with EXACTLY ONE row in
    pp's verdict ledger for that token -- not "the code reads like it
    should", an actual assertion against a ledger that rejects/short-circuits
    duplicate tokens the way pp's unique index does.

    Also re-drives a THIRD time with the identical call_key/result after the
    stage has already completed (a duplicate host resubmit), and asserts the
    ledger still holds exactly one row -- the exactly-once property survives
    beyond the single stalled-infra recovery this scenario was built around.
    """
    disp = _FakeDispatcherTransportFailThenLedger(required_cross_vendor=True)
    res = _begin(disp, tmp_path)
    cfile = res["cursor_path"]
    host_bridge.submit_host_result(
        disp, cursor_file=cfile, call_key="generate-0",
        result={"text": "edited foo.py", "cost_usd": 0.10,
                "tokens_in": 100, "tokens_out": 50, "model": "claude-opus-4"})
    judge_result = {"outcome": "pass", "judge_producer": "codex", "cost_usd": 0.05,
                    "tokens_in": 20, "tokens_out": 10}
    judge_call_key = "judge-run-1-stage-1-att-1-0"

    # First attempt: transport fails, no row is written.
    res = host_bridge.submit_host_result(
        disp, cursor_file=cfile, call_key=judge_call_key, result=judge_result)
    assert res["state"] == "stalled_infra"
    assert disp.verdict_ledger == {}, "a failed transport call must not write a row"

    # Re-drive with the SAME call_key/result, exactly as a stalled-infra
    # recovery caller would.
    disp.recover()
    res2 = host_bridge.submit_host_result(
        disp, cursor_file=cfile, call_key=judge_call_key, result=judge_result)
    assert res2["status"] == "complete"
    assert list(disp.verdict_ledger.keys()) == [judge_call_key], (
        "the re-drive must write exactly one row, under the SAME token"
    )
    assert len(disp.verdict_ledger) == 1

    # Duplicate resubmit after completion (e.g. a retried host call arriving
    # late): must not create a second row.
    res3 = host_bridge.submit_host_result(
        disp, cursor_file=cfile, call_key=judge_call_key, result=judge_result)
    assert len(disp.verdict_ledger) == 1, (
        "a duplicate resubmit of an already-completed judge call must not "
        f"insert a second verdict row; ledger={disp.verdict_ledger!r}"
    )


def test_recover_stalled_stage_via_resume_action(tmp_path):
    """recover_stalled_stage (the W2-4 recovery function reachable only via
    `hydra resume --action recover-stalled-stage`) drives a stalled_infra
    cursor to completion: worktree merges, finalize runs, and record_verdict
    is not double-recorded."""
    from pathlib import Path
    _init_repo(tmp_path)
    disp = _FakeDispatcherVerdictTransportFail(required_cross_vendor=True)
    res = host_bridge.begin_stage(
        disp, workflow_id="wf-rec", run_id="run-rec",
        project_path=str(tmp_path), request_text="add a feature file",
        project_root=str(tmp_path), isolate=True)
    wt = res["host_action"]["cwd"]
    Path(wt, "feature.py").write_text("print('hi')\n", encoding="utf-8")

    res = host_bridge.submit_host_result(
        disp, cursor_file=res["cursor_path"], call_key="generate-0",
        result={"text": "added feature.py"})
    res = host_bridge.submit_host_result(
        disp, cursor_file=res["cursor_path"], call_key="judge-run-rec-stage-1-att-1-0",
        result={"outcome": "pass", "judge_producer": "codex"})
    assert res["state"] == "stalled_infra"
    # The worktree must still be present -- recovery needs it.
    assert Path(wt).exists()

    disp.recover()
    rec = host_bridge.recover_stalled_stage(disp, cursor_file=res["cursor_path"])

    assert rec["status"] == "complete"
    assert rec["merge"]["merged"] is True
    assert (tmp_path / "feature.py").exists()
    assert not Path(wt).exists()
    assert disp.count("finalize_stage") == 1
    assert disp.count("finalize_run") == 1


def test_recover_stalled_stage_legacy_surfaced_shape_merges_from_branch(tmp_path):
    """The pre-fix shape: a cursor already finalized 'surfaced' with a
    preserved_branch and no verdict_recorded_for (the worktree is gone, but
    _preserve_non_complete_work already committed the change to the branch).
    Recovery must re-issue record_verdict and merge directly from the branch
    without needing a live worktree."""
    _init_repo(tmp_path)
    branch = "attended/legacy-run"
    subprocess.run(["git", "checkout", "-b", branch], cwd=tmp_path,
                   capture_output=True, text=True, check=False)
    (tmp_path / "legacy_feature.py").write_text("print('legacy')\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, capture_output=True, text=True)
    subprocess.run(["git", "commit", "-m", "legacy work", "--no-verify"],
                   cwd=tmp_path, capture_output=True, text=True)
    subprocess.run(["git", "checkout", "master"], cwd=tmp_path,
                   capture_output=True, text=True, check=False)
    subprocess.run(["git", "checkout", "main"], cwd=tmp_path,
                   capture_output=True, text=True, check=False)

    disp = FakeDispatcher()
    cfile = tmp_path / ".hydra" / "wf-legacy" / "attended" / "run_legacy.json"
    cfile.parent.mkdir(parents=True, exist_ok=True)
    cursor = {
        "schema": host_bridge.CURSOR_SCHEMA,
        "kind": "engineering",
        "workflow_id": "wf-legacy",
        "run_id": "run_legacy",
        "stage_id": "stage-1",
        "attempt_id": "att-1",
        "project_path": str(tmp_path),
        "repo_root": str(tmp_path),
        "branch": branch,
        "preserved_branch": branch,
        "state": "surfaced",
        "outcome": "revise",
        "final_status": "surfaced",
        "cost_usd": 0.10,
        "tokens_in": 100,
        "tokens_out": 50,
        "smoke_status": None,
        "finalized": True,
        "charged": True,   # the original (buggy) submit already charged this
        "pending_verdict_payload": {
            "attempt_id": "att-1",
            "judge_producer": "codex",
            "judge_model_id": "codex-default",
            "outcome": "pass",
            "critique_md": "looks good",
            "score_json": {},
            "rubric_id": "rfc-2119-normative",
            "idempotency_token": "judge-0",
        },
        "merge": {"merged": False, "error": "discarded_non_complete"},
    }
    host_bridge.save_cursor(cfile, cursor)

    rec = host_bridge.recover_stalled_stage(disp, cursor_file=cfile)

    assert rec["ok"] is not False, rec.get("error")
    assert disp.count("record_verdict") == 1
    verdict_call = next(a for _s, t, a, _q in disp.calls if t == "record_verdict")
    assert verdict_call["idempotency_token"] == "judge-0"
    assert rec["merge"]["merged"] is True
    assert (tmp_path / "legacy_feature.py").exists()
    # already_charged must be honoured: the caller (CLI) is responsible for
    # skipping charge_and_gate, but recover_stalled_stage itself never touches
    # budget -- confirm the flag survives untouched.
    assert rec["already_charged"] is True


def test_recover_stalled_stage_legacy_surfaced_failing_smoke_reverts_merge(tmp_path, monkeypatch):
    """WS2 fix: in the legacy 'surfaced' recovery shape the merge from the
    preserved branch necessarily runs before smoke can (the worktree is
    already gone, so repo_root is the only place left to run it). If smoke
    then FAILS, the recovery must not leave the merge silently landed while
    the cursor/pp ledger both say 'surfaced' -- it must revert the merge
    commit it just created, and the repo's actual file state (not a mock call
    count) must prove the code did not stay in the tree."""
    _init_repo(tmp_path)
    base_sha = _git(["rev-parse", "HEAD"], tmp_path).stdout.strip()
    branch = "attended/legacy-run-failing-smoke"
    subprocess.run(["git", "checkout", "-b", branch], cwd=tmp_path,
                   capture_output=True, text=True, check=False)
    (tmp_path / "legacy_feature.py").write_text("print('legacy')\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, capture_output=True, text=True)
    subprocess.run(["git", "commit", "-m", "legacy work", "--no-verify"],
                   cwd=tmp_path, capture_output=True, text=True)
    subprocess.run(["git", "checkout", "master"], cwd=tmp_path,
                   capture_output=True, text=True, check=False)
    subprocess.run(["git", "checkout", "main"], cwd=tmp_path,
                   capture_output=True, text=True, check=False)

    # Override the autouse "smoke always passes" fixture: this test needs the
    # post-merge smoke to FAIL to exercise the revert path.
    monkeypatch.setattr(host_bridge, "_run_smoke",
                        lambda *a, **k: ("fail", "unit tests failed"))

    disp = FakeDispatcher()
    cfile = tmp_path / ".hydra" / "wf-legacy2" / "attended" / "run_legacy2.json"
    cfile.parent.mkdir(parents=True, exist_ok=True)
    cursor = {
        "schema": host_bridge.CURSOR_SCHEMA,
        "kind": "engineering",
        "workflow_id": "wf-legacy2",
        "run_id": "run_legacy2",
        "stage_id": "stage-1",
        "attempt_id": "att-1",
        "project_path": str(tmp_path),
        "repo_root": str(tmp_path),
        "branch": branch,
        "preserved_branch": branch,
        "state": "surfaced",
        "outcome": "revise",
        "final_status": "surfaced",
        "cost_usd": 0.10,
        "tokens_in": 100,
        "tokens_out": 50,
        "smoke_status": None,
        "finalized": True,
        "charged": True,
        "pending_verdict_payload": {
            "attempt_id": "att-1",
            "judge_producer": "codex",
            "judge_model_id": "codex-default",
            "outcome": "pass",
            "critique_md": "looks good",
            "score_json": {},
            "rubric_id": "rfc-2119-normative",
            "idempotency_token": "judge-1",
        },
        "merge": {"merged": False, "error": "discarded_non_complete"},
    }
    host_bridge.save_cursor(cfile, cursor)

    rec = host_bridge.recover_stalled_stage(disp, cursor_file=cfile)

    assert rec["ok"] is not False, rec.get("error")
    # The merge itself must have succeeded (that part of the ordering is
    # unchanged and correct) ...
    assert rec["merge"]["merged"] is True
    # ... but since smoke failed, the code must not remain in the tree: the
    # merge commit must have been reverted, and the repo's HEAD must be back
    # at the pre-merge base (the observable that actually matters, not a
    # mocked call count).
    assert rec["merge"]["reverted"] is True
    head_after = _git(["rev-parse", "HEAD"], tmp_path).stdout.strip()
    assert head_after != base_sha  # a revert commit was added, not a hard reset
    assert not (tmp_path / "legacy_feature.py").exists(), (
        "smoke failed but the merged file is still present in the working "
        "tree -- the merge was not actually reverted")
    # The cursor and pp ledger both agree the stage did not pass.
    assert rec["status"] == "surfaced"
    assert rec["final_status"] == "surfaced"


def test_recover_stalled_stage_surfaced_no_preserved_branch_falls_back_to_branch(tmp_path):
    """A cleanly-committed engineer leaves the worktree with nothing
    uncommitted, so `_preserve_non_complete_work` never sets
    `preserved_branch` -- but `cursor["branch"]` still names the real branch
    with every commit. Recovery must fall back to it instead of refusing, and
    must record on the cursor + trace which branch it used and that it came
    from the fallback (not preserved_branch)."""
    _init_repo(tmp_path)
    branch = "attended/clean-commit-run"
    subprocess.run(["git", "checkout", "-b", branch], cwd=tmp_path,
                   capture_output=True, text=True, check=False)
    (tmp_path / "clean_feature.py").write_text("print('clean')\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, capture_output=True, text=True)
    subprocess.run(["git", "commit", "-m", "clean work", "--no-verify"],
                   cwd=tmp_path, capture_output=True, text=True)
    subprocess.run(["git", "checkout", "master"], cwd=tmp_path,
                   capture_output=True, text=True, check=False)
    subprocess.run(["git", "checkout", "main"], cwd=tmp_path,
                   capture_output=True, text=True, check=False)

    disp = FakeDispatcher()
    cfile = tmp_path / ".hydra" / "wf-clean" / "attended" / "run_clean.json"
    cfile.parent.mkdir(parents=True, exist_ok=True)
    cursor = {
        "schema": host_bridge.CURSOR_SCHEMA,
        "kind": "engineering",
        "workflow_id": "wf-clean",
        "run_id": "run_clean",
        "stage_id": "stage-1",
        "attempt_id": "att-1",
        "project_path": str(tmp_path),
        "repo_root": str(tmp_path),
        "branch": branch,
        # NOTE: no preserved_branch -- the engineer committed everything
        # cleanly, so _preserve_non_complete_work never ran/never set it.
        "state": "surfaced",
        "outcome": "revise",
        "final_status": "surfaced",
        "cost_usd": 0.10,
        "tokens_in": 100,
        "tokens_out": 50,
        "smoke_status": None,
        "finalized": True,
        "charged": True,
        "pending_verdict_payload": {
            "attempt_id": "att-1",
            "judge_producer": "codex",
            "judge_model_id": "codex-default",
            "outcome": "pass",
            "critique_md": "looks good",
            "score_json": {},
            "rubric_id": "rfc-2119-normative",
            "idempotency_token": "judge-clean-0",
        },
        "merge": {"merged": False, "error": "discarded_non_complete"},
    }
    host_bridge.save_cursor(cfile, cursor)

    rec = host_bridge.recover_stalled_stage(disp, cursor_file=cfile)

    assert rec["ok"] is not False, rec.get("error")
    assert rec["merge"]["merged"] is True
    assert (tmp_path / "clean_feature.py").exists()
    saved = host_bridge.load_cursor(cfile)
    assert saved["recovery_branch"] == branch
    assert saved["recovery_branch_source"] == "branch_fallback"


def test_recover_stalled_stage_surfaced_no_branch_at_all_refuses(tmp_path):
    """When neither preserved_branch nor a valid cursor['branch'] exists,
    recovery must still refuse cleanly with a clear error rather than
    fabricating a branch to merge from."""
    _init_repo(tmp_path)
    disp = FakeDispatcher()
    cfile = tmp_path / ".hydra" / "wf-nobranch" / "attended" / "run_nobranch.json"
    cfile.parent.mkdir(parents=True, exist_ok=True)
    cursor = {
        "schema": host_bridge.CURSOR_SCHEMA,
        "kind": "engineering",
        "workflow_id": "wf-nobranch",
        "run_id": "run_nobranch",
        "stage_id": "stage-1",
        "attempt_id": "att-1",
        "project_path": str(tmp_path),
        "repo_root": str(tmp_path),
        # branch names a ref that was never created (e.g. gc'd or never
        # pushed) -- must not be treated as recoverable.
        "branch": "attended/never-existed",
        "state": "surfaced",
        "outcome": "revise",
        "final_status": "surfaced",
        "finalized": True,
        "charged": True,
        "merge": {"merged": False, "error": "discarded_non_complete"},
    }
    host_bridge.save_cursor(cfile, cursor)

    rec = host_bridge.recover_stalled_stage(disp, cursor_file=cfile)

    assert rec["ok"] is False
    assert "no recoverable" in rec["error"] or "no preserved_branch" in rec["error"]
    assert disp.count("record_verdict") == 0


def test_recover_stalled_stage_branch_with_no_commits_beyond_base_surfaces_honestly(tmp_path):
    """The fallback branch exists but was branched off HEAD and never
    advanced (no commits beyond base) -- there is nothing to merge.
    Recovery must surface `no_changes_to_merge` honestly rather than
    reporting success."""
    _init_repo(tmp_path)
    branch = "attended/empty-branch-run"
    subprocess.run(["git", "branch", branch], cwd=tmp_path,
                   capture_output=True, text=True, check=False)

    disp = FakeDispatcher()
    cfile = tmp_path / ".hydra" / "wf-empty" / "attended" / "run_empty.json"
    cfile.parent.mkdir(parents=True, exist_ok=True)
    cursor = {
        "schema": host_bridge.CURSOR_SCHEMA,
        "kind": "engineering",
        "workflow_id": "wf-empty",
        "run_id": "run_empty",
        "stage_id": "stage-1",
        "attempt_id": "att-1",
        "project_path": str(tmp_path),
        "repo_root": str(tmp_path),
        "branch": branch,
        "state": "surfaced",
        "outcome": "revise",
        "final_status": "surfaced",
        "finalized": True,
        "charged": True,
        "pending_verdict_payload": {
            "attempt_id": "att-1",
            "judge_producer": "codex",
            "judge_model_id": "codex-default",
            "outcome": "pass",
            "critique_md": "looks good",
            "score_json": {},
            "rubric_id": "rfc-2119-normative",
            "idempotency_token": "judge-empty-0",
        },
        "merge": {"merged": False, "error": "discarded_non_complete"},
    }
    host_bridge.save_cursor(cfile, cursor)

    rec = host_bridge.recover_stalled_stage(disp, cursor_file=cfile)

    assert rec["ok"] is False
    assert "no_changes_to_merge" in rec["error"]
    saved = host_bridge.load_cursor(cfile)
    # Must not claim complete/passed when there was nothing to merge.
    assert saved.get("final_status") != "complete"


def test_revert_merge_commit_conflict_clean_abort(tmp_path):
    """When git revert fails but nothing else is wrong with repo_root, the
    subsequent `git revert --abort` succeeds and cleanly restores the repo.
    This must be distinguishable, on the repo's REAL state, from the case
    where the abort itself fails (next test).

    A revert of the tip commit against a working tree that exactly matches
    it can never conflict on its own (the reverse patch is guaranteed to
    apply cleanly against the very tree it was diffed from) -- the only real
    way `git revert` fails here is exactly the "unrelated concurrent
    activity" scenario named in the fix: a pre-existing, already-in-progress
    (and independently abortable) revert sequencer left on an unrelated file.
    That is reproduced for real below (an actual `git revert --no-commit`
    that genuinely conflicts on `g.txt`), not simulated. The observable that
    proves a clean abort: no revert sequencer state is left behind
    (`.git/REVERT_HEAD` absent, `git status --porcelain` empty), the
    unrelated file is restored to its pre-seed content, and HEAD is
    unchanged."""
    _init_repo(tmp_path)
    base_branch = subprocess.run(
        ["git", "branch", "--show-current"], cwd=tmp_path,
        capture_output=True, text=True, check=False,
    ).stdout.strip()

    (tmp_path / "g.txt").write_text("x\n", encoding="utf-8")
    _git(["add", "-A"], tmp_path)
    _git(["commit", "-m", "c0", "--no-verify"], tmp_path)

    (tmp_path / "g.txt").write_text("y\n", encoding="utf-8")
    _git(["add", "-A"], tmp_path)
    _git(["commit", "-m", "c1-seed-target", "--no-verify"], tmp_path)
    seed_target_sha = _git(["rev-parse", "HEAD"], tmp_path).stdout.strip()

    (tmp_path / "g.txt").write_text("z\n", encoding="utf-8")
    _git(["add", "-A"], tmp_path)
    _git(["commit", "-m", "c2", "--no-verify"], tmp_path)

    _git(["checkout", "-b", "feature"], tmp_path)
    (tmp_path / "conflict.py").write_text("feature-change\n", encoding="utf-8")
    _git(["add", "-A"], tmp_path)
    _git(["commit", "-m", "cf", "--no-verify"], tmp_path)

    _git(["checkout", base_branch], tmp_path)
    pre_merge_base = _git(["rev-parse", "HEAD"], tmp_path).stdout.strip()
    merge_res = _git(["merge", "--no-ff", "--no-edit", "feature"], tmp_path)
    assert merge_res.returncode == 0, merge_res.stderr
    merge_sha = _git(["rev-parse", "HEAD"], tmp_path).stdout.strip()

    # Seed a REAL, independently-abortable conflicted revert sequencer on the
    # unrelated g.txt (context "y" no longer matches current "z" -- a
    # genuine conflict, not a dirty-tree preflight refusal), then leave it
    # dangling exactly as unrelated concurrent activity would.
    seed = _git(["revert", "--no-commit", seed_target_sha], tmp_path)
    assert seed.returncode != 0, "expected the seed revert to genuinely conflict"
    assert (tmp_path / ".git" / "REVERT_HEAD").exists()
    assert _git(["rev-parse", "HEAD"], tmp_path).stdout.strip() == merge_sha

    out = host_bridge._revert_merge_commit(
        str(tmp_path), merge_sha, expected_base=pre_merge_base)

    assert out["reverted"] is False
    assert out["abort_failed"] is False
    assert out["abort_state"] == "clean"
    assert out["error"] and "revert_failed" in out["error"]
    assert "abort_failed" not in out["error"]

    # Real observables (not a mock call count): the sequencer state left by
    # the seeded conflict is gone, the unrelated file is back to its clean
    # pre-seed content, and HEAD never moved -- proof the abort actually ran
    # for real and restored repo_root.
    assert not (tmp_path / ".git" / "REVERT_HEAD").exists()
    status = _git(["status", "--porcelain"], tmp_path).stdout
    assert status.strip() == "", f"expected a clean repo after abort, got: {status!r}"
    assert (tmp_path / "g.txt").read_text(encoding="utf-8") == "z\n"
    assert _git(["rev-parse", "HEAD"], tmp_path).stdout.strip() == merge_sha


def test_revert_merge_commit_preflight_refusal_is_not_reported_as_abort_failed(tmp_path):
    """Regression guard for the false-positive the naive fix would have
    introduced: when `git revert` refuses BEFORE ever starting a sequencer
    (here, a dirty uncommitted edit overlapping the file it must touch --
    git's real, unmocked "local changes would be overwritten" preflight
    check), the subsequent `git revert --abort` legitimately exits nonzero
    ("no revert in progress") even though repo_root was never left dirty by
    this call at all. Checking the abort's exit code alone would misreport
    this extremely common shape as abort_failed=True and send an operator to
    inspect a repo that was already clean. The real observable that proves
    it: no sequencer state (`.git/REVERT_HEAD` / `.git/sequencer`) was ever
    created, so `abort_failed` must be False even though `git revert --abort`
    itself returned nonzero."""
    _init_repo(tmp_path)
    base_branch = subprocess.run(
        ["git", "branch", "--show-current"], cwd=tmp_path,
        capture_output=True, text=True, check=False,
    ).stdout.strip()

    (tmp_path / "conflict.py").write_text("line1\n", encoding="utf-8")
    _git(["add", "-A"], tmp_path)
    _git(["commit", "-m", "c1", "--no-verify"], tmp_path)

    _git(["checkout", "-b", "feature2"], tmp_path)
    (tmp_path / "conflict.py").write_text("feature-change\n", encoding="utf-8")
    _git(["add", "-A"], tmp_path)
    _git(["commit", "-m", "c2-feature", "--no-verify"], tmp_path)

    _git(["checkout", base_branch], tmp_path)
    pre_merge_base = _git(["rev-parse", "HEAD"], tmp_path).stdout.strip()
    merge_res = _git(["merge", "--no-ff", "--no-edit", "feature2"], tmp_path)
    assert merge_res.returncode == 0
    merge_sha = _git(["rev-parse", "HEAD"], tmp_path).stdout.strip()

    # Dirty uncommitted edit overlapping the file the revert must touch --
    # git refuses the revert upfront, before any sequencer exists.
    (tmp_path / "conflict.py").write_text("uncommitted-dirty-edit\n", encoding="utf-8")

    out = host_bridge._revert_merge_commit(
        str(tmp_path), merge_sha, expected_base=pre_merge_base)

    assert out["reverted"] is False
    # The key assertion this test exists for: NOT abort_failed, despite
    # `git revert --abort` itself having exited nonzero underneath.
    assert out["abort_failed"] is False
    assert out["error"] and "revert_failed" in out["error"]
    assert "abort_failed" not in out["error"]

    # Real observables: no sequencer was ever created (nothing to clean up),
    # the dirty edit git refused to touch is untouched, and HEAD never moved.
    assert not (tmp_path / ".git" / "REVERT_HEAD").exists()
    assert (tmp_path / "conflict.py").read_text(encoding="utf-8") == "uncommitted-dirty-edit\n"
    assert _git(["rev-parse", "HEAD"], tmp_path).stdout.strip() == merge_sha


def test_revert_merge_commit_genuine_abort_failure_leaves_sequencer_state(tmp_path, monkeypatch):
    """The failure mode this fix targets for real: a genuine sequencer is
    active (seeded exactly as in the clean-abort test -- a real, conflicting
    `git revert --no-commit` on an unrelated file, left dangling as unrelated
    concurrent activity would) and the abort that should tear it down does
    not. Only the `revert --abort` call itself is intercepted to not execute
    (simulating e.g. a permission/lock failure on that specific operation);
    the preceding revert failure and the seeded conflict are both real,
    unmocked git state. The distinguishing observable from the clean-abort
    case: the sequencer file genuinely still exists on disk afterward."""
    _init_repo(tmp_path)
    base_branch = subprocess.run(
        ["git", "branch", "--show-current"], cwd=tmp_path,
        capture_output=True, text=True, check=False,
    ).stdout.strip()

    (tmp_path / "g.txt").write_text("x\n", encoding="utf-8")
    _git(["add", "-A"], tmp_path)
    _git(["commit", "-m", "c0", "--no-verify"], tmp_path)

    (tmp_path / "g.txt").write_text("y\n", encoding="utf-8")
    _git(["add", "-A"], tmp_path)
    _git(["commit", "-m", "c1-seed-target", "--no-verify"], tmp_path)
    seed_target_sha = _git(["rev-parse", "HEAD"], tmp_path).stdout.strip()

    (tmp_path / "g.txt").write_text("z\n", encoding="utf-8")
    _git(["add", "-A"], tmp_path)
    _git(["commit", "-m", "c2", "--no-verify"], tmp_path)

    _git(["checkout", "-b", "feature3"], tmp_path)
    (tmp_path / "conflict.py").write_text("feature-change\n", encoding="utf-8")
    _git(["add", "-A"], tmp_path)
    _git(["commit", "-m", "cf", "--no-verify"], tmp_path)

    _git(["checkout", base_branch], tmp_path)
    pre_merge_base = _git(["rev-parse", "HEAD"], tmp_path).stdout.strip()
    merge_res = _git(["merge", "--no-ff", "--no-edit", "feature3"], tmp_path)
    assert merge_res.returncode == 0, merge_res.stderr
    merge_sha = _git(["rev-parse", "HEAD"], tmp_path).stdout.strip()

    seed = _git(["revert", "--no-commit", seed_target_sha], tmp_path)
    assert seed.returncode != 0, "expected the seed revert to genuinely conflict"
    assert (tmp_path / ".git" / "REVERT_HEAD").exists()

    real_git = host_bridge._git

    def _sabotage_abort_only(args, cwd, *a, **k):
        if args[:2] == ["revert", "--abort"]:
            # Do NOT run the real abort -- the seeded sequencer state is left
            # exactly as-is, as if the abort attempt itself failed to tear
            # it down (disk lock, permission error, etc).
            return subprocess.CompletedProcess(
                args=["git", *args], returncode=1, stdout="",
                stderr="fatal: simulated abort failure (sequencer untouched)",
            )
        return real_git(args, cwd, *a, **k)

    monkeypatch.setattr(host_bridge, "_git", _sabotage_abort_only)

    out = host_bridge._revert_merge_commit(
        str(tmp_path), merge_sha, expected_base=pre_merge_base)

    assert out["reverted"] is False
    assert out["abort_failed"] is True
    assert out["abort_state"] == "active"
    assert out["error"] and "abort_failed" in out["error"]

    # Real observable that distinguishes this from both the clean-abort case
    # AND the preflight-refusal case: the sequencer state is still genuinely
    # present on disk -- repo_root was NOT restored, which is exactly what
    # the operator needs to know before retrying anything.
    assert (tmp_path / ".git" / "REVERT_HEAD").exists()
    assert _git(["rev-parse", "HEAD"], tmp_path).stdout.strip() == merge_sha


def test_revert_merge_commit_active_vs_unknown_produce_distinguishable_state(tmp_path, monkeypatch):
    """Regression guard against a future bool-collapse: ``abort_failed`` is
    True for BOTH the "active" (genuine sequencer left dirty) and "unknown"
    (git dir unresolvable, cleanliness unverified) cases -- that shared bool
    is exactly the kind of value a future consumer could key on and silently
    discard the distinction ``abort_state`` exists to preserve. This test
    pins that ``abort_state`` (and the error text derived from it) must stay
    genuinely different between the two cases, not merely the same truthy
    ``abort_failed``, using two real, independently-seeded scenarios rather
    than mocked call counts.

    Scenario A ("active"): a real conflicting sequencer is seeded and the
    `revert --abort` call is sabotaged to not run, so the sequencer
    genuinely remains on disk afterward -- mirrors
    test_revert_merge_commit_genuine_abort_failure_leaves_sequencer_state.

    Scenario B ("unknown"): the same seeded conflict, but only
    `rev-parse --git-dir` is sabotaged; the real `revert --abort`
    underneath is left unmocked and genuinely succeeds, so the repo is
    actually clean afterward, but the function never got to verify that --
    mirrors
    test_revert_merge_commit_uninspectable_git_dir_fails_toward_abort_failed.

    If a future change collapsed both branches onto a single
    ``abort_failed`` bool without keeping ``abort_state``/error text
    distinct, this test fails."""

    def _seed_conflicted_merge(repo_path, branch_name):
        _init_repo(repo_path)
        base_branch = subprocess.run(
            ["git", "branch", "--show-current"], cwd=repo_path,
            capture_output=True, text=True, check=False,
        ).stdout.strip()

        (repo_path / "g.txt").write_text("x\n", encoding="utf-8")
        _git(["add", "-A"], repo_path)
        _git(["commit", "-m", "c0", "--no-verify"], repo_path)

        (repo_path / "g.txt").write_text("y\n", encoding="utf-8")
        _git(["add", "-A"], repo_path)
        _git(["commit", "-m", "c1-seed-target", "--no-verify"], repo_path)
        seed_target_sha = _git(["rev-parse", "HEAD"], repo_path).stdout.strip()

        (repo_path / "g.txt").write_text("z\n", encoding="utf-8")
        _git(["add", "-A"], repo_path)
        _git(["commit", "-m", "c2", "--no-verify"], repo_path)

        _git(["checkout", "-b", branch_name], repo_path)
        (repo_path / "conflict.py").write_text("feature-change\n", encoding="utf-8")
        _git(["add", "-A"], repo_path)
        _git(["commit", "-m", "cf", "--no-verify"], repo_path)

        _git(["checkout", base_branch], repo_path)
        pre_merge_base = _git(["rev-parse", "HEAD"], repo_path).stdout.strip()
        merge_res = _git(["merge", "--no-ff", "--no-edit", branch_name], repo_path)
        assert merge_res.returncode == 0, merge_res.stderr
        merge_sha = _git(["rev-parse", "HEAD"], repo_path).stdout.strip()

        seed = _git(["revert", "--no-commit", seed_target_sha], repo_path)
        assert seed.returncode != 0, "expected the seed revert to genuinely conflict"
        assert (repo_path / ".git" / "REVERT_HEAD").exists()
        return merge_sha, pre_merge_base

    # Scenario A: sequencer genuinely stays active (abort sabotaged).
    repo_a = tmp_path / "repo_a"
    repo_a.mkdir()
    merge_sha_a, pre_merge_base_a = _seed_conflicted_merge(repo_a, "feature-active")
    real_git = host_bridge._git

    def _sabotage_abort_only(args, cwd, *a, **k):
        if args[:2] == ["revert", "--abort"]:
            return subprocess.CompletedProcess(
                args=["git", *args], returncode=1, stdout="",
                stderr="fatal: simulated abort failure (sequencer untouched)",
            )
        return real_git(args, cwd, *a, **k)

    monkeypatch.setattr(host_bridge, "_git", _sabotage_abort_only)
    out_active = host_bridge._revert_merge_commit(
        str(repo_a), merge_sha_a, expected_base=pre_merge_base_a)
    monkeypatch.undo()

    # Scenario B: sequencer state genuinely unverifiable (git-dir sabotaged),
    # even though the real abort underneath actually succeeds.
    repo_b = tmp_path / "repo_b"
    repo_b.mkdir()
    merge_sha_b, pre_merge_base_b = _seed_conflicted_merge(repo_b, "feature-unknown")
    real_git_b = host_bridge._git

    def _sabotage_git_dir_resolution(args, cwd, *a, **k):
        if args[:2] == ["rev-parse", "--git-dir"]:
            return subprocess.CompletedProcess(
                args=["git", *args], returncode=128, stdout="",
                stderr="fatal: simulated inability to resolve the git dir",
            )
        return real_git_b(args, cwd, *a, **k)

    monkeypatch.setattr(host_bridge, "_git", _sabotage_git_dir_resolution)
    out_unknown = host_bridge._revert_merge_commit(
        str(repo_b), merge_sha_b, expected_base=pre_merge_base_b)
    monkeypatch.undo()

    # Both share the same truthy abort_failed bool -- that alone must not
    # be treated as proof the two cases are the same.
    assert out_active["abort_failed"] is True
    assert out_unknown["abort_failed"] is True

    # The distinguishing fact must survive as a distinct abort_state value.
    assert out_active["abort_state"] == "active"
    assert out_unknown["abort_state"] == "unknown"
    assert out_active["abort_state"] != out_unknown["abort_state"]

    # And as distinct, non-overlapping error markers -- a consumer reading
    # only out["error"] must also be able to tell the two apart.
    assert out_active["error"] and "sequencer state still present" in out_active["error"]
    assert "abort_state_unknown" not in out_active["error"]
    assert out_unknown["error"] and "abort_state_unknown" in out_unknown["error"]
    assert "sequencer state still present" not in out_unknown["error"]

    # Confirm the two real, independently-seeded repos actually ended up in
    # different physical states, so the distinction being pinned above is
    # real and not an artifact of identical mocking.
    assert (repo_a / ".git" / "REVERT_HEAD").exists()
    assert not (repo_b / ".git" / "REVERT_HEAD").exists()


def test_revert_merge_commit_uninspectable_git_dir_fails_toward_abort_failed(tmp_path, monkeypatch):
    """Regression guard for the fail-open defect: if repo_root's git dir
    cannot be resolved after the abort attempt, that is a THIRD, distinct
    fact from both "sequencer active" and "sequencer clean" -- the state is
    genuinely unverified, and the function must fail TOWARD abort_failed
    (report it as unresolved, not silently as clean).

    This seeds a REAL dangling conflicted sequencer, then only intercepts
    the `git rev-parse --git-dir` call (simulating e.g. a disk or permission
    error at exactly that check) -- the real `git revert --abort` call
    underneath is left completely unmodified and genuinely runs, and
    genuinely succeeds at cleaning up the seeded conflict (confirmed below
    by an independent filesystem check that bypasses the function). The
    point this proves: even though the repo ends up ACTUALLY clean, the
    function must not report "clean" -- it never got to verify that, so it
    must report "unknown" regardless of what really happened. It is not
    allowed to peek at ground truth or get credit for a lucky outcome; only
    a real, completed check is allowed to produce "clean"."""
    _init_repo(tmp_path)
    base_branch = subprocess.run(
        ["git", "branch", "--show-current"], cwd=tmp_path,
        capture_output=True, text=True, check=False,
    ).stdout.strip()

    (tmp_path / "g.txt").write_text("x\n", encoding="utf-8")
    _git(["add", "-A"], tmp_path)
    _git(["commit", "-m", "c0", "--no-verify"], tmp_path)

    (tmp_path / "g.txt").write_text("y\n", encoding="utf-8")
    _git(["add", "-A"], tmp_path)
    _git(["commit", "-m", "c1-seed-target", "--no-verify"], tmp_path)
    seed_target_sha = _git(["rev-parse", "HEAD"], tmp_path).stdout.strip()

    (tmp_path / "g.txt").write_text("z\n", encoding="utf-8")
    _git(["add", "-A"], tmp_path)
    _git(["commit", "-m", "c2", "--no-verify"], tmp_path)

    _git(["checkout", "-b", "feature5"], tmp_path)
    (tmp_path / "conflict.py").write_text("feature-change\n", encoding="utf-8")
    _git(["add", "-A"], tmp_path)
    _git(["commit", "-m", "cf", "--no-verify"], tmp_path)

    _git(["checkout", base_branch], tmp_path)
    pre_merge_base = _git(["rev-parse", "HEAD"], tmp_path).stdout.strip()
    merge_res = _git(["merge", "--no-ff", "--no-edit", "feature5"], tmp_path)
    assert merge_res.returncode == 0, merge_res.stderr
    merge_sha = _git(["rev-parse", "HEAD"], tmp_path).stdout.strip()

    seed = _git(["revert", "--no-commit", seed_target_sha], tmp_path)
    assert seed.returncode != 0, "expected the seed revert to genuinely conflict"
    assert (tmp_path / ".git" / "REVERT_HEAD").exists()

    real_git = host_bridge._git

    def _sabotage_git_dir_resolution(args, cwd, *a, **k):
        if args[:2] == ["rev-parse", "--git-dir"]:
            return subprocess.CompletedProcess(
                args=["git", *args], returncode=128, stdout="",
                stderr="fatal: simulated inability to resolve the git dir",
            )
        return real_git(args, cwd, *a, **k)

    monkeypatch.setattr(host_bridge, "_git", _sabotage_git_dir_resolution)

    out = host_bridge._revert_merge_commit(
        str(tmp_path), merge_sha, expected_base=pre_merge_base)

    assert out["reverted"] is False
    # The key assertions this test exists for: uninspectable fails TOWARD
    # abort_failed=True, with a marker distinct from the "found active" case
    # -- an operator reading the error must be able to tell "we checked and
    # it's dirty" apart from "we couldn't check at all".
    assert out["abort_state"] == "unknown"
    assert out["abort_failed"] is True
    assert out["error"] and "abort_state_unknown" in out["error"]
    assert "sequencer state still present" not in out["error"]

    # Independent real observable (bypassing the function's own -- sabotaged
    # -- git-dir check): the real, unmodified `git revert --abort` call
    # underneath actually ran and actually succeeded, so the sequencer is
    # genuinely gone and the repo is genuinely clean. The function had no
    # way to know that (its own visibility into that fact was cut), and
    # correctly reported "unknown" / abort_failed=True anyway -- proving it
    # fails toward "go look" on real ignorance rather than defaulting to
    # "clean" just because that happens to match reality here.
    assert not (tmp_path / ".git" / "REVERT_HEAD").exists()
    status = _git(["status", "--porcelain"], tmp_path).stdout
    assert status.strip() == ""


def test_revert_merge_commit_resolves_git_dir_for_linked_worktree(tmp_path):
    """The abort-vs-state check must resolve repo_root's REAL git dir via
    `git rev-parse --git-dir`, not assume `<repo_root>/.git` -- for a linked
    worktree that assumption is wrong (the git dir lives under the main
    repo's `.git/worktrees/<name>`), which would make the sequencer-state
    check silently never fire (permanently see "no state" and always report
    abort_failed=False, masking genuine failures) or, if some other path
    assumption were used, look in the wrong place entirely. Reproduce the
    exact clean-abort scenario from the dedicated test above, but with
    repo_root itself being a real `git worktree add` checkout, and confirm
    the function still correctly proves via the worktree's real git dir that
    the abort actually cleaned up the seeded conflict."""
    from pathlib import Path

    main_repo = tmp_path / "main"
    main_repo.mkdir()
    _init_repo(main_repo)
    base_branch = subprocess.run(
        ["git", "branch", "--show-current"], cwd=main_repo,
        capture_output=True, text=True, check=False,
    ).stdout.strip()

    (main_repo / "g.txt").write_text("x\n", encoding="utf-8")
    _git(["add", "-A"], main_repo)
    _git(["commit", "-m", "c0", "--no-verify"], main_repo)

    (main_repo / "g.txt").write_text("y\n", encoding="utf-8")
    _git(["add", "-A"], main_repo)
    _git(["commit", "-m", "c1-seed-target", "--no-verify"], main_repo)
    seed_target_sha = _git(["rev-parse", "HEAD"], main_repo).stdout.strip()

    (main_repo / "g.txt").write_text("z\n", encoding="utf-8")
    _git(["add", "-A"], main_repo)
    _git(["commit", "-m", "c2", "--no-verify"], main_repo)

    _git(["checkout", "-b", "feature4"], main_repo)
    (main_repo / "conflict.py").write_text("feature-change\n", encoding="utf-8")
    _git(["add", "-A"], main_repo)
    _git(["commit", "-m", "cf", "--no-verify"], main_repo)
    _git(["checkout", base_branch], main_repo)

    wt_path = tmp_path / "linked-wt"
    wt_branch = "attended/linked-wt"
    wt_res = _git(["worktree", "add", "-b", wt_branch, str(wt_path), base_branch], main_repo)
    assert wt_res.returncode == 0, wt_res.stderr
    assert not (wt_path / ".git").is_dir(), "expected a linked worktree (gitdir file, not a real .git dir)"

    pre_merge_base = _git(["rev-parse", "HEAD"], wt_path).stdout.strip()
    merge_res = _git(["merge", "--no-ff", "--no-edit", "feature4"], wt_path)
    assert merge_res.returncode == 0, merge_res.stderr
    merge_sha = _git(["rev-parse", "HEAD"], wt_path).stdout.strip()

    seed = _git(["revert", "--no-commit", seed_target_sha], wt_path)
    assert seed.returncode != 0, "expected the seed revert to genuinely conflict"

    # The real git dir for this worktree, resolved the same way the fix
    # does -- used only to assert on, never assumed by the test setup above.
    real_git_dir = Path(
        _git(["rev-parse", "--git-dir"], wt_path).stdout.strip()
    )
    if not real_git_dir.is_absolute():
        real_git_dir = wt_path / real_git_dir
    assert real_git_dir.resolve() != (wt_path / ".git").resolve()
    assert (real_git_dir / "REVERT_HEAD").exists()

    out = host_bridge._revert_merge_commit(
        str(wt_path), merge_sha, expected_base=pre_merge_base)

    assert out["reverted"] is False
    assert out["abort_failed"] is False
    assert out["abort_state"] == "clean"
    assert out["error"] and "revert_failed" in out["error"]

    # Real observable, checked at the worktree's actual git dir (not
    # `<wt_path>/.git`): the seeded sequencer is genuinely gone.
    assert not (real_git_dir / "REVERT_HEAD").exists()
    status = _git(["status", "--porcelain"], wt_path).stdout
    assert status.strip() == "", f"expected a clean worktree after abort, got: {status!r}"


# --------------------------------------------------------------------------- #
# Merge no-op / already-merged detection (data-loss fix)                      #
# --------------------------------------------------------------------------- #
# _merge_branch_back's old guard only caught "branch tip IS current HEAD"
# (branch_sha == base). For a branch that was ALREADY merged earlier (tip
# is an ANCESTOR of HEAD, not equal to it), `git merge --no-ff --no-edit
# <branch>` prints "Already up to date.", exits 0, and creates no commit --
# yet the old code unconditionally read `rev-parse HEAD` afterward and
# reported it as a fabricated "merged" sha. `recover_stalled_stage` then
# reverted that unrelated commit on any non-passing outcome. These tests
# pin the fix (before/after HEAD comparison) and its downstream safety
# consequence.

def test_merge_branch_back_already_merged_branch_reports_no_merge_no_sha(tmp_path):
    """A branch whose tip is an ancestor of HEAD (already merged earlier)
    must be reported as merged=False with the already_merged marker and NO
    sha -- not a fabricated merged=True pointing at whatever unrelated
    commit HEAD already was.

    This is the test that FAILS if Fix 1 is reverted: with the old
    branch_sha == base guard, branch_sha (an ancestor, not equal to base)
    does not match and the code falls through to git merge, which no-ops,
    then unconditionally reports merged=True with HEAD's unrelated sha."""
    _init_repo(tmp_path)
    base_branch = subprocess.run(
        ["git", "branch", "--show-current"], cwd=tmp_path,
        capture_output=True, text=True, check=False,
    ).stdout.strip()

    branch = "attended/already-merged"
    _git(["checkout", "-b", branch], tmp_path)
    (tmp_path / "feature.py").write_text("print(1)\n", encoding="utf-8")
    _git(["add", "-A"], tmp_path)
    _git(["commit", "-m", "feature work", "--no-verify"], tmp_path)
    _git(["checkout", base_branch], tmp_path)

    # Merge it in for real, exactly once, BEFORE calling the function under
    # test -- this is what "already merged" means.
    merge_res = _git(["merge", "--no-ff", "--no-edit", branch], tmp_path)
    assert merge_res.returncode == 0, merge_res.stderr

    # An unrelated later commit lands on HEAD -- this is the commit that
    # must NOT be mistaken for a fresh merge of `branch`.
    (tmp_path / "unrelated.py").write_text("print(2)\n", encoding="utf-8")
    _git(["add", "-A"], tmp_path)
    _git(["commit", "-m", "unrelated later work", "--no-verify"], tmp_path)
    unrelated_sha = _git(["rev-parse", "HEAD"], tmp_path).stdout.strip()

    out = host_bridge._merge_branch_back(str(tmp_path), branch)

    assert out["merged"] is False
    assert out["sha"] is None
    assert out["error"] == "already_merged"
    # The repo must be untouched -- no new commit, no rewritten HEAD.
    assert _git(["rev-parse", "HEAD"], tmp_path).stdout.strip() == unrelated_sha


def test_merge_branch_back_genuine_merge_still_works_and_reports_base(tmp_path):
    """The real merge path is unaffected by the fix: a branch with genuine
    new commits merges normally, reports merged=True with the new sha, and
    now additionally reports the pre-merge base for provenance."""
    _init_repo(tmp_path)
    base_branch = subprocess.run(
        ["git", "branch", "--show-current"], cwd=tmp_path,
        capture_output=True, text=True, check=False,
    ).stdout.strip()
    pre_merge_base = _git(["rev-parse", "HEAD"], tmp_path).stdout.strip()

    branch = "attended/genuine-merge"
    _git(["checkout", "-b", branch], tmp_path)
    (tmp_path / "feature2.py").write_text("print(2)\n", encoding="utf-8")
    _git(["add", "-A"], tmp_path)
    _git(["commit", "-m", "feature2 work", "--no-verify"], tmp_path)
    _git(["checkout", base_branch], tmp_path)

    out = host_bridge._merge_branch_back(str(tmp_path), branch)

    assert out["merged"] is True
    assert out["error"] is None
    assert out["base"] == pre_merge_base
    head = _git(["rev-parse", "HEAD"], tmp_path).stdout.strip()
    assert out["sha"] == head
    assert head != pre_merge_base
    assert (tmp_path / "feature2.py").exists()


def test_merge_branch_back_tip_equals_head_still_reports_no_changes_to_merge(tmp_path):
    """The fast-path guard (branch tip IS current HEAD) must still work and
    remain distinguishable from already_merged."""
    _init_repo(tmp_path)
    head = _git(["rev-parse", "HEAD"], tmp_path).stdout.strip()
    branch = "attended/tip-equals-head"
    _git(["branch", branch], tmp_path)  # branch created AT current HEAD, no new commits

    out = host_bridge._merge_branch_back(str(tmp_path), branch)

    assert out["merged"] is False
    assert out["sha"] is None
    assert out["error"] == "no_changes_to_merge"
    assert _git(["rev-parse", "HEAD"], tmp_path).stdout.strip() == head


def test_revert_merge_commit_refuses_provenance_mismatch_even_when_head_equals_it(tmp_path):
    """Fix 2's safety invariant: _revert_merge_commit must refuse to revert
    a commit it did not create, EVEN WHEN HEAD == merge_sha (the only
    precondition the old code checked). This reproduces the live
    incident's core defect directly: a caller passes a sha that happens to
    be HEAD but was never produced by merging from the caller's claimed
    expected_base.

    This is the test that FAILS if Fix 2 is reverted (or expected_base is
    dropped/ignored): the old code has no way to distinguish "a merge
    commit this call produced" from "literally any commit that happens to
    be HEAD right now", so it would proceed to revert unrelated_sha below,
    discarding an operator's real work."""
    _init_repo(tmp_path)

    # A commit that is NOT a merge this recovery created -- an ordinary
    # single-parent commit an operator made, standing in for the live
    # incident's "merge of a different branch carrying a real fix".
    (tmp_path / "operator_work.py").write_text("print(3)\n", encoding="utf-8")
    _git(["add", "-A"], tmp_path)
    _git(["commit", "-m", "operators real work", "--no-verify"], tmp_path)
    unrelated_sha = _git(["rev-parse", "HEAD"], tmp_path).stdout.strip()

    # A claimed "expected_base" that does NOT match this commit's parent --
    # the mismatch that must trigger refusal.
    bogus_base = "0" * 40

    out = host_bridge._revert_merge_commit(
        str(tmp_path), unrelated_sha, expected_base=bogus_base)

    assert out["reverted"] is False
    assert "provenance_mismatch" in out["error"]
    # The repo must be completely untouched -- no revert attempted at all.
    assert _git(["rev-parse", "HEAD"], tmp_path).stdout.strip() == unrelated_sha
    assert (tmp_path / "operator_work.py").exists()
    status = _git(["status", "--porcelain"], tmp_path).stdout
    assert status.strip() == ""


def test_revert_merge_commit_accepts_matching_provenance_and_still_reverts(tmp_path):
    """The provenance guard must not break the legitimate case: a real
    merge commit whose mainline parent genuinely equals the caller's
    observed expected_base still reverts correctly."""
    _init_repo(tmp_path)
    base_branch = subprocess.run(
        ["git", "branch", "--show-current"], cwd=tmp_path,
        capture_output=True, text=True, check=False,
    ).stdout.strip()
    pre_merge_base = _git(["rev-parse", "HEAD"], tmp_path).stdout.strip()

    branch = "attended/provenance-ok"
    _git(["checkout", "-b", branch], tmp_path)
    (tmp_path / "feature3.py").write_text("print(3)\n", encoding="utf-8")
    _git(["add", "-A"], tmp_path)
    _git(["commit", "-m", "feature3 work", "--no-verify"], tmp_path)
    _git(["checkout", base_branch], tmp_path)

    merge_res = _git(["merge", "--no-ff", "--no-edit", branch], tmp_path)
    assert merge_res.returncode == 0, merge_res.stderr
    merge_sha = _git(["rev-parse", "HEAD"], tmp_path).stdout.strip()

    out = host_bridge._revert_merge_commit(
        str(tmp_path), merge_sha, expected_base=pre_merge_base)

    assert out["reverted"] is True
    assert not (tmp_path / "feature3.py").exists()


def test_recover_stalled_stage_already_merged_branch_reproduces_live_incident(tmp_path):
    """Direct reproduction of the live incident: a repo with an
    already-merged branch, plus an UNRELATED LATER commit at HEAD carrying
    real operator work, driven through recover_stalled_stage with a
    non-passing outcome. The unrelated commit must survive intact -- no
    revert of any kind may touch it.

    Before the fix, this exact shape caused _merge_branch_back to report
    merged=True with the unrelated commit's sha, and the subsequent revert
    (triggered by the non-passing outcome below) discarded it."""
    _init_repo(tmp_path)
    base_branch = subprocess.run(
        ["git", "branch", "--show-current"], cwd=tmp_path,
        capture_output=True, text=True, check=False,
    ).stdout.strip()

    branch = "attended/already-merged-recovery"
    _git(["checkout", "-b", branch], tmp_path)
    (tmp_path / "old_feature.py").write_text("print(4)\n", encoding="utf-8")
    _git(["add", "-A"], tmp_path)
    _git(["commit", "-m", "old feature work", "--no-verify"], tmp_path)
    _git(["checkout", base_branch], tmp_path)
    merge_res = _git(["merge", "--no-ff", "--no-edit", branch], tmp_path)
    assert merge_res.returncode == 0, merge_res.stderr

    # This stands in for the "760b2bb" commit in the live incident: a real,
    # unrelated, later merge carrying a genuine fix that must never be
    # silently reverted.
    (tmp_path / "important_fix.py").write_text("print(5)\n", encoding="utf-8")
    _git(["add", "-A"], tmp_path)
    _git(["commit", "-m", "unrelated real fix landed later", "--no-verify"], tmp_path)
    protected_sha = _git(["rev-parse", "HEAD"], tmp_path).stdout.strip()

    disp = FakeDispatcher()
    cfile = tmp_path / ".hydra" / "wf-incident" / "attended" / "run_incident.json"
    cfile.parent.mkdir(parents=True, exist_ok=True)
    cursor = {
        "schema": host_bridge.CURSOR_SCHEMA,
        "kind": "engineering",
        "workflow_id": "wf-incident",
        "run_id": "run_incident",
        "stage_id": "stage-1",
        "attempt_id": "att-1",
        "project_path": str(tmp_path),
        "repo_root": str(tmp_path),
        "branch": branch,
        "preserved_branch": branch,
        "state": "surfaced",
        "outcome": "revise",  # non-passing outcome
        "final_status": "surfaced",
        "cost_usd": 0.10,
        "tokens_in": 100,
        "tokens_out": 50,
        "smoke_status": None,
        "finalized": True,
        "charged": True,
        "pending_verdict_payload": {
            "attempt_id": "att-1",
            "judge_producer": "codex",
            "judge_model_id": "codex-default",
            "outcome": "revise",
            "critique_md": "needs work",
            "score_json": {},
            "rubric_id": "rfc-2119-normative",
            "idempotency_token": "judge-incident-0",
        },
        "merge": {"merged": False, "error": "discarded_non_complete"},
    }
    host_bridge.save_cursor(cfile, cursor)

    rec = host_bridge.recover_stalled_stage(disp, cursor_file=cfile)

    # Recovery correctly fails to merge (there was nothing new to merge from
    # the already-merged branch) -- but the point of this test is what did
    # NOT happen: no revert of the protected, unrelated commit.
    assert rec["merge"]["merged"] is False
    assert rec["merge"]["error"] == "already_merged"
    assert rec["merge"].get("reverted") is not True
    head_after = _git(["rev-parse", "HEAD"], tmp_path).stdout.strip()
    assert head_after == protected_sha, (
        "the unrelated, already-landed commit was touched by recovery -- "
        "this is the exact data-loss shape from the live incident")
    assert (tmp_path / "important_fix.py").exists()
    assert (tmp_path / "old_feature.py").exists()


# --------------------------------------------------------------------------- #
# E2-27: judge_model_id validation / normalization                            #
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _clear_judge_pin_cache():
    """The doctor probe is cached process-wide; keep tests independent."""
    host_bridge._reset_judge_model_pin_cache()
    yield
    host_bridge._reset_judge_model_pin_cache()


class _FakeDispatcherWithDoctor(FakeDispatcher):
    """FakeDispatcher serving pp's doctor judge_capabilities map, and
    enforcing pp's real record_verdict judge-model pin."""

    def __init__(self, *, enforce_pin: bool = True, **kw):
        super().__init__(**kw)
        self._enforce_pin = enforce_pin
        self.verdicts: list[dict] = []

    def call_mcp(self, server, tool, args, squad_id=None):
        if tool == "doctor":
            self.calls.append((server, tool, dict(args), squad_id))
            return {"status": "done", "result": {"judge_capabilities": {
                "codex": {"critique_model": "gpt-5.4"},
                "agy": {"critique_model": "gemini-3.1-pro-preview"},
                "claude": {"critique_model": None},
            }}}
        if tool == "record_verdict":
            self.calls.append((server, tool, dict(args), squad_id))
            self.verdicts.append(dict(args))
            if self._enforce_pin and args.get("judge_producer") == "codex" \
                    and args.get("judge_model_id") not in {"gpt-5.4", "gpt-5.5"}:
                return {"status": "failed", "error": (
                    "judge_producer=codex must record judge_model_id in "
                    "{gpt-5.4, gpt-5.5} because pp_codex.critique is pinned "
                    "to those models (default or escalated)")}
            return {"status": "done", "result": {"verdict_id": "v-1"}}
        return super().call_mcp(server, tool, args, squad_id)


def _drive_to_judge(disp, tmp_path):
    res = _begin(disp, tmp_path)
    cfile = res["cursor_path"]
    res = host_bridge.submit_host_result(
        disp, cursor_file=cfile, call_key="generate-0",
        result={"text": "edited foo.py", "cost_usd": 0.10,
                "model": "claude-opus-4-8"})
    return cfile, res


def test_judge_host_action_carries_allowed_model_ids(tmp_path):
    disp = _FakeDispatcherWithDoctor(required_cross_vendor=True)
    _cfile, res = _drive_to_judge(disp, tmp_path)
    allowed = res["host_action"]["allowed_judge_model_ids"]
    assert allowed["codex"] == ["gpt-5.4", "gpt-5.5"]
    assert allowed["agy"] == ["gemini-3.1-pro-preview"]
    assert allowed["claude"] == []
    # The instruction the host relays to the judge names the pinned ids.
    assert "allowed_judge_model_ids" in res["host_action"]["instructions"]
    assert "Never invent" in res["host_action"]["instructions"]
    # doctor ran read-only under the engineering RBAC scope.
    assert ("pp_harness", "doctor", {}, "engineering") in disp.calls


def test_mislabeled_codex_model_id_is_normalized_not_fatal(tmp_path):
    """E2-27: the live incident -- a judge reporting gpt-5.1-codex on a PASS.

    The verdict must be recorded against pp's pinned id, the stage must
    complete, and the judge's own claim must be preserved for audit.
    """
    disp = _FakeDispatcherWithDoctor(required_cross_vendor=True)
    cfile, _ = _drive_to_judge(disp, tmp_path)
    res = host_bridge.submit_host_result(
        disp, cursor_file=cfile,
        call_key="judge-run-1-stage-1-att-1-0",
        result={"outcome": "pass", "critique_md": "looks good",
                "judge_producer": "codex",
                "judge_model_id": "gpt-5.1-codex", "cost_usd": 0.05})

    assert res["status"] == "complete"
    assert res["final_status"] == "complete"
    assert res["stage_outcome"] == "pass"
    assert len(disp.verdicts) == 1
    v = disp.verdicts[0]
    assert v["judge_model_id"] == "gpt-5.4"
    assert v["judge_producer"] == "codex"
    assert v["score_json"]["judge_model_id_reported"] == "gpt-5.1-codex"


def test_missing_model_id_normalizes_to_pin(tmp_path):
    """A judge that reports no model id must not send a made-up default."""
    disp = _FakeDispatcherWithDoctor(required_cross_vendor=True)
    cfile, _ = _drive_to_judge(disp, tmp_path)
    res = host_bridge.submit_host_result(
        disp, cursor_file=cfile,
        call_key="judge-run-1-stage-1-att-1-0",
        result={"outcome": "pass", "critique_md": "ok",
                "judge_producer": "codex", "cost_usd": 0.05})
    assert res["status"] == "complete"
    assert disp.verdicts[0]["judge_model_id"] == "gpt-5.4"
    assert disp.verdicts[0]["score_json"]["judge_model_id_reported"] is None


def test_escalated_codex_model_id_passes_through(tmp_path):
    """gpt-5.5 is a legitimate pp escalation -- do NOT rewrite it to gpt-5.4."""
    disp = _FakeDispatcherWithDoctor(required_cross_vendor=True)
    cfile, _ = _drive_to_judge(disp, tmp_path)
    res = host_bridge.submit_host_result(
        disp, cursor_file=cfile,
        call_key="judge-run-1-stage-1-att-1-0",
        result={"outcome": "pass", "critique_md": "ok",
                "judge_producer": "codex",
                "judge_model_id": "gpt-5.5", "cost_usd": 0.05})
    assert res["status"] == "complete"
    assert disp.verdicts[0]["judge_model_id"] == "gpt-5.5"
    assert "judge_model_id_reported" not in disp.verdicts[0]["score_json"]


def test_same_vendor_claude_model_id_is_not_normalized(tmp_path):
    """pp pins no Claude critique model -- the reported id must survive."""
    disp = _FakeDispatcherWithDoctor(required_cross_vendor=False)
    cfile, _ = _drive_to_judge(disp, tmp_path)
    res = host_bridge.submit_host_result(
        disp, cursor_file=cfile,
        call_key="judge-run-1-stage-1-att-1-0",
        result={"outcome": "pass", "critique_md": "ok",
                "judge_producer": "claude-same-vendor-host",
                "judge_model_id": "claude-haiku-4", "cost_usd": 0.02})
    assert res["status"] == "complete"
    assert disp.verdicts[0]["judge_model_id"] == "claude-haiku-4"
    assert disp.verdicts[0]["judge_producer"] == "claude-same-vendor-host"


def test_record_verdict_pin_error_is_retryable_cursor_stays_await_judge(tmp_path):
    """E2-27 (3): a pp pin rejection must NOT surface a passing stage.

    Simulated by emptying the pin map so host-side normalization is a no-op
    and the mislabeled id reaches pp, exactly as it did before this fix.
    """
    disp = _FakeDispatcherWithDoctor(required_cross_vendor=True)
    cfile, judge_res = _drive_to_judge(disp, tmp_path)
    judge_key = judge_res["host_action"]["call_key"]
    host_bridge._JUDGE_MODEL_PIN_CACHE = {"codex": (), "agy": (), "claude": ()}

    res = host_bridge.submit_host_result(
        disp, cursor_file=cfile, call_key=judge_key,
        result={"outcome": "pass", "critique_md": "looks good",
                "judge_producer": "codex",
                "judge_model_id": "gpt-5.1-codex", "cost_usd": 0.05})

    assert res["ok"] is False
    assert res["retryable"] is True
    assert "must record judge_model_id" in res["error"]
    assert "allowed_judge_model_ids" in res
    # Cursor is NOT terminal and the same judge step is still pending.
    assert res["status"] == "awaiting_host"
    assert res["state"] == "await_judge"
    assert res["host_action"]["call_key"] == judge_key
    assert disp.count("finalize_stage") == 0
    assert disp.count("finalize_run") == 0
    assert host_bridge.load_cursor(cfile)["state"] == "await_judge"

    # A corrected resubmit under the SAME call_key completes the stage.
    host_bridge._reset_judge_model_pin_cache()
    res2 = host_bridge.submit_host_result(
        disp, cursor_file=cfile, call_key=judge_key,
        result={"outcome": "pass", "critique_md": "looks good",
                "judge_producer": "codex",
                "judge_model_id": "gpt-5.4", "cost_usd": 0.05})
    assert res2["status"] == "complete"
    assert res2["final_status"] == "complete"
    # Judge cost was accrued exactly once across both submits.
    assert res2["cost_usd"] == pytest.approx(0.15)


def test_unknown_judge_producer_is_retryable_without_advancing(tmp_path):
    disp = _FakeDispatcherWithDoctor(required_cross_vendor=True)
    cfile, judge_res = _drive_to_judge(disp, tmp_path)
    judge_key = judge_res["host_action"]["call_key"]

    res = host_bridge.submit_host_result(
        disp, cursor_file=cfile, call_key=judge_key,
        result={"outcome": "pass", "critique_md": "ok",
                "judge_producer": "definitely-not-a-vendor",
                "judge_model_id": "who-knows", "cost_usd": 0.05})

    assert res["ok"] is False and res["retryable"] is True
    assert "not a supported judge vendor" in res["error"]
    assert sorted(res["allowed_judge_model_ids"]) == ["agy", "claude", "codex"]
    assert res["state"] == "await_judge"
    assert res["host_action"]["call_key"] == judge_key
    # Nothing was written to the pp ledger and no judge cost was accrued.
    assert disp.count("record_verdict") == 0
    assert res["cost_usd"] == pytest.approx(0.10)   # generate only


def test_unknown_judge_producer_correction_budget_is_bounded(tmp_path):
    """A host that keeps re-reporting a bad producer must not livelock."""
    disp = _FakeDispatcherWithDoctor(required_cross_vendor=True,
                                     enforce_pin=False)
    cfile, judge_res = _drive_to_judge(disp, tmp_path)
    judge_key = judge_res["host_action"]["call_key"]
    bad = {"outcome": "pass", "critique_md": "ok",
           "judge_producer": "definitely-not-a-vendor",
           "judge_model_id": "who-knows", "cost_usd": 0.05}

    for _ in range(host_bridge._MAX_JUDGE_MODEL_CORRECTIONS):
        res = host_bridge.submit_host_result(
            disp, cursor_file=cfile, call_key=judge_key, result=dict(bad))
        assert res["ok"] is False

    # Budget exhausted -- the stage proceeds instead of bouncing forever.
    res = host_bridge.submit_host_result(
        disp, cursor_file=cfile, call_key=judge_key, result=dict(bad))
    assert res.get("retryable") is not True
    assert res["state"] in host_bridge._TERMINAL
    assert disp.count("record_verdict") == 1
