"""WS-AUTH caller-capability enforcement tests for Xenia ticket MCP server.

Covers (per spec):
  - send_response/execute_approved WITHOUT a caller token -> CALLER_CAPABILITY_INVALID
  - WITH a valid minted caller token (allow-listed agent, correct binding) -> existing checks proceed
  - Token for a NON-allow-listed agent -> CALLER_CAPABILITY_INVALID then FORBIDDEN_ACTOR
  - Replayed caller token (same jti) -> CALLER_CAPABILITY_REPLAY
  - Token with wrong capability binding -> CALLER_CAPABILITY_INVALID
  - Token with wrong ticket_id binding -> CALLER_CAPABILITY_INVALID
  - verified-actor-replaces-self-reported: capability token actor != arg actor -> VERIFIED one governs
  - Degraded capability token (no HYDRA_OPERATOR_KEY) -> CALLER_CAPABILITY_INVALID (fail-closed)
  - mint_caller_capability seam: produces valid token for legitimate callers

All tests are pure-Python — no subprocess, no daemon.
"""
from __future__ import annotations

import json
import os
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
HYDRA_ROOT = Path(__file__).resolve().parents[4]
# Portable: HYDRA_XENIA_ROOT env override -> Xenia checked out beside the Hydra repo.
XENIA_ROOT = Path(os.environ.get("HYDRA_XENIA_ROOT", str(HYDRA_ROOT.parent / "Xenia")))
sys.path.insert(0, str(HYDRA_ROOT))
sys.path.insert(0, str(XENIA_ROOT))

from mcp_servers.xenia_tickets.clearance import mint_clearance_token
from mcp_servers.xenia_tickets.server import (
    _tool_handlers,
    mint_caller_capability,
)
from hydra_core.auth.capability import mint_capability


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_CAP_KEY_HEX = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
_TICKET_ID = "000001"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@contextmanager
def _env(**env: str | None):
    """Temporarily patch os.environ."""
    old = {k: os.environ.get(k) for k in env}
    for k, v in env.items():
        if v:
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
        "subject": "WS-AUTH test ticket",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "sla": {"first_response_due": datetime.now(timezone.utc).isoformat(), "breached": False},
        "history": [],
        "recommendations": [],
    }
    (tasks / f"TICKET-{ticket_id}.json").write_text(json.dumps(ticket), encoding="utf-8")


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
        f"issued_by: hermes\n"
        f"expires_at: {expires_at}\n"
    )
    (approvals_dir / f"APPROVAL-{ticket_id}-{seq}.yaml").write_text(content, encoding="utf-8")


def _mint_clearance(body: str, key: str = "cafebabe") -> str:
    """Mint a clearance token and JSON-encode it."""
    with _env(XENIA_CONTEXT_SIGNING_KEY=key):
        token_dict = mint_clearance_token(body, None)
    assert token_dict is not None
    return json.dumps(token_dict)


def _mint_cap(
    actor_id: str = "hermes",
    capability: str = "xenia.send_response",
    ticket_id: str = _TICKET_ID,
    key: str = _CAP_KEY_HEX,
    jti: str | None = None,
) -> str:
    """Mint a caller-capability token and JSON-encode it."""
    with _env(HYDRA_OPERATOR_KEY=key):
        token_dict = mint_caller_capability(
            actor_id=actor_id,
            capability=capability,
            ticket_id=ticket_id,
            jti=jti,
        )
    return json.dumps(token_dict)


def _call_send(tmp_path: Path, body: str = "Your ticket is resolved.",
               actor: str = "hermes",
               cap_token=None, clearance_token=None,
               clearance_key: str = "cafebabe") -> dict:
    with _env(HYDRA_XENIA_ROOT=str(tmp_path), XENIA_CONTEXT_SIGNING_KEY=clearance_key,
              HYDRA_OPERATOR_KEY=_CAP_KEY_HEX):
        handlers = _tool_handlers()
        args: dict = {"ticket_id": _TICKET_ID, "body": body, "actor": actor}
        if cap_token is not None:
            args["capability_token"] = cap_token
        if clearance_token is not None:
            args["clearance_token"] = clearance_token
        return handlers["xenia-tickets.send_response"](args)


def _call_exec(tmp_path: Path, actor: str = "hermes",
               cap_token=None, approval_id: str = f"APPROVAL-{_TICKET_ID}-001") -> dict:
    with _env(HYDRA_XENIA_ROOT=str(tmp_path), HYDRA_OPERATOR_KEY=_CAP_KEY_HEX):
        handlers = _tool_handlers()
        args: dict = {
            "ticket_id": _TICKET_ID,
            "action": "refund",
            "scope": "billing",
            "approval_id": approval_id,
            "actor": actor,
        }
        if cap_token is not None:
            args["capability_token"] = cap_token
        return handlers["xenia-tickets.execute_approved"](args)


