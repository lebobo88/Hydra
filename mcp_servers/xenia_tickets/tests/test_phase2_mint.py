"""WS-AUTH Phase 2 — Trusted mint + dispatch integration tests.

Tests the trusted mint helper (mint_for_tool.py) and its integration with
the server-side verification (server.py send_response / execute_approved).

Security invariants verified:
  1. mint_token_for_tool has NO actor_id parameter. Identity comes ONLY from
     CLAUDE_HOOK_AGENT_NAME (env). No CLAUDE_AGENT_NAME fallback.
  2. A forged capability_token in tool args is OVERWRITTEN by the hook mint.
     The server records the TRUSTED env identity, not the forged one.
  3. HYDRA_OPERATOR_KEY is never exposed in any output.
  4. CLAUDE_HOOK_AGENT_NAME unset/empty -> None (fail-closed).
  5. HYDRA_OPERATOR_KEY unset -> None (fail-closed, no degraded token emitted).
  6. Minted token round-trips through server verify as an OBJECT (not a string).
  7. top-level ticket_id absent (even with TICKET-* text in body) -> None.
  8. Unknown tool name -> None.

All tests are pure-Python — no subprocess, no daemon.
"""
from __future__ import annotations

import inspect
import json
import os
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
HYDRA_ROOT = Path(__file__).resolve().parents[4]
XENIA_ROOT = Path("C:/AiAppDeployments/Xenia")
sys.path.insert(0, str(HYDRA_ROOT))
sys.path.insert(0, str(XENIA_ROOT))

from mcp_servers.xenia_tickets.clearance import mint_clearance_token       # noqa: E402
import mcp_servers.xenia_tickets.mint_for_tool as _mint_module              # noqa: E402
from mcp_servers.xenia_tickets.mint_for_tool import mint_token_for_tool    # noqa: E402
from mcp_servers.xenia_tickets.server import (                             # noqa: E402
    _tool_handlers,
    mint_caller_capability,
)
from hydra_core.auth.capability import verify_capability                   # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_CAP_KEY_HEX = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
_TICKET_ID = "000042"
_TRUSTED_AGENT = "hermes"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@contextmanager
def _env(**env: str | None):
    """Temporarily patch os.environ."""
    old = {k: os.environ.get(k) for k in env}
    for k, v in env.items():
        if v is not None:
            os.environ[k] = v
        else:
            os.environ.pop(k, None)
    try:
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _make_ticket(tmp_path: Path, ticket_id: str = _TICKET_ID) -> None:
    tasks = tmp_path / "hearth" / "tasks"
    tasks.mkdir(parents=True, exist_ok=True)
    ticket = {
        "ticket_id": ticket_id,
        "status": "open",
        "priority": "P3",
        "intent": None,
        "customer_ref": "customer:aabbcc",
        "subject": "Phase 2 WS-AUTH test",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "sla": {
            "first_response_due": datetime.now(timezone.utc).isoformat(),
            "breached": False,
        },
        "history": [],
        "recommendations": [],
    }
    (tasks / f"TICKET-{ticket_id}.json").write_text(
        json.dumps(ticket), encoding="utf-8"
    )


def _make_approval(tmp_path: Path, ticket_id: str = _TICKET_ID,
                   action: str = "refund", scope: str = "billing",
                   seq: str = "001") -> None:
    approvals_dir = tmp_path / "hearth" / "approvals"
    approvals_dir.mkdir(parents=True, exist_ok=True)
    expires_at = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    content = (
        f"ticket_id: {ticket_id}\n"
        f"status: approved\n"
        f"action: {action}\n"
        f"scope: {scope}\n"
        f"issued_by: some-human-approver\n"
        f"expires_at: {expires_at}\n"
    )
    (approvals_dir / f"APPROVAL-{ticket_id}-{seq}.yaml").write_text(
        content, encoding="utf-8"
    )


def _mint_clearance(body: str, key: str = "cafebabe") -> str:
    with _env(XENIA_CONTEXT_SIGNING_KEY=key):
        token_dict = mint_clearance_token(body, None)
    assert token_dict is not None
    return json.dumps(token_dict)


def _call_send(
    tmp_path: Path,
    body: str = "Your ticket is resolved.",
    cap_token=None,
    clearance_token=None,
    clearance_key: str = "cafebabe",
) -> dict:
    with _env(HYDRA_XENIA_ROOT=str(tmp_path),
              XENIA_CONTEXT_SIGNING_KEY=clearance_key,
              HYDRA_OPERATOR_KEY=_CAP_KEY_HEX):
        handlers = _tool_handlers()
        args: dict = {"ticket_id": _TICKET_ID, "body": body}
        if cap_token is not None:
            args["capability_token"] = cap_token
        if clearance_token is not None:
            args["clearance_token"] = clearance_token
        return handlers["xenia-tickets.send_response"](args)


def _call_exec(
    tmp_path: Path,
    cap_token=None,
    approval_id: str = f"APPROVAL-{_TICKET_ID}-001",
) -> dict:
    with _env(HYDRA_XENIA_ROOT=str(tmp_path),
              HYDRA_OPERATOR_KEY=_CAP_KEY_HEX):
        handlers = _tool_handlers()
        args: dict = {
            "ticket_id": _TICKET_ID,
            "action": "refund",
            "scope": "billing",
            "approval_id": approval_id,
        }
        if cap_token is not None:
            args["capability_token"] = cap_token
        return handlers["xenia-tickets.execute_approved"](args)


# ===========================================================================
# Fix 1 enforcement: no actor_id parameter in the public signature
# ===========================================================================

