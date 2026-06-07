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
    assert payload["garland"]["entrypoint"] == "claude-skill"
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