# ===========================================================================
# Tests: missing / invalid capability token
# ===========================================================================

class TestMissingCapabilityToken:
    """No capability_token arg -> CALLER_CAPABILITY_INVALID (fail-closed)."""

    def test_send_response_no_cap_token_blocked(self, tmp_path):
        _make_ticket(tmp_path)
        body = "Your ticket is resolved."
        clearance = _mint_clearance(body)
        result = _call_send(tmp_path, body, clearance_token=clearance)
        assert result.get("error", {}).get("code") == "CALLER_CAPABILITY_INVALID"

    def test_execute_approved_no_cap_token_blocked(self, tmp_path):
        _make_ticket(tmp_path)
        _make_approval(tmp_path)
        result = _call_exec(tmp_path)
        assert result.get("error", {}).get("code") == "CALLER_CAPABILITY_INVALID"

    def test_send_response_invalid_json_cap_token_blocked(self, tmp_path):
        """Garbage JSON string -> CALLER_CAPABILITY_INVALID."""
        _make_ticket(tmp_path)
        body = "Your ticket is resolved."
        clearance = _mint_clearance(body)
        result = _call_send(tmp_path, body, cap_token="not-valid-json{{{",
                            clearance_token=clearance)
        assert result.get("error", {}).get("code") == "CALLER_CAPABILITY_INVALID"

    def test_execute_approved_invalid_json_cap_token_blocked(self, tmp_path):
        _make_ticket(tmp_path)
        _make_approval(tmp_path)
        result = _call_exec(tmp_path, cap_token="not-valid-json{{{")
        assert result.get("error", {}).get("code") == "CALLER_CAPABILITY_INVALID"


# ===========================================================================
# Tests: valid minted caller token -> existing checks proceed
# ===========================================================================

class TestValidCallerToken:
    """A valid minted caller token + allow-listed agent + correct binding -> proceeds."""

    def test_send_response_with_valid_cap_token_succeeds(self, tmp_path):
        """Valid cap token + valid clearance + clean body -> ok."""
        _make_ticket(tmp_path)
        body = "Your ticket has been resolved. Thank you for your patience."
        cap_token = _mint_cap(actor_id="hermes")
        clearance = _mint_clearance(body)
        result = _call_send(tmp_path, body, cap_token=cap_token, clearance_token=clearance)
        assert "error" not in result, result
        assert result.get("ok") is True

    def test_execute_approved_with_valid_cap_token_succeeds(self, tmp_path):
        """Valid cap token (execute_approved) + valid approval artifact -> ok."""
        _make_ticket(tmp_path)
        _make_approval(tmp_path)
        cap_token = _mint_cap(actor_id="hermes", capability="xenia.execute_approved")
        result = _call_exec(tmp_path, actor="hermes", cap_token=cap_token)
        assert "error" not in result, result
        assert result.get("ok") is True

    def test_send_response_escalation_handoff_succeeds(self, tmp_path):
        """escalation-handoff is also on the allow-list."""
        _make_ticket(tmp_path)
        body = "Your ticket has been escalated and resolved."
        cap_token = _mint_cap(actor_id="escalation-handoff")
        clearance = _mint_clearance(body)
        result = _call_send(tmp_path, body, actor="escalation-handoff",
                            cap_token=cap_token, clearance_token=clearance)
        assert "error" not in result, result
        assert result.get("ok") is True


# ===========================================================================
# Tests: non-allow-listed agent -> FORBIDDEN_ACTOR
# ===========================================================================

class TestNonAllowListedAgent:
    """Capability token for a non-allow-listed agent -> FORBIDDEN_ACTOR."""

    def test_send_response_non_allowlisted_forbidden(self, tmp_path):
        _make_ticket(tmp_path)
        body = "Your ticket is resolved."
        clearance = _mint_clearance(body)
        cap_token = _mint_cap(actor_id="iris")  # iris not on allow-list
        result = _call_send(tmp_path, body, actor="iris",
                            cap_token=cap_token, clearance_token=clearance)
        assert result.get("error", {}).get("code") == "FORBIDDEN_ACTOR"

    def test_execute_approved_non_allowlisted_forbidden(self, tmp_path):
        _make_ticket(tmp_path)
        _make_approval(tmp_path)
        cap_token = _mint_cap(actor_id="plutus", capability="xenia.execute_approved")
        result = _call_exec(tmp_path, actor="plutus", cap_token=cap_token)
        assert result.get("error", {}).get("code") == "FORBIDDEN_ACTOR"

    def test_send_response_human_actor_forbidden(self, tmp_path):
        """'human' was removed from the allow-list (fix #6)."""
        _make_ticket(tmp_path)
        body = "Your ticket is resolved."
        clearance = _mint_clearance(body)
        cap_token = _mint_cap(actor_id="human")
        result = _call_send(tmp_path, body, actor="human",
                            cap_token=cap_token, clearance_token=clearance)
        assert result.get("error", {}).get("code") == "FORBIDDEN_ACTOR"