class TestNoActorIdParameter:
    """mint_token_for_tool must have NO actor_id parameter.

    The function signature itself enforces that callers cannot supply an
    identity — it must come exclusively from CLAUDE_HOOK_AGENT_NAME env.
    """

    def test_signature_has_no_actor_id_param(self):
        """mint_token_for_tool signature must NOT contain actor_id."""
        sig = inspect.signature(mint_token_for_tool)
        assert "actor_id" not in sig.parameters, (
            "mint_token_for_tool must NOT have an actor_id parameter. "
            "Identity comes exclusively from CLAUDE_HOOK_AGENT_NAME env var."
        )

    def test_only_tool_name_and_ticket_id_params(self):
        """Function accepts only tool_name and ticket_id (both keyword-only)."""
        sig = inspect.signature(mint_token_for_tool)
        param_names = set(sig.parameters.keys())
        assert param_names == {"tool_name", "ticket_id"}, (
            f"Expected only {{tool_name, ticket_id}}, got {param_names}"
        )

    def test_calling_with_actor_id_raises_type_error(self):
        """Passing actor_id= must raise TypeError (param does not exist)."""
        with pytest.raises(TypeError):
            mint_token_for_tool(  # type: ignore[call-arg]
                tool_name="xenia-tickets.send_response",
                ticket_id=_TICKET_ID,
                actor_id="hermes",  # must not be accepted
            )

    def test_no_claude_agent_name_fallback_in_source(self):
        """mint_token_for_tool code must NOT use CLAUDE_AGENT_NAME as an env lookup."""
        import ast
        src = inspect.getsource(_mint_module)
        # Check that CLAUDE_AGENT_NAME never appears in a string literal that is
        # used as an os.environ key — i.e. no os.environ.get("CLAUDE_AGENT_NAME")
        # or os.environ["CLAUDE_AGENT_NAME"] anywhere in the code.
        # We parse the AST to check for string-constant "CLAUDE_AGENT_NAME" that
        # appears as an argument to os.environ.get / os.environ.__getitem__.
        tree = ast.parse(src)
        found = False
        for node in ast.walk(tree):
            # os.environ.get("CLAUDE_AGENT_NAME", ...) or os.environ["CLAUDE_AGENT_NAME"]
            if isinstance(node, ast.Call):
                func = node.func
                if (isinstance(func, ast.Attribute) and func.attr == "get"
                        and isinstance(func.value, ast.Attribute)
                        and func.value.attr == "environ"):
                    if node.args and isinstance(node.args[0], ast.Constant):
                        if node.args[0].value == "CLAUDE_AGENT_NAME":
                            found = True
            if isinstance(node, ast.Subscript):
                if (isinstance(node.value, ast.Attribute)
                        and node.value.attr == "environ"):
                    sl = node.slice
                    if isinstance(sl, ast.Constant) and sl.value == "CLAUDE_AGENT_NAME":
                        found = True
        assert not found, (
            "mint_for_tool.py must NOT use os.environ.get('CLAUDE_AGENT_NAME') "
            "or os.environ['CLAUDE_AGENT_NAME']. "
            "Identity must come ONLY from CLAUDE_HOOK_AGENT_NAME."
        )


# ===========================================================================
# Tests: mint_token_for_tool — core trusted mint helper
# ===========================================================================

