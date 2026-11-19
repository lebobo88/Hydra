"""Tests for ``plugins/hydra/hooks/hydra_hook.py`` (Hydra#86).

Every test invokes the hook as a subprocess (``sys.executable``) against a
``tmp_path`` project, with ``CLAUDE_PROJECT_DIR`` set to that project and
``HYDRA_HOOK_REPLACE_PROMPT`` / ``HYDRA_HOOK_RECORD_SHAPES`` /
``HYDRA_DISPATCH_TICKET_TTL_S`` scrubbed from the environment unless the
test itself sets them. Hermetic: no daemons, no network, no real
``~/.hydra``.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from hydra_core import dispatch_seam, host_bridge
from hydra_core import native_packs

HOOK_PATH = (Path(__file__).resolve().parents[1]
             / "plugins" / "hydra" / "hooks" / "hydra_hook.py")

_SCRUB_ENV_KEYS = (
    "HYDRA_HOOK_REPLACE_PROMPT",
    "HYDRA_HOOK_RECORD_SHAPES",
    "HYDRA_DISPATCH_TICKET_TTL_S",
)


def _base_env(project_dir: Path | None, extra: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ)
    for k in _SCRUB_ENV_KEYS:
        env.pop(k, None)
    if project_dir is None:
        env.pop("CLAUDE_PROJECT_DIR", None)
    else:
        env["CLAUDE_PROJECT_DIR"] = str(project_dir)
    if extra:
        env.update(extra)
    return env


def _run_hook(
    mode: str | None, payload: dict | str, project_dir: Path | None, *,
    cwd: Path | None = None, extra_env: dict[str, str] | None = None,
    hook_path: Path = HOOK_PATH, isolated: bool = False,
) -> subprocess.CompletedProcess:
    stdin_text = payload if isinstance(payload, str) else json.dumps(payload)
    argv = [sys.executable]
    if isolated:
        argv.append("-I")
    argv.append(str(hook_path))
    if mode is not None:
        argv.append(mode)
    return subprocess.run(
        argv,
        input=stdin_text,
        capture_output=True,
        text=True,
        cwd=str(cwd) if cwd is not None else None,
        env=_base_env(project_dir, extra_env),
        timeout=30,
    )


def _spawn_payload(
    subagent_type: str, prompt: str = "do the thing", *, extra_tool_input: dict | None = None,
    tool_use_id: str = "tu-1",
) -> dict:
    tool_input = {"subagent_type": subagent_type, "prompt": prompt, "description": "d"}
    if extra_tool_input:
        tool_input.update(extra_tool_input)
    return {
        "hook_event_name": "PreToolUse",
        "tool_name": "Agent",
        "tool_use_id": tool_use_id,
        "tool_input": tool_input,
    }


def _audit_lines(root: Path) -> list[dict]:
    p = root / ".hydra" / "hydra_hook.audit.jsonl"
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]


def _shapes_lines(root: Path) -> list[dict]:
    p = root / ".hydra" / "hydra_hook.shapes.jsonl"
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]


def _write_ticket(
    root: Path, workflow_id: str, run_id: str, call_key: str, agent_type: str, *,
    kind: str = "engineer", squad_slug: str | None = None, now: datetime | None = None,
    prompt: str = "do the thing",
) -> Path:
    host_action = {"call_key": call_key, "agent_type": agent_type, "prompt": prompt}
    path = dispatch_seam.write_dispatch_ticket(
        root, workflow_id, run_id, host_action,
        kind=kind, squad_slug=squad_slug, budget_usd=None, budget_remaining_usd=None,
        now=now,
    )
    assert path is not None, "fixture ticket failed to write"
    return path


def _ticket_id_of(path: Path) -> str:
    return json.loads(path.read_text(encoding="utf-8"))["ticket_id"]


def _write_cursor(root: Path, workflow_id: str, run_id: str, pending_action: dict) -> None:
    cursor = {"schema": host_bridge.CURSOR_SCHEMA, "pending_action": pending_action}
    cpath = host_bridge.cursor_path(root, workflow_id, run_id)
    host_bridge.save_cursor(cpath, cursor)


def _load_hook_module():
    spec = importlib.util.spec_from_file_location("hydra_hook_under_test", HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


# --------------------------------------------------------------------------- #
# Governed-name table                                                        #
# --------------------------------------------------------------------------- #

_GOVERNED_TABLE = [
    ("engineer", True, "engineer"),
    ("hydra:engineer", True, "engineer"),
    ("plan-author", True, "plan-author"),
    ("hydra:plan-author", True, "plan-author"),
    ("executive-suite:boardroom", True, "executive-suite:boardroom"),
    ("boardroom", False, None),
    ("general-purpose", False, None),
    ("hydra:hydra-router", False, None),
]


@pytest.mark.parametrize("name,governed,norm", _GOVERNED_TABLE)
def test_governed_name_table_in_process(name, governed, norm):
    hook = _load_hook_module()
    if governed:
        assert (hook.is_governed(name), hook.normalize(name)) == (True, norm)
    else:
        assert hook.is_governed(name) is False


@pytest.mark.parametrize("name,governed,norm", _GOVERNED_TABLE)
def test_governed_name_table_subprocess(tmp_path, name, governed, norm):
    root = tmp_path / "project"
    root.mkdir()
    res = _run_hook("pre", _spawn_payload(name), root)
    assert res.returncode == 0
    assert res.stdout == ""
    lines = _audit_lines(root)
    if governed:
        assert len(lines) == 1
        assert lines[0]["decision"] == "would_deny"
        assert lines[0]["reason"] == "no_ticket"
    else:
        assert lines == []
        assert not (root / ".hydra" / "hydra_hook.audit.jsonl").exists()


# --------------------------------------------------------------------------- #
# Ticket present / native lead                                               #
# --------------------------------------------------------------------------- #

def test_ticket_present_engineer(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    tpath = _write_ticket(root, "wf-1", "run-1", "generate-0", "engineer", kind="engineer")
    res = _run_hook("pre", _spawn_payload("hydra:engineer"), root)
    assert res.returncode == 0
    assert res.stdout == ""
    lines = _audit_lines(root)
    assert len(lines) == 1
    assert lines[0]["decision"] == "found"
    assert lines[0]["ticket_id"] == _ticket_id_of(tpath)
    assert lines[0]["live_candidates"] == 1


def test_ticket_present_reverse_plan_author(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    tpath = _write_ticket(root, "wf-2", "run-2", "plan-0", "hydra:plan-author",
                           kind="squad", squad_slug="planning")
    res = _run_hook("pre", _spawn_payload("plan-author"), root)
    assert res.returncode == 0
    lines = _audit_lines(root)
    assert lines[0]["decision"] == "found"
    assert lines[0]["ticket_id"] == _ticket_id_of(tpath)


def test_native_lead_boardroom_qualified_vs_bare(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    tpath = _write_ticket(root, "wf-3", "run-3", "exec-0", "executive-suite:boardroom",
                           kind="squad", squad_slug="executive")
    res = _run_hook("pre", _spawn_payload("executive-suite:boardroom"), root)
    assert res.returncode == 0
    lines = _audit_lines(root)
    assert lines[0]["decision"] == "found"
    assert lines[0]["ticket_id"] == _ticket_id_of(tpath)

    root2 = tmp_path / "project2"
    root2.mkdir()
    _write_ticket(root2, "wf-3", "run-3", "exec-0", "executive-suite:boardroom",
                  kind="squad", squad_slug="executive")
    res2 = _run_hook("pre", _spawn_payload("boardroom"), root2)
    assert res2.returncode == 0
    assert res2.stdout == ""
    assert _audit_lines(root2) == []
    assert not (root2 / ".hydra" / "hydra_hook.audit.jsonl").exists()


# --------------------------------------------------------------------------- #
# Newest wins / tie                                                          #
# --------------------------------------------------------------------------- #

def test_newest_wins(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    now = datetime.now(timezone.utc)
    older = _write_ticket(root, "wf-4", "run-4", "gen-older", "engineer",
                           now=now - timedelta(minutes=10))
    newer = _write_ticket(root, "wf-4", "run-4", "gen-newer", "engineer",
                           now=now - timedelta(minutes=5))
    res = _run_hook("pre", _spawn_payload("engineer"), root)
    assert res.returncode == 0
    lines = _audit_lines(root)
    assert lines[0]["decision"] == "found"
    assert lines[0]["ticket_id"] == _ticket_id_of(newer)
    assert lines[0]["live_candidates"] == 2
    assert _ticket_id_of(newer) != _ticket_id_of(older)


def test_tie_breaks_on_min_ticket_id(tmp_path):
    now = datetime.now(timezone.utc)

    root_a = tmp_path / "project_a"
    root_a.mkdir()
    t1 = _write_ticket(root_a, "wf-5", "run-5", "gen-a", "engineer", now=now)
    t2 = _write_ticket(root_a, "wf-5", "run-5", "gen-b", "engineer", now=now)
    expected = min(_ticket_id_of(t1), _ticket_id_of(t2))
    res_a = _run_hook("pre", _spawn_payload("engineer"), root_a)
    assert res_a.returncode == 0
    lines_a = _audit_lines(root_a)
    assert lines_a[0]["ticket_id"] == expected
    assert lines_a[0]["live_candidates"] == 2

    # Opposite write order -> same winner.
    root_b = tmp_path / "project_b"
    root_b.mkdir()
    t2b = _write_ticket(root_b, "wf-5", "run-5", "gen-b", "engineer", now=now)
    t1b = _write_ticket(root_b, "wf-5", "run-5", "gen-a", "engineer", now=now)
    expected_b = min(_ticket_id_of(t1b), _ticket_id_of(t2b))
    res_b = _run_hook("pre", _spawn_payload("engineer"), root_b)
    assert res_b.returncode == 0
    lines_b = _audit_lines(root_b)
    assert lines_b[0]["ticket_id"] == expected_b
    assert expected_b == expected


# --------------------------------------------------------------------------- #
# created_at validation                                                      #
# --------------------------------------------------------------------------- #

def test_created_at_missing_is_unreadable(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    tpath = _write_ticket(root, "wf-6", "run-6", "gen-0", "engineer")
    data = json.loads(tpath.read_text(encoding="utf-8"))
    del data["created_at"]
    tpath.write_text(json.dumps(data), encoding="utf-8")

    res = _run_hook("pre", _spawn_payload("engineer"), root)
    assert res.returncode == 0
    lines = _audit_lines(root)
    assert lines[0]["decision"] == "would_deny"
    assert lines[0]["reason"] == "unreadable"


def test_created_at_naive_is_unreadable(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    tpath = _write_ticket(root, "wf-7", "run-7", "gen-0", "engineer")
    data = json.loads(tpath.read_text(encoding="utf-8"))
    data["created_at"] = "2026-01-01T00:00:00"
    tpath.write_text(json.dumps(data), encoding="utf-8")

    res = _run_hook("pre", _spawn_payload("engineer"), root)
    assert res.returncode == 0
    lines = _audit_lines(root)
    assert lines[0]["decision"] == "would_deny"
    assert lines[0]["reason"] == "unreadable"


def test_created_at_invalid_next_to_valid_ticket_still_found(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    bad = _write_ticket(root, "wf-8", "run-8", "gen-bad", "engineer")
    data = json.loads(bad.read_text(encoding="utf-8"))
    del data["created_at"]
    bad.write_text(json.dumps(data), encoding="utf-8")

    good = _write_ticket(root, "wf-8", "run-8", "gen-good", "engineer")

    res = _run_hook("pre", _spawn_payload("engineer"), root)
    assert res.returncode == 0
    lines = _audit_lines(root)
    assert lines[0]["decision"] == "found"
    assert lines[0]["ticket_id"] == _ticket_id_of(good)
    assert lines[0]["live_candidates"] == 1


# --------------------------------------------------------------------------- #
# Missing / expired / type-mismatch / unreadable                             #
# --------------------------------------------------------------------------- #

def test_ticket_missing_no_ticket(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    res = _run_hook("pre", _spawn_payload("engineer"), root)
    assert res.returncode == 0
    assert res.stdout == ""
    lines = _audit_lines(root)
    assert len(lines) == 1
    assert lines[0]["decision"] == "would_deny"
    assert lines[0]["reason"] == "no_ticket"


def test_expired_ticket(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    _write_ticket(root, "wf-9", "run-9", "gen-0", "engineer",
                  now=datetime(2000, 1, 1, tzinfo=timezone.utc))
    res = _run_hook("pre", _spawn_payload("engineer"), root)
    assert res.returncode == 0
    lines = _audit_lines(root)
    assert lines[0]["decision"] == "would_deny"
    assert lines[0]["reason"] == "expired"
    assert lines[0]["live_candidates"] == 0


def test_type_mismatch(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    _write_ticket(root, "wf-10", "run-10", "gen-0", "engineer")
    res = _run_hook("pre", _spawn_payload("hydra:judge-cross-vendor"), root)
    assert res.returncode == 0
    lines = _audit_lines(root)
    assert lines[0]["decision"] == "would_deny"
    assert lines[0]["reason"] == "type_mismatch"


def test_unreadable_ticket_file(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    tdir = root / ".hydra" / "wf-11" / "attended" / "tickets"
    tdir.mkdir(parents=True)
    (tdir / "bad.json").write_text("not json{", encoding="utf-8")
    res = _run_hook("pre", _spawn_payload("engineer"), root)
    assert res.returncode == 0
    lines = _audit_lines(root)
    assert lines[0]["decision"] == "would_deny"
    assert lines[0]["reason"] == "unreadable"


# --------------------------------------------------------------------------- #
# Prompt match / mismatch / replacement                                     #
# --------------------------------------------------------------------------- #

def test_prompt_match_verified(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    pending_action = {"call_key": "gen-0", "agent_type": "engineer", "prompt": "same prompt"}
    _write_ticket(root, "wf-12", "run-12", "gen-0", "engineer", prompt="same prompt")
    _write_cursor(root, "wf-12", "run-12", pending_action)

    res = _run_hook("pre", _spawn_payload("engineer", prompt="same prompt"), root)
    assert res.returncode == 0
    lines = _audit_lines(root)
    assert lines[0]["prompt_check"] == "verified"
    assert lines[0]["prompt_match"] is True
    assert lines[0]["would_replace"] is False


def test_prompt_mismatch_and_cursor_unavailable(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    pending_action = {"call_key": "gen-0", "agent_type": "engineer", "prompt": "original"}
    _write_ticket(root, "wf-13", "run-13", "gen-0", "engineer", prompt="original")
    _write_cursor(root, "wf-13", "run-13", pending_action)

    res = _run_hook("pre", _spawn_payload("engineer", prompt="different"), root)
    assert res.returncode == 0
    lines = _audit_lines(root)
    assert lines[0]["prompt_match"] is False
    assert lines[0]["would_replace"] is True
    assert lines[0]["prompt_check"] == "verified"

    # Delete the cursor -> cursor_unavailable.
    cpath = host_bridge.cursor_path(root, "wf-13", "run-13")
    cpath.unlink()
    res2 = _run_hook("pre", _spawn_payload("engineer", prompt="different"), root)
    assert res2.returncode == 0
    lines2 = _audit_lines(root)
    assert lines2[1]["prompt_match"] is None
    assert lines2[1]["would_replace"] is False
    assert lines2[1]["prompt_check"] == "cursor_unavailable"


def test_replacement_on(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    pending_action = {"call_key": "gen-0", "agent_type": "engineer", "prompt": "original"}
    _write_ticket(root, "wf-14", "run-14", "gen-0", "engineer", prompt="original")
    _write_cursor(root, "wf-14", "run-14", pending_action)

    payload = _spawn_payload("engineer", prompt="different",
                              extra_tool_input={"run_in_background": True, "model": "x"})
    res = _run_hook("pre", payload, root, extra_env={"HYDRA_HOOK_REPLACE_PROMPT": "1"})
    assert res.returncode == 0
    out = json.loads(res.stdout)
    assert list(out.keys()) == ["hookSpecificOutput"]
    hso = out["hookSpecificOutput"]
    assert hso["hookEventName"] == "PreToolUse"
    assert "permissionDecision" not in hso
    updated = hso["updatedInput"]
    assert updated["prompt"] == "original"
    for key in ("subagent_type", "description", "run_in_background", "model"):
        assert updated[key] == payload["tool_input"][key]


def test_replacement_off(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    pending_action = {"call_key": "gen-0", "agent_type": "engineer", "prompt": "original"}
    _write_ticket(root, "wf-15", "run-15", "gen-0", "engineer", prompt="original")
    _write_cursor(root, "wf-15", "run-15", pending_action)

    payload = _spawn_payload("engineer", prompt="different")
    res = _run_hook("pre", payload, root)
    assert res.returncode == 0
    assert res.stdout == ""

    # Flag on, cursor deleted: still empty stdout.
    cpath = host_bridge.cursor_path(root, "wf-15", "run-15")
    cpath.unlink()
    res2 = _run_hook("pre", payload, root, extra_env={"HYDRA_HOOK_REPLACE_PROMPT": "1"})
    assert res2.returncode == 0
    assert res2.stdout == ""


# --------------------------------------------------------------------------- #
# PostToolUse usage                                                          #
# --------------------------------------------------------------------------- #

def _post_payload(subagent_type: str, tool_response: dict, tool_use_id: str = "tu-post") -> dict:
    return {
        "hook_event_name": "PostToolUse",
        "tool_name": "Agent",
        "tool_use_id": tool_use_id,
        "tool_input": {"subagent_type": subagent_type, "prompt": "p"},
        "tool_response": tool_response,
    }


def test_usage_captured(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    tpath = _write_ticket(root, "wf-16", "run-16", "gen-0", "engineer")
    tool_response = {
        "status": "completed",
        "totalTokens": 1234,
        "usage": {
            "input_tokens": 10, "output_tokens": 20,
            "cache_creation_input_tokens": 30, "cache_read_input_tokens": 40,
        },
        "totalDurationMs": 5678.5,
        "totalToolUseCount": 3,
        "resolvedModel": "claude-opus-5-5[1m]",
        "agentId": "a1b2c3",
    }
    res = _run_hook("post", _post_payload("engineer", tool_response), root)
    assert res.returncode == 0
    lines = _audit_lines(root)
    line = lines[0]
    assert line["usage_status"] == "recorded"
    assert line["usage_scope"] == "final_request_only"
    assert line["ticket_id"] == _ticket_id_of(tpath)
    assert line["total_tokens"] == 1234
    assert line["input_tokens"] == 10
    assert line["output_tokens"] == 20
    assert line["cache_creation_input_tokens"] == 30
    assert line["cache_read_input_tokens"] == 40
    assert line["total_duration_ms"] == 5678.5
    assert line["total_tool_use_count"] == 3
    assert line["resolved_model"] == "claude-opus-5-5[1m]"
    assert line["status"] == "completed"
    assert line["agent_id"] == "a1b2c3"
    assert "usage" not in line


def test_safe_metadata_numerics_and_strings(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    _write_ticket(root, "wf-17", "run-17", "gen-0", "engineer")
    tool_response = {
        "status": "a" * 129,
        "totalTokens": True,
        "usage": {"input_tokens": "12", "output_tokens": 5},
    }
    res = _run_hook("post", _post_payload("engineer", tool_response), root)
    assert res.returncode == 0
    lines = _audit_lines(root)
    assert lines[0]["total_tokens"] is None
    assert lines[0]["input_tokens"] is None
    assert lines[0]["status"] is None
    assert lines[0]["usage_status"] == "recorded"


def test_async_launch(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    _write_ticket(root, "wf-18", "run-18", "gen-0", "engineer")
    tool_response = {
        "status": "async_launched", "agentId": "a1", "description": "d",
        "prompt": "p", "outputFile": "f", "resolvedModel": "m",
    }
    res = _run_hook("post", _post_payload("engineer", tool_response), root)
    assert res.returncode == 0
    lines = _audit_lines(root)
    assert lines[0]["usage_status"] == "usage_unavailable"
    assert lines[0]["reason"] == "async_launched"
    for key in ("total_tokens", "input_tokens", "output_tokens",
                "cache_creation_input_tokens", "cache_read_input_tokens",
                "total_duration_ms", "total_tool_use_count", "resolved_model"):
        assert lines[0][key] is None


def test_async_with_usage_still_unavailable(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    _write_ticket(root, "wf-19", "run-19", "gen-0", "engineer")
    tool_response = {
        "status": "async_launched", "totalTokens": 9,
        "usage": {"input_tokens": 5, "output_tokens": 4},
    }
    res = _run_hook("post", _post_payload("engineer", tool_response), root)
    assert res.returncode == 0
    lines = _audit_lines(root)
    assert lines[0]["usage_status"] == "usage_unavailable"
    assert lines[0]["reason"] == "async_launched"
    assert lines[0]["total_tokens"] is None
    assert lines[0]["input_tokens"] is None


def test_no_usage_in_response(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    _write_ticket(root, "wf-20", "run-20", "gen-0", "engineer")
    res = _run_hook("post", _post_payload("engineer", {"status": "completed"}), root)
    assert res.returncode == 0
    lines = _audit_lines(root)
    assert lines[0]["usage_status"] == "usage_unavailable"
    assert lines[0]["reason"] == "no_usage_in_response"


# --------------------------------------------------------------------------- #
# Malformed stdin / ungoverned / no project dir                              #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("mode", ["pre", "post"])
@pytest.mark.parametrize("raw", [
    "", "not json{", "[]", '"x"',
    json.dumps({"tool_name": "Agent", "tool_input": "s"}),
])
def test_malformed_stdin(tmp_path, mode, raw):
    root = tmp_path / "project"
    root.mkdir()
    res = _run_hook(mode, raw, root)
    assert res.returncode == 0
    assert res.stdout == ""


def test_ungoverned_general_purpose(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    res = _run_hook("pre", _spawn_payload("general-purpose"), root)
    assert res.returncode == 0
    assert res.stdout == ""
    assert not (root / ".hydra" / "hydra_hook.audit.jsonl").exists()


def test_no_project_dir(tmp_path):
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    res = _run_hook("pre", _spawn_payload("engineer"), None, cwd=cwd)
    assert res.returncode == 0
    assert not (cwd / ".hydra").exists()


# --------------------------------------------------------------------------- #
# No sensitive text                                                          #
# --------------------------------------------------------------------------- #

def test_no_sensitive_text_leak(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    sentinel = "S3NT1NEL leak/?"
    _write_ticket(root, "wf-21", "run-21", "gen-0", "engineer")

    pre_payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Agent",
        "tool_use_id": "tu-1",
        "tool_input": {
            "subagent_type": "engineer",
            "prompt": sentinel,
            "description": sentinel,
            sentinel: "v",
        },
    }
    res_pre = _run_hook("pre", pre_payload, root, extra_env={"HYDRA_HOOK_RECORD_SHAPES": "1"})
    assert res_pre.returncode == 0

    post_payload = {
        "hook_event_name": "PostToolUse",
        "tool_name": "Agent",
        "tool_use_id": "tu-2",
        "tool_input": {"subagent_type": "engineer", "prompt": "p"},
        "tool_response": {
            "status": sentinel,
            "content": sentinel,
            "resolvedModel": sentinel,
            "agentId": sentinel,
            sentinel: 1,
        },
    }
    res_post = _run_hook("post", post_payload, root, extra_env={"HYDRA_HOOK_RECORD_SHAPES": "1"})
    assert res_post.returncode == 0

    audit_text = (root / ".hydra" / "hydra_hook.audit.jsonl").read_text(encoding="utf-8")
    shapes_text = (root / ".hydra" / "hydra_hook.shapes.jsonl").read_text(encoding="utf-8")
    assert "S3NT1NEL" not in audit_text
    assert "S3NT1NEL" not in shapes_text

    post_lines = [json.loads(l) for l in audit_text.splitlines() if json.loads(l).get("event") == "post"]
    assert post_lines[0]["status"] is None
    assert post_lines[0]["resolved_model"] is None
    assert post_lines[0]["agent_id"] is None

    shape_records = [json.loads(l) for l in shapes_text.splitlines()]
    assert any("<key>" in json.dumps(r["shape"]) for r in shape_records)
    assert any('"tool_input"' in json.dumps(r["shape"]) for r in shape_records)
    assert any('"subagent_type"' in json.dumps(r["shape"]) for r in shape_records)


def test_shapes_file_absent_when_recorder_off(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    res = _run_hook("pre", _spawn_payload("engineer"), root)
    assert res.returncode == 0
    assert not (root / ".hydra" / "hydra_hook.shapes.jsonl").exists()


# --------------------------------------------------------------------------- #
# Write confinement (file-level symlink)                                     #
# --------------------------------------------------------------------------- #

def test_write_confinement_audit_file_symlink(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_file = outside / "secret.txt"
    original_bytes = b"do-not-touch"
    outside_file.write_bytes(original_bytes)

    hydra_dir = root / ".hydra"
    hydra_dir.mkdir()
    link = hydra_dir / "hydra_hook.audit.jsonl"
    try:
        os.symlink(str(outside_file), str(link))
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"platform refused to create a symlink: {exc}")

    res = _run_hook("pre", _spawn_payload("engineer"), root)
    assert res.returncode == 0
    assert res.stdout == ""
    assert outside_file.read_bytes() == original_bytes


# --------------------------------------------------------------------------- #
# Write confinement (dir-level symlink / junction) -- Binding Addition A/B   #
# --------------------------------------------------------------------------- #

def _assert_outside_untouched(outside: Path) -> None:
    assert not (outside / "hydra_hook.audit.jsonl").exists()
    assert not (outside / "hydra_hook.shapes.jsonl").exists()


def test_write_confinement_hydra_dir_symlink(tmp_path):
    root = tmp_path / "project_sym"
    root.mkdir()
    outside = tmp_path / "outside_sym"
    outside.mkdir()

    variants_ran: list[str] = []
    variants_skipped: list[str] = []

    try:
        os.symlink(str(outside), str(root / ".hydra"), target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        variants_skipped.append(f"symlink: {exc}")
    else:
        res = _run_hook("pre", _spawn_payload("engineer"), root)
        assert res.returncode == 0
        assert res.stdout == ""
        _assert_outside_untouched(outside)

        res2 = _run_hook("pre", _spawn_payload("engineer"), root,
                          extra_env={"HYDRA_HOOK_RECORD_SHAPES": "1"})
        assert res2.returncode == 0
        assert res2.stdout == ""
        _assert_outside_untouched(outside)
        variants_ran.append("symlink")

    if sys.platform == "win32":
        root_j = tmp_path / "project_junction"
        root_j.mkdir()
        outside_j = tmp_path / "outside_junction"
        outside_j.mkdir()
        junction_target = root_j / ".hydra"
        jres = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(junction_target), str(outside_j)],
            capture_output=True, text=True,
        )
        if jres.returncode != 0:
            variants_skipped.append(f"junction: mklink failed: {jres.stderr or jres.stdout}")
        else:
            res = _run_hook("pre", _spawn_payload("engineer"), root_j)
            assert res.returncode == 0
            assert res.stdout == ""
            _assert_outside_untouched(outside_j)

            res2 = _run_hook("pre", _spawn_payload("engineer"), root_j,
                              extra_env={"HYDRA_HOOK_RECORD_SHAPES": "1"})
            assert res2.returncode == 0
            assert res2.stdout == ""
            _assert_outside_untouched(outside_j)
            variants_ran.append("junction")
    else:
        variants_skipped.append("junction: not Windows")

    if not variants_ran:
        pytest.skip("platform refused every dir-symlink/junction variant: "
                    + "; ".join(variants_skipped))
    # Record which variants ran, for the result summary (pytest -q captures
    # this on failure only; harmless when all pass).
    print(f"dir-confinement variants ran={variants_ran} skipped={variants_skipped}")


# --------------------------------------------------------------------------- #
# Pin tests                                                                  #
# --------------------------------------------------------------------------- #

def test_pin_canonical_prompt_text():
    hook = _load_hook_module()
    actions = [
        {"prompt": "hello"},
        {"prompt": "hello", "instructions": "do X"},
        {"prompt": "hello", "instructions": "do X", "artifact_text": "stuff"},
        {"prompt": "héllo wörld éè \U0001F600"},
        {},
    ]
    for action in actions:
        assert hook.canonical_prompt_text(action) == dispatch_seam.canonical_prompt_text(action)


def test_pin_cursor_path():
    hook = _load_hook_module()
    for run_id in ("r-1", "a/b", ""):
        assert hook.cursor_path("/proj", "wf-x", run_id) == host_bridge.cursor_path(
            "/proj", "wf-x", run_id)


def test_pin_schema_constants():
    hook = _load_hook_module()
    assert hook.TICKET_SCHEMA == dispatch_seam.TICKET_SCHEMA
    assert hook.CURSOR_SCHEMA == host_bridge.CURSOR_SCHEMA


def test_pin_native_leads():
    hook = _load_hook_module()
    expected = {p.qualified_lead_agent for p in native_packs.NATIVE_PACKS.values()}
    assert hook.NATIVE_LEADS == expected


def test_pin_bare_governed_agents_exist():
    hook = _load_hook_module()
    agents_dir = HOOK_PATH.resolve().parents[1] / "agents"
    for name in hook.BARE_GOVERNED:
        assert (agents_dir / f"{name}.md").exists(), f"missing agents/{name}.md"


# --------------------------------------------------------------------------- #
# Import independence                                                       #
# --------------------------------------------------------------------------- #

def test_import_independence_isolated_outside_repo(tmp_path):
    outside_dir = tmp_path / "outside_copy"
    outside_dir.mkdir()
    copied = outside_dir / "hydra_hook.py"
    shutil.copy(str(HOOK_PATH), str(copied))

    root = tmp_path / "project"
    root.mkdir()
    _write_ticket(root, "wf-22", "run-22", "gen-0", "engineer")

    res = _run_hook("pre", _spawn_payload("engineer"), root, hook_path=copied, isolated=True)
    assert res.returncode == 0

    lines = _audit_lines(root)
    assert lines[0]["decision"] == "found"

    diff_text = copied.read_text(encoding="utf-8")
    assert "import hydra_core" not in diff_text
    assert "from hydra_core" not in diff_text