# ===========================================================================
# Tests: single-use (JTI replay)
# ===========================================================================

class TestCallerTokenSingleUse:
    """Same jti used twice -> CALLER_CAPABILITY_REPLAY on second use."""

    def test_send_response_replay_blocked(self, tmp_path):
        """First use succeeds; second use of same jti -> CALLER_CAPABILITY_REPLAY."""
        _make_ticket(tmp_path)
        fixed_jti = "replay-test-jti-0001"
        body = "Your ticket is resolved."
        cap_token = _mint_cap(actor_id="hermes", jti=fixed_jti)
        clearance1 = _mint_clearance(body)

        # First use — should succeed.
        result1 = _call_send(tmp_path, body, cap_token=cap_token, clearance_token=clearance1)
        assert "error" not in result1, f"First use should succeed: {result1}"

        # Second use — same jti, same ticket -> CALLER_CAPABILITY_REPLAY.
        clearance2 = _mint_clearance(body)  # fresh clearance but same jti
        result2 = _call_send(tmp_path, body, cap_token=cap_token, clearance_token=clearance2)
        assert result2.get("error", {}).get("code") == "CALLER_CAPABILITY_REPLAY", result2

    def test_execute_approved_replay_blocked(self, tmp_path):
        """execute_approved: same jti used twice -> CALLER_CAPABILITY_REPLAY on second."""
        _make_ticket(tmp_path)
        _make_approval(tmp_path)
        fixed_jti = "exec-replay-jti-0001"
        cap_token = _mint_cap(actor_id="hermes", capability="xenia.execute_approved",
                              jti=fixed_jti)

        # First use — should succeed.
        result1 = _call_exec(tmp_path, actor="hermes", cap_token=cap_token)
        assert "error" not in result1, f"First use should succeed: {result1}"

        # Second use — different approval artifact needed (first was consumed), but
        # the JTI replay gate fires before the approval check.
        _make_approval(tmp_path, seq="002")
        with _env(HYDRA_XENIA_ROOT=str(tmp_path), HYDRA_OPERATOR_KEY=_CAP_KEY_HEX):
            handlers = _tool_handlers()
            result2 = handlers["xenia-tickets.execute_approved"]({
                "ticket_id": _TICKET_ID,
                "action": "refund",
                "scope": "billing",
                "approval_id": f"APPROVAL-{_TICKET_ID}-002",
                "actor": "hermes",
                "capability_token": cap_token,
            })
        assert result2.get("error", {}).get("code") == "CALLER_CAPABILITY_REPLAY", result2

    def test_fresh_jti_after_replay_succeeds(self, tmp_path):
        """After a jti is consumed, a new token with a different jti succeeds."""
        _make_ticket(tmp_path)
        body = "Your ticket is resolved. Fresh token."

        # Use first token.
        cap1 = _mint_cap(actor_id="hermes")
        clearance1 = _mint_clearance(body)
        result1 = _call_send(tmp_path, body, cap_token=cap1, clearance_token=clearance1)
        assert "error" not in result1, result1

        # Fresh token (different auto-generated jti).
        body2 = "Your ticket is resolved. Second response."
        cap2 = _mint_cap(actor_id="hermes")  # new jti auto-generated
        clearance2 = _mint_clearance(body2)
        result2 = _call_send(tmp_path, body2, cap_token=cap2, clearance_token=clearance2)
        assert "error" not in result2, result2


# ===========================================================================
# Tests: wrong capability/ticket binding
# ===========================================================================