class TestMintTokenForTool:
    """Trusted mint helper produces correct, server-verifiable tokens."""

    def test_mint_send_response_trusted_identity(self):
        """CLAUDE_HOOK_AGENT_NAME=hermes -> correct token fields."""
        with _env(CLAUDE_HOOK_AGENT_NAME=_TRUSTED_AGENT,
                  HYDRA_OPERATOR_KEY=_CAP_KEY_HEX):
            token = mint_token_for_tool(
                tool_name="xenia-tickets.send_response",
                ticket_id=_TICKET_ID,
            )
        assert token is not None
        assert token["actor_id"] == _TRUSTED_AGENT
        assert token["actor_kind"] == "agent"
        assert token["capability"] == "xenia.send_response"
        assert token["resource_id"] == _TICKET_ID
        assert token["workflow_id"] == _TICKET_ID
        assert "jti" in token and token["jti"]
        assert token["sig"]["value"] is not None
        assert not token["sig"].get("degraded", False)

    def test_mint_execute_approved_trusted_identity(self):
        with _env(CLAUDE_HOOK_AGENT_NAME=_TRUSTED_AGENT,
                  HYDRA_OPERATOR_KEY=_CAP_KEY_HEX):
            token = mint_token_for_tool(
                tool_name="xenia-tickets.execute_approved",
                ticket_id=_TICKET_ID,
            )
        assert token is not None
        assert token["actor_id"] == _TRUSTED_AGENT
        assert token["capability"] == "xenia.execute_approved"

    def test_mint_produces_server_verifiable_token(self):
        """Token from mint_token_for_tool passes verify_capability (OBJECT round-trip)."""
        with _env(CLAUDE_HOOK_AGENT_NAME=_TRUSTED_AGENT,
                  HYDRA_OPERATOR_KEY=_CAP_KEY_HEX):
            token = mint_token_for_tool(
                tool_name="xenia-tickets.send_response",
                ticket_id=_TICKET_ID,
            )
        assert token is not None
        # token is a dict (OBJECT) — verify accepts it directly.
        with _env(HYDRA_OPERATOR_KEY=_CAP_KEY_HEX):
            result = verify_capability(
                token,                            # OBJECT, not a JSON string
                expected_capability="xenia.send_response",
                expected_actor_kind="agent",
                expected_resource_id=_TICKET_ID,
                expected_workflow_id=_TICKET_ID,
            )
        assert result["valid"] is True, f"Token (object) should verify: {result['reason']}"
        assert result["actor_id"] == _TRUSTED_AGENT

    def test_token_is_object_not_string(self):
        """mint_token_for_tool returns a dict, never a JSON string."""
        with _env(CLAUDE_HOOK_AGENT_NAME=_TRUSTED_AGENT,
                  HYDRA_OPERATOR_KEY=_CAP_KEY_HEX):
            token = mint_token_for_tool(
                tool_name="xenia-tickets.send_response",
                ticket_id=_TICKET_ID,
            )
        assert token is not None
        assert isinstance(token, dict), (
            f"mint_token_for_tool must return a dict (OBJECT), got {type(token).__name__}"
        )

    def test_different_calls_produce_unique_jtis(self):
        with _env(CLAUDE_HOOK_AGENT_NAME=_TRUSTED_AGENT,
                  HYDRA_OPERATOR_KEY=_CAP_KEY_HEX):
            t1 = mint_token_for_tool(tool_name="xenia-tickets.send_response", ticket_id=_TICKET_ID)
            t2 = mint_token_for_tool(tool_name="xenia-tickets.send_response", ticket_id=_TICKET_ID)
        assert t1 is not None and t2 is not None
        assert t1["jti"] != t2["jti"], "Each mint must produce a unique jti"

    # ---- fail-closed: identity unset -------------------------------------------

    def test_no_agent_name_env_returns_none(self):
        """CLAUDE_HOOK_AGENT_NAME unset -> None (fail-closed)."""
        with _env(CLAUDE_HOOK_AGENT_NAME=None, HYDRA_OPERATOR_KEY=_CAP_KEY_HEX):
            token = mint_token_for_tool(
                tool_name="xenia-tickets.send_response",
                ticket_id=_TICKET_ID,
            )
        assert token is None

    def test_empty_agent_name_env_returns_none(self):
        """CLAUDE_HOOK_AGENT_NAME='' -> None (fail-closed)."""
        with _env(CLAUDE_HOOK_AGENT_NAME="", HYDRA_OPERATOR_KEY=_CAP_KEY_HEX):
            token = mint_token_for_tool(
                tool_name="xenia-tickets.send_response",
                ticket_id=_TICKET_ID,
            )
        assert token is None

    def test_only_whitespace_agent_name_returns_none(self):
        """CLAUDE_HOOK_AGENT_NAME='   ' (whitespace only) -> None (fail-closed)."""
        with _env(CLAUDE_HOOK_AGENT_NAME="   ", HYDRA_OPERATOR_KEY=_CAP_KEY_HEX):
            token = mint_token_for_tool(
                tool_name="xenia-tickets.send_response",
                ticket_id=_TICKET_ID,
            )
        assert token is None

    def test_claude_agent_name_fallback_not_used(self):
        """CLAUDE_HOOK_AGENT_NAME unset, CLAUDE_AGENT_NAME set -> still None.

        There must be NO fallback to CLAUDE_AGENT_NAME.
        """
        with _env(CLAUDE_HOOK_AGENT_NAME=None,
                  CLAUDE_AGENT_NAME="hermes",
                  HYDRA_OPERATOR_KEY=_CAP_KEY_HEX):
            token = mint_token_for_tool(
                tool_name="xenia-tickets.send_response",
                ticket_id=_TICKET_ID,
            )
        assert token is None, (
            "mint_token_for_tool must NOT fall back to CLAUDE_AGENT_NAME. "
            "CLAUDE_HOOK_AGENT_NAME is the only trusted identity source."
        )

    # ---- fail-closed: absent signing key ----------------------------------------

    def test_no_operator_key_returns_none(self):
        """HYDRA_OPERATOR_KEY unset -> mint_token_for_tool returns None (Fix 6).

        Previously this returned a degraded token; after Fix 6 the function is
        fully fail-closed: absent key -> None -> hook exits 2.  The server
        never sees a degraded envelope.
        """
        with _env(CLAUDE_HOOK_AGENT_NAME=_TRUSTED_AGENT, HYDRA_OPERATOR_KEY=None):
            token = mint_token_for_tool(
                tool_name="xenia-tickets.send_response",
                ticket_id=_TICKET_ID,
            )
        assert token is None, (
            "HYDRA_OPERATOR_KEY absent must return None (fail-closed). "
            "Do NOT return a degraded token — that leaks a trust-looking envelope."
        )

    def test_degraded_token_constructed_manually_rejected_by_server(self, tmp_path):
        """A manually-constructed degraded token (sig.value=None) -> server rejects.

        Since mint_token_for_tool now returns None when the key is absent (Fix 6),
        we construct a degraded token by calling mint_caller_capability directly
        (without the key) to verify the server-side rejection path still works.
        """
        _make_ticket(tmp_path)
        body = "Your ticket is resolved."
        clearance = _mint_clearance(body)

        # Construct a degraded token by calling the low-level mint with no key.
        with _env(CLAUDE_HOOK_AGENT_NAME=_TRUSTED_AGENT, HYDRA_OPERATOR_KEY=None):
            degraded_token = mint_caller_capability(
                actor_id=_TRUSTED_AGENT,
                capability="xenia.send_response",
                ticket_id=_TICKET_ID,
            )
        assert degraded_token["sig"]["value"] is None, "Should produce degraded token"
        assert degraded_token["sig"].get("degraded") is True

        result = _call_send(tmp_path, body,
                            cap_token=json.dumps(degraded_token),
                            clearance_token=clearance)
        assert result.get("error", {}).get("code") == "CALLER_CAPABILITY_INVALID"

    # ---- fail-closed: missing top-level ticket_id --------------------------------

    def test_empty_ticket_id_returns_none(self):
        """ticket_id='' -> None."""
        with _env(CLAUDE_HOOK_AGENT_NAME=_TRUSTED_AGENT, HYDRA_OPERATOR_KEY=_CAP_KEY_HEX):
            token = mint_token_for_tool(
                tool_name="xenia-tickets.send_response",
                ticket_id="",
            )
        assert token is None

    def test_whitespace_ticket_id_returns_none(self):
        """ticket_id='   ' (whitespace) -> None."""
        with _env(CLAUDE_HOOK_AGENT_NAME=_TRUSTED_AGENT, HYDRA_OPERATOR_KEY=_CAP_KEY_HEX):
            token = mint_token_for_tool(
                tool_name="xenia-tickets.send_response",
                ticket_id="   ",
            )
        assert token is None

    # ---- fail-closed: unknown tool name ------------------------------------------

    def test_unknown_tool_name_returns_none(self):
        """Unrecognised tool name -> None (cannot map capability)."""
        with _env(CLAUDE_HOOK_AGENT_NAME=_TRUSTED_AGENT, HYDRA_OPERATOR_KEY=_CAP_KEY_HEX):
            token = mint_token_for_tool(
                tool_name="xenia-tickets.unknown_action",
                ticket_id=_TICKET_ID,
            )
        assert token is None

    def test_empty_tool_name_returns_none(self):
        """Empty tool_name -> None."""
        with _env(CLAUDE_HOOK_AGENT_NAME=_TRUSTED_AGENT, HYDRA_OPERATOR_KEY=_CAP_KEY_HEX):
            token = mint_token_for_tool(
                tool_name="",
                ticket_id=_TICKET_ID,
            )
        assert token is None


