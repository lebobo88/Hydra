"""Cross-vendor judge finding (item 3/6, HIGH): MCP-layer JSON strictness.

Two distinct guarantees, exercised at both ends of `mcp_servers/hydra_control
/server.py` (loaded offline by path -- no MCP transport, no subprocess, no
network; mirrors `test_hydra_control_schema_parity.py`'s loader):

  - PERSISTENCE (`_run_submit_host_result`'s host-result staging file) must
    FAIL STRUCTURALLY on a non-finite value -- it is a WRITE, and the
    existing `workflow_submit_host_result` wrapper already turns any raised
    exception into a `{"ok": False, "error": ...}` structured response.
  - RESPONSES (the real-SDK `_call_tool` handler and the bare-stdio loop)
    must remain valid JSON for the client no matter what -- sanitize any
    non-finite value to `null` with an explicit marker, never raise, never
    emit an invalid `NaN`/`Infinity` token.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_hydra_control_server():
    spec = importlib.util.spec_from_file_location(
        "hydra_control_server_under_test_strict_json",
        REPO_ROOT / "mcp_servers" / "hydra_control" / "server.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Persistence: strict, fails with a structured error.
# ---------------------------------------------------------------------------

def test_submit_host_result_persistence_refuses_non_finite_value(tmp_path, monkeypatch):
    server = _load_hydra_control_server()
    monkeypatch.setattr(server, "_HYDRA_ROOT", tmp_path)

    cli_calls: list[list[str]] = []

    def _stub_run_cli_json(cli_args, **_kw):
        cli_calls.append(cli_args)
        return {"ok": True}

    monkeypatch.setattr(server, "_run_cli_json", _stub_run_cli_json)

    hostile_result = {"status": "done", "cost_usd": float("inf")}
    with pytest.raises(ValueError, match="cost_usd"):
        server._run_submit_host_result("wf-abc123", "run-1", "call-1", hostile_result)

    # Never reached the CLI subprocess -- the refusal happens at the
    # persistence write, before any downstream call.
    assert cli_calls == []
    # And no partial/bare-NaN file was left behind on disk.
    res_dir = tmp_path / ".hydra" / "wf-abc123" / "attended"
    written = list(res_dir.glob("hostresult-*.json")) if res_dir.exists() else []
    assert written == []


def test_submit_host_result_persistence_writes_normally_for_finite_result(tmp_path, monkeypatch):
    server = _load_hydra_control_server()
    monkeypatch.setattr(server, "_HYDRA_ROOT", tmp_path)
    monkeypatch.setattr(server, "_run_cli_json", lambda *a, **k: {"ok": True})

    clean_result = {"status": "done", "cost_usd": 1.25}
    server._run_submit_host_result("wf-abc123", "run-1", "call-1", clean_result)

    res_dir = tmp_path / ".hydra" / "wf-abc123" / "attended"
    written = list(res_dir.glob("hostresult-*.json"))
    assert len(written) == 1
    on_disk = json.loads(written[0].read_text(encoding="utf-8"))
    assert on_disk == clean_result


def test_workflow_submit_host_result_wrapper_surfaces_structured_error(tmp_path, monkeypatch):
    """The tool-handler wrapper (`workflow_submit_host_result` inside
    `_tool_handlers()`) already catches any exception from
    `_run_submit_host_result` and returns `{"ok": False, "error": ...}` --
    confirm the non-finite-value ValueError flows through that same path
    rather than crashing the handler."""
    server = _load_hydra_control_server()
    monkeypatch.setattr(server, "_HYDRA_ROOT", tmp_path)
    monkeypatch.setattr(server, "_run_cli_json", lambda *a, **k: {"ok": True})

    handlers = server._tool_handlers()
    handler = handlers["hydra.workflow.submit_host_result"]
    out = handler({
        "workflow_id": "wf-abc123",
        "run_id": "run-1",
        "call_key": "call-1",
        "result": {"status": "done", "cost_usd": float("nan")},
    })
    assert out["ok"] is False
    assert "cost_usd" in out["error"]


# ---------------------------------------------------------------------------
# Tool responses: stay valid JSON, sanitize with a marker.
# ---------------------------------------------------------------------------

def test_dumps_tool_response_safe_sanitizes_instead_of_raising():
    from hydra_core.strict_json import dumps_tool_response_safe

    hostile = {"id": 1, "result": {"cost_usd": float("inf"), "ok": True}}
    text = dumps_tool_response_safe(hostile, label="tool_response:test")

    # Always valid JSON -- json.loads must never raise here.
    parsed = json.loads(text)
    assert parsed["result"]["cost_usd"] is None
    assert "$.result.cost_usd" in parsed["_non_finite_fields_sanitized"]
    assert "Infinity" not in text


def test_dumps_tool_response_safe_leaves_clean_payload_unchanged():
    from hydra_core.strict_json import dumps_tool_response_safe

    clean = {"id": 1, "result": {"cost_usd": 1.5, "ok": True}}
    text = dumps_tool_response_safe(clean, label="tool_response:test")
    assert json.loads(text) == clean
    assert "_non_finite_fields_sanitized" not in text


def test_sanitize_non_finite_reports_every_offending_path():
    from hydra_core.strict_json import sanitize_non_finite

    payload = {"a": float("nan"), "b": [1, float("inf"), 3], "c": {"d": float("-inf")}}
    sanitized, paths = sanitize_non_finite(payload)
    assert sanitized == {"a": None, "b": [1, None, 3], "c": {"d": None}}
    assert set(paths) == {"$.a", "$.b[1]", "$.c.d"}


def test_sanitize_non_finite_allows_shared_acyclic_reference():
    """Companion to `strict_json.find_non_finite_field`'s item 4/6 fix: a
    shared-but-not-ancestor reference must be walked normally here too, not
    mistaken for a cycle and left un-sanitized."""
    from hydra_core.strict_json import sanitize_non_finite

    child = {"x": float("nan")}
    payload = {"a": child, "b": child}
    sanitized, paths = sanitize_non_finite(payload)
    assert sanitized == {"a": {"x": None}, "b": {"x": None}}
    assert set(paths) == {"$.a.x", "$.b.x"}