class TestCapabilityBinding:
    """Token must be bound to the correct capability and ticket_id."""

    def test_send_response_wrong_capability_rejected(self, tmp_path):
        """Token for xenia.execute_approved -> rejected by send_response."""
        _make_ticket(tmp_path)
        body = "Your ticket is resolved."
        clearance = _mint_clearance(body)
        # Wrong capability binding
        cap_token = _mint_cap(actor_id="hermes", capability="xenia.execute_approved")
        result = _call_send(tmp_path, body, cap_token=cap_token, clearance_token=clearance)
        assert result.get("error", {}).get("code") == "CALLER_CAPABILITY_INVALID"

    def test_execute_approved_wrong_capability_rejected(self, tmp_path):
        """Token for xenia.send_response -> rejected by execute_approved."""
        _make_ticket(tmp_path)
        _make_approval(tmp_path)
        # Wrong capability
        cap_token = _mint_cap(actor_id="hermes", capability="xenia.send_response")
        result = _call_exec(tmp_path, actor="hermes", cap_token=cap_token)
        assert result.get("error", {}).get("code") == "CALLER_CAPABILITY_INVALID"

    def test_send_response_wrong_ticket_id_rejected(self, tmp_path):
        """Token bound to a different ticket_id -> rejected."""
        _make_ticket(tmp_path)
        body = "Your ticket is resolved."
        clearance = _mint_clearance(body)
        # Token bound to ticket 999999, but request is for 000001
        cap_token = _mint_cap(actor_id="hermes", ticket_id="999999")
        result = _call_send(tmp_path, body, cap_token=cap_token, clearance_token=clearance)
        assert result.get("error", {}).get("code") == "CALLER_CAPABILITY_INVALID"

    def test_execute_approved_wrong_ticket_id_rejected(self, tmp_path):
        """Token bound to wrong ticket_id -> rejected."""
        _make_ticket(tmp_path)
        _make_approval(tmp_path)
        cap_token = _mint_cap(actor_id="hermes", capability="xenia.execute_approved",
                              ticket_id="999999")
        result = _call_exec(tmp_path, actor="hermes", cap_token=cap_token)
        assert result.get("error", {}).get("code") == "CALLER_CAPABILITY_INVALID"

    def test_send_response_expired_cap_token_rejected(self, tmp_path):
        """Expired capability token -> CALLER_CAPABILITY_INVALID."""
        _make_ticket(tmp_path)
        body = "Your ticket is resolved."
        clearance = _mint_clearance(body)
        # Mint token that expired in the past
        ts = int(time.time()) - 3600
        with _env(HYDRA_OPERATOR_KEY=_CAP_KEY_HEX):
            token_dict = mint_capability({
                "v": 1,
                "actor_id": "hermes",
                "actor_kind": "agent",
                "capability": "xenia.send_response",
                "resource_id": _TICKET_ID,
                "workflow_id": _TICKET_ID,
                "issued_at": ts,
                "exp": ts + 1,  # already expired
            })
        cap_token = json.dumps(token_dict)
        result = _call_send(tmp_path, body, cap_token=cap_token, clearance_token=clearance)
        assert result.get("error", {}).get("code") == "CALLER_CAPABILITY_INVALID"


# ===========================================================================
# Tests: verified-actor-replaces-self-reported
# ===========================================================================

class TestVerifiedActorReplacesArg:
    """The VERIFIED actor_id from the capability token governs allow-list check,
    NOT the self-reported actor arg.  Mismatched arg actor is ignored."""

    def test_send_response_verified_actor_governs_not_arg(self, tmp_path):
        """Token actor_id=hermes (allowed), arg actor=iris (not allowed).
        Verified actor governs -> succeeds (hermes is on allow-list)."""
        _make_ticket(tmp_path)
        body = "Your ticket is resolved."
        clearance = _mint_clearance(body)
        # Cap token says "hermes" — hermes is allowed.
        cap_token = _mint_cap(actor_id="hermes")
        # Arg says "iris" — would be forbidden if arg governed.
        result = _call_send(tmp_path, body, actor="iris",
                            cap_token=cap_token, clearance_token=clearance)
        # Verified hermes -> allowed. History should record "hermes", not "iris".
        assert "error" not in result, (
            f"Verified hermes should pass even though arg says iris: {result}"
        )
        assert result.get("ok") is True

    def test_send_response_verified_non_allowed_actor_blocked_regardless_of_arg(self, tmp_path):
        """Token actor_id=rogue (not allowed), arg actor=hermes (allowed).
        Verified actor governs -> FORBIDDEN_ACTOR."""
        _make_ticket(tmp_path)
        body = "Your ticket is resolved."
        clearance = _mint_clearance(body)
        cap_token = _mint_cap(actor_id="rogue")  # not on allow-list
        # Arg says hermes — but verified actor is rogue, which governs.
        result = _call_send(tmp_path, body, actor="hermes",
                            cap_token=cap_token, clearance_token=clearance)
        assert result.get("error", {}).get("code") == "FORBIDDEN_ACTOR", (
            f"Rogue verified actor should produce FORBIDDEN_ACTOR: {result}"
        )

    def test_execute_approved_verified_actor_governs(self, tmp_path):
        """execute_approved: token=hermes, arg=iris -> verified hermes governs -> allowed."""
        _make_ticket(tmp_path)
        _make_approval(tmp_path)
        cap_token = _mint_cap(actor_id="hermes", capability="xenia.execute_approved")
        result = _call_exec(tmp_path, actor="iris",  # arg says non-allowed
                            cap_token=cap_token)
        assert "error" not in result, (
            f"Verified hermes should pass even though arg says iris: {result}"
        )
        assert result.get("ok") is True