# ===========================================================================
# Fix 3: token as OBJECT round-trips through server verify
# ===========================================================================

class TestTokenObjectRoundTrip:
    """The minted token dict must be accepted by the server as an OBJECT."""

    def test_token_object_accepted_by_server_send_response(self, tmp_path):
        """Pass token as dict (object) to send_response -> server accepts."""
        _make_ticket(tmp_path)
        body = "Your ticket has been resolved. Thank you."

        with _env(CLAUDE_HOOK_AGENT_NAME="hermes", HYDRA_OPERATOR_KEY=_CAP_KEY_HEX):
            token_dict = mint_token_for_tool(
                tool_name="xenia-tickets.send_response",
                ticket_id=_TICKET_ID,
            )
        assert token_dict is not None
        assert isinstance(token_dict, dict)

        clearance = _mint_clearance(body)
        # Pass the raw dict — server.py's isinstance(capability_token, dict) path.
        with _env(HYDRA_XENIA_ROOT=str(tmp_path),
                  XENIA_CONTEXT_SIGNING_KEY="cafebabe",
                  HYDRA_OPERATOR_KEY=_CAP_KEY_HEX):
            handlers = _tool_handlers()
            result = handlers["xenia-tickets.send_response"]({
                "ticket_id": _TICKET_ID,
                "body": body,
                "actor": "hermes",
                "capability_token": token_dict,   # OBJECT — not JSON string
                "clearance_token": clearance,
            })
        assert "error" not in result, f"Token as object should be accepted: {result}"
        assert result.get("ok") is True

    def test_token_object_accepted_by_server_execute_approved(self, tmp_path):
        """Pass token as dict (object) to execute_approved -> server accepts."""
        _make_ticket(tmp_path)
        _make_approval(tmp_path)

        with _env(CLAUDE_HOOK_AGENT_NAME="hermes", HYDRA_OPERATOR_KEY=_CAP_KEY_HEX):
            token_dict = mint_token_for_tool(
                tool_name="xenia-tickets.execute_approved",
                ticket_id=_TICKET_ID,
            )
        assert token_dict is not None

        with _env(HYDRA_XENIA_ROOT=str(tmp_path), HYDRA_OPERATOR_KEY=_CAP_KEY_HEX):
            handlers = _tool_handlers()
            result = handlers["xenia-tickets.execute_approved"]({
                "ticket_id": _TICKET_ID,
                "action": "refund",
                "scope": "billing",
                "approval_id": f"APPROVAL-{_TICKET_ID}-001",
                "actor": "hermes",
                "capability_token": token_dict,   # OBJECT
            })
        assert "error" not in result, f"Token as object should be accepted: {result}"
        assert result.get("ok") is True

    def test_json_serialised_token_also_accepted(self, tmp_path):
        """JSON-string token is also accepted by server (compatibility)."""
        _make_ticket(tmp_path)
        body = "Your ticket is resolved."

        with _env(CLAUDE_HOOK_AGENT_NAME="hermes", HYDRA_OPERATOR_KEY=_CAP_KEY_HEX):
            token_dict = mint_token_for_tool(
                tool_name="xenia-tickets.send_response",
                ticket_id=_TICKET_ID,
            )
        assert token_dict is not None
        clearance = _mint_clearance(body)

        result = _call_send(tmp_path, body,
                            cap_token=json.dumps(token_dict),  # JSON string
                            clearance_token=clearance)
        assert "error" not in result, f"JSON-string token also accepted: {result}"
        assert result.get("ok") is True


# ===========================================================================
# Fix 1+4: forged token overwrite — CLAUDE_HOOK_AGENT_NAME governs
# ===========================================================================

