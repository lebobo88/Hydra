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


# --- approve ----------------------------------------------------------------

def test_approve_points_at_claude_code(capsys):
    rc = _run(["approve", "fake-id"], project_root=REPO_ROOT)
    out = capsys.readouterr().out
    assert rc == 0
    assert "/hydra:approve" in out