# ===========================================================================
# Tests: degraded capability token (no HYDRA_OPERATOR_KEY)
# ===========================================================================

class TestDegradedCapabilityToken:
    """When HYDRA_OPERATOR_KEY is absent at MINT time, token is degraded.
    Degraded tokens are REJECTED at verify time (fail-closed)."""

    def test_send_response_degraded_cap_token_blocked(self, tmp_path):
        """Degraded cap token (no key) -> CALLER_CAPABILITY_INVALID."""
        _make_ticket(tmp_path)
        body = "Your ticket is resolved."
        clearance = _mint_clearance(body)
        # Mint with no key -> degraded
        with _env(HYDRA_OPERATOR_KEY=""):
            degraded_token = mint_caller_capability(
                actor_id="hermes",
                capability="xenia.send_response",
                ticket_id=_TICKET_ID,
            )
        cap_token = json.dumps(degraded_token)
        # Even with a real key at verify time, degraded is rejected
        result = _call_send(tmp_path, body, cap_token=cap_token, clearance_token=clearance)
        assert result.get("error", {}).get("code") == "CALLER_CAPABILITY_INVALID"

    def test_execute_approved_degraded_cap_token_blocked(self, tmp_path):
        """Degraded cap token -> CALLER_CAPABILITY_INVALID for execute_approved."""
        _make_ticket(tmp_path)
        _make_approval(tmp_path)
        with _env(HYDRA_OPERATOR_KEY=""):
            degraded_token = mint_caller_capability(
                actor_id="hermes",
                capability="xenia.execute_approved",
                ticket_id=_TICKET_ID,
            )
        cap_token = json.dumps(degraded_token)
        result = _call_exec(tmp_path, actor="hermes", cap_token=cap_token)
        assert result.get("error", {}).get("code") == "CALLER_CAPABILITY_INVALID"

    def test_no_operator_key_at_verify_time_fail_closed(self, tmp_path):
        """Valid token minted but HYDRA_OPERATOR_KEY cleared at verify time -> fail closed."""
        _make_ticket(tmp_path)
        body = "Your ticket is resolved."
        clearance = _mint_clearance(body)
        cap_token = _mint_cap(actor_id="hermes")  # validly minted
        # Verify without key -> fail closed
        with _env(HYDRA_XENIA_ROOT=str(tmp_path), XENIA_CONTEXT_SIGNING_KEY="cafebabe",
                  HYDRA_OPERATOR_KEY=""):
            handlers = _tool_handlers()
            result = handlers["xenia-tickets.send_response"]({
                "ticket_id": _TICKET_ID,
                "body": body,
                "actor": "hermes",
                "capability_token": cap_token,
                "clearance_token": clearance,
            })
        assert result.get("error", {}).get("code") == "CALLER_CAPABILITY_INVALID"


# ===========================================================================
# Tests: mint_caller_capability seam
# ===========================================================================