class TestForgedTokenOverwritten:
    """Hook's trusted mint overwrites any agent-supplied capability_token."""

    def test_trusted_mint_overwrites_forged_token_server_sees_env_actor(self, tmp_path):
        """Simulates hook: forged rogue token in args replaced by hermes env token."""
        _make_ticket(tmp_path)
        body = "Your ticket is resolved."
        clearance = _mint_clearance(body)

        # Produce a forged token (actor=rogue).
        with _env(HYDRA_OPERATOR_KEY=_CAP_KEY_HEX):
            forged = mint_caller_capability(
                actor_id="rogue",
                capability="xenia.send_response",
                ticket_id=_TICKET_ID,
            )

        # Trusted hook mints with CLAUDE_HOOK_AGENT_NAME=hermes.
        with _env(CLAUDE_HOOK_AGENT_NAME="hermes", HYDRA_OPERATOR_KEY=_CAP_KEY_HEX):
            trusted = mint_token_for_tool(
                tool_name="xenia-tickets.send_response",
                ticket_id=_TICKET_ID,
            )
        assert trusted is not None
        # Verify trusted actor IS hermes (from env), NOT rogue.
        assert trusted["actor_id"] == "hermes"
        assert forged["actor_id"] == "rogue"

        # Hook overwrites: send trusted token (forged discarded).
        result = _call_send(tmp_path, body,
                            cap_token=json.dumps(trusted),
                            clearance_token=clearance)
        assert "error" not in result, f"Trusted hermes token must pass: {result}"

        # Server records "hermes", not "rogue".
        ticket_data = json.loads(
            (tmp_path / "hearth" / "tasks" / f"TICKET-{_TICKET_ID}.json")
            .read_text(encoding="utf-8")
        )
        history = ticket_data["history"]
        entries = [e for e in history if e.get("kind") == "response"]
        assert len(entries) == 1
        assert entries[0]["actor"] == "hermes", (
            f"Actor must be 'hermes' (trusted env), not 'rogue' (forged). "
            f"Got: {entries[0]['actor']!r}"
        )

    def test_forged_non_allowlisted_token_rejected_directly(self, tmp_path):
        """Forged token (actor=rogue) reaching server directly -> FORBIDDEN_ACTOR."""
        _make_ticket(tmp_path)
        body = "Forged body."
        clearance = _mint_clearance(body)

        with _env(HYDRA_OPERATOR_KEY=_CAP_KEY_HEX):
            forged = mint_caller_capability(
                actor_id="rogue",
                capability="xenia.send_response",
                ticket_id=_TICKET_ID,
            )
        result = _call_send(tmp_path, body,
                            cap_token=json.dumps(forged),
                            clearance_token=clearance)
        assert result.get("error", {}).get("code") == "FORBIDDEN_ACTOR"


# ===========================================================================
# e2e: trusted mint -> server accepts/rejects
# ===========================================================================

class TestE2ETrustedMintToServer:

    def test_trusted_hermes_send_response_accepted(self, tmp_path):
        _make_ticket(tmp_path)
        body = "Your ticket has been resolved. Thank you."
        with _env(CLAUDE_HOOK_AGENT_NAME="hermes", HYDRA_OPERATOR_KEY=_CAP_KEY_HEX):
            token = mint_token_for_tool(tool_name="xenia-tickets.send_response",
                                        ticket_id=_TICKET_ID)
        assert token is not None
        clearance = _mint_clearance(body)
        result = _call_send(tmp_path, body,
                            cap_token=json.dumps(token), clearance_token=clearance)
        assert "error" not in result, f"send_response should succeed: {result}"
        assert result.get("ok") is True

    def test_trusted_escalation_handoff_send_response_accepted(self, tmp_path):
        _make_ticket(tmp_path)
        body = "Your ticket has been escalated and resolved."
        with _env(CLAUDE_HOOK_AGENT_NAME="escalation-handoff", HYDRA_OPERATOR_KEY=_CAP_KEY_HEX):
            token = mint_token_for_tool(tool_name="xenia-tickets.send_response",
                                        ticket_id=_TICKET_ID)
        assert token is not None
        clearance = _mint_clearance(body)
        result = _call_send(tmp_path, body,
                            cap_token=json.dumps(token), clearance_token=clearance)
        assert "error" not in result, f"escalation-handoff should succeed: {result}"
        assert result.get("ok") is True

    def test_trusted_hermes_execute_approved_accepted(self, tmp_path):
        _make_ticket(tmp_path)
        _make_approval(tmp_path)
        with _env(CLAUDE_HOOK_AGENT_NAME="hermes", HYDRA_OPERATOR_KEY=_CAP_KEY_HEX):
            token = mint_token_for_tool(tool_name="xenia-tickets.execute_approved",
                                        ticket_id=_TICKET_ID)
        assert token is not None
        result = _call_exec(tmp_path, cap_token=json.dumps(token))
        assert "error" not in result, f"execute_approved should succeed: {result}"
        assert result.get("ok") is True

    def test_non_allowlisted_trusted_identity_rejected(self, tmp_path):
        """CLAUDE_HOOK_AGENT_NAME=iris (not on allow-list) -> FORBIDDEN_ACTOR."""
        _make_ticket(tmp_path)
        body = "Trying from non-allowed agent."
        clearance = _mint_clearance(body)
        with _env(CLAUDE_HOOK_AGENT_NAME="iris", HYDRA_OPERATOR_KEY=_CAP_KEY_HEX):
            token = mint_token_for_tool(tool_name="xenia-tickets.send_response",
                                        ticket_id=_TICKET_ID)
        assert token is not None, "mint succeeds even for non-allow-listed agent"
        result = _call_send(tmp_path, body,
                            cap_token=json.dumps(token), clearance_token=clearance)
        assert result.get("error", {}).get("code") == "FORBIDDEN_ACTOR"

    def test_absent_capability_token_blocked(self, tmp_path):
        _make_ticket(tmp_path)
        body = "Your ticket is resolved."
        clearance = _mint_clearance(body)
        result = _call_send(tmp_path, body, clearance_token=clearance)
        assert result.get("error", {}).get("code") == "CALLER_CAPABILITY_INVALID"

    def test_verified_actor_equals_env_identity_not_arg(self, tmp_path):
        """History actor == CLAUDE_HOOK_AGENT_NAME, not any self-reported arg."""
        _make_ticket(tmp_path)
        body = "Resolved via trusted dispatch."
        clearance = _mint_clearance(body)
        with _env(CLAUDE_HOOK_AGENT_NAME="hermes", HYDRA_OPERATOR_KEY=_CAP_KEY_HEX):
            token = mint_token_for_tool(tool_name="xenia-tickets.send_response",
                                        ticket_id=_TICKET_ID)
        assert token is not None
        with _env(HYDRA_XENIA_ROOT=str(tmp_path),
                  XENIA_CONTEXT_SIGNING_KEY="cafebabe",
                  HYDRA_OPERATOR_KEY=_CAP_KEY_HEX):
            handlers = _tool_handlers()
            result = handlers["xenia-tickets.send_response"]({
                "ticket_id": _TICKET_ID,
                "body": body,
                "actor": "escalation-handoff",   # self-reported — must be ignored
                "capability_token": json.dumps(token),
                "clearance_token": clearance,
            })
        assert "error" not in result, f"Should succeed: {result}"
        ticket_data = json.loads(
            (tmp_path / "hearth" / "tasks" / f"TICKET-{_TICKET_ID}.json")
            .read_text(encoding="utf-8")
        )
        entries = [e for e in ticket_data["history"] if e.get("kind") == "response"]
        assert entries[0]["actor"] == "hermes", (
            "Actor must be 'hermes' (env identity), not 'escalation-handoff' (arg). "
            f"Got: {entries[0]['actor']!r}"
        )


