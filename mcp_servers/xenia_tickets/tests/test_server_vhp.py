"""Tests for XEN-VHP-1 through XEN-VHP-4 server-side safety enforcement.

Covers:
  VHP-1: PII scan (normalized) blocks send_response for EMAIL/SSN/CC/PHONE;
         normalization: HTML-unescape, NFKC, dotted SSN, dotted card
  VHP-2: Signed clearance token (sign.py scheme) required;
         sign.py interop test (mint via sign.py -> verify via clearance.py);
         missing/invalid/tampered/degraded token blocked;
         actor='human' blocked (bypass removed); "human" removed from allow-list
  VHP-3: Non-allow-listed actor -> FORBIDDEN_ACTOR (send_response, execute_approved);
         execute_approved: actor missing/empty -> MISSING_FIELD (not silently skipped);
         approval binding: missing ticket_id/scope -> rejected; substring approval_id -> rejected
  VHP-4: Expanded money lexicon (remit, goodwill, adjustment, euros, bucks, "send you");
         money body without/with approval; money body + expired approval

All tests are pure-Python — no subprocess needed (sign.py is imported directly).
"""
from __future__ import annotations

import json
import os
import sys
import textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path
from contextlib import contextmanager

import pytest

# ---------------------------------------------------------------------------
# Path setup: Hydra root AND Xenia root so we can import sign.py directly.
# ---------------------------------------------------------------------------
HYDRA_ROOT = Path(__file__).resolve().parents[4]
XENIA_ROOT = Path("C:/AiAppDeployments/Xenia")
sys.path.insert(0, str(HYDRA_ROOT))
# Insert Xenia root so `from tools.context_token.sign import mint` works
sys.path.insert(0, str(XENIA_ROOT))