class TestMintCallerCapabilitySeam:
    """Verify mint_caller_capability produces correct tokens for legitimate callers."""

    def test_mint_returns_signed_token(self):
        """With HYDRA_OPERATOR_KEY set, mint returns a non-degraded signed token."""
        with _env(HYDRA_OPERATOR_KEY=_CAP_KEY_HEX):
            token = mint_caller_capability(
                actor_id="hermes",
                capability="xenia.send_response",
                ticket_id=_TICKET_ID,
            )
        assert token["sig"]["value"] is not None
        assert token["sig"].get("degraded") is None
        assert token["actor_id"] == "hermes"
        assert token["actor_kind"] == "agent"
        assert token["capability"] == "xenia.send_response"
        assert token["resource_id"] == _TICKET_ID
        assert token["workflow_id"] == _TICKET_ID
        assert "jti" in token and token["jti"]

    def test_mint_auto_jti_different_per_call(self):
        """Each mint call produces a unique jti."""
        with _env(HYDRA_OPERATOR_KEY=_CAP_KEY_HEX):
            t1 = mint_caller_capability(
                actor_id="hermes", capability="xenia.send_response", ticket_id=_TICKET_ID)
            t2 = mint_caller_capability(
                actor_id="hermes", capability="xenia.send_response", ticket_id=_TICKET_ID)
        assert t1["jti"] != t2["jti"], "Each token must have a unique jti"

    def test_mint_explicit_jti_preserved(self):
        """Explicit jti is preserved in the minted token."""
        with _env(HYDRA_OPERATOR_KEY=_CAP_KEY_HEX):
            token = mint_caller_capability(
                actor_id="hermes", capability="xenia.send_response",
                ticket_id=_TICKET_ID, jti="my-nonce-fixed")
        assert token["jti"] == "my-nonce-fixed"

    def test_mint_degraded_when_no_key(self):
        """No HYDRA_OPERATOR_KEY -> degraded token (sig.value=None)."""
        with _env(HYDRA_OPERATOR_KEY=""):
            token = mint_caller_capability(
                actor_id="hermes", capability="xenia.send_response", ticket_id=_TICKET_ID)
        assert token["sig"]["value"] is None
        assert token["sig"].get("degraded") is True

    def test_mint_e2e_send_response(self, tmp_path):
        """Full e2e: mint -> pass as capability_token -> send_response succeeds."""
        _make_ticket(tmp_path)
        body = "Your ticket has been reviewed and resolved."
        # Mint cap token
        with _env(HYDRA_OPERATOR_KEY=_CAP_KEY_HEX):
            cap_token_dict = mint_caller_capability(
                actor_id="hermes",
                capability="xenia.send_response",
                ticket_id=_TICKET_ID,
            )
        cap_token_json = json.dumps(cap_token_dict)
        # Mint clearance token
        clearance = _mint_clearance(body)
        # Call send_response
        with _env(HYDRA_XENIA_ROOT=str(tmp_path), XENIA_CONTEXT_SIGNING_KEY="cafebabe",
                  HYDRA_OPERATOR_KEY=_CAP_KEY_HEX):
            handlers = _tool_handlers()
            result = handlers["xenia-tickets.send_response"]({
                "ticket_id": _TICKET_ID,
                "body": body,
                "actor": "hermes",
                "capability_token": cap_token_json,
                "clearance_token": clearance,
            })
        assert "error" not in result, result
        assert result.get("ok") is True

    def test_mint_e2e_execute_approved(self, tmp_path):
        """Full e2e: mint -> pass as capability_token -> execute_approved succeeds."""
        _make_ticket(tmp_path)
        _make_approval(tmp_path)
        with _env(HYDRA_OPERATOR_KEY=_CAP_KEY_HEX):
            cap_token_dict = mint_caller_capability(
                actor_id="hermes",
                capability="xenia.execute_approved",
                ticket_id=_TICKET_ID,
            )
        cap_token_json = json.dumps(cap_token_dict)
        with _env(HYDRA_XENIA_ROOT=str(tmp_path), HYDRA_OPERATOR_KEY=_CAP_KEY_HEX):
            handlers = _tool_handlers()
            result = handlers["xenia-tickets.execute_approved"]({
                "ticket_id": _TICKET_ID,
                "action": "refund",
                "scope": "billing",
                "approval_id": f"APPROVAL-{_TICKET_ID}-001",
                "actor": "hermes",
                "capability_token": cap_token_json,
            })
        assert "error" not in result, result
        assert result.get("ok") is True

    def test_mint_raw_dict_accepted_as_capability_token(self, tmp_path):
        """Raw dict (not JSON string) is also accepted as capability_token."""
        _make_ticket(tmp_path)
        body = "Your ticket is resolved."
        clearance = _mint_clearance(body)
        with _env(HYDRA_OPERATOR_KEY=_CAP_KEY_HEX):
            cap_token_dict = mint_caller_capability(
                actor_id="hermes",
                capability="xenia.send_response",
                ticket_id=_TICKET_ID,
            )
        with _env(HYDRA_XENIA_ROOT=str(tmp_path), XENIA_CONTEXT_SIGNING_KEY="cafebabe",
                  HYDRA_OPERATOR_KEY=_CAP_KEY_HEX):
            handlers = _tool_handlers()
            result = handlers["xenia-tickets.send_response"]({
                "ticket_id": _TICKET_ID,
                "body": body,
                "actor": "hermes",
                "capability_token": cap_token_dict,  # raw dict, not JSON string
                "clearance_token": clearance,
            })
        assert "error" not in result, result
        assert result.get("ok") is True


# ===========================================================================
# Tests: Fix 1 — audit attribution (executor actor == verified token actor)
# ===========================================================================

