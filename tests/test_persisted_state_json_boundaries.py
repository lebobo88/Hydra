"""Close the remaining permissive ``json.dumps``/``json.dump`` sites at
PERSISTED-STATE boundaries (episodic memory, the eights pending-call spool,
the attended cursor, the ingest exactly-once ledger, the sibling-repo
registry, a squad's jest-config edit, and tool-usage analytics).

Classification recap (see the nine-site table in the change summary for the
full reader-and-consequence reasoning):
  STRICT   -- memory.py (payload + both cell-tag sites), host_bridge.py's
             `save_cursor`, ingest.py's `_write_ledger`, repo_registry.py's
             `_atomic_write_repos_json`, squad_node.py's
             `_ensure_jest_excludes` (a full-file rewrite of a THIRD-PARTY
             config it doesn't own the schema of).
  SANITIZE -- pending_spool.py's `SpooledCall.to_json` (a corrupt/refused row
             would be a stuck, never-re-readable spool entry) and
             tool_analytics.py's `flush_to_file` (a per-line JSONL append
             whose surrounding loop only clears its buffer once ALL lines
             succeed -- a strict refusal mid-loop would re-queue already
             -written lines and get stuck on the same poisoned call forever).

Every RFC-8259 validity claim below uses `json.loads(..., parse_constant=...)`
rather than a bare `json.loads`, which ACCEPTS bare NaN/Infinity tokens and
would therefore pass even on genuinely invalid output.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import uuid4

import pytest


def _reject_bare_constant(name: str):
    raise AssertionError(f"output is not valid RFC 8259 JSON: bare {name!r} token")


def _rfc8259_loads(text: str):
    return json.loads(text, parse_constant=_reject_bare_constant)


# ---------------------------------------------------------------------------
# memory.py:103 -- append_episodic's payload column (STRICT)
# ---------------------------------------------------------------------------

def test_append_episodic_refuses_non_finite_payload(tmp_path):
    from hydra_core.memory import append_episodic

    db = tmp_path / "episodic.db"
    with pytest.raises(ValueError, match="non-finite value"):
        append_episodic(
            uuid4(), "note", {"budget_usd": float("nan")}, db=db,
            cells=["qian"],
        )


def test_append_episodic_finite_payload_round_trips(tmp_path):
    """Control: an ordinary payload is unaffected and round-trips exactly."""
    from hydra_core.memory import append_episodic, resolve_episodic

    db = tmp_path / "episodic.db"
    payload = {"budget_usd": 12.5, "note": "ok", "count": 3}
    ref = append_episodic(uuid4(), "note", payload, db=db, cells=["qian"])
    resolved = resolve_episodic(ref.key, db=db)
    assert resolved["payload"] == payload


# ---------------------------------------------------------------------------
# memory.py:98 -- append_episodic's inferred-cells column (STRICT)
# ---------------------------------------------------------------------------

def test_append_episodic_refuses_non_finite_inferred_cells(tmp_path, monkeypatch):
    """`cells=None` runs the rules-first classifier; a hostile/buggy
    classifier that returns a non-finite value must not be silently written
    into a column later used for cell-membership (redaction/RBAC) decisions."""
    import hydra_core.memory as memory_module

    monkeypatch.setattr(memory_module, "classify", lambda **kw: [float("nan")])
    db = tmp_path / "episodic.db"
    with pytest.raises(ValueError, match="non-finite value"):
        memory_module.append_episodic(uuid4(), "note", {"ok": True}, db=db)


# ---------------------------------------------------------------------------
# memory.py:138 -- tag_episodic's merged-cells update (STRICT)
# ---------------------------------------------------------------------------

def test_tag_episodic_refuses_non_finite_merged_cells(tmp_path, monkeypatch):
    """`validate_cells` normally rejects anything that isn't a known Cell
    value before this site is ever reached; this test bypasses that upstream
    filter (as a corrupted/forged caller might) to prove the write-time
    guard itself refuses independently rather than relying solely on
    `validate_cells` never letting a bad value through."""
    import hydra_core.memory as memory_module

    monkeypatch.setattr(memory_module, "validate_cells", lambda cells: cells)
    db = tmp_path / "episodic.db"
    ref = memory_module.append_episodic(
        uuid4(), "note", {"ok": True}, db=db, cells=["qian"],
    )
    with pytest.raises(ValueError, match="non-finite value"):
        memory_module.tag_episodic(ref.key, [float("nan")], db=db)


def test_tag_episodic_finite_cells_round_trip(tmp_path):
    """Control: ordinary tag_episodic usage is unaffected."""
    from hydra_core.memory import append_episodic, tag_episodic, resolve_episodic

    db = tmp_path / "episodic.db"
    ref = append_episodic(uuid4(), "note", {"ok": True}, db=db, cells=["qian"])
    merged = tag_episodic(ref.key, ["kun"], db=db)
    assert set(merged) == {"qian", "kun"}
    resolved = resolve_episodic(ref.key, db=db)
    assert set(resolved["cells"]) == {"qian", "kun"}


# ---------------------------------------------------------------------------
# pending_spool.py:83 -- SpooledCall.to_json (SANITIZE)
# ---------------------------------------------------------------------------

def test_spooled_call_to_json_sanitizes_non_finite_arg_and_stays_readable():
    from hydra_core.eights.pending_spool import SpooledCall

    sc = SpooledCall(
        id="abc", tool="attest", args={"budget_usd": float("nan")},
        spooled_at="2026-01-01T00:00:00+00:00",
    )
    text = sc.to_json()
    parsed = _rfc8259_loads(text)  # would raise on a bare NaN token
    assert parsed["args"]["budget_usd"] is None
    assert any("budget_usd" in p for p in parsed["_non_finite_fields_sanitized"])

    # The row must remain re-readable (the whole point of sanitizing here):
    reloaded = SpooledCall.from_json(text)
    assert reloaded.args["budget_usd"] is None
    assert reloaded.id == "abc"


def test_spooled_call_to_json_finite_args_round_trip():
    """Control: ordinary spooled args are unaffected."""
    from hydra_core.eights.pending_spool import SpooledCall

    sc = SpooledCall(
        id="abc", tool="attest", args={"budget_usd": 4.5, "note": "ok"},
        spooled_at="2026-01-01T00:00:00+00:00",
    )
    text = sc.to_json()
    reloaded = SpooledCall.from_json(text)
    assert reloaded.args == {"budget_usd": 4.5, "note": "ok"}
    assert "_non_finite_fields_sanitized" not in text


# ---------------------------------------------------------------------------
# host_bridge.py:1480 -- save_cursor (STRICT)
# ---------------------------------------------------------------------------

def test_save_cursor_refuses_non_finite(tmp_path):
    from hydra_core.host_bridge import save_cursor

    path = tmp_path / "cursor.json"
    with pytest.raises(ValueError, match="non-finite value"):
        save_cursor(path, {"schema": 1, "cost_usd": float("inf")})
    assert not path.exists()


def test_save_cursor_finite_round_trips(tmp_path):
    from hydra_core.host_bridge import save_cursor, load_cursor, CURSOR_SCHEMA

    path = tmp_path / "cursor.json"
    cursor = {"schema": CURSOR_SCHEMA, "cost_usd": 4.5, "stage": "generate"}
    save_cursor(path, cursor)
    assert load_cursor(path) == cursor


# ---------------------------------------------------------------------------
# ingest.py:858 -- _write_ledger (STRICT)
# ---------------------------------------------------------------------------

def test_write_ledger_refuses_non_finite(tmp_path):
    from hydra_core.ingest import _write_ledger, ingest_ledger_path

    with pytest.raises(ValueError, match="non-finite value"):
        _write_ledger(tmp_path, "wf-1", {float("nan")})  # type: ignore[arg-type]
    assert not ingest_ledger_path(tmp_path, "wf-1").exists()


def test_write_ledger_finite_ids_round_trip(tmp_path):
    from hydra_core.ingest import _write_ledger, load_ingested_ids

    _write_ledger(tmp_path, "wf-1", {"env-a", "env-b"})
    assert load_ingested_ids(tmp_path, "wf-1") == {"env-a", "env-b"}


# ---------------------------------------------------------------------------
# repo_registry.py:427 -- _atomic_write_repos_json (STRICT)
# ---------------------------------------------------------------------------

def test_atomic_write_repos_json_refuses_non_finite(tmp_path, monkeypatch):
    import hydra_core.repo_registry as repo_registry_module

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    with pytest.raises(ValueError, match="non-finite value"):
        repo_registry_module._atomic_write_repos_json(
            {"hydra": float("nan")}  # type: ignore[dict-item]
        )
    assert not repo_registry_module._repos_json_path().exists()


def test_atomic_write_repos_json_finite_round_trips(tmp_path, monkeypatch):
    import hydra_core.repo_registry as repo_registry_module

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    repo_registry_module._atomic_write_repos_json({"hydra": "/repos/hydra"})
    data = json.loads(repo_registry_module._repos_json_path().read_text(encoding="utf-8"))
    assert data == {"hydra": "/repos/hydra"}


# ---------------------------------------------------------------------------
# squad_node.py:2914 -- _ensure_jest_excludes (STRICT: don't rewrite a
# third-party file we don't own the schema of if it can't be represented)
# ---------------------------------------------------------------------------

def test_ensure_jest_excludes_refuses_non_finite_and_leaves_file_untouched(tmp_path):
    from hydra_core.squad_node import _ensure_jest_excludes

    cfg = tmp_path / "jest.config.json"
    original = json.dumps({"testPathIgnorePatterns": [], "hostile": float("nan")})
    # Written with plain json.dumps (allow_nan=True default) to simulate an
    # operator's pre-existing file containing a value we could never have
    # produced ourselves -- exactly the "we don't own this schema" case.
    cfg.write_text(original, encoding="utf-8")

    assert _ensure_jest_excludes(cfg) is False
    assert cfg.read_text(encoding="utf-8") == original


def test_ensure_jest_excludes_finite_file_gets_patterns_added(tmp_path):
    from hydra_core.squad_node import _ensure_jest_excludes

    cfg = tmp_path / "jest.config.json"
    cfg.write_text(json.dumps({"testPathIgnorePatterns": []}), encoding="utf-8")

    assert _ensure_jest_excludes(cfg) is True
    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert "<rootDir>/.harness/" in data["testPathIgnorePatterns"]
    assert "<rootDir>/.hydra/" in data["testPathIgnorePatterns"]


# ---------------------------------------------------------------------------
# tool_analytics.py:137 -- ToolUsageTracker.flush_to_file (SANITIZE)
# ---------------------------------------------------------------------------

def test_flush_to_file_sanitizes_non_finite_duration_and_stays_valid_json(tmp_path):
    from hydra_core.tool_analytics import ToolUsageTracker

    tracker = ToolUsageTracker()
    tracker.record(
        workflow_id="wf-1", squad_id="engineering", node_name="generate",
        server="pp_harness", tool="generate", status="done",
        duration_ms=float("inf"),
    )
    out = tmp_path / "tool_usage.jsonl"
    count = tracker.flush_to_file(out)
    assert count == 1

    line = out.read_text(encoding="utf-8").strip()
    parsed = _rfc8259_loads(line)  # would raise on a bare Infinity token
    assert parsed["duration_ms"] is None
    assert any("duration_ms" in p for p in parsed["_non_finite_fields_sanitized"])


def test_flush_to_file_finite_calls_all_survive_the_loop(tmp_path):
    """Control, and the loop-survival property the SANITIZE choice exists
    for: multiple ordinary calls in one flush all get written AND the
    in-memory buffer is fully cleared (a strict refusal on any one call
    would otherwise abort the loop partway through)."""
    from hydra_core.tool_analytics import ToolUsageTracker

    tracker = ToolUsageTracker()
    for i in range(3):
        tracker.record(
            workflow_id="wf-1", squad_id="engineering", node_name="generate",
            server="pp_harness", tool=f"tool_{i}", status="done",
            duration_ms=float(i),
        )
    out = tmp_path / "tool_usage.jsonl"
    count = tracker.flush_to_file(out)
    assert count == 3
    assert tracker._calls == []

    lines = out.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 3
    for line in lines:
        parsed = json.loads(line)
        assert "_non_finite_fields_sanitized" not in parsed


# ---------------------------------------------------------------------------
# cli.py gateway-* config writes (STRICT, reclassified from the CLI-stdout
# sanitize policy -- these three sites write PERSISTED OPERATOR
# CONFIGURATION to disk, not a printed command result).
# ---------------------------------------------------------------------------

def test_gateway_export_backends_refuses_non_finite_and_leaves_registry_untouched(
    tmp_path, monkeypatch
):
    import argparse
    import hydra_core.dispatcher as dispatcher_module
    from hydra_core import cli as cli_module

    registry = tmp_path / "backends.json"
    original = json.dumps({"pre_existing": {"command": "true"}})
    registry.write_text(original, encoding="utf-8")
    monkeypatch.setattr(dispatcher_module, "BACKEND_REGISTRY", registry)
    monkeypatch.setattr(
        dispatcher_module, "_load_user_scope_mcp",
        lambda: {"hostile": {"command": "true", "env": {"budget": float("nan")}}},
    )
    # `_cmd_gateway_export_backends` does `from .dispatcher import
    # _load_user_scope_mcp, BACKEND_REGISTRY` INSIDE the function, so
    # patching the dispatcher module's attributes (not cli_module's) is
    # what actually takes effect at call time.
    with pytest.raises(ValueError, match="non-finite value"):
        cli_module._cmd_gateway_export_backends(argparse.Namespace())
    assert registry.read_text(encoding="utf-8") == original


def test_gateway_remove_old_backends_refuses_non_finite_and_leaves_claude_json_untouched(
    tmp_path, monkeypatch
):
    import argparse
    import hydra_core.dispatcher as dispatcher_module
    from hydra_core import cli as cli_module

    registry = tmp_path / "backends.json"
    registry.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(dispatcher_module, "BACKEND_REGISTRY", registry)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))

    claude_json = tmp_path / ".claude.json"
    original = json.dumps({
        # The non-finite value sits on a KEPT entry (mcpServers.hydra_gateway
        # survives the prune, unlike old_one below) so it is still present
        # in `raw` at write time -- this proves the refusal happens on the
        # POST-PRUNE write, not merely on whatever gets deleted.
        "mcpServers": {
            "hydra_gateway": {"command": "true", "meta": float("inf")},
            "old_one": {"command": "true"},
        },
    })
    claude_json.write_text(original, encoding="utf-8")

    with pytest.raises(ValueError, match="non-finite value"):
        cli_module._cmd_gateway_remove_old_backends(argparse.Namespace())
    assert claude_json.read_text(encoding="utf-8") == original


def test_gateway_setup_refuses_non_finite_and_leaves_registry_untouched(
    tmp_path, monkeypatch
):
    import argparse
    from pathlib import Path as PathClass
    import hydra_core.dispatcher as dispatcher_module
    from hydra_core import cli as cli_module

    registry = tmp_path / "backends.json"
    original = json.dumps({"pre_existing": {"command": "true"}})
    registry.write_text(original, encoding="utf-8")
    monkeypatch.setattr(dispatcher_module, "BACKEND_REGISTRY", registry)

    hostile_templates = json.dumps({
        "hostile": {
            "type": "stdio", "command": "true", "args": [],
            "env": {"budget": float("nan")}, "required": True,
        },
    })
    real_read_text = PathClass.read_text

    def fake_read_text(self, *a, **kw):
        if self.name == "gateway_templates.json":
            return hostile_templates
        return real_read_text(self, *a, **kw)

    monkeypatch.setattr(PathClass, "read_text", fake_read_text)

    with pytest.raises(ValueError, match="non-finite value"):
        cli_module._cmd_gateway_setup(argparse.Namespace())
    assert registry.read_text(encoding="utf-8") == original