# ===========================================================================
# Fix 8: key never in any output
# ===========================================================================

class TestKeyNeverInOutput:

    def test_key_not_in_token_dict(self):
        """HYDRA_OPERATOR_KEY must not appear in the minted token."""
        with _env(CLAUDE_HOOK_AGENT_NAME=_TRUSTED_AGENT,
                  HYDRA_OPERATOR_KEY=_CAP_KEY_HEX):
            token = mint_token_for_tool(
                tool_name="xenia-tickets.send_response",
                ticket_id=_TICKET_ID,
            )
        assert token is not None
        token_str = json.dumps(token)
        assert _CAP_KEY_HEX.lower() not in token_str.lower(), (
            "HYDRA_OPERATOR_KEY must never appear in token output"
        )

    def test_key_id_is_not_key_material(self):
        """sig.key_id is an identifier, not the key material."""
        with _env(CLAUDE_HOOK_AGENT_NAME=_TRUSTED_AGENT,
                  HYDRA_OPERATOR_KEY=_CAP_KEY_HEX):
            token = mint_token_for_tool(
                tool_name="xenia-tickets.send_response",
                ticket_id=_TICKET_ID,
            )
        assert token is not None
        key_id = token.get("sig", {}).get("key_id", "")
        assert key_id != _CAP_KEY_HEX, "sig.key_id must not be the key material"


# ===========================================================================
# Fix A: exact tool name allow-list
#
# Only canonical xenia-tickets server forms are accepted.
# Bare names and foreign-prefix tools must be REJECTED (return None).
# ===========================================================================

class TestExactToolAllowList:
    """mint_token_for_tool only accepts the xenia-tickets server canonical forms.

    Fix A security invariant: a foreign tool whose name merely ends in
    send_response/execute_approved must NOT receive a token.
    """

    # --- accepted: canonical xenia-tickets forms ----------------------------

    @pytest.mark.parametrize("tool_name,expected_cap", [
        ("xenia-tickets.send_response",              "xenia.send_response"),
        ("xenia-tickets.execute_approved",           "xenia.execute_approved"),
        ("xenia_tickets.send_response",              "xenia.send_response"),
        ("xenia_tickets.execute_approved",           "xenia.execute_approved"),
        ("mcp__xenia-tickets__send_response",        "xenia.send_response"),
        ("mcp__xenia-tickets__execute_approved",     "xenia.execute_approved"),
        ("mcp__xenia_tickets__send_response",        "xenia.send_response"),
        ("mcp__xenia_tickets__execute_approved",     "xenia.execute_approved"),
    ])
    def test_canonical_xenia_tools_accepted(self, tool_name, expected_cap):
        """Canonical xenia-tickets tool names must be accepted."""
        with _env(CLAUDE_HOOK_AGENT_NAME=_TRUSTED_AGENT,
                  HYDRA_OPERATOR_KEY=_CAP_KEY_HEX):
            token = mint_token_for_tool(tool_name=tool_name, ticket_id=_TICKET_ID)
        assert token is not None, f"canonical xenia tool {tool_name!r} must be accepted"
        assert token["capability"] == expected_cap

    # --- rejected: bare names (no server segment) ---------------------------

    @pytest.mark.parametrize("tool_name", [
        "send_response",
        "execute_approved",
    ])
    def test_bare_tool_names_rejected(self, tool_name):
        """Bare names without xenia-tickets server segment must be REJECTED (Fix A).

        Without a server segment the caller cannot be verified as the Xenia
        ticket server; accepting bare names would allow any tool with a
        matching suffix to obtain a Xenia capability token.
        """
        with _env(CLAUDE_HOOK_AGENT_NAME=_TRUSTED_AGENT,
                  HYDRA_OPERATOR_KEY=_CAP_KEY_HEX):
            token = mint_token_for_tool(tool_name=tool_name, ticket_id=_TICKET_ID)
        assert token is None, (
            f"Bare tool name {tool_name!r} must be REJECTED. "
            "Only canonical xenia-tickets server forms are accepted."
        )

    # --- rejected: foreign server prefix + matching suffix ------------------

    @pytest.mark.parametrize("tool_name", [
        "mcp__evil__send_response",
        "mcp__evil__x_send_response",
        "mcp__evil__execute_approved",
        "mcp__attacker__xenia-tickets_send_response",
        "some-other-server.send_response",
        "xenia-billing.send_response",
        "xenia-tickets-fake.send_response",
    ])
    def test_foreign_tool_suffix_match_rejected(self, tool_name):
        """Foreign tools ending in send_response/execute_approved must be REJECTED.

        Fix A: the tool name must identify the xenia-tickets server segment,
        not merely end with the right action name.
        """
        with _env(CLAUDE_HOOK_AGENT_NAME=_TRUSTED_AGENT,
                  HYDRA_OPERATOR_KEY=_CAP_KEY_HEX):
            token = mint_token_for_tool(tool_name=tool_name, ticket_id=_TICKET_ID)
        assert token is None, (
            f"Foreign tool {tool_name!r} must be REJECTED (Fix A). "
            "A token must NOT be minted for a non-xenia-tickets tool."
        )

    def test_capability_map_contains_only_xenia_tickets_server_keys(self):
        """_TOOL_TO_CAPABILITY must not contain bare names or foreign prefixes."""
        import mcp_servers.xenia_tickets.mint_for_tool as m
        for key in m._TOOL_TO_CAPABILITY:
            # Every key must contain the xenia-tickets (or xenia_tickets) server segment.
            assert ("xenia-tickets" in key or "xenia_tickets" in key), (
                f"_TOOL_TO_CAPABILITY key {key!r} does not contain the "
                "xenia-tickets server segment. Bare names are not allowed (Fix A)."
            )
            # No bare single-word keys.
            assert key not in ("send_response", "execute_approved"), (
                f"Bare key {key!r} must not be in _TOOL_TO_CAPABILITY (Fix A)."
            )