class TestAuditAttribution:
    """The history/audit entry actor must be the VERIFIED token actor_id,
    not the self-reported arg actor and not the approval artifact's issued_by."""

    def _load_ticket_history(self, tmp_path: Path, ticket_id: str = _TICKET_ID) -> list:
        import json as _json
        p = tmp_path / "hearth" / "tasks" / f"TICKET-{ticket_id}.json"
        return _json.loads(p.read_text(encoding="utf-8"))["history"]

    def test_send_response_history_actor_is_verified_token_actor(self, tmp_path):
        """send_response history entry actor == verified token actor_id, not arg actor."""
        _make_ticket(tmp_path)
        body = "Your ticket has been resolved by our team."
        clearance = _mint_clearance(body)
        # Cap token says "hermes"; arg actor says "escalation-handoff" (different but allowed).
        cap_token = _mint_cap(actor_id="hermes")
        with _env(HYDRA_XENIA_ROOT=str(tmp_path), XENIA_CONTEXT_SIGNING_KEY="cafebabe",
                  HYDRA_OPERATOR_KEY=_CAP_KEY_HEX):
            handlers = _tool_handlers()
            result = handlers["xenia-tickets.send_response"]({
                "ticket_id": _TICKET_ID,
                "body": body,
                "actor": "escalation-handoff",  # self-reported arg — must be IGNORED
                "capability_token": cap_token,
                "clearance_token": clearance,
            })
        assert "error" not in result, result

        history = self._load_ticket_history(tmp_path)
        response_entries = [e for e in history if e.get("kind") == "response"]
        assert len(response_entries) == 1
        recorded_actor = response_entries[0]["actor"]
        assert recorded_actor == "hermes", (
            f"History actor must be the VERIFIED token actor ('hermes'), "
            f"not the self-reported arg ('escalation-handoff'). Got: {recorded_actor!r}"
        )

    def test_execute_approved_history_actor_is_verified_token_actor(self, tmp_path):
        """execute_approved history entry actor == verified token actor_id (the executor),
        NOT issued_by (the approval artifact's issuer), NOT the self-reported arg."""
        _make_ticket(tmp_path)
        # approval artifact says issued_by=some-approver
        approvals_dir = tmp_path / "hearth" / "approvals"
        approvals_dir.mkdir(parents=True, exist_ok=True)
        expires_at = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        content = (
            f"ticket_id: {_TICKET_ID}\n"
            f"status: approved\n"
            f"action: refund\n"
            f"scope: billing\n"
            f"issued_by: some-human-approver\n"  # distinct from executor
            f"expires_at: {expires_at}\n"
        )
        (approvals_dir / f"APPROVAL-{_TICKET_ID}-001.yaml").write_text(content, encoding="utf-8")

        # Cap token says "hermes" (the executor); arg says "escalation-handoff"
        cap_token = _mint_cap(actor_id="hermes", capability="xenia.execute_approved")
        with _env(HYDRA_XENIA_ROOT=str(tmp_path), HYDRA_OPERATOR_KEY=_CAP_KEY_HEX):
            handlers = _tool_handlers()
            result = handlers["xenia-tickets.execute_approved"]({
                "ticket_id": _TICKET_ID,
                "action": "refund",
                "scope": "billing",
                "approval_id": f"APPROVAL-{_TICKET_ID}-001",
                "actor": "escalation-handoff",  # self-reported — must be IGNORED
                "capability_token": cap_token,
            })
        assert "error" not in result, result

        history = self._load_ticket_history(tmp_path)
        exec_entries = [e for e in history if e.get("kind") == "approved-action"]
        assert len(exec_entries) == 1
        entry = exec_entries[0]

        # executor (actor) == verified token actor, NOT self-reported arg, NOT issued_by
        assert entry["actor"] == "hermes", (
            f"History actor must be the VERIFIED token actor ('hermes'), "
            f"not self-reported arg ('escalation-handoff') or issued_by. Got: {entry['actor']!r}"
        )
        # issued_by is still present and distinct from executor
        assert entry.get("issued_by") == "some-human-approver", (
            f"issued_by must be preserved separately. Got: {entry.get('issued_by')!r}"
        )
        assert entry["actor"] != entry.get("issued_by"), (
            "executor actor and issued_by should be distinct in audit trail"
        )

    def test_execute_approved_both_fields_present(self, tmp_path):
        """Both actor (executor) and issued_by (approver) are present in history entry."""
        _make_ticket(tmp_path)
        _make_approval(tmp_path)  # issued_by defaults to "hermes" in _make_approval
        cap_token = _mint_cap(actor_id="escalation-handoff",
                              capability="xenia.execute_approved")
        with _env(HYDRA_XENIA_ROOT=str(tmp_path), HYDRA_OPERATOR_KEY=_CAP_KEY_HEX):
            handlers = _tool_handlers()
            result = handlers["xenia-tickets.execute_approved"]({
                "ticket_id": _TICKET_ID,
                "action": "refund",
                "scope": "billing",
                "approval_id": f"APPROVAL-{_TICKET_ID}-001",
                "actor": "hermes",
                "capability_token": cap_token,
            })
        assert "error" not in result, result

        history = self._load_ticket_history(tmp_path)
        exec_entries = [e for e in history if e.get("kind") == "approved-action"]
        assert len(exec_entries) == 1
        entry = exec_entries[0]
        # actor = verified executor (escalation-handoff from token)
        assert entry["actor"] == "escalation-handoff"
        # issued_by = approval artifact issuer (hermes from _make_approval)
        assert "issued_by" in entry
        assert entry["issued_by"] == "hermes"