from mcp_servers.xenia_tickets.clearance import mint_clearance_token, verify_clearance_token
from mcp_servers.xenia_tickets.server import (
    _err,
    _is_money_commitment,
    _scan_pii,
    _check_actor_authz,
    _SEND_RESPONSE_ALLOWED_ACTORS,
    _EXECUTE_APPROVED_ALLOWED_ACTORS,
    _tool_handlers,
    _find_valid_approval,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@contextmanager
def _env(**env: str | None):
    """Context manager: temporarily patch os.environ."""
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


def _make_ticket(tmp_path: Path, ticket_id: str = "000001") -> dict:
    """Write a minimal ticket JSON."""
    tasks = tmp_path / "hearth" / "tasks"
    tasks.mkdir(parents=True, exist_ok=True)
    ticket = {
        "ticket_id":    ticket_id,
        "status":       "open",
        "priority":     "P3",
        "intent":       None,
        "customer_ref": "customer:aabbcc",
        "subject":      "Test ticket",
        "created_at":   datetime.now(timezone.utc).isoformat(),
        "updated_at":   datetime.now(timezone.utc).isoformat(),
        "sla": {"first_response_due": datetime.now(timezone.utc).isoformat(), "breached": False},
        "history":         [],
        "recommendations": [],
    }
    (tasks / f"TICKET-{ticket_id}.json").write_text(json.dumps(ticket, indent=2), encoding="utf-8")
    return ticket


def _make_approval(
    approvals_dir: Path,
    ticket_id: str,
    seq: str = "001",
    action: str = "send_response",
    scope: str = "monetary",
    expires_delta: timedelta = timedelta(hours=1),
    status: str = "approved",
    issued_by: str = "hermes",
    include_ticket_id: bool = True,
    include_scope: bool = True,
) -> Path:
    """Write an approval YAML artifact."""
    approvals_dir.mkdir(parents=True, exist_ok=True)
    expires_at = (datetime.now(timezone.utc) + expires_delta).isoformat()
    lines = []
    if include_ticket_id:
        lines.append(f"ticket_id: {ticket_id}")
    lines.append(f"status: {status}")
    lines.append(f"action: {action}")
    if include_scope:
        lines.append(f"scope: {scope}")
    lines.append(f"issued_by: {issued_by}")
    lines.append(f"expires_at: {expires_at}")
    content = "\n".join(lines) + "\n"
    p = approvals_dir / f"APPROVAL-{ticket_id}-{seq}.yaml"
    p.write_text(content, encoding="utf-8")
    return p


def _mint_token(body: str, key: str = "cafebabe", extra: dict | None = None) -> str:
    """Mint a clearance token via clearance.py (mirrors sign.py) and JSON-encode it."""
    with _env(XENIA_CONTEXT_SIGNING_KEY=key):
        token_dict = mint_clearance_token(body, extra)
    assert token_dict is not None, "mint returned None — key not configured"
    return json.dumps(token_dict)


def _send(tmp_path: Path, body: str, actor: str = "hermes",
          clearance_token=None, approval_id: str = "",
          key: str = "cafebabe") -> dict:
    """Call send_response handler with the given args."""
    with _env(HYDRA_XENIA_ROOT=str(tmp_path), XENIA_CONTEXT_SIGNING_KEY=key):
        handlers = _tool_handlers()
        args: dict = {"ticket_id": "000001", "body": body, "actor": actor}
        if clearance_token is not None:
            args["clearance_token"] = clearance_token
        if approval_id:
            args["approval_id"] = approval_id
        return handlers["xenia-tickets.send_response"](args)


# ---------------------------------------------------------------------------
# VHP-2: sign.py INTEROP tests
# ---------------------------------------------------------------------------
# These tests import sign.py from Xenia directly and verify that a token minted
# by sign.py verifies through clearance.py's verify_clearance_token, and that
# a token minted for body A rejects body B.

class TestSignPyInterop:
    """Interop: token minted by sign.py must verify via clearance.py and vice-versa."""

    def _sign_py_mint(self, token_dict: dict, key: str) -> dict:
        """Mint a token using sign.py's mint() directly."""
        from tools.context_token.sign import mint as sign_mint
        with _env(XENIA_CONTEXT_SIGNING_KEY=key):
            return sign_mint(token_dict)

    def test_sign_py_token_verifies_in_clearance(self):
        """Token minted by sign.py for body A must verify in clearance.py."""
        key = "aabbccddeeff0011"
        body = "Your ticket has been resolved."
        token_dict = self._sign_py_mint({"body": body}, key)
        token_json = json.dumps(token_dict)
        with _env(XENIA_CONTEXT_SIGNING_KEY=key):
            result = verify_clearance_token(body, token_json)
        assert result["ok"] is True, f"Expected valid, got: {result}"

    def test_sign_py_token_for_body_a_rejects_body_b(self):
        """Token minted for body A must be rejected when verifying against body B."""
        key = "aabbccddeeff0011"
        body_a = "Your ticket has been resolved."
        body_b = "Your ticket has been closed."
        token_dict = self._sign_py_mint({"body": body_a}, key)
        token_json = json.dumps(token_dict)
        with _env(XENIA_CONTEXT_SIGNING_KEY=key):
            result = verify_clearance_token(body_b, token_json)
        assert result["ok"] is False
        assert "mismatch" in result["reason"].lower() or "body" in result["reason"].lower()

    def test_clearance_mint_verifies_in_sign_py(self):
        """Token minted by clearance.py must also verify using sign.py's verify()."""
        from tools.context_token.sign import verify as sign_verify
        key = "aabbccddeeff0011"
        body = "We have reviewed your case."
        with _env(XENIA_CONTEXT_SIGNING_KEY=key):
            token_dict = mint_clearance_token(body)
        assert token_dict is not None
        with _env(XENIA_CONTEXT_SIGNING_KEY=key):
            result = sign_verify(token_dict)
        assert result["valid"] is True, f"sign.py verify failed: {result}"

    def test_degraded_sign_py_token_rejected_by_server(self):
        """sign.py degraded token (no key) must be REJECTED by server (fail closed)."""
        key = "aabbccddeeff0011"
        body = "Your ticket has been resolved."
        # Mint degraded (no key)
        from tools.context_token.sign import mint as sign_mint
        with _env(XENIA_CONTEXT_SIGNING_KEY=""):
            degraded = sign_mint({"body": body})
        token_json = json.dumps(degraded)
        with _env(XENIA_CONTEXT_SIGNING_KEY=key):
            result = verify_clearance_token(body, token_json)
        assert result["ok"] is False
        assert "degraded" in result["reason"].lower()


# ---------------------------------------------------------------------------
# VHP-2: clearance.py unit tests
# ---------------------------------------------------------------------------

class TestClearanceToken:
    def test_valid_token_dict_verifies(self):
        key = "cafebabe"
        body = "Hello customer"
        with _env(XENIA_CONTEXT_SIGNING_KEY=key):
            token_dict = mint_clearance_token(body)
        assert token_dict is not None
        token_json = json.dumps(token_dict)
        with _env(XENIA_CONTEXT_SIGNING_KEY=key):
            result = verify_clearance_token(body, token_json)
        assert result["ok"] is True

    def test_missing_token_blocked(self):
        with _env(XENIA_CONTEXT_SIGNING_KEY="cafebabe"):
            result = verify_clearance_token("Hello customer", None)
        assert result["ok"] is False
        assert "missing" in result["reason"].lower()

    def test_empty_token_blocked(self):
        with _env(XENIA_CONTEXT_SIGNING_KEY="cafebabe"):
            result = verify_clearance_token("Hello customer", "")
        assert result["ok"] is False

    def test_tampered_body_blocked(self):
        key = "cafebabe"
        original = "Hello customer"
        tampered = "Hello customer TAMPERED"
        with _env(XENIA_CONTEXT_SIGNING_KEY=key):
            token_dict = mint_clearance_token(original)
        token_json = json.dumps(token_dict)
        with _env(XENIA_CONTEXT_SIGNING_KEY=key):
            result = verify_clearance_token(tampered, token_json)
        assert result["ok"] is False
        assert "mismatch" in result["reason"].lower() or "body" in result["reason"].lower()

    def test_wrong_sig_value_blocked(self):
        key = "cafebabe"
        body = "Hello customer"
        with _env(XENIA_CONTEXT_SIGNING_KEY=key):
            token_dict = mint_clearance_token(body)
        # Corrupt the sig value
        token_dict["sig"]["value"] = "AAABBBCCC"
        token_json = json.dumps(token_dict)
        with _env(XENIA_CONTEXT_SIGNING_KEY=key):
            result = verify_clearance_token(body, token_json)
        assert result["ok"] is False

    def test_no_key_configured_fail_closed(self):
        """No signing key + any token -> fail closed."""
        with _env(XENIA_CONTEXT_SIGNING_KEY=""):
            result = verify_clearance_token("Hello customer", '{"body":"Hello customer","sig":{"alg":"HMAC-SHA256","key_id":"default","value":"AAABBB"}}')
        assert result["ok"] is False

    def test_no_key_no_token_fail_closed(self):
        """No signing key + no token -> fail closed."""
        with _env(XENIA_CONTEXT_SIGNING_KEY=""):
            result = verify_clearance_token("Hello customer", None)
        assert result["ok"] is False

    def test_degraded_token_rejected(self):
        """Degraded token (sig.degraded=True) must be rejected."""
        key = "cafebabe"
        body = "Hello customer"
        degraded = {"body": body, "sig": {"alg": "HMAC-SHA256", "key_id": "default", "value": None, "degraded": True}}
        token_json = json.dumps(degraded)
        with _env(XENIA_CONTEXT_SIGNING_KEY=key):
            result = verify_clearance_token(body, token_json)
        assert result["ok"] is False
        assert "degraded" in result["reason"].lower()

    def test_bare_string_token_rejected(self):
        """A bare string (old scheme) is not valid JSON dict -> rejected."""
        key = "cafebabe"
        with _env(XENIA_CONTEXT_SIGNING_KEY=key):
            result = verify_clearance_token("Hello customer", "notajsonobject")
        assert result["ok"] is False


# ---------------------------------------------------------------------------
# VHP-2: send_response integration
# ---------------------------------------------------------------------------

class TestSendResponseClearance:
    def test_missing_token_blocked(self, tmp_path):
        _make_ticket(tmp_path, "000001")
        with _env(HYDRA_XENIA_ROOT=str(tmp_path), XENIA_CONTEXT_SIGNING_KEY="cafebabe"):
            handlers = _tool_handlers()
            result = handlers["xenia-tickets.send_response"]({
                "ticket_id": "000001",
                "body": "Your ticket is resolved.",
                "actor": "hermes",
            })
        assert result.get("error", {}).get("code") == "CLEARANCE_INVALID"

    def test_invalid_token_blocked(self, tmp_path):
        _make_ticket(tmp_path, "000001")
        with _env(HYDRA_XENIA_ROOT=str(tmp_path), XENIA_CONTEXT_SIGNING_KEY="cafebabe"):
            handlers = _tool_handlers()
            result = handlers["xenia-tickets.send_response"]({
                "ticket_id": "000001",
                "body": "Your ticket is resolved.",
                "actor": "hermes",
                "clearance_token": "totally-wrong-not-json",
            })
        assert result.get("error", {}).get("code") == "CLEARANCE_INVALID"

    def test_valid_token_allowed(self, tmp_path):
        _make_ticket(tmp_path, "000001")
        body = "Your ticket is resolved."
        token_json = _mint_token(body)
        result = _send(tmp_path, body, clearance_token=token_json)
        assert "error" not in result, result
        assert result.get("ok") is True

    def test_human_actor_no_token_blocked(self, tmp_path):
        """actor='human' must now produce FORBIDDEN_ACTOR (human removed from allow-list #6)."""
        _make_ticket(tmp_path, "000001")
        with _env(HYDRA_XENIA_ROOT=str(tmp_path), XENIA_CONTEXT_SIGNING_KEY="cafebabe"):
            handlers = _tool_handlers()
            result = handlers["xenia-tickets.send_response"]({
                "ticket_id": "000001",
                "body": "Your ticket is resolved.",
                "actor": "human",
            })
        # human is NOT on the allow-list anymore -> FORBIDDEN_ACTOR fires first
        assert result.get("error", {}).get("code") == "FORBIDDEN_ACTOR"

    def test_no_signing_key_fail_closed(self, tmp_path):
        """XENIA_CONTEXT_SIGNING_KEY absent -> fail closed regardless of token."""
        _make_ticket(tmp_path, "000001")
        with _env(HYDRA_XENIA_ROOT=str(tmp_path), XENIA_CONTEXT_SIGNING_KEY=""):
            handlers = _tool_handlers()
            result = handlers["xenia-tickets.send_response"]({
                "ticket_id": "000001",
                "body": "Your ticket is resolved.",
                "actor": "hermes",
                "clearance_token": '{"body":"Your ticket is resolved.","sig":{"alg":"HMAC-SHA256","key_id":"default","value":"AAABBB"}}',
            })
        assert result.get("error", {}).get("code") == "CLEARANCE_INVALID"

    def test_degraded_token_blocked(self, tmp_path):
        _make_ticket(tmp_path, "000001")
        body = "Your ticket is resolved."
        degraded = {"body": body, "sig": {"alg": "HMAC-SHA256", "key_id": "default", "value": None, "degraded": True}}
        with _env(HYDRA_XENIA_ROOT=str(tmp_path), XENIA_CONTEXT_SIGNING_KEY="cafebabe"):
            handlers = _tool_handlers()
            result = handlers["xenia-tickets.send_response"]({
                "ticket_id": "000001",
                "body": body,
                "actor": "hermes",
                "clearance_token": json.dumps(degraded),
            })
        assert result.get("error", {}).get("code") == "CLEARANCE_INVALID"


# ---------------------------------------------------------------------------
# VHP-3: Actor allow-list unit tests
# ---------------------------------------------------------------------------

class TestActorAllowList:
    def test_human_not_in_send_response_allowlist(self):
        """'human' must NOT be in _SEND_RESPONSE_ALLOWED_ACTORS (fix #6)."""
        assert "human" not in _SEND_RESPONSE_ALLOWED_ACTORS

    def test_human_not_in_execute_approved_allowlist(self):
        """'human' must NOT be in _EXECUTE_APPROVED_ALLOWED_ACTORS (fix #6)."""
        assert "human" not in _EXECUTE_APPROVED_ALLOWED_ACTORS

    def test_hermes_allowed_send_response(self):
        assert _check_actor_authz("hermes", _SEND_RESPONSE_ALLOWED_ACTORS) is None

    def test_escalation_handoff_allowed_send_response(self):
        assert _check_actor_authz("escalation-handoff", _SEND_RESPONSE_ALLOWED_ACTORS) is None

    def test_unknown_actor_forbidden_send_response(self):
        result = _check_actor_authz("rogue-agent", _SEND_RESPONSE_ALLOWED_ACTORS)
        assert result is not None
        assert result["error"]["code"] == "FORBIDDEN_ACTOR"

    def test_plutus_forbidden_send_response(self):
        result = _check_actor_authz("plutus", _SEND_RESPONSE_ALLOWED_ACTORS)
        assert result is not None
        assert result["error"]["code"] == "FORBIDDEN_ACTOR"

    def test_iris_forbidden_send_response(self):
        result = _check_actor_authz("iris", _SEND_RESPONSE_ALLOWED_ACTORS)
        assert result is not None
        assert result["error"]["code"] == "FORBIDDEN_ACTOR"

    def test_human_forbidden_send_response(self):
        """'human' must produce FORBIDDEN_ACTOR (removed from allow-list)."""
        result = _check_actor_authz("human", _SEND_RESPONSE_ALLOWED_ACTORS)
        assert result is not None
        assert result["error"]["code"] == "FORBIDDEN_ACTOR"

    def test_hermes_allowed_execute_approved(self):
        assert _check_actor_authz("hermes", _EXECUTE_APPROVED_ALLOWED_ACTORS) is None

    def test_unknown_actor_forbidden_execute_approved(self):
        result = _check_actor_authz("unknown", _EXECUTE_APPROVED_ALLOWED_ACTORS)
        assert result is not None
        assert result["error"]["code"] == "FORBIDDEN_ACTOR"


class TestExecuteApprovedActorRequired:
    """Fix #1: execute_approved must require actor unconditionally."""

    def test_missing_actor_blocked(self, tmp_path):
        """No actor supplied -> MISSING_FIELD (not silently skipped)."""
        _make_ticket(tmp_path, "000001")
        approvals_dir = tmp_path / "hearth" / "approvals"
        _make_approval(approvals_dir, "000001", action="refund", scope="billing")
        with _env(HYDRA_XENIA_ROOT=str(tmp_path)):
            handlers = _tool_handlers()
            result = handlers["xenia-tickets.execute_approved"]({
                "ticket_id":   "000001",
                "action":      "refund",
                "scope":       "billing",
                "approval_id": "APPROVAL-000001-001",
                # No actor
            })
        assert result.get("error", {}).get("code") == "MISSING_FIELD"

    def test_empty_actor_blocked(self, tmp_path):
        """Empty actor -> MISSING_FIELD."""
        _make_ticket(tmp_path, "000001")
        approvals_dir = tmp_path / "hearth" / "approvals"
        _make_approval(approvals_dir, "000001", action="refund", scope="billing")
        with _env(HYDRA_XENIA_ROOT=str(tmp_path)):
            handlers = _tool_handlers()
            result = handlers["xenia-tickets.execute_approved"]({
                "ticket_id":   "000001",
                "action":      "refund",
                "scope":       "billing",
                "approval_id": "APPROVAL-000001-001",
                "actor":       "",
            })
        assert result.get("error", {}).get("code") == "MISSING_FIELD"

    def test_non_allowlisted_actor_forbidden(self, tmp_path):
        _make_ticket(tmp_path, "000001")
        approvals_dir = tmp_path / "hearth" / "approvals"
        _make_approval(approvals_dir, "000001", action="refund", scope="billing")
        with _env(HYDRA_XENIA_ROOT=str(tmp_path)):
            handlers = _tool_handlers()
            result = handlers["xenia-tickets.execute_approved"]({
                "ticket_id":   "000001",
                "action":      "refund",
                "scope":       "billing",
                "approval_id": "APPROVAL-000001-001",
                "actor":       "iris",
            })
        assert result.get("error", {}).get("code") == "FORBIDDEN_ACTOR"

    def test_allowlisted_actor_proceeds(self, tmp_path):
        _make_ticket(tmp_path, "000001")
        approvals_dir = tmp_path / "hearth" / "approvals"
        _make_approval(approvals_dir, "000001", action="refund", scope="billing")
        with _env(HYDRA_XENIA_ROOT=str(tmp_path)):
            handlers = _tool_handlers()
            result = handlers["xenia-tickets.execute_approved"]({
                "ticket_id":   "000001",
                "action":      "refund",
                "scope":       "billing",
                "approval_id": "APPROVAL-000001-001",
                "actor":       "hermes",
            })
        assert "error" not in result, result
        assert result.get("ok") is True

    def test_send_response_non_allowlisted_actor_forbidden(self, tmp_path):
        _make_ticket(tmp_path, "000001")
        body = "Your ticket is resolved."
        token_json = _mint_token(body)
        result = _send(tmp_path, body, actor="metis", clearance_token=token_json)
        assert result.get("error", {}).get("code") == "FORBIDDEN_ACTOR"


# ---------------------------------------------------------------------------
# VHP-3: Approval artifact binding exactness (fix #3)
# ---------------------------------------------------------------------------

class TestApprovalBinding:

    def _approvals_dir(self, tmp_path: Path) -> Path:
        d = tmp_path / "hearth" / "approvals"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def test_exact_approval_id_accepted(self, tmp_path):
        """Exact stem match -> accepted."""
        _make_ticket(tmp_path, "000001")
        approvals_dir = self._approvals_dir(tmp_path)
        _make_approval(approvals_dir, "000001", seq="001", action="send_response", scope="monetary")
        body = "We will issue a refund of $50."
        token_json = _mint_token(body)
        result = _send(tmp_path, body, clearance_token=token_json, approval_id="APPROVAL-000001-001")
        assert "error" not in result, result
        assert result.get("ok") is True

    def test_substring_approval_id_rejected(self, tmp_path):
        """Substring approval_id -> rejected (no partial/inclusion match)."""
        _make_ticket(tmp_path, "000001")
        approvals_dir = self._approvals_dir(tmp_path)
        _make_approval(approvals_dir, "000001", seq="001", action="send_response", scope="monetary")
        body = "We will issue a refund of $50."
        token_json = _mint_token(body)
        # Pass only a substring of the stem
        result = _send(tmp_path, body, clearance_token=token_json, approval_id="000001")
        assert result.get("error", {}).get("code") == "APPROVAL_REQUIRED"

    def test_artifact_missing_ticket_id_rejected(self, tmp_path):
        """Artifact without ticket_id -> rejected."""
        _make_ticket(tmp_path, "000001")
        approvals_dir = self._approvals_dir(tmp_path)
        _make_approval(approvals_dir, "000001", seq="002",
                       action="send_response", scope="monetary",
                       include_ticket_id=False)
        body = "We will issue a refund of $50."
        token_json = _mint_token(body)
        result = _send(tmp_path, body, clearance_token=token_json, approval_id="APPROVAL-000001-002")
        assert result.get("error", {}).get("code") == "APPROVAL_REQUIRED"

    def test_artifact_missing_scope_rejected(self, tmp_path):
        """Artifact without scope -> rejected."""
        _make_ticket(tmp_path, "000001")
        approvals_dir = self._approvals_dir(tmp_path)
        _make_approval(approvals_dir, "000001", seq="003",
                       action="send_response", scope="monetary",
                       include_scope=False)
        body = "We will issue a refund of $50."
        token_json = _mint_token(body)
        result = _send(tmp_path, body, clearance_token=token_json, approval_id="APPROVAL-000001-003")
        assert result.get("error", {}).get("code") == "APPROVAL_REQUIRED"

    def test_mismatched_ticket_id_rejected(self, tmp_path):
        """Artifact with wrong ticket_id -> rejected."""
        _make_ticket(tmp_path, "000001")
        # Write an artifact for ticket 999999, but request is for 000001
        _make_ticket(tmp_path, "999999")
        approvals_dir = self._approvals_dir(tmp_path)
        expires_at = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        content = textwrap.dedent(f"""\
            ticket_id: 999999
            status: approved
            action: send_response
            scope: monetary
            issued_by: hermes
            expires_at: {expires_at}
        """)
        wrong_artifact = approvals_dir / "APPROVAL-000001-004.yaml"
        wrong_artifact.write_text(content, encoding="utf-8")
        body = "We will issue a refund of $50."
        token_json = _mint_token(body)
        result = _send(tmp_path, body, clearance_token=token_json, approval_id="APPROVAL-000001-004")
        assert result.get("error", {}).get("code") == "APPROVAL_REQUIRED"

    def test_mismatched_scope_rejected(self, tmp_path):
        """Artifact with scope 'other' -> rejected when server asks for 'monetary'."""
        _make_ticket(tmp_path, "000001")
        approvals_dir = self._approvals_dir(tmp_path)
        _make_approval(approvals_dir, "000001", seq="005",
                       action="send_response", scope="other")
        body = "We will issue a refund of $50."
        token_json = _mint_token(body)
        result = _send(tmp_path, body, clearance_token=token_json, approval_id="APPROVAL-000001-005")
        assert result.get("error", {}).get("code") == "APPROVAL_REQUIRED"


# ---------------------------------------------------------------------------
# VHP-1: PII scan unit tests (including normalization)
# ---------------------------------------------------------------------------

class TestPiiScan:
    def test_email_detected(self):
        assert "EMAIL" in _scan_pii("Please contact rob@example.com for details.")

    def test_ssn_detected(self):
        assert "US_SSN" in _scan_pii("SSN: 123-45-6789")

    def test_credit_card_detected(self):
        assert "CREDIT_CARD" in _scan_pii("Card number 4111111111111111 was charged.")

    def test_phone_detected(self):
        assert "PHONE" in _scan_pii("Call us at (555) 867-5309")

    def test_clean_body_no_pii(self):
        assert _scan_pii("Your ticket has been resolved. Thank you for contacting support.") == []

    def test_multiple_categories(self):
        cats = _scan_pii("Email rob@example.com SSN 123-45-6789")
        assert "EMAIL" in cats
        assert "US_SSN" in cats

    # Normalization cases
    def test_html_encoded_email_detected(self):
        """HTML-encoded @ sign -> still detected after unescape."""
        cats = _scan_pii("Contact rob&#64;example.com now")
        assert "EMAIL" in cats, f"Expected EMAIL in {cats}"

    def test_html_encoded_email_lt_gt(self):
        """&lt;email&gt; -> decoded -> detected."""
        cats = _scan_pii("Email: &lt;rob@example.com&gt;")
        assert "EMAIL" in cats, f"Expected EMAIL in {cats}"

    def test_dotted_ssn_detected(self):
        """123.45.6789 -> should be caught (dot separator stripped)."""
        cats = _scan_pii("Your SSN is 123.45.6789")
        assert "US_SSN" in cats, f"Expected US_SSN in {cats}"

    def test_dotted_credit_card_detected(self):
        """4111.1111.1111.1111 -> should be caught (dot separator stripped)."""
        cats = _scan_pii("Card: 4111.1111.1111.1111")
        assert "CREDIT_CARD" in cats, f"Expected CREDIT_CARD in {cats}"


class TestSendResponsePii:
    def test_email_in_body_blocked(self, tmp_path):
        _make_ticket(tmp_path, "000001")
        body = "Your account user@example.com has been updated."
        token_json = _mint_token(body)
        result = _send(tmp_path, body, clearance_token=token_json)
        assert result.get("error", {}).get("code") == "PII_DETECTED"
        assert "EMAIL" in result["error"]["message"]

    def test_ssn_in_body_blocked(self, tmp_path):
        _make_ticket(tmp_path, "000001")
        body = "Your SSN 123-45-6789 was found in the records."
        token_json = _mint_token(body)
        result = _send(tmp_path, body, clearance_token=token_json)
        assert result.get("error", {}).get("code") == "PII_DETECTED"
        assert "US_SSN" in result["error"]["message"]

    def test_clean_body_passes_pii_check(self, tmp_path):
        _make_ticket(tmp_path, "000001")
        body = "Your ticket has been resolved. Thank you."
        token_json = _mint_token(body)
        result = _send(tmp_path, body, clearance_token=token_json)
        assert "error" not in result, result
        assert result.get("ok") is True


# ---------------------------------------------------------------------------
# VHP-4: Money lexicon unit tests (expanded)
# ---------------------------------------------------------------------------

class TestMoneyLexicon:
    # Original terms
    def test_refund_detected(self):
        assert _is_money_commitment("We will issue a refund to your account.")

    def test_credit_detected(self):
        assert _is_money_commitment("A credit of $20 will be applied.")

    def test_reimburse_detected(self):
        assert _is_money_commitment("We will reimburse you for the charge.")

    def test_dollar_amount_detected(self):
        assert _is_money_commitment("Your refund of $50 is being processed.")

    def test_usd_amount_detected(self):
        assert _is_money_commitment("We will pay USD 100 to settle.")

    def test_we_will_pay_detected(self):
        assert _is_money_commitment("We will pay for the damages.")

    def test_discount_detected(self):
        assert _is_money_commitment("A discount has been applied.")

    def test_neutral_body_not_detected(self):
        assert not _is_money_commitment("Your ticket has been reviewed and resolved.")

    # Expanded terms (fix #4)
    def test_remit_detected(self):
        assert _is_money_commitment("We will remit fifty euros to your account.")

    def test_goodwill_detected(self):
        assert _is_money_commitment("A goodwill adjustment will be applied to your account.")

    def test_adjustment_detected(self):
        assert _is_money_commitment("An adjustment of $30 has been issued.")

    def test_send_you_detected(self):
        assert _is_money_commitment("We'll send you fifty bucks as compensation.")

    def test_euros_amount_detected(self):
        assert _is_money_commitment("We owe you 50 euros for the inconvenience.")

    def test_bucks_amount_detected(self):
        assert _is_money_commitment("Here are 20 bucks toward your next order.")

    def test_waive_detected(self):
        assert _is_money_commitment("We will waive the fee for this month.")

    # Evasion examples codex flagged (fix #4)
    def test_remit_euros_evasion(self):
        """'We will remit fifty euros' -> must require approval."""
        assert _is_money_commitment("We will remit fifty euros to your account.")

    def test_send_you_bucks_evasion(self):
        """'We'll send you fifty bucks' -> must require approval."""
        assert _is_money_commitment("We'll send you fifty bucks for the trouble.")

    def test_goodwill_adjustment_evasion(self):
        """'A goodwill adjustment will be applied' -> must require approval."""
        assert _is_money_commitment("A goodwill adjustment will be applied to your bill.")


# ---------------------------------------------------------------------------
# VHP-4: send_response — money body integration
# ---------------------------------------------------------------------------

class TestSendResponseMoneyApproval:
    def test_money_body_without_approval_blocked(self, tmp_path):
        _make_ticket(tmp_path, "000001")
        body = "We will issue a refund of $50 to your account."
        token_json = _mint_token(body)
        result = _send(tmp_path, body, clearance_token=token_json)
        assert result.get("error", {}).get("code") == "APPROVAL_REQUIRED"

    def test_money_body_with_valid_approval_allowed(self, tmp_path):
        _make_ticket(tmp_path, "000001")
        approvals_dir = tmp_path / "hearth" / "approvals"
        _make_approval(approvals_dir, "000001", action="send_response", scope="monetary")
        body = "We will issue a refund of $50 to your account."
        token_json = _mint_token(body)
        result = _send(tmp_path, body, clearance_token=token_json, approval_id="APPROVAL-000001-001")
        assert "error" not in result, result
        assert result.get("ok") is True

    def test_money_body_with_expired_approval_blocked(self, tmp_path):
        _make_ticket(tmp_path, "000001")
        approvals_dir = tmp_path / "hearth" / "approvals"
        _make_approval(approvals_dir, "000001", action="send_response", scope="monetary",
                       expires_delta=timedelta(hours=-1))
        body = "We will issue a refund of $50 to your account."
        token_json = _mint_token(body)
        result = _send(tmp_path, body, clearance_token=token_json, approval_id="APPROVAL-000001-001")
        assert result.get("error", {}).get("code") == "APPROVAL_REQUIRED"

    def test_remit_euros_requires_approval(self, tmp_path):
        _make_ticket(tmp_path, "000001")
        body = "We will remit fifty euros to your account."
        token_json = _mint_token(body)
        result = _send(tmp_path, body, clearance_token=token_json)
        assert result.get("error", {}).get("code") == "APPROVAL_REQUIRED"

    def test_goodwill_adjustment_requires_approval(self, tmp_path):
        _make_ticket(tmp_path, "000001")
        body = "A goodwill adjustment will be applied to your account."
        token_json = _mint_token(body)
        result = _send(tmp_path, body, clearance_token=token_json)
        assert result.get("error", {}).get("code") == "APPROVAL_REQUIRED"

    def test_send_you_bucks_requires_approval(self, tmp_path):
        _make_ticket(tmp_path, "000001")
        body = "We'll send you fifty bucks for the trouble."
        token_json = _mint_token(body)
        result = _send(tmp_path, body, clearance_token=token_json)
        assert result.get("error", {}).get("code") == "APPROVAL_REQUIRED"


# ---------------------------------------------------------------------------
# Enforcement order
# ---------------------------------------------------------------------------

class TestEnforcementOrder:
    def test_forbidden_actor_fires_before_clearance(self, tmp_path):
        _make_ticket(tmp_path, "000001")
        body = "Your ticket is resolved."
        token_json = _mint_token(body)
        result = _send(tmp_path, body, actor="rogue", clearance_token=token_json)
        assert result["error"]["code"] == "FORBIDDEN_ACTOR"

    def test_clearance_fires_before_pii(self, tmp_path):
        _make_ticket(tmp_path, "000001")
        body = "Email rob@example.com SSN 123-45-6789"
        with _env(HYDRA_XENIA_ROOT=str(tmp_path), XENIA_CONTEXT_SIGNING_KEY="cafebabe"):
            handlers = _tool_handlers()
            result = handlers["xenia-tickets.send_response"]({
                "ticket_id": "000001",
                "body": body,
                "actor": "hermes",
                # No clearance_token
            })
        assert result["error"]["code"] == "CLEARANCE_INVALID"

    def test_pii_fires_before_money(self, tmp_path):
        _make_ticket(tmp_path, "000001")
        body = "We will refund $50 to rob@example.com."
        token_json = _mint_token(body)
        result = _send(tmp_path, body, clearance_token=token_json)
        assert result["error"]["code"] == "PII_DETECTED"


# ---------------------------------------------------------------------------
# 2a: Raw dict token acceptance (sign.py mint() returns a dict, not a JSON str)
# ---------------------------------------------------------------------------

class TestRawDictTokenAcceptance:
    """fix 2a — verify_clearance_token accepts a raw dict without str() coercion."""

    def test_raw_sign_py_dict_verifies(self):
        """Raw dict from sign.py mint() must verify without JSON-encoding."""
        from tools.context_token.sign import mint as sign_mint
        key = "aabbccddeeff0011"
        body = "Your ticket is resolved."
        with _env(XENIA_CONTEXT_SIGNING_KEY=key):
            raw_dict = sign_mint({"body": body})
            result = verify_clearance_token(body, raw_dict)
        assert result["ok"] is True, f"raw dict should verify: {result}"

    def test_raw_clearance_dict_verifies(self):
        """Raw dict from mint_clearance_token() also verifies directly."""
        key = "deadbeef1234"
        body = "We have resolved your case."
        with _env(XENIA_CONTEXT_SIGNING_KEY=key):
            raw_dict = mint_clearance_token(body)
            assert raw_dict is not None
            result = verify_clearance_token(body, raw_dict)
        assert result["ok"] is True, f"raw clearance dict should verify: {result}"

    def test_raw_dict_wrong_body_rejected(self):
        """Raw dict minted for body A must reject body B even as a dict."""
        key = "deadbeef1234"
        body_a = "Resolved."
        body_b = "Tampered."
        with _env(XENIA_CONTEXT_SIGNING_KEY=key):
            raw_dict = mint_clearance_token(body_a)
            result = verify_clearance_token(body_b, raw_dict)
        assert result["ok"] is False

    def test_json_str_still_works(self):
        """JSON-string form must continue to work after fix 2a."""
        key = "deadbeef1234"
        body = "Your issue is resolved."
        with _env(XENIA_CONTEXT_SIGNING_KEY=key):
            raw_dict = mint_clearance_token(body)
        token_json = json.dumps(raw_dict)
        with _env(XENIA_CONTEXT_SIGNING_KEY=key):
            result = verify_clearance_token(body, token_json)
        assert result["ok"] is True


# ---------------------------------------------------------------------------
# 2b: Non-dict sig fails closed — no AttributeError crash
# ---------------------------------------------------------------------------

class TestNonDictSigFailClosed:
    """fix 2b — any non-dict sig shape must fail closed, never crash."""

    def _verify(self, sig_value, body: str = "Hello", key: str = "cafebabe") -> dict:
        token = {"body": body, "sig": sig_value}
        with _env(XENIA_CONTEXT_SIGNING_KEY=key):
            return verify_clearance_token(body, json.dumps(token))

    def test_sig_is_string_fails_closed(self):
        result = self._verify("not-a-dict")
        assert result["ok"] is False
        assert "dict" in result["reason"].lower()

    def test_sig_is_list_fails_closed(self):
        result = self._verify(["HMAC-SHA256", "somevalue"])
        assert result["ok"] is False
        assert "dict" in result["reason"].lower()

    def test_sig_is_int_fails_closed(self):
        result = self._verify(42)
        assert result["ok"] is False

    def test_sig_is_none_treated_as_absent(self):
        """sig=null -> no sig field equivalent -> fail closed."""
        token = {"body": "Hello"}  # no sig at all
        with _env(XENIA_CONTEXT_SIGNING_KEY="cafebabe"):
            result = verify_clearance_token("Hello", json.dumps(token))
        assert result["ok"] is False

    def test_sig_alg_not_string_fails_closed(self):
        """sig.alg is an int -> fail closed."""
        result = self._verify({"alg": 12345, "key_id": "k", "value": "abc"})
        assert result["ok"] is False

    def test_sig_value_not_string_fails_closed(self):
        """sig.value is a list -> fail closed."""
        result = self._verify({"alg": "HMAC-SHA256", "key_id": "k", "value": [1, 2, 3]})
        assert result["ok"] is False

    def test_top_level_list_fails_closed(self):
        """Top-level token is a JSON array -> fail closed."""
        with _env(XENIA_CONTEXT_SIGNING_KEY="cafebabe"):
            result = verify_clearance_token("Hello", json.dumps([1, 2, 3]))
        assert result["ok"] is False

    def test_top_level_string_fails_closed(self):
        """Top-level token is a bare JSON string -> fail closed."""
        with _env(XENIA_CONTEXT_SIGNING_KEY="cafebabe"):
            result = verify_clearance_token("Hello", json.dumps("just-a-string"))
        assert result["ok"] is False


# ---------------------------------------------------------------------------
# Canonicalization guard — non-serializable / malformed raw dict
# ---------------------------------------------------------------------------

class TestCanonicalizationGuard:
    """verify_clearance_token must NEVER raise on any input shape; malformed
    raw dicts (sets, circular refs, non-serializable values) -> CLEARANCE_INVALID.
    """

    def _verify_raw(self, token_dict: dict, body: str = "Hello",
                    key: str = "cafebabe") -> dict:
        """Pass a raw (possibly malformed) dict directly to verify_clearance_token."""
        with _env(XENIA_CONTEXT_SIGNING_KEY=key):
            return verify_clearance_token(body, token_dict)

    def test_set_value_fails_closed_no_exception(self):
        """Token dict with a set() value -> rejected, no TypeError/ValueError raised."""
        token = {
            "body": "Hello",
            "extra": {1, 2, 3},  # sets are not JSON-serializable
            "sig": {"alg": "HMAC-SHA256", "key_id": "default", "value": "somevalue"},
        }
        result = self._verify_raw(token)
        assert result["ok"] is False
        assert "reason" in result

    def test_non_serializable_bytes_fails_closed(self):
        """Token dict with bytes value -> rejected, no exception."""
        token = {
            "body": "Hello",
            "data": b"\xff\xfe",  # bytes are not JSON-serializable
            "sig": {"alg": "HMAC-SHA256", "key_id": "default", "value": "somevalue"},
        }
        result = self._verify_raw(token)
        assert result["ok"] is False

    def test_integer_dict_key_fails_closed(self):
        """Token dict with integer key -> json.dumps raises TypeError; must fail closed."""
        # Construct with mixed-type keys — can't use dict literal {42: ...} and str
        # keys together reliably, so build via dict()
        token = {"body": "Hello",
                 "sig": {"alg": "HMAC-SHA256", "key_id": "default", "value": "somevalue"}}
        token[42] = "integer-key"  # type: ignore[index]
        result = self._verify_raw(token)
        assert result["ok"] is False

    def test_canonical_error_reason_mentions_canonicalization(self):
        """The rejection reason for a non-serializable dict should reference canonicalization."""
        token = {
            "body": "Hello",
            "bad": {1, 2},
            "sig": {"alg": "HMAC-SHA256", "key_id": "default", "value": "x"},
        }
        result = self._verify_raw(token)
        assert result["ok"] is False
        assert "canonicalization" in result["reason"].lower()


# ---------------------------------------------------------------------------
# Outermost try/except — verify_clearance_token NEVER raises for any input
# ---------------------------------------------------------------------------

class TestVerifyNeverRaises:
    """Belt test: verify_clearance_token must return a dict for every exotic input,
    never propagate an exception, regardless of request_body or token type.
    """

    def test_request_body_none_rejected_no_raise(self):
        """request_body=None -> rejected (not a str), no AttributeError."""
        with _env(XENIA_CONTEXT_SIGNING_KEY="cafebabe"):
            result = verify_clearance_token(None, '{"body":"x","sig":{"alg":"HMAC-SHA256","key_id":"k","value":"v"}}')  # type: ignore[arg-type]
        assert result["ok"] is False
        assert "reason" in result

    def test_request_body_int_rejected_no_raise(self):
        """request_body=42 -> rejected, no TypeError."""
        with _env(XENIA_CONTEXT_SIGNING_KEY="cafebabe"):
            result = verify_clearance_token(42, '{"body":"x","sig":{"alg":"HMAC-SHA256","key_id":"k","value":"v"}}')  # type: ignore[arg-type]
        assert result["ok"] is False

    def test_request_body_bytes_rejected_no_raise(self):
        """request_body=b'hello' -> rejected, no crash."""
        with _env(XENIA_CONTEXT_SIGNING_KEY="cafebabe"):
            result = verify_clearance_token(b"hello", None)  # type: ignore[arg-type]
        assert result["ok"] is False

    def test_token_integer_rejected_no_raise(self):
        """token=42 (not str/dict) -> rejected, no crash."""
        with _env(XENIA_CONTEXT_SIGNING_KEY="cafebabe"):
            result = verify_clearance_token("hello", 42)  # type: ignore[arg-type]
        assert result["ok"] is False

    def test_raising_dict_subclass_get_rejected_no_raise(self):
        """A dict subclass whose .get() raises -> rejected, no crash.
        This exercises the outermost try/except belt.
        """
        class RaisingDict(dict):
            def get(self, key, default=None):
                if key == "sig":
                    raise RuntimeError("deliberate .get() explosion")
                return super().get(key, default)

        token = RaisingDict({"body": "hello"})
        with _env(XENIA_CONTEXT_SIGNING_KEY="cafebabe"):
            result = verify_clearance_token("hello", token)
        assert result["ok"] is False
        assert "reason" in result

    def test_key_env_var_mid_flight_cleared(self):
        """Even if _load_key raises (e.g. hex decode error on bad env), no crash."""
        # A non-hex, non-utf8-decodable env value would normally cause bytes.fromhex
        # to fall through to utf-8; verify that even a weird value doesn't raise.
        with _env(XENIA_CONTEXT_SIGNING_KEY="not-hex-but-utf8-fine"):
            # Key loaded as UTF-8 bytes — should work or fail closed, not crash
            result = verify_clearance_token("hello", '{"body":"hello","sig":{"alg":"HMAC-SHA256","key_id":"k","value":"AAAA"}}')
        assert result["ok"] is False  # HMAC mismatch, but no exception
        assert "reason" in result


# ---------------------------------------------------------------------------
# 3a: Action matching is case-sensitive
# ---------------------------------------------------------------------------

class TestActionCaseSensitive:
    """fix 3a — action field in approval artifact must match EXACTLY (case-sensitive)."""

    def _approvals_dir(self, tmp_path: Path) -> Path:
        d = tmp_path / "hearth" / "approvals"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def test_lowercase_action_matches(self, tmp_path):
        """send_response == send_response -> accepted."""
        _make_ticket(tmp_path, "000001")
        _make_approval(self._approvals_dir(tmp_path), "000001",
                       action="send_response", scope="monetary")
        body = "We will issue a refund of $50."
        token_json = _mint_token(body)
        result = _send(tmp_path, body, clearance_token=token_json,
                       approval_id="APPROVAL-000001-001")
        assert "error" not in result, result

    def test_uppercase_action_rejected(self, tmp_path):
        """Artifact action='SEND_RESPONSE' must NOT match request action='send_response'."""
        _make_ticket(tmp_path, "000001")
        approvals_dir = self._approvals_dir(tmp_path)
        # Write artifact with uppercase action
        expires_at = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        content = textwrap.dedent(f"""\
            ticket_id: 000001
            status: approved
            action: SEND_RESPONSE
            scope: monetary
            issued_by: hermes
            expires_at: {expires_at}
        """)
        (approvals_dir / "APPROVAL-000001-002.yaml").write_text(content, encoding="utf-8")
        body = "We will issue a refund of $50."
        token_json = _mint_token(body)
        result = _send(tmp_path, body, clearance_token=token_json,
                       approval_id="APPROVAL-000001-002")
        assert result.get("error", {}).get("code") == "APPROVAL_REQUIRED", \
            f"Uppercase action should be rejected: {result}"

    def test_mixedcase_action_rejected(self, tmp_path):
        """Artifact action='Send_Response' -> rejected."""
        _make_ticket(tmp_path, "000001")
        approvals_dir = self._approvals_dir(tmp_path)
        expires_at = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        content = textwrap.dedent(f"""\
            ticket_id: 000001
            status: approved
            action: Send_Response
            scope: monetary
            issued_by: hermes
            expires_at: {expires_at}
        """)
        (approvals_dir / "APPROVAL-000001-003.yaml").write_text(content, encoding="utf-8")
        body = "We will issue a refund of $50."
        token_json = _mint_token(body)
        result = _send(tmp_path, body, clearance_token=token_json,
                       approval_id="APPROVAL-000001-003")
        assert result.get("error", {}).get("code") == "APPROVAL_REQUIRED"


# ---------------------------------------------------------------------------
# 3b: approval_id is stem-only (no filename-with-extension acceptance)
# ---------------------------------------------------------------------------

class TestApprovalIdStemOnly:
    """fix 3b — approval_id must match stem exactly; passing the .yaml filename rejected."""

    def _approvals_dir(self, tmp_path: Path) -> Path:
        d = tmp_path / "hearth" / "approvals"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def test_stem_match_accepted(self, tmp_path):
        _make_ticket(tmp_path, "000001")
        _make_approval(self._approvals_dir(tmp_path), "000001",
                       action="send_response", scope="monetary")
        body = "We will issue a refund of $50."
        token_json = _mint_token(body)
        result = _send(tmp_path, body, clearance_token=token_json,
                       approval_id="APPROVAL-000001-001")
        assert "error" not in result, result

    def test_filename_with_extension_rejected(self, tmp_path):
        """Passing 'APPROVAL-000001-001.yaml' (with .yaml) must be rejected."""
        _make_ticket(tmp_path, "000001")
        _make_approval(self._approvals_dir(tmp_path), "000001",
                       action="send_response", scope="monetary")
        body = "We will issue a refund of $50."
        token_json = _mint_token(body)
        result = _send(tmp_path, body, clearance_token=token_json,
                       approval_id="APPROVAL-000001-001.yaml")
        assert result.get("error", {}).get("code") == "APPROVAL_REQUIRED", \
            f"filename-with-extension should be rejected: {result}"


# ---------------------------------------------------------------------------
# 3c: Duplicate ticket_id key in approval artifact fails closed
# ---------------------------------------------------------------------------

class TestDuplicateTicketIdKey:
    """fix 3c — duplicate ticket_id key in YAML must be sentinel-rejected."""

    def test_duplicate_ticket_id_rejected(self, tmp_path):
        """An artifact with ticket_id: 000001 / ticket_id: 000002 must fail closed."""
        _make_ticket(tmp_path, "000001")
        approvals_dir = tmp_path / "hearth" / "approvals"
        approvals_dir.mkdir(parents=True, exist_ok=True)
        expires_at = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        # Two ticket_id keys — ambiguous/potentially forged
        content = textwrap.dedent(f"""\
            ticket_id: 000001
            ticket_id: 000002
            status: approved
            action: send_response
            scope: monetary
            issued_by: hermes
            expires_at: {expires_at}
        """)
        (approvals_dir / "APPROVAL-000001-dup.yaml").write_text(content, encoding="utf-8")
        body = "We will issue a refund of $50."
        token_json = _mint_token(body)
        result = _send(tmp_path, body, clearance_token=token_json,
                       approval_id="APPROVAL-000001-dup")
        assert result.get("error", {}).get("code") == "APPROVAL_REQUIRED", \
            f"Duplicate ticket_id should be rejected: {result}"


# ---------------------------------------------------------------------------
# 5: Slash-separated and zero-width PII detection
# ---------------------------------------------------------------------------

class TestPiiSeparatorStrip:
    """fix 5 — slash and zero-width character separators are stripped before backstop scan."""

    def test_slash_separated_ssn_detected(self):
        """123/45/6789 -> US_SSN detected."""
        cats = _scan_pii("SSN: 123/45/6789")
        assert "US_SSN" in cats, f"Expected US_SSN in {cats}"

    def test_zwsp_separated_ssn_detected(self):
        """Zero-width space (U+200B) between SSN groups -> detected."""
        # Insert U+200B between groups
        cats = _scan_pii("SSN: 123​45​6789")
        assert "US_SSN" in cats, f"Expected US_SSN in {cats}"

    def test_zwnj_separated_ssn_detected(self):
        """Zero-width non-joiner (U+200C) -> detected."""
        cats = _scan_pii("SSN: 123‌45‌6789")
        assert "US_SSN" in cats, f"Expected US_SSN in {cats}"

    def test_slash_separated_card_detected(self):
        """4111/1111/1111/1111 -> CREDIT_CARD detected."""
        cats = _scan_pii("Card: 4111/1111/1111/1111")
        assert "CREDIT_CARD" in cats, f"Expected CREDIT_CARD in {cats}"

    def test_zwsp_separated_card_detected(self):
        """Zero-width space between card groups -> CREDIT_CARD detected."""
        cats = _scan_pii("Card: 4111​1111​1111​1111")
        assert "CREDIT_CARD" in cats, f"Expected CREDIT_CARD in {cats}"
