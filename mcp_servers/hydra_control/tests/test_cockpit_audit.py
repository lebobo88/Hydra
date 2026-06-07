"""Tests for the hydra.cockpit.audit tool (C5 eights audit filing).

Strategy:
  - Import the _tool_handlers() factory directly so no subprocess is needed.
  - Patch _file_cockpit_audit_envelope for most tests (isolates the MCP tool
    logic from the EightsAttestor / spool implementation).
  - One integration test exercises the full _file_cockpit_audit_envelope path
    with a stubbed attestor to confirm spool-safe detection works.

Coverage:
  1. cockpit_audit happy path — valid args → calls filer, returns {ok:true}
  2. cockpit_audit spooled path — filer returns spooled:true → propagated
  3. cockpit_audit required-field validation (action, actor, project, trace_id)
  4. cockpit_audit workflow_id regex validation (valid + invalid)
  5. cockpit_audit optional fields (option, detail) passed through
  6. cockpit_audit exception in filer → {ok:true, spooled:true} (never crashes)
  7. _file_cockpit_audit_envelope with no attestor → {ok:true, spooled:true}
  8. _file_cockpit_audit_envelope with stubbed attestor, live filing → spooled:False
  9. _file_cockpit_audit_envelope with stubbed attestor, spool occurs → spooled:True
 10. hydra.control.ping and hydra.workflow.resume still present and unchanged
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# Ensure hydra_control is importable from the repo root
_HERE = Path(__file__).resolve().parent.parent
_HYDRA_ROOT = _HERE.parent.parent
sys.path.insert(0, str(_HYDRA_ROOT))

from mcp_servers.hydra_control.server import (  # noqa: E402
    _WORKFLOW_ID_RE,
    _file_cockpit_audit_envelope,
    _tool_handlers,
)

# ---------------------------------------------------------------------------
# Helper: get the cockpit_audit handler from the live _tool_handlers() dict
# ---------------------------------------------------------------------------

def _get_audit_handler():
    return _tool_handlers()["hydra.cockpit.audit"]


def _get_ping_handler():
    return _tool_handlers()["hydra.control.ping"]


def _get_resume_handler():
    return _tool_handlers()["hydra.workflow.resume"]


# ---------------------------------------------------------------------------
# Minimal valid args fixture
# ---------------------------------------------------------------------------

VALID_ARGS: dict[str, Any] = {
    "action": "launch",
    "actor": "hydra-cockpit",
    "project": "Hydra",
    "trace_id": "hcp_1234567890_abcd1234",
}

VALID_WORKFLOW_ID = "5ebd4268-5de0-4dbf-a82d-42c596d4818e"


# ===========================================================================
# 1. Happy path — valid args, filer returns {ok:true, spooled:false}
# ===========================================================================

def test_cockpit_audit_happy_path():
    handler = _get_audit_handler()
    with patch(
        "mcp_servers.hydra_control.server._file_cockpit_audit_envelope",
        return_value={"ok": True, "spooled": False},
    ) as mock_filer:
        result = handler(dict(VALID_ARGS))
    assert result["ok"] is True
    assert result.get("spooled") is False
    mock_filer.assert_called_once()
    call_kw = mock_filer.call_args.kwargs
    assert call_kw["action"] == "launch"
    assert call_kw["actor"] == "hydra-cockpit"
    assert call_kw["project"] == "Hydra"
    assert call_kw["trace_id"] == "hcp_1234567890_abcd1234"


# ===========================================================================
# 2. Spool path — filer returns spooled:true → propagated to caller
# ===========================================================================

def test_cockpit_audit_spooled_true_propagated():
    handler = _get_audit_handler()
    with patch(
        "mcp_servers.hydra_control.server._file_cockpit_audit_envelope",
        return_value={"ok": True, "spooled": True},
    ):
        result = handler(dict(VALID_ARGS))
    assert result["ok"] is True
    assert result["spooled"] is True


# ===========================================================================
# 3. Required-field validation
# ===========================================================================

@pytest.mark.parametrize("missing_field", ["action", "actor", "project", "trace_id"])
def test_cockpit_audit_missing_required_field(missing_field: str):
    handler = _get_audit_handler()
    args = {k: v for k, v in VALID_ARGS.items() if k != missing_field}
    result = handler(args)
    assert result["ok"] is False
    assert missing_field in result.get("error", "")


def test_cockpit_audit_empty_action():
    handler = _get_audit_handler()
    args = {**VALID_ARGS, "action": ""}
    result = handler(args)
    assert result["ok"] is False
    assert "action" in result["error"]


def test_cockpit_audit_empty_actor():
    handler = _get_audit_handler()
    args = {**VALID_ARGS, "actor": ""}
    result = handler(args)
    assert result["ok"] is False
    assert "actor" in result["error"]


# ===========================================================================
# 4. workflow_id validation
# ===========================================================================

@pytest.mark.parametrize("good_wfid", [
    VALID_WORKFLOW_ID,
    "abc123",
    "A",
    "wf_1234",
    "5ebd4268",
])
def test_cockpit_audit_valid_workflow_id(good_wfid: str):
    handler = _get_audit_handler()
    args = {**VALID_ARGS, "workflow_id": good_wfid}
    with patch(
        "mcp_servers.hydra_control.server._file_cockpit_audit_envelope",
        return_value={"ok": True, "spooled": False},
    ):
        result = handler(args)
    assert result["ok"] is True


@pytest.mark.parametrize("bad_wfid", [
    "",                 # empty string (after strip → passes empty check, triggers regex only if non-empty)
    "-bad",             # starts with hyphen
    "a" * 65,          # too long (>64 chars after first)
    "bad id",          # space
    "bad/id",          # slash
    "bad;id",          # semicolon
])
def test_cockpit_audit_invalid_workflow_id(bad_wfid: str):
    handler = _get_audit_handler()
    args = {**VALID_ARGS, "workflow_id": bad_wfid}
    with patch(
        "mcp_servers.hydra_control.server._file_cockpit_audit_envelope",
        return_value={"ok": True, "spooled": False},
    ) as mock_filer:
        result = handler(args)
    if bad_wfid == "":
        # Empty workflow_id is treated as absent (no validation trigger)
        assert result["ok"] is True
    else:
        assert result["ok"] is False
        assert result["error"] == "invalid_workflow_id"
        mock_filer.assert_not_called()


# ===========================================================================
# 5. Optional fields (option, detail) passed through
# ===========================================================================

def test_cockpit_audit_optional_fields_passed_through():
    handler = _get_audit_handler()
    args = {
        **VALID_ARGS,
        "workflow_id": VALID_WORKFLOW_ID,
        "option": "80",
        "detail": "live launch for testing",
    }
    with patch(
        "mcp_servers.hydra_control.server._file_cockpit_audit_envelope",
        return_value={"ok": True, "spooled": False},
    ) as mock_filer:
        result = handler(args)
    assert result["ok"] is True
    call_kw = mock_filer.call_args.kwargs
    assert call_kw["option"] == "80"
    assert call_kw["detail"] == "live launch for testing"
    assert call_kw["workflow_id"] == VALID_WORKFLOW_ID


def test_cockpit_audit_absent_optional_fields_are_none():
    handler = _get_audit_handler()
    with patch(
        "mcp_servers.hydra_control.server._file_cockpit_audit_envelope",
        return_value={"ok": True, "spooled": False},
    ) as mock_filer:
        handler(dict(VALID_ARGS))
    call_kw = mock_filer.call_args.kwargs
    assert call_kw["option"] is None
    assert call_kw["detail"] is None
    assert call_kw["workflow_id"] is None


# ===========================================================================
# 6. Exception in filer → {ok:true, spooled:true} — never crashes caller
# ===========================================================================

def test_cockpit_audit_filer_exception_returns_degraded():
    handler = _get_audit_handler()
    with patch(
        "mcp_servers.hydra_control.server._file_cockpit_audit_envelope",
        side_effect=RuntimeError("simulated crash"),
    ):
        result = handler(dict(VALID_ARGS))
    # Must NOT raise; must return ok:true with spooled:true
    assert result["ok"] is True
    assert result["spooled"] is True
    assert "RuntimeError" in result.get("reason", "")


# ===========================================================================
# 7. _file_cockpit_audit_envelope with no attestor → {ok:true, spooled:true}
# ===========================================================================

def test_file_cockpit_audit_envelope_no_attestor():
    """When _get_attestor() returns None (hydra_core not importable), the
    function must still return {ok:true, spooled:true, reason:...}."""
    with patch("mcp_servers.hydra_control.server._get_attestor", return_value=None):
        result = _file_cockpit_audit_envelope(
            action="launch",
            actor="hydra-cockpit",
            project="Hydra",
            trace_id="hcp_abc",
        )
    assert result["ok"] is True
    assert result["spooled"] is True


# ===========================================================================
# 8. _file_cockpit_audit_envelope with stubbed attestor — live filing
#    (pending_count unchanged → spooled:False)
# ===========================================================================

def test_file_cockpit_audit_envelope_live_filing():
    mock_attestor = MagicMock()
    mock_attestor.pending_count.return_value = 0  # count stable → no spool
    mock_attestor.envelope_record.return_value = {"receipt": "uuid-abc"}

    with patch("mcp_servers.hydra_control.server._get_attestor", return_value=mock_attestor):
        result = _file_cockpit_audit_envelope(
            action="approve",
            actor="hydra-cockpit",
            project="Hydra",
            trace_id="hcp_approve_xyz",
            workflow_id=VALID_WORKFLOW_ID,
        )
    assert result["ok"] is True
    assert result["spooled"] is False
    mock_attestor.envelope_record.assert_called_once()
    envelope_arg = mock_attestor.envelope_record.call_args[0][0]
    assert envelope_arg["type"] == "cockpit_write"
    assert envelope_arg["action"] == "approve"
    assert envelope_arg["workflow_id"] == VALID_WORKFLOW_ID
    assert "id" in envelope_arg  # uuid4 minted
    assert envelope_arg["origin_squad"] == "hydra-cockpit"


# ===========================================================================
# 9. _file_cockpit_audit_envelope — spool detected (count increased)
# ===========================================================================

def test_file_cockpit_audit_envelope_spool_detected():
    mock_attestor = MagicMock()
    # pending_count returns 0 then 1 → spool occurred
    mock_attestor.pending_count.side_effect = [0, 1]
    mock_attestor.envelope_record.return_value = None  # simulated offline

    with patch("mcp_servers.hydra_control.server._get_attestor", return_value=mock_attestor):
        result = _file_cockpit_audit_envelope(
            action="reject",
            actor="hydra-cockpit",
            project="Hydra",
            trace_id="hcp_reject_abc",
        )
    assert result["ok"] is True
    assert result["spooled"] is True


# ===========================================================================
# 10. Existing tools (ping, resume) are unchanged
# ===========================================================================

def test_ping_still_present_and_returns_ok():
    handler = _get_ping_handler()
    result = handler({})
    assert result["ok"] is True
    assert result["server"] == "hydra_control"
    assert "hydra_root" in result
    assert "ts" in result


def test_resume_still_present_and_validates_workflow_id():
    handler = _get_resume_handler()
    result = handler({"workflow_id": "-bad", "action": "approve"})
    assert result["ok"] is False
    assert result["error"] == "invalid_workflow_id"


def test_resume_still_validates_action():
    handler = _get_resume_handler()
    result = handler({"workflow_id": VALID_WORKFLOW_ID, "action": "bogus"})
    assert result["ok"] is False
    assert result["error"] == "invalid_action"


def test_all_three_tools_in_handlers():
    handlers = _tool_handlers()
    assert "hydra.control.ping" in handlers
    assert "hydra.workflow.resume" in handlers
    assert "hydra.cockpit.audit" in handlers
    assert len(handlers) == 3