# ===========================================================================
# Tests: Fix 2 — shared golden vector matches TS sig literal
# ===========================================================================

class TestSharedGoldenVector:
    """The Python golden vector must match the TS golden exactly, proving byte-identical
    canonical JSON + HMAC-SHA256 across Python and TypeScript implementations."""

    # Mirror C:\AiAppDeployments\TheEights\daemon\test\capability.test.ts constants exactly.
    _TS_GOLDEN_KEY_HEX = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
    _TS_GOLDEN_KEY_ID = "golden-1"
    _TS_GOLDEN_JTI = "fixed-golden-jti-001"
    _TS_GOLDEN_SIG = "vwWp9w23fYQIRQG17mR-Uw6-bXrMxzsinPkGjSJv50I"
    _TS_GOLDEN_PAYLOAD = {
        "v": 1,
        "actor_id": "golden@hydra.test",
        "actor_kind": "human",
        "capability": "hitl_approve",
        "resource_id": "wf-golden-001",
        "workflow_id": "wf-golden-001",
        "issued_at": 1749600000,
        "exp": 1749600900,
        "jti": "fixed-golden-jti-001",
    }

    def test_python_mint_produces_ts_golden_sig(self):
        """mint_capability with the TS golden payload+key must produce the TS golden sig.
        If this fails, Python and TS canonical formats have diverged — an interop bug."""
        from hydra_core.auth.capability import mint_capability
        with _env(HYDRA_OPERATOR_KEY=self._TS_GOLDEN_KEY_HEX,
                  HYDRA_OPERATOR_KEY_ID=self._TS_GOLDEN_KEY_ID):
            token = mint_capability(self._TS_GOLDEN_PAYLOAD)
        assert token["sig"]["value"] == self._TS_GOLDEN_SIG, (
            f"Python sig {token['sig']['value']!r} != TS golden sig {self._TS_GOLDEN_SIG!r}. "
            "This is an interop bug: Python and TypeScript canonical formats have diverged."
        )

    def test_python_verify_accepts_ts_golden_token(self):
        """A token built from TS golden payload + TS golden sig must verify under Python."""
        from hydra_core.auth.capability import verify_capability
        token = dict(self._TS_GOLDEN_PAYLOAD)
        token["sig"] = {
            "alg": "HMAC-SHA256",
            "key_id": self._TS_GOLDEN_KEY_ID,
            "value": self._TS_GOLDEN_SIG,
        }
        with _env(HYDRA_OPERATOR_KEY=self._TS_GOLDEN_KEY_HEX,
                  HYDRA_OPERATOR_KEY_ID=self._TS_GOLDEN_KEY_ID):
            result = verify_capability(
                token,
                expected_capability="hitl_approve",
                now=self._TS_GOLDEN_PAYLOAD["issued_at"] + 1,
            )
        assert result["valid"] is True, (
            f"Python verify rejected TS golden token: {result['reason']}"
        )
        assert result["jti"] == self._TS_GOLDEN_JTI

    def test_tampered_ts_golden_token_rejected(self):
        """Tampering the TS golden token must cause Python verify to reject it."""
        from hydra_core.auth.capability import verify_capability
        token = dict(self._TS_GOLDEN_PAYLOAD)
        token["actor_id"] = "injected@evil.com"  # tamper
        token["sig"] = {
            "alg": "HMAC-SHA256",
            "key_id": self._TS_GOLDEN_KEY_ID,
            "value": self._TS_GOLDEN_SIG,
        }
        with _env(HYDRA_OPERATOR_KEY=self._TS_GOLDEN_KEY_HEX):
            result = verify_capability(
                token,
                expected_capability="hitl_approve",
                now=self._TS_GOLDEN_PAYLOAD["issued_at"] + 1,
            )
        assert result["valid"] is False
