"""tests/test_capability.py

Unit tests for hydra_core.auth.capability — WS-AUTH foundation.

Covers:
- mint + verify happy path
- expired (now >= exp, strict boundary)
- wrong expected_capability
- wrong actor_kind
- tampered payload
- no key -> degraded mint + verify fail-closed
- malformed token shapes ({}, [], "x", None, int, sig not dict, value not str, bad alg, missing sig)
- exp type guard: float, float('inf'), str, bool, int-subclass -> rejected no raise (verify)
- exp exact-int at mint: bool/float/int-subclass explicit exp -> TypeError at mint
- adversarial dict SUBCLASS (even with .get raising BaseException) -> rejected by exact-type guard, no raise
- v=True fails operator verifier (type(True) is bool, not int)
- unknown/"" operator in CLI -> degraded token, not signed
- verify_operator_capability rejects actor_id "unknown" explicitly
- mint_for_approval / apply_approval builds correct payload + sets state.operator_capability
- verify_operator_capability strict verifier (rejects non-human, empty actor_id, wrong workflow)
- workflow_id / resource_id replay binding (cross-workflow token rejected)
- CLI _cmd_resume_locked wires mint (degraded when no key/unknown-op, real when key+id set)
- mint: default exp when neither exp nor ttl_seconds given (900s)
- GOLDEN VECTOR: Hydra mint produces identical sig to Xenia sign.py (CI-safe)
- LIVE INTEROP: Hydra-minted token verifies under Xenia sign.py (skipped when unavailable)
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Module under test
# ---------------------------------------------------------------------------
from hydra_core.auth.capability import (
    apply_approval,
    mint_capability,
    mint_for_approval,
    verify_capability,
    verify_operator_capability,
)
from hydra_core.state import HydraState


# ---------------------------------------------------------------------------
# Constants / helpers
# ---------------------------------------------------------------------------

TEST_KEY_HEX = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
TEST_KEY_ID = "test-key-1"

# Portable resolution: HYDRA_XENIA_ROOT env override -> sibling of the Hydra
# repo root (this file is <repo>/tests/test_capability.py, so parents[1] is the
# repo root and .parent is the AI-app base that also holds the Xenia checkout).
_XENIA_ROOT = Path(
    os.environ.get("HYDRA_XENIA_ROOT", str(Path(__file__).resolve().parents[1].parent / "Xenia"))
)
_XENIA_SIGN_PATH = _XENIA_ROOT / "tools" / "context_token"

# Golden vector — SHARED with TheEights TS suite (daemon/test/capability.test.ts).
#
# These constants are IDENTICAL to the TS golden in capability.test.ts:
#   GOLDEN_KEY_HEX, GOLDEN_KEY_ID, GOLDEN_PAYLOAD (incl. jti), GOLDEN_EXPECTED_SIG.
# Both suites assert the same sig literal, proving Python and TypeScript produce
# byte-identical canonical JSON and HMAC-SHA256 signatures for the same payload.
#
# TS source: TheEights/daemon/test/capability.test.ts (sibling repo checkout)
#   GOLDEN_PAYLOAD.jti    = "fixed-golden-jti-001"
#   GOLDEN_EXPECTED_SIG   = "vwWp9w23fYQIRQG17mR-Uw6-bXrMxzsinPkGjSJv50I"
#
# If this assertion ever fails it means Python and TS canonical formats have
# diverged — a real interop bug that MUST be fixed before either is deployed.
_GOLDEN_KEY_HEX = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
_GOLDEN_KEY_ID = "golden-1"
_GOLDEN_PAYLOAD = {
    "v": 1,
    "actor_id": "golden@hydra.test",
    "actor_kind": "human",
    "capability": "hitl_approve",
    "resource_id": "wf-golden-001",
    "workflow_id": "wf-golden-001",
    "issued_at": 1749600000,
    "exp": 1749600900,
    "jti": "fixed-golden-jti-001",   # identical to TS GOLDEN_PAYLOAD.jti
}
# Byte-identical to TS GOLDEN_EXPECTED_SIG — shared interop proof.
_GOLDEN_SIG_VALUE = "vwWp9w23fYQIRQG17mR-Uw6-bXrMxzsinPkGjSJv50I"


def _base_payload(now: int | None = None) -> dict:
    ts = now or int(time.time())
    return {
        "v": 1,
        "actor_id": "rob@example.com",
        "actor_kind": "human",
        "capability": "approval",
        "resource_id": "wf-test-001",
        "workflow_id": "wf-test-001",
        "issued_at": ts,
        "exp": ts + 900,
    }


# ---------------------------------------------------------------------------
# 1. Happy path: mint + verify
# ---------------------------------------------------------------------------

def test_mint_verify_happy_path(monkeypatch):
    monkeypatch.setenv("HYDRA_OPERATOR_KEY", TEST_KEY_HEX)
    monkeypatch.setenv("HYDRA_OPERATOR_KEY_ID", TEST_KEY_ID)

    payload = _base_payload()
    token = mint_capability(payload)

    assert token["sig"]["alg"] == "HMAC-SHA256"
    assert token["sig"]["key_id"] == TEST_KEY_ID
    assert isinstance(token["sig"]["value"], str)
    assert token["sig"].get("degraded") is None
    assert "exp" in token  # strict expiry must be present

    result = verify_capability(token, expected_capability="approval")
    assert result["valid"] is True
    assert result["reason"] == "signature valid"
    assert result["actor_id"] == "rob@example.com"
    assert result["actor_kind"] == "human"


# ---------------------------------------------------------------------------
# 2. Expired token — strict >= boundary
# ---------------------------------------------------------------------------

def test_verify_expired_at_boundary(monkeypatch):
    """now == exp must be expired (strict >=)."""
    monkeypatch.setenv("HYDRA_OPERATOR_KEY", TEST_KEY_HEX)

    ts = 1000000
    payload = {**_base_payload(now=ts), "exp": ts + 60}
    token = mint_capability(payload, now=ts)

    # exactly at exp boundary — must be expired
    result = verify_capability(token, expected_capability="approval", now=ts + 60)
    assert result["valid"] is False
    assert "expired" in result["reason"]


def test_verify_expired_past(monkeypatch):
    monkeypatch.setenv("HYDRA_OPERATOR_KEY", TEST_KEY_HEX)

    past = int(time.time()) - 7200
    payload = {**_base_payload(now=past), "exp": past + 1}
    token = mint_capability(payload, now=past)

    result = verify_capability(token, expected_capability="approval", now=int(time.time()))
    assert result["valid"] is False
    assert "expired" in result["reason"]


def test_verify_valid_just_before_exp(monkeypatch):
    """now == exp - 1 must still be valid."""
    monkeypatch.setenv("HYDRA_OPERATOR_KEY", TEST_KEY_HEX)

    ts = 1000000
    payload = {**_base_payload(now=ts), "exp": ts + 60}
    token = mint_capability(payload, now=ts)

    result = verify_capability(token, expected_capability="approval", now=ts + 59)
    assert result["valid"] is True


# ---------------------------------------------------------------------------
# 3. Wrong expected_capability
# ---------------------------------------------------------------------------

def test_verify_wrong_capability(monkeypatch):
    monkeypatch.setenv("HYDRA_OPERATOR_KEY", TEST_KEY_HEX)
    token = mint_capability(_base_payload())
    result = verify_capability(token, expected_capability="venom_execute")
    assert result["valid"] is False
    assert "capability mismatch" in result["reason"]


# ---------------------------------------------------------------------------
# 4. Wrong actor_kind
# ---------------------------------------------------------------------------

def test_verify_wrong_actor_kind(monkeypatch):
    monkeypatch.setenv("HYDRA_OPERATOR_KEY", TEST_KEY_HEX)
    token = mint_capability(_base_payload())
    result = verify_capability(
        token,
        expected_capability="approval",
        expected_actor_kind="service",
    )
    assert result["valid"] is False
    assert "actor_kind mismatch" in result["reason"]


# ---------------------------------------------------------------------------
# 5. Tampered payload -> invalid
# ---------------------------------------------------------------------------

def test_verify_tampered_payload(monkeypatch):
    monkeypatch.setenv("HYDRA_OPERATOR_KEY", TEST_KEY_HEX)
    token = mint_capability(_base_payload())
    tampered = dict(token)
    tampered["actor_id"] = "attacker@evil.com"
    result = verify_capability(tampered, expected_capability="approval")
    assert result["valid"] is False
    assert "mismatch" in result["reason"]


# ---------------------------------------------------------------------------
# 6. No key -> degraded mint + verify fail-closed
# ---------------------------------------------------------------------------

def test_no_key_degraded_mint(monkeypatch):
    monkeypatch.delenv("HYDRA_OPERATOR_KEY", raising=False)
    monkeypatch.delenv("HYDRA_OPERATOR_KEY_ID", raising=False)

    token = mint_capability(_base_payload())
    assert token["sig"]["value"] is None
    assert token["sig"].get("degraded") is True

    result = verify_capability(token, expected_capability="approval")
    assert result["valid"] is False
    assert "degraded" in result["reason"]


def test_no_key_fail_closed_on_signed_token(monkeypatch):
    """Signed token but key removed at verify time -> fail closed."""
    monkeypatch.setenv("HYDRA_OPERATOR_KEY", TEST_KEY_HEX)
    token = mint_capability(_base_payload())

    monkeypatch.delenv("HYDRA_OPERATOR_KEY", raising=False)
    result = verify_capability(token, expected_capability="approval")
    assert result["valid"] is False
    assert "no operator key" in result["reason"]


# ---------------------------------------------------------------------------
# 7. Malformed token shapes -> verify returns invalid, never raises
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad_token, label", [
    ({}, "empty dict"),
    ([], "list"),
    ("x", "string"),
    (None, "none"),
    (42, "int"),
    ({"v": 1, "sig": "not-a-dict"}, "sig not plain-dict string"),
    ({"v": 1, "sig": {"alg": "HMAC-SHA256", "key_id": "k", "value": 12345}}, "value not str"),
    ({"v": 1, "sig": {"alg": "UNKNOWN-ALG", "key_id": "k", "value": "abc"}}, "bad alg"),
    ({"v": 1}, "missing sig"),
])
def test_verify_malformed_no_raise(monkeypatch, bad_token: Any, label: str):
    monkeypatch.setenv("HYDRA_OPERATOR_KEY", TEST_KEY_HEX)
    result = verify_capability(bad_token, expected_capability="approval")
    assert result["valid"] is False, f"Expected invalid for: {label}"


# ---------------------------------------------------------------------------
# 8. exp type guard: float, Infinity, str, bool -> rejected, no raise
# ---------------------------------------------------------------------------

def _make_signed_token_with_exp(monkeypatch, exp_value: Any) -> dict:
    """Build a structurally valid HMAC-signed token with a bad exp field."""
    monkeypatch.setenv("HYDRA_OPERATOR_KEY", TEST_KEY_HEX)
    from hydra_core.auth.capability import _canonical_body, _compute_sig, _load_operator_key
    body = {
        "v": 1,
        "actor_id": "rob@example.com",
        "actor_kind": "human",
        "capability": "approval",
        "issued_at": 1000000,
        "exp": exp_value,
    }
    key_bytes, key_id = _load_operator_key()
    canonical = _canonical_body(body)
    sig_val = _compute_sig(canonical, key_bytes)
    body["sig"] = {"alg": "HMAC-SHA256", "key_id": key_id, "value": sig_val}
    return body


@pytest.mark.parametrize("bad_exp, label", [
    (float("inf"), "float infinity"),
    (1.5, "float 1.5"),
    ("123", "string 123"),
    (True, "bool True"),
    (False, "bool False"),
])
def test_verify_invalid_exp_type_no_raise(monkeypatch, bad_exp: Any, label: str):
    token = _make_signed_token_with_exp(monkeypatch, bad_exp)
    result = verify_capability(token, expected_capability="approval")
    assert result["valid"] is False, f"Expected invalid for exp={label}"
    # Must never raise.


def test_verify_missing_exp_invalid(monkeypatch):
    """exp missing -> invalid (strict expiry rule)."""
    monkeypatch.setenv("HYDRA_OPERATOR_KEY", TEST_KEY_HEX)
    from hydra_core.auth.capability import _canonical_body, _compute_sig, _load_operator_key
    body = {
        "v": 1,
        "actor_id": "rob@example.com",
        "actor_kind": "human",
        "capability": "approval",
        "issued_at": int(time.time()),
        # no "exp" field
    }
    key_bytes, key_id = _load_operator_key()
    canonical = _canonical_body(body)
    sig_val = _compute_sig(canonical, key_bytes)
    body["sig"] = {"alg": "HMAC-SHA256", "key_id": key_id, "value": sig_val}
    result = verify_capability(body, expected_capability="approval")
    assert result["valid"] is False
    assert "exp" in result["reason"]


# ---------------------------------------------------------------------------
# 9. dict subclass -> rejected by exact-type guard before any method is called
# ---------------------------------------------------------------------------

class _RaisingDictSubclass(dict):
    """A dict subclass whose .get raises RuntimeError for hostile testing."""
    def get(self, key, default=None):
        if key == "exp":
            raise RuntimeError("simulated .get explosion")
        return super().get(key, default)


class _BaseExceptionDictSubclass(dict):
    """A dict subclass whose .get raises BaseException (bypasses except Exception)."""
    def get(self, key, default=None):
        raise BaseException("malicious BaseException from .get")  # noqa: TRY002


def test_verify_dict_subclass_rejected_before_get(monkeypatch):
    """A dict subclass must be rejected by the exact-type guard before any
    .get is invoked — so even a .get that raises BaseException is never called."""
    monkeypatch.setenv("HYDRA_OPERATOR_KEY", TEST_KEY_HEX)
    bad = _RaisingDictSubclass({"v": 1, "sig": {"alg": "HMAC-SHA256", "key_id": "k", "value": "abc"}})
    # The exact-type guard rejects this before .get("exp") would be called.
    result = verify_capability(bad, expected_capability="approval")
    assert result["valid"] is False
    assert "plain dict" in result["reason"]


def test_verify_baseexception_subclass_no_propagation(monkeypatch):
    """dict subclass whose .get raises BaseException -> verify returns invalid,
    does NOT propagate BaseException (rejected by exact-type guard first)."""
    monkeypatch.setenv("HYDRA_OPERATOR_KEY", TEST_KEY_HEX)
    bad = _BaseExceptionDictSubclass()
    result = verify_capability(bad, expected_capability="approval")
    assert result["valid"] is False


def test_verify_operator_dict_subclass_rejected(monkeypatch):
    monkeypatch.setenv("HYDRA_OPERATOR_KEY", TEST_KEY_HEX)
    bad = _RaisingDictSubclass({"v": 1, "sig": {"alg": "HMAC-SHA256", "key_id": "k", "value": "abc"}})
    result = verify_operator_capability(
        bad,
        expected_capability="approval",
        expected_workflow_id="wf-x",
        expected_resource_id="wf-x",
    )
    assert result["valid"] is False
    assert "plain dict" in result["reason"]


# ---------------------------------------------------------------------------
# 9b. exp exact-int enforcement at mint time
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad_exp, label", [
    (True, "bool True"),
    (False, "bool False"),
    (1.5, "float 1.5"),
    (float("inf"), "float infinity"),
    ("123", "string"),
])
def test_mint_rejects_non_exact_int_exp(monkeypatch, bad_exp: Any, label: str):
    """Explicit non-exact-int exp must raise TypeError at mint time."""
    monkeypatch.setenv("HYDRA_OPERATOR_KEY", TEST_KEY_HEX)
    payload = {
        "v": 1, "actor_id": "rob@example.com", "actor_kind": "human",
        "capability": "approval", "exp": bad_exp,
    }
    with pytest.raises(TypeError, match="exp must be an exact int"):
        mint_capability(payload)


def test_mint_rejects_int_subclass_exp(monkeypatch):
    """An int subclass used as exp must be rejected at mint (exact-type rule)."""
    monkeypatch.setenv("HYDRA_OPERATOR_KEY", TEST_KEY_HEX)

    class _IntSubclass(int):
        pass

    payload = {
        "v": 1, "actor_id": "rob@example.com", "actor_kind": "human",
        "capability": "approval", "exp": _IntSubclass(int(time.time()) + 900),
    }
    with pytest.raises(TypeError, match="exp must be an exact int"):
        mint_capability(payload)


def test_mint_accepts_exact_int_exp(monkeypatch):
    """An exact int exp must be accepted and preserved verbatim."""
    monkeypatch.setenv("HYDRA_OPERATOR_KEY", TEST_KEY_HEX)
    ts = int(time.time())
    payload = {
        "v": 1, "actor_id": "rob@example.com", "actor_kind": "human",
        "capability": "approval", "exp": ts + 600,
    }
    token = mint_capability(payload)
    assert token["exp"] == ts + 600
    assert type(token["exp"]) is int


# ---------------------------------------------------------------------------
# 10. Workflow / resource replay-binding
# ---------------------------------------------------------------------------

def test_verify_workflow_id_binding_mismatch(monkeypatch):
    monkeypatch.setenv("HYDRA_OPERATOR_KEY", TEST_KEY_HEX)
    token = mint_capability(_base_payload())
    result = verify_capability(
        token,
        expected_capability="approval",
        expected_workflow_id="wf-other",
    )
    assert result["valid"] is False
    assert "workflow_id mismatch" in result["reason"]


def test_verify_workflow_id_binding_match(monkeypatch):
    monkeypatch.setenv("HYDRA_OPERATOR_KEY", TEST_KEY_HEX)
    token = mint_capability(_base_payload())
    result = verify_capability(
        token,
        expected_capability="approval",
        expected_workflow_id="wf-test-001",
    )
    assert result["valid"] is True


def test_verify_resource_id_binding_mismatch(monkeypatch):
    monkeypatch.setenv("HYDRA_OPERATOR_KEY", TEST_KEY_HEX)
    token = mint_capability(_base_payload())
    result = verify_capability(
        token,
        expected_capability="approval",
        expected_resource_id="wf-wrong-resource",
    )
    assert result["valid"] is False
    assert "resource_id mismatch" in result["reason"]


def test_verify_replay_token_for_other_workflow(monkeypatch):
    """Token minted for workflow A must be rejected when verified against B."""
    monkeypatch.setenv("HYDRA_OPERATOR_KEY", TEST_KEY_HEX)
    payload_a = {
        "v": 1,
        "actor_id": "rob@example.com",
        "actor_kind": "human",
        "capability": "approval",
        "resource_id": "wf-aaa",
        "workflow_id": "wf-aaa",
        "exp": int(time.time()) + 900,
    }
    token_a = mint_capability(payload_a)
    result = verify_capability(
        token_a,
        expected_capability="approval",
        expected_workflow_id="wf-bbb",
    )
    assert result["valid"] is False
    assert "workflow_id mismatch" in result["reason"]


# ---------------------------------------------------------------------------
# 10b. exp int-subclass in verify (exact-type guard via _is_valid_exp)
# ---------------------------------------------------------------------------

def test_verify_int_subclass_exp_normalized_and_valid(monkeypatch):
    """After json normalization an int subclass in exp becomes a plain int.
    The token is now valid — normalization neutralizes the subclass attack."""
    monkeypatch.setenv("HYDRA_OPERATOR_KEY", TEST_KEY_HEX)

    class _IntSubclass(int):
        pass

    from hydra_core.auth.capability import _canonical_body, _compute_sig, _load_operator_key
    ts = int(time.time())
    bad_exp = _IntSubclass(ts + 900)
    body = {
        "v": 1,
        "actor_id": "rob@example.com",
        "actor_kind": "human",
        "capability": "approval",
        "issued_at": ts,
        "exp": bad_exp,
    }
    key_bytes, key_id = _load_operator_key()
    canonical = _canonical_body(body)
    sig_val = _compute_sig(canonical, key_bytes)
    body["sig"] = {"alg": "HMAC-SHA256", "key_id": key_id, "value": sig_val}
    result = verify_capability(body, expected_capability="approval")
    # After normalization _IntSubclass -> plain int with the correct value.
    assert result["valid"] is True, (
        f"int subclass exp should pass after normalization: {result['reason']}"
    )


# ---------------------------------------------------------------------------
# 10c. v=True rejected by operator verifier (fix 4a)
# ---------------------------------------------------------------------------

def test_verify_operator_rejects_v_true(monkeypatch):
    """v=True must be rejected because type(True) is bool, not int."""
    monkeypatch.setenv("HYDRA_OPERATOR_KEY", TEST_KEY_HEX)
    # Build a token with v=True — note: True == 1 so a naïve != 1 check passes.
    from hydra_core.auth.capability import _canonical_body, _compute_sig, _load_operator_key
    ts = int(time.time())
    body = {
        "v": True,   # bool, not int
        "actor_id": "rob@example.com",
        "actor_kind": "human",
        "capability": "approval",
        "resource_id": "wf-x",
        "workflow_id": "wf-x",
        "issued_at": ts,
        "exp": ts + 900,
    }
    key_bytes, key_id = _load_operator_key()
    canonical = _canonical_body(body)
    sig_val = _compute_sig(canonical, key_bytes)
    body["sig"] = {"alg": "HMAC-SHA256", "key_id": key_id, "value": sig_val}
    result = verify_operator_capability(
        body,
        expected_capability="approval",
        expected_workflow_id="wf-x",
        expected_resource_id="wf-x",
    )
    assert result["valid"] is False
    assert "exactly int 1" in result["reason"]


# ---------------------------------------------------------------------------
# 10d. "unknown" actor_id rejected by operator verifier (fix 4b)
# ---------------------------------------------------------------------------

def test_verify_operator_rejects_unknown_actor_id(monkeypatch):
    """actor_id='unknown' (the CLI sentinel) must be rejected by the strict verifier."""
    monkeypatch.setenv("HYDRA_OPERATOR_KEY", TEST_KEY_HEX)
    payload = {
        "v": 1,
        "actor_id": "unknown",
        "actor_kind": "human",
        "capability": "approval",
        "resource_id": "wf-x",
        "workflow_id": "wf-x",
        "exp": int(time.time()) + 900,
    }
    token = mint_capability(payload)
    result = verify_operator_capability(
        token,
        expected_capability="approval",
        expected_workflow_id="wf-x",
        expected_resource_id="wf-x",
    )
    assert result["valid"] is False
    assert "actor_id" in result["reason"]


# ---------------------------------------------------------------------------
# 10e. CLI: unknown/empty operator -> degraded token (fix 4b)
# ---------------------------------------------------------------------------

def test_cli_resume_approve_unknown_operator_mints_degraded(monkeypatch, tmp_path):
    """When HYDRA_OPERATOR_ID resolves to empty or 'unknown', CLI must mint
    a degraded token (not a signed one), regardless of key availability."""
    monkeypatch.setenv("HYDRA_OPERATOR_KEY", TEST_KEY_HEX)  # key IS set
    monkeypatch.delenv("HYDRA_OPERATOR_ID", raising=False)   # but no operator id

    wf_id = "wf-cli-unknown-op"
    pending = {"workflow_id": wf_id, "gate_node": "approval", "reason": "high_risk"}
    mock_sup = _make_mock_sup(wf_id, pending)

    with patch("hydra_core.supervisor.build_supervisor", return_value=mock_sup), \
         patch("hydra_core.supervisor._PurePythonRunner", type(None)):
        from hydra_core.cli import _cmd_resume_locked
        import argparse
        args = argparse.Namespace(project=str(tmp_path), live=False, verbose=False)
        _cmd_resume_locked(args, tmp_path, wf_id, "approve", None)

    calls = mock_sup.update_state.call_args_list
    assert len(calls) >= 1
    patch_dict = calls[0][0][1]
    assert "operator_capability" in patch_dict
    cap = patch_dict["operator_capability"]
    # Even though the key is set, unknown operator must produce a degraded token.
    assert cap["sig"].get("degraded") is True, (
        "Unknown operator must produce degraded token even when key is available"
    )
    assert cap["sig"]["value"] is None


def test_cli_resume_approve_empty_operator_id_mints_degraded(monkeypatch, tmp_path):
    """HYDRA_OPERATOR_ID='' (empty string) must also produce a degraded token."""
    monkeypatch.setenv("HYDRA_OPERATOR_KEY", TEST_KEY_HEX)
    monkeypatch.setenv("HYDRA_OPERATOR_ID", "")

    wf_id = "wf-cli-empty-op"
    pending = {"workflow_id": wf_id, "gate_node": "approval", "reason": "high_risk"}
    mock_sup = _make_mock_sup(wf_id, pending)

    with patch("hydra_core.supervisor.build_supervisor", return_value=mock_sup), \
         patch("hydra_core.supervisor._PurePythonRunner", type(None)):
        from hydra_core.cli import _cmd_resume_locked
        import argparse
        args = argparse.Namespace(project=str(tmp_path), live=False, verbose=False)
        _cmd_resume_locked(args, tmp_path, wf_id, "approve", None)

    calls = mock_sup.update_state.call_args_list
    assert len(calls) >= 1
    cap = calls[0][0][1].get("operator_capability", {})
    assert cap.get("sig", {}).get("degraded") is True


# ---------------------------------------------------------------------------
# 11. verify_operator_capability strict verifier
# ---------------------------------------------------------------------------

def test_verify_operator_happy_path(monkeypatch):
    monkeypatch.setenv("HYDRA_OPERATOR_KEY", TEST_KEY_HEX)
    payload = {
        "v": 1,
        "actor_id": "rob@example.com",
        "actor_kind": "human",
        "capability": "approval",
        "resource_id": "wf-op-001",
        "workflow_id": "wf-op-001",
        "exp": int(time.time()) + 900,
    }
    token = mint_capability(payload)
    result = verify_operator_capability(
        token,
        expected_capability="approval",
        expected_workflow_id="wf-op-001",
        expected_resource_id="wf-op-001",
    )
    assert result["valid"] is True
    assert result["actor_id"] == "rob@example.com"


def test_verify_operator_rejects_non_human(monkeypatch):
    monkeypatch.setenv("HYDRA_OPERATOR_KEY", TEST_KEY_HEX)
    payload = {
        "v": 1,
        "actor_id": "svc@bot",
        "actor_kind": "service",
        "capability": "approval",
        "resource_id": "wf-x",
        "workflow_id": "wf-x",
        "exp": int(time.time()) + 900,
    }
    token = mint_capability(payload)
    result = verify_operator_capability(
        token,
        expected_capability="approval",
        expected_workflow_id="wf-x",
        expected_resource_id="wf-x",
    )
    assert result["valid"] is False
    assert "actor_kind" in result["reason"]


def test_verify_operator_rejects_empty_actor_id(monkeypatch):
    monkeypatch.setenv("HYDRA_OPERATOR_KEY", TEST_KEY_HEX)
    payload = {
        "v": 1,
        "actor_id": "",
        "actor_kind": "human",
        "capability": "approval",
        "resource_id": "wf-x",
        "workflow_id": "wf-x",
        "exp": int(time.time()) + 900,
    }
    token = mint_capability(payload)
    result = verify_operator_capability(
        token,
        expected_capability="approval",
        expected_workflow_id="wf-x",
        expected_resource_id="wf-x",
    )
    assert result["valid"] is False
    assert "actor_id" in result["reason"]


def test_verify_operator_rejects_wrong_workflow(monkeypatch):
    monkeypatch.setenv("HYDRA_OPERATOR_KEY", TEST_KEY_HEX)
    payload = {
        "v": 1,
        "actor_id": "rob@example.com",
        "actor_kind": "human",
        "capability": "approval",
        "resource_id": "wf-aaa",
        "workflow_id": "wf-aaa",
        "exp": int(time.time()) + 900,
    }
    token = mint_capability(payload)
    result = verify_operator_capability(
        token,
        expected_capability="approval",
        expected_workflow_id="wf-bbb",
        expected_resource_id="wf-aaa",
    )
    assert result["valid"] is False
    assert "workflow_id mismatch" in result["reason"]


def test_verify_operator_never_raises_on_garbage(monkeypatch):
    monkeypatch.setenv("HYDRA_OPERATOR_KEY", TEST_KEY_HEX)
    for bad in [None, [], "x", 42, {}, {"v": 2}]:
        result = verify_operator_capability(
            bad,
            expected_capability="approval",
            expected_workflow_id="wf-x",
            expected_resource_id="wf-x",
        )
        assert result["valid"] is False


# ---------------------------------------------------------------------------
# 12. mint_for_approval + apply_approval
# ---------------------------------------------------------------------------

def test_mint_for_approval_builds_payload(monkeypatch):
    monkeypatch.setenv("HYDRA_OPERATOR_KEY", TEST_KEY_HEX)
    monkeypatch.setenv("HYDRA_OPERATOR_KEY_ID", TEST_KEY_ID)

    pending_hitl = {
        "workflow_id": "wf-999",
        "reason": "high_risk",
        "gate_node": "approval",
        "summary": "Approve dispatch of engineering squad",
        "options": ["approve", "reject"],
    }
    token = mint_for_approval(
        workflow_id="wf-999",
        pending_hitl=pending_hitl,
        operator="rob@example.com",
        ttl_seconds=300,
    )

    assert token["actor_id"] == "rob@example.com"
    assert token["actor_kind"] == "human"
    assert token["capability"] == "approval"   # gate_node priority over reason
    assert token["workflow_id"] == "wf-999"
    assert token["exp"] - token["issued_at"] == 300
    assert "exp" in token

    result = verify_capability(
        token,
        expected_capability="approval",
        expected_actor_kind="human",
    )
    assert result["valid"] is True


def test_mint_for_approval_fallback_to_reason(monkeypatch):
    monkeypatch.setenv("HYDRA_OPERATOR_KEY", TEST_KEY_HEX)
    pending_hitl = {"workflow_id": "wf-888", "reason": "policy_breach"}
    token = mint_for_approval(
        workflow_id="wf-888",
        pending_hitl=pending_hitl,
        operator="ops@corp.com",
    )
    assert token["capability"] == "policy_breach"


def test_apply_approval_sets_state_field(monkeypatch):
    monkeypatch.setenv("HYDRA_OPERATOR_KEY", TEST_KEY_HEX)
    monkeypatch.setenv("HYDRA_OPERATOR_KEY_ID", TEST_KEY_ID)

    state = HydraState(root_goal="test goal")
    state.pending_hitl = {
        "workflow_id": str(state.workflow_id),
        "reason": "high_risk",
        "gate_node": "approval",
        "summary": "Approve?",
        "options": ["approve", "reject"],
    }
    assert state.operator_capability is None

    apply_approval(state, operator="rob@example.com")

    assert state.operator_capability is not None
    cap = state.operator_capability
    assert cap["actor_id"] == "rob@example.com"
    assert cap["actor_kind"] == "human"
    assert cap["capability"] == "approval"
    assert cap["sig"]["alg"] == "HMAC-SHA256"
    assert "exp" in cap

    result = verify_capability(cap, expected_capability="approval")
    assert result["valid"] is True


def test_apply_approval_no_pending_hitl(monkeypatch):
    monkeypatch.setenv("HYDRA_OPERATOR_KEY", TEST_KEY_HEX)
    state = HydraState(root_goal="no gate test")
    state.pending_hitl = None
    apply_approval(state, operator="admin@example.com")
    assert state.operator_capability is not None
    assert state.operator_capability["capability"] == "hitl_approve"


def test_apply_approval_degraded_when_no_key(monkeypatch):
    monkeypatch.delenv("HYDRA_OPERATOR_KEY", raising=False)
    state = HydraState(root_goal="degraded test")
    state.pending_hitl = {
        "workflow_id": str(state.workflow_id),
        "gate_node": "approval",
        "reason": "high_risk",
    }
    apply_approval(state, operator="ops@example.com")
    assert state.operator_capability is not None
    assert state.operator_capability["sig"]["degraded"] is True
    assert state.operator_capability["sig"]["value"] is None
    result = verify_capability(
        state.operator_capability, expected_capability="approval"
    )
    assert result["valid"] is False


# ---------------------------------------------------------------------------
# 13. CLI _cmd_resume_locked wires mint on approve
# ---------------------------------------------------------------------------

def _make_mock_sup(workflow_id: str, pending_hitl: dict):
    mock_sup = MagicMock()
    mock_sup.get_state.return_value = MagicMock(
        values={
            "pending_hitl": pending_hitl,
            "phase": "approval",
            "budget": {
                "budget_usd": 50.0, "spent_usd": 0.0,
                "token_limit": 200000, "spent_tokens": 0,
            },
        }
    )
    mock_sup.update_state.return_value = None
    mock_sup.invoke.return_value = {"phase": "done"}
    return mock_sup


def test_cli_resume_approve_mints_real_capability(monkeypatch, tmp_path):
    """When HYDRA_OPERATOR_KEY is set, approve writes a signed capability token."""
    monkeypatch.setenv("HYDRA_OPERATOR_KEY", TEST_KEY_HEX)
    monkeypatch.setenv("HYDRA_OPERATOR_KEY_ID", TEST_KEY_ID)
    monkeypatch.setenv("HYDRA_OPERATOR_ID", "cli-operator@test.com")

    wf_id = "wf-cli-test-001"
    pending = {
        "workflow_id": wf_id,
        "gate_node": "approval",
        "reason": "high_risk",
        "summary": "Approve?",
        "options": ["approve", "reject"],
    }
    mock_sup = _make_mock_sup(wf_id, pending)

    with patch("hydra_core.supervisor.build_supervisor", return_value=mock_sup), \
         patch("hydra_core.supervisor._PurePythonRunner", type(None)):
        from hydra_core.cli import _cmd_resume_locked
        import argparse
        args = argparse.Namespace(project=str(tmp_path), live=False, verbose=False)
        _cmd_resume_locked(args, tmp_path, wf_id, "approve", None)

    calls = mock_sup.update_state.call_args_list
    assert len(calls) >= 1
    patch_dict = calls[0][0][1]
    assert "operator_capability" in patch_dict, (
        "operator_capability must be in the state patch on approve"
    )
    cap = patch_dict["operator_capability"]
    assert cap["sig"]["value"] is not None
    assert cap["sig"].get("degraded") is None
    assert cap["actor_id"] == "cli-operator@test.com"
    assert cap["capability"] == "approval"


def test_cli_resume_approve_degraded_when_no_key(monkeypatch, tmp_path):
    """When HYDRA_OPERATOR_KEY is unset, approve writes a degraded token
    but does NOT block the workflow."""
    monkeypatch.delenv("HYDRA_OPERATOR_KEY", raising=False)
    monkeypatch.setenv("HYDRA_OPERATOR_ID", "ops@test.com")

    wf_id = "wf-cli-test-002"
    pending = {"workflow_id": wf_id, "gate_node": "approval", "reason": "high_risk"}
    mock_sup = _make_mock_sup(wf_id, pending)

    with patch("hydra_core.supervisor.build_supervisor", return_value=mock_sup), \
         patch("hydra_core.supervisor._PurePythonRunner", type(None)):
        from hydra_core.cli import _cmd_resume_locked
        import argparse
        args = argparse.Namespace(project=str(tmp_path), live=False, verbose=False)
        _cmd_resume_locked(args, tmp_path, wf_id, "approve", None)

    calls = mock_sup.update_state.call_args_list
    assert len(calls) >= 1
    patch_dict = calls[0][0][1]
    assert "operator_capability" in patch_dict
    cap = patch_dict["operator_capability"]
    assert cap["sig"].get("degraded") is True
    assert cap["sig"]["value"] is None


def test_cli_resume_reject_does_not_mint(monkeypatch, tmp_path):
    """reject action must NOT write operator_capability."""
    monkeypatch.setenv("HYDRA_OPERATOR_KEY", TEST_KEY_HEX)

    wf_id = "wf-cli-test-003"
    pending = {"workflow_id": wf_id, "gate_node": "approval", "reason": "high_risk"}
    mock_sup = _make_mock_sup(wf_id, pending)

    with patch("hydra_core.supervisor.build_supervisor", return_value=mock_sup), \
         patch("hydra_core.supervisor._PurePythonRunner", type(None)):
        from hydra_core.cli import _cmd_resume_locked
        import argparse
        args = argparse.Namespace(project=str(tmp_path), live=False, verbose=False)
        _cmd_resume_locked(args, tmp_path, wf_id, "reject", None)

    for call in mock_sup.update_state.call_args_list:
        call_args = call[0]
        call_patch = call_args[1] if len(call_args) > 1 else {}
        assert "operator_capability" not in call_patch, (
            "reject must not write operator_capability"
        )


# ---------------------------------------------------------------------------
# 14. mint: ttl_seconds, default exp
# ---------------------------------------------------------------------------

def test_mint_ttl_seconds(monkeypatch):
    monkeypatch.setenv("HYDRA_OPERATOR_KEY", TEST_KEY_HEX)
    now = int(time.time())
    payload = {
        "v": 1, "actor_id": "svc@hydra", "actor_kind": "service",
        "capability": "dispatch", "ttl_seconds": 60,
    }
    token = mint_capability(payload, now=now)
    assert token["exp"] == now + 60
    assert "ttl_seconds" not in token


def test_mint_default_exp(monkeypatch):
    """No exp, no ttl_seconds -> exp = issued_at + 900."""
    monkeypatch.setenv("HYDRA_OPERATOR_KEY", TEST_KEY_HEX)
    now = int(time.time())
    payload = {
        "v": 1, "actor_id": "svc@hydra", "actor_kind": "service",
        "capability": "dispatch",
    }
    token = mint_capability(payload, now=now)
    assert "exp" in token
    assert token["exp"] == now + 900


def test_mint_missing_required_field(monkeypatch):
    monkeypatch.setenv("HYDRA_OPERATOR_KEY", TEST_KEY_HEX)
    with pytest.raises(ValueError, match="missing required fields"):
        mint_capability({"v": 1, "actor_id": "x"})


def test_mint_does_not_mutate_caller(monkeypatch):
    monkeypatch.setenv("HYDRA_OPERATOR_KEY", TEST_KEY_HEX)
    original = _base_payload()
    original_copy = dict(original)
    mint_capability(original)
    assert original == original_copy


# ---------------------------------------------------------------------------
# 15. GOLDEN VECTOR — CI-safe byte-identical interop proof
# ---------------------------------------------------------------------------

def test_golden_vector_hydra_produces_same_sig(monkeypatch):
    """Hydra mint must produce the SAME sig value as Xenia sign.py for the
    golden payload — proves byte-identical canonical format without Xenia path."""
    monkeypatch.setenv("HYDRA_OPERATOR_KEY", _GOLDEN_KEY_HEX)
    monkeypatch.setenv("HYDRA_OPERATOR_KEY_ID", _GOLDEN_KEY_ID)

    token = mint_capability(_GOLDEN_PAYLOAD)
    assert token["sig"]["value"] == _GOLDEN_SIG_VALUE, (
        f"Golden vector mismatch: got {token['sig']['value']!r}, "
        f"expected {_GOLDEN_SIG_VALUE!r}"
    )


def test_golden_vector_hydra_verify_accepts(monkeypatch):
    """Pre-built golden token must pass verify_capability."""
    monkeypatch.setenv("HYDRA_OPERATOR_KEY", _GOLDEN_KEY_HEX)
    monkeypatch.setenv("HYDRA_OPERATOR_KEY_ID", _GOLDEN_KEY_ID)

    token = dict(_GOLDEN_PAYLOAD)
    token["sig"] = {
        "alg": "HMAC-SHA256",
        "key_id": _GOLDEN_KEY_ID,
        "value": _GOLDEN_SIG_VALUE,
    }
    result = verify_capability(
        token,
        expected_capability="hitl_approve",
        now=_GOLDEN_PAYLOAD["issued_at"] + 1,  # inside validity window
    )
    assert result["valid"] is True, f"Golden verify failed: {result['reason']}"


def test_golden_vector_tampered_rejected(monkeypatch):
    monkeypatch.setenv("HYDRA_OPERATOR_KEY", _GOLDEN_KEY_HEX)
    token = dict(_GOLDEN_PAYLOAD)
    token["actor_id"] = "injected@evil.com"
    token["sig"] = {
        "alg": "HMAC-SHA256",
        "key_id": _GOLDEN_KEY_ID,
        "value": _GOLDEN_SIG_VALUE,
    }
    result = verify_capability(
        token,
        expected_capability="hitl_approve",
        now=_GOLDEN_PAYLOAD["issued_at"] + 1,
    )
    assert result["valid"] is False


# ---------------------------------------------------------------------------
# 15b. jti — mint auto-generates; verify returns verified jti; golden payload
# ---------------------------------------------------------------------------

def test_mint_auto_generates_jti(monkeypatch):
    """mint_capability auto-generates a jti when absent in the payload."""
    monkeypatch.setenv("HYDRA_OPERATOR_KEY", TEST_KEY_HEX)
    payload = _base_payload()
    assert "jti" not in payload
    token = mint_capability(payload)
    assert "jti" in token
    assert isinstance(token["jti"], str)
    assert len(token["jti"]) > 0


def test_mint_preserves_explicit_jti(monkeypatch):
    """mint_capability preserves an explicit jti supplied by the caller."""
    monkeypatch.setenv("HYDRA_OPERATOR_KEY", TEST_KEY_HEX)
    payload = {**_base_payload(), "jti": "my-fixed-nonce-abc123"}
    token = mint_capability(payload)
    assert token["jti"] == "my-fixed-nonce-abc123"


def test_mint_rejects_empty_jti(monkeypatch):
    """mint_capability raises TypeError for an explicit empty jti."""
    monkeypatch.setenv("HYDRA_OPERATOR_KEY", TEST_KEY_HEX)
    payload = {**_base_payload(), "jti": ""}
    with pytest.raises(TypeError, match="jti must be a non-empty str"):
        mint_capability(payload)


def test_mint_rejects_non_str_jti(monkeypatch):
    """mint_capability raises TypeError for a non-str jti."""
    monkeypatch.setenv("HYDRA_OPERATOR_KEY", TEST_KEY_HEX)
    payload = {**_base_payload(), "jti": 12345}
    with pytest.raises(TypeError, match="jti must be a non-empty str"):
        mint_capability(payload)


def test_verify_returns_verified_jti(monkeypatch):
    """verify_capability returns the jti from the HMAC-verified token."""
    monkeypatch.setenv("HYDRA_OPERATOR_KEY", TEST_KEY_HEX)
    payload = {**_base_payload(), "jti": "test-jti-12345"}
    token = mint_capability(payload)
    result = verify_capability(token, expected_capability="approval")
    assert result["valid"] is True
    assert result["jti"] == "test-jti-12345"


def test_verify_auto_jti_returned(monkeypatch):
    """verify_capability returns the auto-generated jti from the verified token."""
    monkeypatch.setenv("HYDRA_OPERATOR_KEY", TEST_KEY_HEX)
    token = mint_capability(_base_payload())
    assert "jti" in token  # auto-generated
    result = verify_capability(token, expected_capability="approval")
    assert result["valid"] is True
    assert result["jti"] == token["jti"]
    assert isinstance(result["jti"], str)
    assert len(result["jti"]) > 0


def test_verify_jti_none_on_failure(monkeypatch):
    """verify_capability returns jti=None on any verification failure."""
    monkeypatch.setenv("HYDRA_OPERATOR_KEY", TEST_KEY_HEX)
    token = mint_capability(_base_payload())
    # Tamper to cause failure
    tampered = dict(token)
    tampered["actor_id"] = "evil@attacker.com"
    result = verify_capability(tampered, expected_capability="approval")
    assert result["valid"] is False
    assert result["jti"] is None


def test_verify_golden_payload_returns_jti(monkeypatch):
    """Golden payload (with fixed jti) verifies and returns the fixed jti."""
    monkeypatch.setenv("HYDRA_OPERATOR_KEY", _GOLDEN_KEY_HEX)
    monkeypatch.setenv("HYDRA_OPERATOR_KEY_ID", _GOLDEN_KEY_ID)
    token = dict(_GOLDEN_PAYLOAD)
    token["sig"] = {
        "alg": "HMAC-SHA256",
        "key_id": _GOLDEN_KEY_ID,
        "value": _GOLDEN_SIG_VALUE,
    }
    result = verify_capability(
        token,
        expected_capability="hitl_approve",
        now=_GOLDEN_PAYLOAD["issued_at"] + 1,
    )
    assert result["valid"] is True
    # jti value is identical to TS GOLDEN_PAYLOAD.jti — shared interop proof.
    assert result["jti"] == "fixed-golden-jti-001"


def test_verify_old_token_no_jti_still_valid(monkeypatch):
    """A token without jti (old format, pre-Run-C) must still verify (jti is optional).
    verify returns jti=None for tokens that predate jti."""
    monkeypatch.setenv("HYDRA_OPERATOR_KEY", TEST_KEY_HEX)
    from hydra_core.auth.capability import _canonical_body, _compute_sig, _load_operator_key
    ts = int(time.time())
    body = {
        "v": 1,
        "actor_id": "rob@example.com",
        "actor_kind": "human",
        "capability": "approval",
        "resource_id": "wf-test-001",
        "workflow_id": "wf-test-001",
        "issued_at": ts,
        "exp": ts + 900,
        # No jti field — old token format
    }
    key_bytes, key_id = _load_operator_key()
    canonical = _canonical_body(body)
    sig_val = _compute_sig(canonical, key_bytes)
    body["sig"] = {"alg": "HMAC-SHA256", "key_id": key_id, "value": sig_val}
    result = verify_capability(body, expected_capability="approval")
    assert result["valid"] is True, f"Old token without jti should still verify: {result['reason']}"
    assert result["jti"] is None  # absent -> None


def test_verify_operator_capability_returns_jti(monkeypatch):
    """verify_operator_capability also returns the verified jti."""
    monkeypatch.setenv("HYDRA_OPERATOR_KEY", TEST_KEY_HEX)
    payload = {
        "v": 1,
        "actor_id": "rob@example.com",
        "actor_kind": "human",
        "capability": "approval",
        "resource_id": "wf-op-jti",
        "workflow_id": "wf-op-jti",
        "exp": int(time.time()) + 900,
        "jti": "op-jti-abc",
    }
    token = mint_capability(payload)
    result = verify_operator_capability(
        token,
        expected_capability="approval",
        expected_workflow_id="wf-op-jti",
        expected_resource_id="wf-op-jti",
    )
    assert result["valid"] is True
    assert result["jti"] == "op-jti-abc"


# ---------------------------------------------------------------------------
# 17. Hostile subclasses — normalization architecture (json round-trip)
#
# Both inner verifiers run json.loads(json.dumps(token)) immediately after
# the type(token) is dict guard.  This strips ALL Python subclasses to plain
# builtins: str subclass -> plain str, int subclass -> plain int, bool stays
# bool, None stays None.  The C JSON encoder uses the string VALUE for str
# subclasses and never calls __repr__/__eq__/__bool__/__str__ on the object.
# After normalization every dunder override is gone; comparisons are honest.
# The exact-type gates (type(x) is str/int/dict) remain as belt-and-suspenders
# for structurally missing / wrong-type fields.
# ---------------------------------------------------------------------------

class _HostileStr(str):
    """str subclass: __eq__/__ne__ always match; strip() returns self.
    After json normalization these overrides are stripped — comparison uses
    the plain str VALUE, which is the correct security property."""

    def __eq__(self, other):  # type: ignore[override]
        return True   # always "equal" — bypasses != if normalization absent

    def __ne__(self, other):  # type: ignore[override]
        return False  # never "not equal"

    def strip(self):
        return self


class _BaseExceptionStr(str):
    """str subclass: comparison methods raise BaseException.
    After json normalization this subclass is gone — plain str compared
    honestly without calling these methods."""

    def __eq__(self, other):  # type: ignore[override]
        raise BaseException("hostile __eq__")  # noqa: TRY002

    def __ne__(self, other):  # type: ignore[override]
        raise BaseException("hostile __ne__")  # noqa: TRY002

    def strip(self):
        raise BaseException("hostile strip")  # noqa: TRY002


def _make_hostile_token(monkeypatch, *, field: str, cls=_HostileStr) -> dict:
    """Mint a real HMAC-valid token, then replace *field* with a hostile
    str subclass.  Canonical bytes are IDENTICAL so HMAC verifies."""
    monkeypatch.setenv("HYDRA_OPERATOR_KEY", TEST_KEY_HEX)
    monkeypatch.setenv("HYDRA_OPERATOR_KEY_ID", TEST_KEY_ID)
    token = mint_capability(_base_payload())
    hostile = cls(token.get(field, ""))
    result = dict(token)
    result[field] = hostile
    from hydra_core.auth.capability import _load_operator_key
    import json as _json, base64, hmac as _hmac, hashlib
    key_bytes, key_id = _load_operator_key()
    body_for_sign = {k: v for k, v in result.items() if k != "sig"}
    canonical = _json.dumps(body_for_sign, sort_keys=True,
                            separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    digest = _hmac.new(key_bytes, canonical, hashlib.sha256).digest()
    sig_val = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    result["sig"] = {"alg": "HMAC-SHA256", "key_id": key_id, "value": sig_val}
    return result


def test_verify_hostile_str_workflow_id_mismatch_after_normalization(monkeypatch):
    """HostileStr workflow_id 'wf-aaa', expected 'wf-bbb':
    after normalization honest comparison -> mismatch -> invalid.
    The __eq__=True override is GONE after json round-trip."""
    monkeypatch.setenv("HYDRA_OPERATOR_KEY", TEST_KEY_HEX)
    monkeypatch.setenv("HYDRA_OPERATOR_KEY_ID", TEST_KEY_ID)
    ts = int(time.time())
    import json as _json, base64, hmac as _hmac, hashlib
    from hydra_core.auth.capability import _load_operator_key
    payload = {
        "v": 1, "actor_id": "rob@example.com", "actor_kind": "human",
        "capability": "approval", "resource_id": "wf-aaa",
        "workflow_id": _HostileStr("wf-aaa"),
        "exp": ts + 900, "issued_at": ts,
    }
    key_bytes, key_id = _load_operator_key()
    body_for_sign = {k: v for k, v in payload.items()}
    canonical = _json.dumps(body_for_sign, sort_keys=True,
                            separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    digest = _hmac.new(key_bytes, canonical, hashlib.sha256).digest()
    sig_val = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    payload["sig"] = {"alg": "HMAC-SHA256", "key_id": key_id, "value": sig_val}
    result = verify_capability(
        payload, expected_capability="approval", expected_workflow_id="wf-bbb",
    )
    assert result["valid"] is False, (
        "HostileStr __eq__=True must NOT bypass workflow_id binding after normalization"
    )
    assert "workflow_id mismatch" in result["reason"]


def test_verify_hostile_str_workflow_id_match_after_normalization(monkeypatch):
    """HostileStr workflow_id 'wf-aaa', expected 'wf-aaa':
    after normalization honest comparison -> match -> valid."""
    monkeypatch.setenv("HYDRA_OPERATOR_KEY", TEST_KEY_HEX)
    monkeypatch.setenv("HYDRA_OPERATOR_KEY_ID", TEST_KEY_ID)
    ts = int(time.time())
    import json as _json, base64, hmac as _hmac, hashlib
    from hydra_core.auth.capability import _load_operator_key
    payload = {
        "v": 1, "actor_id": "rob@example.com", "actor_kind": "human",
        "capability": "approval", "resource_id": "wf-aaa",
        "workflow_id": _HostileStr("wf-aaa"),
        "exp": ts + 900, "issued_at": ts,
    }
    key_bytes, key_id = _load_operator_key()
    body_for_sign = {k: v for k, v in payload.items()}
    canonical = _json.dumps(body_for_sign, sort_keys=True,
                            separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    digest = _hmac.new(key_bytes, canonical, hashlib.sha256).digest()
    sig_val = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    payload["sig"] = {"alg": "HMAC-SHA256", "key_id": key_id, "value": sig_val}
    result = verify_capability(
        payload, expected_capability="approval", expected_workflow_id="wf-aaa",
    )
    assert result["valid"] is True, (
        f"HostileStr matching text should be valid after normalization: {result['reason']}"
    )


def test_verify_sig_degraded_hostile_bool_no_propagation(monkeypatch):
    """sig['degraded'] whose __bool__ raises BaseException must not propagate.
    json normalization strips the hostile subclass before the `is True` check."""
    monkeypatch.setenv("HYDRA_OPERATOR_KEY", TEST_KEY_HEX)
    monkeypatch.setenv("HYDRA_OPERATOR_KEY_ID", TEST_KEY_ID)

    class _HostileBool(int):
        def __bool__(self):  # type: ignore[override]
            raise BaseException("hostile __bool__ on degraded flag")

    token = mint_capability(_base_payload())
    sig_copy = dict(token["sig"])
    # _HostileBool(1) serializes as JSON 1 (integer), normalized to int 1.
    # The `is True` check then sees int 1, not True, so degraded is False — token valid.
    sig_copy["degraded"] = _HostileBool(1)
    token = dict(token)
    token["sig"] = sig_copy
    # Must not raise BaseException from __bool__.
    result = verify_capability(token, expected_capability="approval")
    assert isinstance(result, dict)
    assert "valid" in result


def test_verify_sig_degraded_true_hostile_bool_no_propagation(monkeypatch):
    """sig['degraded']=True (plain) in a degraded token: fail closed safely."""
    monkeypatch.delenv("HYDRA_OPERATOR_KEY", raising=False)
    token = mint_capability(_base_payload())  # degraded mint
    assert token["sig"].get("degraded") is True
    result = verify_capability(token, expected_capability="approval")
    assert result["valid"] is False
    assert "degraded" in result["reason"]


def test_verify_baseexception_str_actor_id_no_propagation(monkeypatch):
    """_BaseExceptionStr in actor_id: normalization strips to plain str
    before any comparison — no BaseException propagates."""
    token = _make_hostile_token(monkeypatch, field="actor_id", cls=_BaseExceptionStr)
    result = verify_capability(token, expected_capability="approval")
    # No BaseException propagated — normalization is the defence.
    assert isinstance(result, dict)
    assert "valid" in result


def test_verify_repr_raises_str_no_propagation_via_normalization(monkeypatch):
    """str subclass with __repr__/__str__ raising BaseException:
    normalization strips to plain str — __repr__ never called."""
    monkeypatch.setenv("HYDRA_OPERATOR_KEY", TEST_KEY_HEX)
    monkeypatch.setenv("HYDRA_OPERATOR_KEY_ID", TEST_KEY_ID)

    class _ReprRaisesStr(str):
        def __repr__(self):  # type: ignore[override]
            raise BaseException("hostile __repr__")
        def __str__(self):  # type: ignore[override]
            raise BaseException("hostile __str__")
        def __eq__(self, other):  # type: ignore[override]
            return True
        def __ne__(self, other):  # type: ignore[override]
            return False

    token = mint_capability(_base_payload())
    token["actor_id"] = _ReprRaisesStr("rob@example.com")
    result = verify_capability(token, expected_capability="approval")
    # After normalization actor_id is plain "rob@example.com" — no __repr__ called.
    assert isinstance(result, dict)
    assert "valid" in result


# ---------------------------------------------------------------------------
# 18. Hostile dict/list SUBCLASSES — BaseException from json.dumps traversal
#
# The outermost wrapper in both verify_capability and verify_operator_capability
# is now `except BaseException` (re-raising KeyboardInterrupt/SystemExit).
# This covers the last attack vector: a dict or list subclass whose overridden
# __iter__ / keys / items / __len__ raises BaseException during json.dumps
# traversal — which escapes a bare `except Exception` guard.
# ---------------------------------------------------------------------------

class _HostileDictSubclass(dict):
    """dict subclass: keys() raises BaseException during json.dumps traversal."""
    def keys(self):
        raise BaseException("hostile keys() on dict subclass")  # noqa: TRY002

    def items(self):
        raise BaseException("hostile items() on dict subclass")  # noqa: TRY002

    def __iter__(self):
        raise BaseException("hostile __iter__ on dict subclass")  # noqa: TRY002


class _HostileListSubclass(list):
    """list subclass: __iter__ raises BaseException during json.dumps traversal."""
    def __iter__(self):
        raise BaseException("hostile __iter__ on list subclass")  # noqa: TRY002

    def __len__(self):
        raise BaseException("hostile __len__ on list subclass")  # noqa: TRY002


def test_verify_hostile_dict_subclass_token_fails_closed(monkeypatch):
    """A dict subclass passed as the token raises BaseException during json.dumps
    traversal — verify must return {valid: False}, not propagate."""
    monkeypatch.setenv("HYDRA_OPERATOR_KEY", TEST_KEY_HEX)
    # type(token) is not dict guard fires first here; but test the full path too.
    bad = _HostileDictSubclass({"v": 1})
    result = verify_capability(bad, expected_capability="approval")
    assert result["valid"] is False
    # No BaseException propagated.


def test_verify_nested_hostile_dict_subclass_fails_closed(monkeypatch):
    """A plain dict token containing a nested dict subclass value raises
    BaseException during json.dumps traversal of the nested value —
    verify must return {valid: False}, not propagate."""
    monkeypatch.setenv("HYDRA_OPERATOR_KEY", TEST_KEY_HEX)
    monkeypatch.setenv("HYDRA_OPERATOR_KEY_ID", TEST_KEY_ID)

    # Build a structurally plausible token with a nested hostile value.
    # We insert the hostile object AFTER the type(token) is dict check
    # would pass (token itself is a plain dict) but INSIDE a field so
    # json.dumps traversal will hit it.
    token = {
        "v": 1,
        "actor_id": "rob@example.com",
        "actor_kind": "human",
        "capability": "approval",
        "resource_id": "wf-test-001",
        "workflow_id": "wf-test-001",
        "issued_at": 1000000,
        "exp": 1000000 + 900,
        "sig": _HostileDictSubclass({"alg": "HMAC-SHA256", "key_id": "k", "value": "abc"}),
    }
    result = verify_capability(token, expected_capability="approval")
    assert result["valid"] is False
    # No BaseException propagated.


def test_verify_nested_hostile_list_subclass_fails_closed(monkeypatch):
    """A plain dict token with a nested list subclass value: BaseException
    during json.dumps -> verify fails closed, no propagation."""
    monkeypatch.setenv("HYDRA_OPERATOR_KEY", TEST_KEY_HEX)
    token = {
        "v": 1,
        "actor_id": "rob@example.com",
        "actor_kind": "human",
        "capability": "approval",
        "scopes": _HostileListSubclass(["read", "write"]),  # extra field
        "issued_at": 1000000,
        "exp": 1000000 + 900,
        "sig": {"alg": "HMAC-SHA256", "key_id": "k", "value": "abc"},
    }
    result = verify_capability(token, expected_capability="approval")
    assert result["valid"] is False
    # No BaseException propagated.


def test_verify_keyboard_interrupt_propagates(monkeypatch):
    """KeyboardInterrupt raised during json.dumps traversal of a nested value
    must NOT be swallowed — the outermost wrapper re-raises it."""
    monkeypatch.setenv("HYDRA_OPERATOR_KEY", TEST_KEY_HEX)
    monkeypatch.setenv("HYDRA_OPERATOR_KEY_ID", TEST_KEY_ID)

    class _KIList(list):
        """list subclass: __iter__ raises KeyboardInterrupt.
        Placed as a nested value in a plain-dict token so the type(token) is dict
        guard passes, but json.dumps hits it during field traversal."""
        def __iter__(self):
            raise KeyboardInterrupt
        def __len__(self):
            return 1  # non-zero so json encoder enters __iter__

    # Plain dict (passes type guard), but contains a hostile nested list.
    token = {
        "v": 1,
        "actor_id": "rob@example.com",
        "actor_kind": "human",
        "capability": "approval",
        "scopes": _KIList(["read"]),  # extra field, reached by json.dumps
        "issued_at": 1000000,
        "exp": 1000000 + 900,
        "sig": {"alg": "HMAC-SHA256", "key_id": TEST_KEY_ID, "value": "abc"},
    }
    with pytest.raises(KeyboardInterrupt):
        verify_capability(token, expected_capability="approval")


def test_verify_system_exit_propagates(monkeypatch):
    """SystemExit raised during json.dumps traversal must propagate."""
    monkeypatch.setenv("HYDRA_OPERATOR_KEY", TEST_KEY_HEX)
    monkeypatch.setenv("HYDRA_OPERATOR_KEY_ID", TEST_KEY_ID)

    class _SEList(list):
        def __iter__(self):
            raise SystemExit(1)
        def __len__(self):
            return 1

    token = {
        "v": 1,
        "actor_id": "rob@example.com",
        "actor_kind": "human",
        "capability": "approval",
        "scopes": _SEList(["read"]),
        "issued_at": 1000000,
        "exp": 1000000 + 900,
        "sig": {"alg": "HMAC-SHA256", "key_id": TEST_KEY_ID, "value": "abc"},
    }
    with pytest.raises(SystemExit):
        verify_capability(token, expected_capability="approval")



# ---------------------------------------------------------------------------
# 19. Static catch-all reason — hostile metaclass __name__ cannot propagate
# ---------------------------------------------------------------------------

def test_verify_catchall_reason_is_static_string(monkeypatch):
    """The outermost except BaseException handler must return the static reason
    string "verification error" — never f"verification error: {type(exc).__name__}"
    or any other interpolation of attacker-influenced data.

    We verify this by inducing a BaseException via a hostile nested structure
    and asserting the exact static reason is returned."""
    monkeypatch.setenv("HYDRA_OPERATOR_KEY", TEST_KEY_HEX)
    monkeypatch.setenv("HYDRA_OPERATOR_KEY_ID", TEST_KEY_ID)

    class _HostileIterList(list):
        """list subclass: __iter__ raises BaseException (not KI/SE) during
        json.dumps traversal of nested field."""
        def __iter__(self):
            raise BaseException("hostile nested iteration")  # noqa: TRY002
        def __len__(self):
            return 1

    token = {
        "v": 1,
        "actor_id": "rob@example.com",
        "actor_kind": "human",
        "capability": "approval",
        "extra": _HostileIterList(["x"]),
        "issued_at": 1000000,
        "exp": 1000000 + 900,
        "sig": {"alg": "HMAC-SHA256", "key_id": TEST_KEY_ID, "value": "abc"},
    }
    result = verify_capability(token, expected_capability="approval")
    assert result["valid"] is False
    # Reason must be the EXACT static string — no interpolation.
    assert result["reason"] == "verification error", (
        f"catch-all must return static reason, got: {result['reason']!r}"
    )


def test_verify_catchall_hostile_metaclass_name_no_propagation(monkeypatch):
    """A hostile metaclass whose __name__ property raises BaseException:
    the catch-all handler must NOT evaluate type(exc).__name__ — it uses the
    static reason string instead, so __name__ is never called."""
    monkeypatch.setenv("HYDRA_OPERATOR_KEY", TEST_KEY_HEX)
    monkeypatch.setenv("HYDRA_OPERATOR_KEY_ID", TEST_KEY_ID)

    class _HostileMeta(type):
        @property
        def __name__(cls):  # type: ignore[override]
            raise BaseException("hostile metaclass __name__")

    # We need to construct an exception whose type has _HostileMeta as metaclass.
    # The BaseException subclass itself is what gets raised during processing.
    class _HostileExc(BaseException, metaclass=_HostileMeta):
        pass

    class _HostileIterList(list):
        """Raises the metaclass-hostile exception during json.dumps traversal."""
        def __iter__(self):
            raise _HostileExc("trigger")
        def __len__(self):
            return 1

    token = {
        "v": 1,
        "actor_id": "rob@example.com",
        "actor_kind": "human",
        "capability": "approval",
        "extra": _HostileIterList(["x"]),
        "issued_at": 1000000,
        "exp": 1000000 + 900,
        "sig": {"alg": "HMAC-SHA256", "key_id": TEST_KEY_ID, "value": "abc"},
    }
    # Must return invalid with static reason — no BaseException from __name__.
    result = verify_capability(token, expected_capability="approval")
    assert result["valid"] is False
    assert result["reason"] == "verification error"


# ---------------------------------------------------------------------------
# 16. LIVE INTEROP — requires Xenia sign.py on disk (skipped in CI otherwise)
# ---------------------------------------------------------------------------

def _xenia_sign_available() -> bool:
    return _XENIA_SIGN_PATH.exists()


@pytest.mark.skipif(
    not _xenia_sign_available(),
    reason="Xenia sign.py not found at expected path",
)
def test_interop_hydra_mint_verifies_under_xenia(monkeypatch):
    monkeypatch.setenv("HYDRA_OPERATOR_KEY", TEST_KEY_HEX)
    monkeypatch.setenv("HYDRA_OPERATOR_KEY_ID", TEST_KEY_ID)
    monkeypatch.setenv("XENIA_CONTEXT_SIGNING_KEY", TEST_KEY_HEX)

    if str(_XENIA_SIGN_PATH) not in sys.path:
        sys.path.insert(0, str(_XENIA_SIGN_PATH))
    import sign as xenia_sign  # noqa: PLC0415

    payload = _base_payload()
    token = mint_capability(payload)

    xenia_result = xenia_sign.verify(token)
    assert xenia_result["valid"] is True, (
        f"Xenia rejected Hydra-minted token: {xenia_result['reason']}"
    )
    hydra_result = verify_capability(token, expected_capability="approval")
    assert hydra_result["valid"] is True


@pytest.mark.skipif(
    not _xenia_sign_available(),
    reason="Xenia sign.py not found at expected path",
)
def test_interop_tamper_rejected_by_both(monkeypatch):
    monkeypatch.setenv("HYDRA_OPERATOR_KEY", TEST_KEY_HEX)
    monkeypatch.setenv("HYDRA_OPERATOR_KEY_ID", TEST_KEY_ID)
    monkeypatch.setenv("XENIA_CONTEXT_SIGNING_KEY", TEST_KEY_HEX)

    if str(_XENIA_SIGN_PATH) not in sys.path:
        sys.path.insert(0, str(_XENIA_SIGN_PATH))
    import sign as xenia_sign  # noqa: PLC0415

    payload = _base_payload()
    token = mint_capability(payload)
    tampered = dict(token)
    tampered["actor_id"] = "injected@evil.com"

    xenia_result = xenia_sign.verify(tampered)
    assert xenia_result["valid"] is False

    hydra_result = verify_capability(tampered, expected_capability="approval")
    assert hydra_result["valid"] is False