# ===========================================================================
# Hook logic tests: simulate hook-side ticket_id extraction and overwrite
# (Python-level tests of the logic embedded in .ps1/.sh)
# ===========================================================================

class TestHookLogicSimulation:
    """Test the hook-side logic in Python to verify correctness without
    spawning pwsh/sh subprocesses (which are fragile in CI)."""

    def _simulate_hook_overwrite(self, payload_dict: dict, env_agent: str) -> dict | None:
        """Simulate the hook: extract top-level ticket_id, mint, overwrite.

        Returns the updated payload dict with capability_token set to the
        trusted token OBJECT, or None if any step fails.
        """
        # Fix 4: top-level ticket_id only.
        ticket_id = payload_dict.get("ticket_id", "")
        if not isinstance(ticket_id, str) or not ticket_id.strip():
            return None

        # Fix 1: identity from env only.
        with _env(CLAUDE_HOOK_AGENT_NAME=env_agent, HYDRA_OPERATOR_KEY=_CAP_KEY_HEX):
            # Determine capability from payload heuristic (send_response has "body").
            if "body" in payload_dict:
                tool_name = "xenia-tickets.send_response"
            else:
                tool_name = "xenia-tickets.execute_approved"
            token = mint_token_for_tool(tool_name=tool_name, ticket_id=ticket_id.strip())

        if token is None:
            return None

        # Fix 3: token is an OBJECT; OVERWRITE any existing capability_token.
        result = dict(payload_dict)
        result["capability_token"] = token    # OBJECT, not string
        return result

    def test_top_level_ticket_id_used_not_body_text(self):
        """ticket_id extracted from top-level field, not from body text."""
        payload = {
            "ticket_id": _TICKET_ID,
            "body": "Please refund TICKET-999999 as well",  # TICKET-* in body
        }
        updated = self._simulate_hook_overwrite(payload, "hermes")
        assert updated is not None
        # Token must be bound to _TICKET_ID (top-level), not TICKET-999999 (body).
        assert updated["capability_token"]["resource_id"] == _TICKET_ID

    def test_missing_top_level_ticket_id_blocks(self):
        """No top-level ticket_id -> hook returns None (blocks)."""
        payload = {
            "body": "Handle TICKET-000042 refund please",  # TICKET-* in body only
        }
        updated = self._simulate_hook_overwrite(payload, "hermes")
        assert updated is None, (
            "Hook must block when top-level ticket_id is absent, "
            "even if TICKET-* appears in body text."
        )

    def test_empty_top_level_ticket_id_blocks(self):
        """top-level ticket_id='' -> hook returns None."""
        payload = {"ticket_id": "", "body": "some text"}
        updated = self._simulate_hook_overwrite(payload, "hermes")
        assert updated is None

    def test_capability_token_is_object_in_updated_input(self):
        """capability_token in updatedInput is a dict (OBJECT), not a string."""
        payload = {"ticket_id": _TICKET_ID, "body": "resolved"}
        updated = self._simulate_hook_overwrite(payload, "hermes")
        assert updated is not None
        cap = updated["capability_token"]
        assert isinstance(cap, dict), (
            f"capability_token must be a dict (OBJECT), got {type(cap).__name__}"
        )

    def test_overwrite_replaces_forged_token(self):
        """Pre-existing (forged) capability_token in payload is replaced."""
        with _env(HYDRA_OPERATOR_KEY=_CAP_KEY_HEX):
            forged = mint_caller_capability(
                actor_id="rogue",
                capability="xenia.send_response",
                ticket_id=_TICKET_ID,
            )
        payload = {
            "ticket_id": _TICKET_ID,
            "body": "hello",
            "capability_token": forged,  # forged token present in args
        }
        updated = self._simulate_hook_overwrite(payload, "hermes")
        assert updated is not None
        cap = updated["capability_token"]
        assert isinstance(cap, dict)
        assert cap["actor_id"] == "hermes", (
            f"capability_token must be the trusted mint (hermes), not forged (rogue). "
            f"Got actor_id={cap['actor_id']!r}"
        )

    def test_e2e_object_token_in_payload_accepted_by_server(self, tmp_path):
        """Object token injected via hook path -> server accepts."""
        _make_ticket(tmp_path)
        body = "Your issue is resolved."
        clearance = _mint_clearance(body)

        payload = {"ticket_id": _TICKET_ID, "body": body}
        updated = self._simulate_hook_overwrite(payload, "hermes")
        assert updated is not None
        token_obj = updated["capability_token"]
        assert isinstance(token_obj, dict)

        with _env(HYDRA_XENIA_ROOT=str(tmp_path),
                  XENIA_CONTEXT_SIGNING_KEY="cafebabe",
                  HYDRA_OPERATOR_KEY=_CAP_KEY_HEX):
            handlers = _tool_handlers()
            result = handlers["xenia-tickets.send_response"]({
                "ticket_id": _TICKET_ID,
                "body": body,
                "actor": "hermes",
                "capability_token": token_obj,   # OBJECT from hook
                "clearance_token": clearance,
            })
        assert "error" not in result, f"Object token from hook must be accepted: {result}"
        assert result.get("ok") is True


