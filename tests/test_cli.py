"""Tests for the hydra_core.cli surface.

The CLI is a thin wrapper. Tests pin exit codes + the presence of key
substrings in stdout/stderr so the user-facing contract (what shows up
when someone runs `hydra doctor`) is regression-proof.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from hydra_core import cli


REPO_ROOT = Path(__file__).resolve().parents[1]


def _run(argv, *, project_root=None):
    """Invoke the CLI in-process with optional --project override."""
    args = []
    if project_root is not None:
        args.extend(["--project", str(project_root)])
    args.extend(argv)
    return cli.main(args)


# --- verify ------------------------------------------------------------------

def test_verify_exits_zero_when_constitution_exists(capsys):
    rc = _run(["verify"], project_root=REPO_ROOT)
    out = capsys.readouterr().out
    assert rc == 0
    payload = json.loads(out)
    assert payload["sha256"]
    assert payload["refusals"] >= 5
    assert payload["path"].endswith("CONSTITUTION.md")


def test_verify_exits_one_when_constitution_missing(tmp_path, capsys):
    rc = _run(["verify"], project_root=tmp_path)
    err = capsys.readouterr().err
    assert rc == 1
    assert "CONSTITUTION.md not found" in err or "FAIL" in err


# --- doctor ------------------------------------------------------------------

def test_doctor_reports_constitution_and_eights_and_cerberus(capsys):
    rc = _run(["doctor"], project_root=REPO_ROOT)
    out = capsys.readouterr().out
    # We accept rc 0 or 1 — depending on optional MCP reachability — but
    # the report substrings must be present.
    assert rc in (0, 1)
    assert "constitution loaded" in out
    assert "squad(s) discovered" in out
    assert "cathedral alias(es)" in out
    assert "TheEights vocabulary" in out
    assert "Cerberus venom registry" in out


def test_doctor_lists_garland_as_active(capsys):
    # garland is now the active creative squad, not a deprecated stub.
    rc = _run(["doctor"], project_root=REPO_ROOT)
    out = capsys.readouterr().out
    garland_line = next((ln for ln in out.splitlines() if "garland" in ln), "")
    assert garland_line, "garland should appear in doctor output"
    assert "[DEPRECATED]" not in garland_line


def test_doctor_renders_deprecated_marker_for_deprecated_squad(tmp_path, capsys):
    # Synthetic fixture: a squad past its deprecated_after date must render the
    # [DEPRECATED] marker in the doctor squad listing. This pins the rendering
    # behavior independent of any real squad's mutable config.
    squad = tmp_path / "squads" / "ghost"
    squad.mkdir(parents=True)
    (squad / "squad.yaml").write_text(
        "name: ghost\nversion: 1.0.0\nentrypoint: stub\n"
        "deprecated_after: 2000-01-01\n",
        encoding="utf-8",
    )
    _run(["doctor"], project_root=tmp_path)
    out = capsys.readouterr().out
    assert "[DEPRECATED]" in out
    assert "ghost" in out


def test_doctor_fails_when_constitution_missing(tmp_path, capsys):
    # Make a project root with no constitution. We still need a squads/ dir
    # to get past the early FAIL exit.
    (tmp_path / "squads" / "noop").mkdir(parents=True)
    (tmp_path / "squads" / "noop" / "squad.yaml").write_text(
        "name: noop\nversion: 1.0.0\nentrypoint: stub\n", encoding="utf-8"
    )
    rc = _run(["doctor"], project_root=tmp_path)
    out = capsys.readouterr().out
    assert rc == 1
    assert "FAIL: constitution" in out


# --- squads ------------------------------------------------------------------

def test_squads_emits_json_for_every_pack(capsys):
    rc = _run(["squads"], project_root=REPO_ROOT)
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert rc == 0
    assert "garland" in payload
    assert "executive" in payload
    assert "engineering" in payload
    assert payload["garland"]["entrypoint"] == "claude-native"
    assert "brand-strategist" in payload["garland"]["agents"]


# --- memory ------------------------------------------------------------------

def test_memory_query_rejects_invalid_cell(capsys):
    rc = _run(["memory", "query", "notacell"], project_root=REPO_ROOT)
    err = capsys.readouterr().err
    assert rc == 1
    payload = json.loads(err)
    assert "invalid cell" in payload["error"]
    assert "qian" in payload["valid"]


def test_memory_query_accepts_valid_cell(capsys):
    rc = _run(["memory", "query", "qian", "--limit", "5"], project_root=REPO_ROOT)
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert rc == 0
    assert payload["cell"] == "qian"
    assert "rows" in payload
    assert payload["count"] >= 0


def test_memory_tag_requires_cells(capsys, tmp_path):
    # Missing --cells should make argparse exit before we reach the handler.
    with pytest.raises(SystemExit):
        _run(["memory", "tag", "ep:foo"], project_root=REPO_ROOT)


def test_memory_tag_round_trip(capsys):
    """Seed a row in the real episodic DB, tag it via the CLI, see merged cells.
    Uses a unique workflow_id so the test artifact is isolated."""
    from hydra_core import memory as mem
    from uuid import uuid4

    wf = f"cli-test-{uuid4()}"
    ref = mem.append_episodic(
        workflow_id=wf, kind="K", payload={"x": 1}, cells=["li"],
    )

    rc = _run(["memory", "tag", ref.key, "--cells", "kan"], project_root=REPO_ROOT)
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert rc == 0
    assert payload["key"] == ref.key
    assert set(payload["cells"]) == {"li", "kan"}


# --- run ---------------------------------------------------------------------

def test_run_smoke_with_stub_dispatcher(capsys):
    """`hydra run --squad garland` reaches the supervisor and emits a JSON
    report. When LangGraph is installed, the workflow halts at the
    approval interrupt for HITL-required squads (garland has ip-clearance +
    media-cost-cap gates), which is correct behavior — the lifecycle
    surfaced HITL rather than auto-approving."""
    rc = _run(["run", "Test goal: outline a Q3 marketing campaign for Helios",
               "--squad", "garland"], project_root=REPO_ROOT)
    out = capsys.readouterr().out
    payload = None
    lines = out.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("{"):
            text = "\n".join(lines[i:])
            try:
                payload = json.loads(text)
                break
            except json.JSONDecodeError:
                continue
    assert payload is not None, f"no JSON found in: {out[-500:]}"
    assert rc == 0
    # Valid terminal phases include `approval` (langgraph interrupt fired),
    # `done`, or `surfaced`. Anything else is a regression.
    assert payload["phase"] in ("done", "surfaced", "approval", "planning")
    assert "garland" in payload["selected_squads"]
    assert payload["workflow_id"]


# --- plan (non-detaching attended planning surface) --------------------------

def _extract_json(out: str):
    lines = out.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("{"):
            try:
                return json.loads("\n".join(lines[i:]))
            except json.JSONDecodeError:
                continue
    return None


def test_plan_halts_before_dispatch_without_executing(capsys, tmp_path, monkeypatch):
    """`hydra plan` runs intake+planner and HALTS before dispatch — it returns
    the TaskState plan with the task still `pending` (never executed). This is
    the non-detaching surface attended (host-bridged) mode drives."""
    monkeypatch.setenv("HYDRA_CHECKPOINT_DB", str(tmp_path / "cp.db"))
    rc = _run(["plan", "fix a small typo in the README", "--squad", "engineering"],
              project_root=REPO_ROOT)
    out = capsys.readouterr().out
    payload = _extract_json(out)
    assert payload is not None, f"no JSON found in: {out[-500:]}"
    assert rc == 0
    assert payload["ok"] is True
    # Single-squad, no approval needed → halts at the dispatch interrupt.
    assert payload["phase"] == "dispatch"
    assert payload["requires_human_approval"] is False
    assert payload["pending_hitl"] is None
    assert "engineering" in payload["selected_squads"]
    # The plan was produced but NOTHING dispatched: the task is still pending
    # and budget is unspent.
    assert payload["tasks"], "expected at least one planned task"
    assert all(t["status"] == "pending" for t in payload["tasks"])
    assert payload["budget"]["spent_usd"] == 0.0
    assert payload["workflow_id"]


def test_plan_surfaces_pending_approval_hitl(capsys, tmp_path, monkeypatch):
    """When the planner requires approval, `hydra plan` honestly returns the
    pending approval HITL (it does NOT pretend pending_hitl is null) and still
    dispatches nothing."""
    monkeypatch.setenv("HYDRA_CHECKPOINT_DB", str(tmp_path / "cp.db"))
    # A multi-squad / high-risk-flavoured goal trips requires_human_approval.
    rc = _run(["plan",
               "Add idempotency-key support to the payments API and notify support"],
              project_root=REPO_ROOT)
    out = capsys.readouterr().out
    payload = _extract_json(out)
    assert payload is not None, f"no JSON found in: {out[-500:]}"
    assert rc == 0
    assert payload["ok"] is True
    if payload["requires_human_approval"]:
        assert payload["pending_hitl"] is not None
        assert payload["pending_hitl"].get("gate_node") == "approval"
        assert payload["phase"] in ("approval", "planning", "intake")
    # Either way, no execution happened.
    assert payload["budget"]["spent_usd"] == 0.0


def test_next_engineering_task_skips_attended_completed():
    """Attended completion is tracked in attended_completed_task_ids (a replace
    channel), NOT task.status — because the `tasks` channel's _append reducer
    can't flip a status in place out-of-graph. _next_engineering_task must honour
    that list so a finished task is never re-picked by the next `step`."""
    from hydra_core.state import HydraState, TaskState
    t1 = TaskState(owner_squad="engineering", description="a")
    t2 = TaskState(owner_squad="engineering", description="b")
    st = HydraState(root_goal="g", tasks=[t1, t2])
    # Nothing completed yet → first task.
    assert cli._next_engineering_task(st).task_id == t1.task_id
    # Mark t1 complete via the replace channel → next is t2.
    st.attended_completed_task_ids = [str(t1.task_id)]
    assert cli._next_engineering_task(st).task_id == t2.task_id
    # Both complete → None.
    st.attended_completed_task_ids = [str(t1.task_id), str(t2.task_id)]
    assert cli._next_engineering_task(st) is None


# --- run --workflow-id -------------------------------------------------------

def test_run_workflow_id_passthrough(capsys):
    """--workflow-id passes a pre-allocated id into the run; the emitted JSON
    must echo that exact id back. Uses --no-checkpoint (pure-Python supervisor)
    to avoid the LangGraph HITL interrupt that would keep the run alive."""
    # Must be a valid UUID4 string — HydraState.workflow_id is UUID-typed.
    # This is the same format the Hydra Cockpit bridge mints via randomUUID().
    pre_id = "c2c2c2c2-c2c2-4c2c-8c2c-c2c2c2c2c2c2"
    rc = _run(
        ["run", "Cockpit C2 --workflow-id passthrough test",
         "--no-checkpoint", "--workflow-id", pre_id],
        project_root=REPO_ROOT,
    )
    out = capsys.readouterr().out
    payload = None
    for i, line in enumerate(out.splitlines()):
        if line.startswith("{"):
            try:
                payload = json.loads("\n".join(out.splitlines()[i:]))
                break
            except json.JSONDecodeError:
                continue
    assert payload is not None, f"no JSON output found: {out[-300:]}"
    assert rc == 0
    assert payload["workflow_id"] == pre_id, (
        f"expected workflow_id={pre_id!r}, got {payload['workflow_id']!r}"
    )


def test_run_workflow_id_invalid_falls_back_to_uuid(capsys):
    """An invalid --workflow-id (contains shell special chars) is rejected at
    validation time; the run falls back to a freshly-minted uuid4() and emits
    a warning. The emitted id must NOT be the rejected value."""
    bad_id = "bad id with spaces"  # fails _WORKFLOW_ID_RE
    import warnings as _w
    with _w.catch_warnings(record=True) as caught:
        _w.simplefilter("always")
        rc = _run(
            ["run", "Cockpit C2 invalid-id fallback test",
             "--no-checkpoint", "--workflow-id", bad_id],
            project_root=REPO_ROOT,
        )
    out = capsys.readouterr().out
    payload = None
    for i, line in enumerate(out.splitlines()):
        if line.startswith("{"):
            try:
                payload = json.loads("\n".join(out.splitlines()[i:]))
                break
            except json.JSONDecodeError:
                continue
    assert payload is not None, f"no JSON output: {out[-300:]}"
    assert rc == 0
    assert payload["workflow_id"] != bad_id, "invalid id must not be used"
    # A uuid4 looks like 8-4-4-4-12 hex or similar — at minimum not empty
    assert len(payload["workflow_id"]) > 4
    # A UserWarning must have been emitted
    warning_texts = [str(w.message) for w in caught if issubclass(w.category, UserWarning)]
    assert any("--workflow-id" in t or "uuid4" in t or "minting" in t for t in warning_texts), (
        f"expected a UserWarning about --workflow-id, got: {warning_texts}"
    )


def test_run_workflow_id_regex_boundary(capsys):
    """Verify the boundary of _WORKFLOW_ID_RE:
      - Valid: uuid4 string, cockpit-style id with hyphens/underscores.
      - Invalid: empty string, starts with hyphen, contains spaces, too long.
    This pins regex parity with hydra_control server.py _WORKFLOW_ID_RE."""
    from hydra_core.cli import _WORKFLOW_ID_RE
    valid = [
        "a",
        "abc123",
        "some-workflow-id",
        "wf_foo_bar",
        "a" * 64,
        "5ebd4268-5de0-4dbf-a82d-42c596d4818e",
    ]
    invalid = [
        "",          # empty
        "-starts-with-hyphen",  # must start with alnum
        "has space",  # space not in alphabet
        "has!bang",   # ! not in alphabet
        "a" * 65,     # too long (max 64 chars total)
    ]
    for v in valid:
        assert _WORKFLOW_ID_RE.match(v), f"expected VALID: {v!r}"
    for v in invalid:
        assert not _WORKFLOW_ID_RE.match(v), f"expected INVALID: {v!r}"


# --- replay (C6) -----------------------------------------------------------

def test_replay_subcommand_is_registered(capsys):
    """The `replay` subcommand must be registered and its --help must not
    raise SystemExit with a 'no such command' message."""
    try:
        _run(["replay", "--help"])
    except SystemExit as e:
        # argparse exits 0 on --help; that is fine.
        assert e.code == 0


def test_replay_rejects_invalid_workflow_id(capsys):
    """Bad source workflow_id (contains shell metachar) → non-zero exit + error JSON
    on stderr. Uses 'bad;id' — the semicolon is rejected by _WORKFLOW_ID_RE and
    is not interpreted as an option flag (doesn't start with '-')."""
    rc = _run(["replay", "bad;id"], project_root=REPO_ROOT)
    err = capsys.readouterr().err
    assert rc == 1
    payload = json.loads(err)
    assert "error" in payload
    assert "bad;id" in payload["error"] or "invalid" in payload["error"].lower()


def test_replay_rejects_bad_from_phase(capsys):
    """--from-phase must be one of the known phases; argparse choices enforcement
    means an unknown phase triggers SystemExit (argparse error, code 2)."""
    valid_wf = "5ebd4268-5de0-4dbf-a82d-42c596d4818e"
    try:
        rc = _run(["replay", valid_wf, "--from-phase", "bogus-phase"],
                  project_root=REPO_ROOT)
        # If we reach here, the handler caught it and returned non-zero
        assert rc == 1
        err = capsys.readouterr().err
        payload = json.loads(err)
        assert "invalid" in payload.get("error", "").lower() or "from_phase" in str(payload)
    except SystemExit as e:
        # argparse exits 2 on invalid choices — also acceptable
        assert e.code == 2


def test_replay_rejects_bad_swap_model(capsys):
    """--swap-model with shell metacharacters is rejected by _MODEL_ID_RE."""
    valid_wf = "5ebd4268-5de0-4dbf-a82d-42c596d4818e"
    rc = _run(["replay", valid_wf, "--swap-model", "model;evil"],
              project_root=REPO_ROOT)
    err = capsys.readouterr().err
    assert rc == 1
    payload = json.loads(err)
    assert "invalid" in payload.get("error", "").lower() or "swap_model" in str(payload)


def test_replay_missing_checkpoint_produces_clean_error(capsys, tmp_path, monkeypatch):
    """Replay of a non-existent workflow → clean error JSON on stderr, exit 1.
    Uses a blank checkpoints.db directory so no real checkpoint exists."""
    monkeypatch.setenv("HYDRA_CHECKPOINT_DB", str(tmp_path / "checkpoints.db"))
    valid_wf = "5ebd4268-5de0-4dbf-a82d-42c596d4818e"
    rc = _run(["replay", valid_wf, "--from-phase", "intake"],
              project_root=REPO_ROOT)
    out = capsys.readouterr()
    assert rc == 1
    # Error must go to stderr as JSON (not a Python traceback)
    err_text = out.err
    assert err_text.strip(), "expected non-empty stderr on missing checkpoint"
    payload = json.loads(err_text)
    assert "error" in payload
    # Clean error — no Python traceback in stderr
    assert "Traceback" not in err_text


def test_replay_known_phase_regex():
    """_KNOWN_PHASES must cover exactly the 8 supervisor phase names."""
    from hydra_core.cli import _KNOWN_PHASES
    expected = {
        "intake", "planning", "approval", "dispatch",
        "executing", "judge", "synthesis", "postcheck",
    }
    assert _KNOWN_PHASES == expected, (
        f"_KNOWN_PHASES mismatch. Got {_KNOWN_PHASES}; expected {expected}"
    )


def test_replay_model_id_re():
    """_MODEL_ID_RE must accept valid model ids and reject shell metacharacters."""
    from hydra_core.cli import _MODEL_ID_RE
    valid = [
        "claude-sonnet-4-6",
        "gpt-4o",
        "gemini-2-flash",
        "openai/o3-mini",
        "a",
    ]
    invalid = [
        "-starts-with-hyphen",
        "model;evil",
        "model|pipe",
        "model$(subshell)",
        "",  # empty
    ]
    for v in valid:
        assert _MODEL_ID_RE.match(v), f"expected VALID: {v!r}"
    for v in invalid:
        assert not _MODEL_ID_RE.match(v), f"expected INVALID: {v!r}"


def test_replay_mints_new_workflow_id(capsys, tmp_path, monkeypatch):
    """Dry replay of an existing checkpoint (via the --no-checkpoint pure-Python
    runner) should produce a NEW workflow_id. We use a workaround: run a workflow
    to create a checkpoint, then replay it and check the ids differ.

    NOTE: This test is skipped if langgraph is not installed (dry replay uses
    the checkpointing supervisor, which requires langgraph).
    """
    try:
        import langgraph  # type: ignore  # noqa
    except ImportError:
        pytest.skip("langgraph not installed — replay dry smoke skipped")

    # Step 1: run a real workflow to produce a checkpoint.
    from uuid import uuid4
    source_id = str(uuid4())
    rc1 = _run(
        ["run", "Replay test source workflow",
         "--squad", "engineering",
         "--workflow-id", source_id],
        project_root=REPO_ROOT,
    )
    # Accept rc 0 or non-zero; we just need the checkpoint to exist.
    capsys.readouterr()  # flush

    # Step 2: replay from the checkpoint (dry — no --live).
    rc2 = _run(
        ["replay", source_id, "--from-phase", "intake"],
        project_root=REPO_ROOT,
    )
    out2 = capsys.readouterr().out

    # If checkpoint was never written (rc1 != 0 before checkpoint phase),
    # replay will exit 1 with checkpoint_not_found — that is fine.
    if rc2 != 0:
        err2 = capsys.readouterr().err
        # Must be a clean JSON error, not a traceback
        assert "Traceback" not in out2
        return

    # If replay succeeded, the output must be JSON with a different id.
    payload = None
    for line in out2.splitlines():
        if line.startswith("{"):
            try:
                payload = json.loads(line + "\n" + "\n".join(
                    [l for l in out2.splitlines()[out2.splitlines().index(line):]]))
                break
            except json.JSONDecodeError:
                continue
    if payload is not None:
        assert payload["source_workflow_id"] == source_id
        assert payload["replay_workflow_id"] != source_id
        assert payload["from_phase"] == "intake"


# --- approve ----------------------------------------------------------------

def test_approve_is_real_resume_now(capsys, tmp_path, monkeypatch):
    # C2 (mesh-console-unification): `approve` is no longer a stub pointing at
    # the Claude Code plugin — it delegates to `resume --action approve`.
    # Unknown workflow → structured not_found + exit 1 (fail-closed),
    # instead of the old print-and-exit-0.
    monkeypatch.setenv("HYDRA_CHECKPOINT_DB", str(tmp_path / "checkpoints.db"))
    rc = _run(["approve", "fake-id"], project_root=REPO_ROOT)
    out = capsys.readouterr().out
    assert rc == 1
    payload = json.loads(out)
    assert payload["error"] == "not_found"
    assert payload["workflow_id"] == "fake-id"


# --- reap (abandoned-workflow GC) -------------------------------------------

def test_is_reapable_predicate():
    """The reap selection predicate: only abandoned non-terminal threads."""
    from hydra_core.cli import _is_reapable
    # Terminal phases are never reapable.
    assert _is_reapable("done", False, 999.0, 24.0) is False
    assert _is_reapable("surfaced", False, 999.0, 24.0) is False
    # A pending HITL gate means a human is genuinely expected — never reap.
    assert _is_reapable("approval", True, 999.0, 24.0) is False
    # Fresh non-terminal work (younger than the threshold) is left alone.
    assert _is_reapable("synthesis", False, 1.0, 24.0) is False
    # Stale, non-terminal, no gate → reapable.
    assert _is_reapable("approval", False, 430.0, 24.0) is True
    assert _is_reapable("synthesis", False, 25.0, 24.0) is True
    # Unknown age (no checkpoint timestamp) counts as old enough.
    assert _is_reapable("approval", False, None, 24.0) is True


def test_reap_dry_run_empty_store(capsys, tmp_path, monkeypatch):
    """reap with no checkpoint DB is a clean no-op (never fabricates work)."""
    monkeypatch.setenv("HYDRA_CHECKPOINT_DB", str(tmp_path / "checkpoints.db"))
    rc = _run(["reap"], project_root=REPO_ROOT)
    out = capsys.readouterr().out
    assert rc == 0
    payload = json.loads(out)
    assert payload["mode"] == "dry-run"  # default is never destructive
    assert payload["candidate_count"] == 0


# --- budget ------------------------------------------------------------------

def test_budget_set_without_workflow_id_errors(capsys):
    """`--set` is a mutation; without a workflow_id it must fail loudly rather
    than silently fall through to the list-all path and drop the write."""
    rc = _run(["budget", "--set", "250"], project_root=REPO_ROOT)
    err = capsys.readouterr().err
    assert rc == 1
    payload = json.loads(err)
    assert "workflow_id" in payload["error"]


def test_budget_invalid_workflow_id(capsys):
    """A malformed workflow_id is rejected. When langgraph is unavailable the
    supervisor gate fires first — either message is an acceptable rc=1."""
    rc = _run(["budget", "bad id!"], project_root=REPO_ROOT)
    err = capsys.readouterr().err
    assert rc == 1
    assert "invalid workflow_id" in err or "langgraph unavailable" in err


def test_budget_list_empty_store(capsys, tmp_path, monkeypatch):
    """List-all against an absent checkpoint DB yields an empty roster (or a
    clean langgraph-unavailable error), never a crash."""
    monkeypatch.setenv("HYDRA_CHECKPOINT_DB", str(tmp_path / "checkpoints.db"))
    rc = _run(["budget"], project_root=REPO_ROOT)
    captured = capsys.readouterr()
    assert rc in (0, 1)
    if rc == 0:
        payload = json.loads(captured.out)
        assert payload["workflows"] == []


# ---------------------------------------------------------------------------
# LV-2: hydra context in attended start_run (RA-12b, attended path)
# ---------------------------------------------------------------------------

def test_attended_step_passes_hydra_workflow_id(tmp_path, monkeypatch, capsys):
    """LV-2 / RA-12b: _cmd_attended_step must pass hydra_workflow_id in the
    pp start_run args so pp's DB can link the attended run row to the Hydra
    workflow — mirrors squad_node._via_mcp's provenance threading.

    TaskState.envelope_id is None by default, so hydra_envelope_id must be
    absent from start_run args (only include non-empty optional fields)."""
    import argparse
    from hydra_core import cli
    from hydra_core.state import HydraState, TaskState

    # Engineering task (no explicit envelope_id)
    task = TaskState(owner_squad="engineering", description="implement the feature")
    state_val = HydraState(root_goal="implement the feature", tasks=[task])
    wf_id = str(state_val.workflow_id)  # real UUID — passes _WORKFLOW_ID_RE

    # Agent stubs so the preflight check passes
    agents_dir = tmp_path / ".claude" / "agents"
    agents_dir.mkdir(parents=True)
    for name in ["engineer.md", "judge-cross-vendor.md", "judge-same-vendor.md"]:
        (agents_dir / name).write_text("# stub\n")

    # Recording dispatcher — captures start_run args
    start_run_calls: list[dict] = []

    class _RecDisp:
        def call_mcp(self, server: str, tool: str, args: dict,
                     *, squad_id: str | None = None) -> dict:
            if tool == "start_run":
                start_run_calls.append(dict(args))
                return {"status": "done", "result": {"run_id": "run-lv2-1"}}
            return {"status": "done", "result": {}}
        def set_squad_packs(self, packs: dict) -> None:
            pass

    class _FakeSnap:
        values = state_val.model_dump(mode="json")

    update_calls: list[dict] = []

    class _FakeSup:
        def get_state(self, config: dict) -> "_FakeSnap":
            return _FakeSnap()
        def update_state(self, config: dict, values: dict) -> None:
            update_calls.append(dict(values))

    monkeypatch.setattr("hydra_core.cli._attended_live_dispatcher",
                        lambda *a, **k: _RecDisp())
    monkeypatch.setattr("hydra_core.supervisor.build_supervisor",
                        lambda **k: _FakeSup())
    # Stub begin_stage to avoid git operations
    monkeypatch.setattr(
        "hydra_core.host_bridge.begin_stage",
        lambda *a, **k: {
            "status": "awaiting_host",
            "cursor_path": str(tmp_path / "c.json"),
            "state": "await_generate",
            "workflow_id": wf_id,
            "run_id": "run-lv2-1",
            "stage_id": "s1",
            "task_id": str(task.task_id),
            "cost_usd": 0.0,
            "tokens_in": 0,
            "tokens_out": 0,
            "host_action": {
                "call_key": "generate-0",
                "agent_type": "engineer",
                "cwd": str(tmp_path),
            },
        },
    )
    monkeypatch.setattr("hydra_core.squad_node._maybe_write_claude_shim",
                        lambda *a, **k: None)

    rc = cli._cmd_attended_step(
        argparse.Namespace(project=str(tmp_path), workflow_id=wf_id, verbose=False))
    capsys.readouterr()  # consume output

    assert rc == 0, f"expected rc=0"
    assert start_run_calls, "start_run was not called"
    sr = start_run_calls[0]
    assert sr.get("hydra_workflow_id") == wf_id, (
        f"hydra_workflow_id missing or wrong in start_run args: {sr}"
    )
    # envelope_id is None → hydra_envelope_id must be absent (only non-empty)
    assert "hydra_envelope_id" not in sr, (
        f"hydra_envelope_id should be absent when task.envelope_id is None: {sr}"
    )


def test_attended_step_threads_hydra_context_block(tmp_path, monkeypatch, capsys):
    """Phase 7b: when start_run returns hydra_context_block, _cmd_attended_step
    must pass it as a kwarg to host_bridge.begin_stage so the engineer prompt
    starts with the context block."""
    import argparse
    from hydra_core import cli
    from hydra_core.state import HydraState, TaskState

    task = TaskState(owner_squad="engineering", description="add a feature")
    state_val = HydraState(root_goal="add a feature", tasks=[task])
    wf_id = str(state_val.workflow_id)

    agents_dir = tmp_path / ".claude" / "agents"
    agents_dir.mkdir(parents=True)
    for name in ["engineer.md", "judge-cross-vendor.md", "judge-same-vendor.md"]:
        (agents_dir / name).write_text("# stub\n")

    ctx_block = "## Hydra context\nworkflow_id: wf-ctx-1"

    class _RecDisp:
        def call_mcp(self, server: str, tool: str, args: dict,
                     *, squad_id: str | None = None) -> dict:
            if tool == "start_run":
                return {"status": "done", "result": {
                    "run_id": "run-ctx-1",
                    "hydra_context_block": ctx_block,
                }}
            return {"status": "done", "result": {}}

        def set_squad_packs(self, packs: dict) -> None:
            pass

    class _FakeSnap:
        values = state_val.model_dump(mode="json")

    class _FakeSup:
        def get_state(self, config: dict) -> "_FakeSnap":
            return _FakeSnap()

        def update_state(self, config: dict, values: dict) -> None:
            pass

    begin_stage_kwargs: list[dict] = []

    def _capture_begin_stage(*args, **kwargs):
        begin_stage_kwargs.append(kwargs)
        return {
            "status": "awaiting_host",
            "cursor_path": str(tmp_path / "c.json"),
            "state": "await_generate",
            "workflow_id": wf_id,
            "run_id": "run-ctx-1",
            "stage_id": "s1",
            "task_id": str(task.task_id),
            "cost_usd": 0.0, "tokens_in": 0, "tokens_out": 0,
            "host_action": {
                "call_key": "generate-0",
                "agent_type": "engineer",
                "cwd": str(tmp_path),
                "prompt": kwargs.get("hydra_context_block", "") + "\n\nREQUEST: add a feature",
            },
        }

    monkeypatch.setattr("hydra_core.cli._attended_live_dispatcher",
                        lambda *a, **k: _RecDisp())
    monkeypatch.setattr("hydra_core.supervisor.build_supervisor",
                        lambda **k: _FakeSup())
    monkeypatch.setattr("hydra_core.host_bridge.begin_stage", _capture_begin_stage)
    monkeypatch.setattr("hydra_core.squad_node._maybe_write_claude_shim",
                        lambda *a, **k: None)

    rc = cli._cmd_attended_step(
        argparse.Namespace(project=str(tmp_path), workflow_id=wf_id, verbose=False))
    capsys.readouterr()

    assert rc == 0, "expected rc=0"
    assert begin_stage_kwargs, "begin_stage was not called"
    assert begin_stage_kwargs[0].get("hydra_context_block") == ctx_block, (
        f"hydra_context_block not threaded to begin_stage: {begin_stage_kwargs[0]}"
    )


# ---------------------------------------------------------------------------
# LV-2 / RA-12a: attended squad path e2e — step, submit, idempotency, skip
# ---------------------------------------------------------------------------

def test_attended_squad_path_completes_charges_once_no_redispatch(
        tmp_path, monkeypatch, capsys):
    """Squad path e2e (claude-skill / garland):

    1. _cmd_attended_step with only a non-engineering task → host_action with
       state=await_squad_agent (begin_squad_stage cursor created).
    2. _cmd_attended_submit completes the task and charges budget once.
    3. Duplicate _cmd_attended_submit reports already_charged (no double-billing).
    4. update_state is called with attended_done_task_ids containing the task_id
       so a subsequent supervisor resume does NOT re-dispatch (RA-12a skip).
    """
    import argparse
    import json as _json
    from hydra_core import cli
    from hydra_core.state import HydraState, TaskState

    task = TaskState(owner_squad="garland", description="write brand copy")
    wf_id = "attended-sq-0001"  # valid for _WORKFLOW_ID_RE
    state_val = HydraState(root_goal="write brand copy", tasks=[task])
    state_dict = state_val.model_dump(mode="json")

    update_state_log: list[dict] = []

    class _FakeSnap:
        values = state_dict

    class _FakeSup:
        def get_state(self, config: dict) -> "_FakeSnap":
            return _FakeSnap()
        def update_state(self, config: dict, values: dict) -> None:
            update_state_log.append(dict(values))

    # Minimal garland pack (claude-skill, with a gatekeeper agent)
    class _FakeAgent:
        slug = "brand-director"
        authority = "gatekeeper"

    class _FakePack:
        slug = "garland"
        entrypoint = "claude-skill"
        agents = [_FakeAgent()]

    monkeypatch.setattr("hydra_core.cli._attended_live_dispatcher",
                        lambda *a, **k: type("_NOP", (), {
                            "call_mcp": lambda s, sv, t, a, **k: {"status": "done", "result": {}},
                            "set_squad_packs": lambda s, p: None,
                        })())
    monkeypatch.setattr("hydra_core.supervisor.build_supervisor",
                        lambda **k: _FakeSup())
    monkeypatch.setattr("hydra_core.squad_loader.discover_squads",
                        lambda *a, **k: {"garland": _FakePack()})
    monkeypatch.setattr("hydra_core.governance.charge_and_gate",
                        lambda state, cost, toks: (False, False))

    # --- step 1: attended step → squad cursor ---
    rc_step = cli._cmd_attended_step(
        argparse.Namespace(project=str(tmp_path), workflow_id=wf_id, verbose=False))
    out_step = capsys.readouterr().out
    assert rc_step == 0, f"step failed (rc={rc_step}): {out_step[:300]}"
    payload_step = _json.loads(out_step)
    assert payload_step["ok"] is True
    assert payload_step["state"] == "await_squad_agent", (
        f"expected await_squad_agent, got {payload_step['state']!r}"
    )
    assert payload_step["host_action"]["agent_type"] == "brand-director"

    cfile = payload_step["cursor_path"]
    call_key = payload_step["host_action"]["call_key"]
    task_id_str = str(task.task_id)
    assert payload_step.get("run_id") == task_id_str, (
        "squad cursor run_id must equal task_id for CLI submit to resolve it"
    )

    # --- step 2: submit → complete ---
    result_file = tmp_path / "agent_result.json"
    result_file.write_text(_json.dumps({
        "text": "Brand brief done.",
        "cost_usd": 0.06,
        "tokens_in": 80,
        "tokens_out": 120,
    }))

    def _do_submit() -> tuple[int, dict]:
        rc = cli._cmd_attended_submit(argparse.Namespace(
            project=str(tmp_path),
            workflow_id=wf_id,
            run_id=task_id_str,
            call_key=call_key,
            result=str(result_file),
        ))
        out = capsys.readouterr().out
        return rc, _json.loads(out)

    rc2, p2 = _do_submit()
    assert rc2 == 0
    assert p2["ok"] is True
    assert p2["status"] == "complete", (
        f"squad task must complete on submit, got {p2['status']!r}"
    )
    assert not p2.get("already_charged"), "first submit must not be already_charged"

    # --- step 3: attended_done_task_ids updated (RA-12a skip guard) ---
    done_updates = [u for u in update_state_log if "attended_done_task_ids" in u]
    assert done_updates, (
        "update_state must be called with attended_done_task_ids after complete"
    )
    assert task_id_str in done_updates[0]["attended_done_task_ids"], (
        f"task {task_id_str!r} not in attended_done_task_ids: "
        f"{done_updates[0]['attended_done_task_ids']}"
    )

    # --- step 4: duplicate submit → already_charged ---
    rc3, p3 = _do_submit()
    assert rc3 == 0
    assert p3.get("already_charged") is True, (
        "duplicate submit must report already_charged to prevent double-billing"
    )