# ===========================================================================
# Fix 8: no raw caller-supplied values in log output
# ===========================================================================

class TestNoRawValuesInLogs:
    """mint_token_for_tool must not echo any raw caller-supplied values in logs.

    An attacker cannot exfiltrate data via error log echo (e.g. a tool_name
    containing key material, or an agent name containing PII).

    Verified invariants:
      - UNKNOWN_TOOL log line does NOT contain the raw tool_name string.
      - KEY_ABSENT log line does NOT contain the key value.
      - IDENTITY_UNSET log line does NOT contain any env value.
    """

    def test_unknown_tool_log_does_not_contain_raw_tool_name(self, capsys):
        """UNKNOWN_TOOL error must not echo the tool_name argument."""
        evil_tool = "xenia-tickets.EVIL-payload-with-secret-data-abc123"
        with _env(CLAUDE_HOOK_AGENT_NAME=_TRUSTED_AGENT,
                  HYDRA_OPERATOR_KEY=_CAP_KEY_HEX):
            token = mint_token_for_tool(
                tool_name=evil_tool,
                ticket_id=_TICKET_ID,
            )
        assert token is None, "Unknown tool must return None"

        captured = capsys.readouterr()
        # The raw evil tool name must NOT appear in stderr output.
        assert evil_tool not in captured.err, (
            "UNKNOWN_TOOL error log must NOT contain the raw tool_name value. "
            f"Found {evil_tool!r} in: {captured.err!r}"
        )
        assert "UNKNOWN_TOOL" in captured.err, "Error code must be present"

    def test_key_absent_log_does_not_contain_key_value(self, capsys):
        """KEY_ABSENT error must not echo any key material."""
        with _env(CLAUDE_HOOK_AGENT_NAME=_TRUSTED_AGENT,
                  HYDRA_OPERATOR_KEY=None):
            token = mint_token_for_tool(
                tool_name="xenia-tickets.send_response",
                ticket_id=_TICKET_ID,
            )
        assert token is None

        captured = capsys.readouterr()
        assert "KEY_ABSENT" in captured.err
        # No key value in log (key is absent, but verify the code path too).
        assert _CAP_KEY_HEX.lower() not in captured.err.lower()

    def test_identity_unset_log_does_not_echo_agent_name(self, capsys):
        """IDENTITY_UNSET error must not echo any agent name."""
        with _env(CLAUDE_HOOK_AGENT_NAME=None,
                  HYDRA_OPERATOR_KEY=_CAP_KEY_HEX):
            token = mint_token_for_tool(
                tool_name="xenia-tickets.send_response",
                ticket_id=_TICKET_ID,
            )
        assert token is None

        captured = capsys.readouterr()
        assert "IDENTITY_UNSET" in captured.err
        # No agent name echoed (it's unset, but the code path must not interpolate).
        assert "CLAUDE_HOOK_AGENT_NAME" not in captured.err or \
               "unset" in captured.err, (
            "Log may mention the env var NAME but must not echo its VALUE"
        )


# ===========================================================================
# Fix 3: non-object token value -> hook-level block
# (Python simulation of the type assertion in .ps1/.sh)
# ===========================================================================

class TestTokenObjectTypeAssertion:
    """The hook asserts that the parsed mint output is a JSON object (dict).

    If mint_for_tool returns something that is not a dict (e.g. a string,
    list, or null due to a bug), the hook must block rather than inject it.
    """

    def _simulate_hook_type_assertion(self, raw_token_output: str) -> dict | None:
        """Simulate the hook-side type assertion (Fix 3) in Python.

        Parses raw_token_output as JSON, asserts it is a dict.
        Returns the parsed token dict, or None if the assertion fails.
        """
        try:
            token_obj = json.loads(raw_token_output)
        except (json.JSONDecodeError, ValueError):
            return None
        if not isinstance(token_obj, dict):
            return None
        return token_obj

    def test_dict_token_passes_type_assertion(self):
        """A JSON object string is parsed to dict and accepted."""
        with _env(CLAUDE_HOOK_AGENT_NAME=_TRUSTED_AGENT,
                  HYDRA_OPERATOR_KEY=_CAP_KEY_HEX):
            token = mint_token_for_tool(
                tool_name="xenia-tickets.send_response",
                ticket_id=_TICKET_ID,
            )
        assert token is not None
        raw_output = json.dumps(token)
        result = self._simulate_hook_type_assertion(raw_output)
        assert result is not None
        assert isinstance(result, dict)

    def test_json_string_instead_of_object_blocked(self):
        """If mint stdout is a JSON string (not object), hook blocks."""
        # Malformed: a JSON string instead of a JSON object.
        raw_output = json.dumps("this is a string not an object")
        result = self._simulate_hook_type_assertion(raw_output)
        assert result is None, "JSON string must fail type assertion (not a dict)"

    def test_json_array_instead_of_object_blocked(self):
        """If mint stdout is a JSON array, hook blocks."""
        raw_output = json.dumps(["actor_id", "hermes"])
        result = self._simulate_hook_type_assertion(raw_output)
        assert result is None, "JSON array must fail type assertion (not a dict)"

    def test_json_null_blocked(self):
        """If mint stdout is JSON null, hook blocks."""
        raw_output = "null"
        result = self._simulate_hook_type_assertion(raw_output)
        assert result is None, "JSON null must fail type assertion"

    def test_invalid_json_blocked(self):
        """If mint stdout is not valid JSON, hook blocks."""
        raw_output = "not json at all"
        result = self._simulate_hook_type_assertion(raw_output)
        assert result is None, "Invalid JSON must fail type assertion"

    def test_empty_stdout_blocked(self):
        """If mint stdout is empty, hook blocks."""
        result = self._simulate_hook_type_assertion("")
        assert result is None, "Empty stdout must fail type assertion"
