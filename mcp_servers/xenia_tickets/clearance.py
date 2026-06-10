"""xenia_tickets.clearance — server-side clearance-token verification.

Mirrors the HMAC-SHA256 scheme from Xenia tools/context_token/sign.py EXACTLY.

Token contract (matches sign.py end-to-end)
--------------------------------------------
A clearance token is a JSON-encoded portable-context token dict produced by
sign.py's `mint()` function.  It contains a `"body"` field holding the
customer-facing response text and a `"sig"` envelope:

  {
    "body": "<the response text>",
    ... (any other context fields)
    "sig": {
      "alg":    "HMAC-SHA256",
      "key_id": "<key_id>",
      "value":  "<base64url-no-pad HMAC digest>"
    }
  }

sign.py canonicalization (mirrored verbatim):
  canonical = json.dumps(token_dict_minus_sig, sort_keys=True,
                          separators=(',', ':')).encode('utf-8')
  sig.value = base64url_nopad( HMAC-SHA256(key_bytes, canonical) )

The server:
  1. Parses the JSON clearance_token string into a dict.
  2. Verifies the sig envelope using sign.py's exact canonicalization.
  3. Asserts token["body"] == the request body (constant-time).
  4. Rejects: no key, degraded/unsigned token, shape mismatch, body mismatch.

Key source
----------
XENIA_CONTEXT_SIGNING_KEY  (env var) — identical to sign.py:
  hex-encoded preferred; falls back to UTF-8 bytes.

Fail-closed contract
--------------------
- No key configured                     -> CLEARANCE_INVALID
- Token missing/empty                   -> CLEARANCE_INVALID
- Token not valid JSON or not a dict    -> CLEARANCE_INVALID
- sig field absent or degraded          -> CLEARANCE_INVALID (degraded = bypass attempt)
- sig.alg != HMAC-SHA256                -> CLEARANCE_INVALID
- HMAC mismatch                         -> CLEARANCE_INVALID
- token["body"] != request body         -> CLEARANCE_INVALID
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os

# Mirror sign.py constants exactly.
_SIG_FIELD = "sig"
_ALG = "HMAC-SHA256"


# ---------------------------------------------------------------------------
# Key loading — identical to sign.py's _load_key()
# ---------------------------------------------------------------------------

def _load_key() -> tuple[bytes | None, str]:
    """Return (key_bytes, key_id).  key_bytes is None when no key is configured.
    Logic is identical to sign.py's _load_key().
    """
    raw = os.environ.get("XENIA_CONTEXT_SIGNING_KEY", "")
    if not raw:
        return None, ""
    key_id = os.environ.get("XENIA_CONTEXT_KEY_ID", "default")
    try:
        key_bytes = bytes.fromhex(raw)
    except ValueError:
        key_bytes = raw.encode("utf-8")
    return key_bytes, key_id


# ---------------------------------------------------------------------------
# Canonicalization — identical to sign.py's _canonical_body()
# ---------------------------------------------------------------------------

def _canonical_body(token_dict: dict) -> bytes:
    """Stable canonical-JSON byte string of the token, excluding 'sig'.
    Mirrors sign.py _canonical_body() exactly:
      json.dumps(body_minus_sig, sort_keys=True, separators=(',',':')).encode('utf-8')
    """
    body = {k: v for k, v in token_dict.items() if k != _SIG_FIELD}
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")


# ---------------------------------------------------------------------------
# HMAC computation — identical to sign.py's _compute_sig()
# ---------------------------------------------------------------------------

def _compute_sig(canonical: bytes, key_bytes: bytes) -> str:
    """Return base64url-encoded HMAC-SHA256 digest (no padding).
    Mirrors sign.py _compute_sig() exactly.
    """
    digest = hmac.new(key_bytes, canonical, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def mint_clearance_token(body: str, extra_fields: dict | None = None) -> dict | None:
    """Mint a signed clearance token dict for *body* using sign.py's scheme.

    The returned dict has the form:
      {"body": body, ...extra_fields, "sig": {"alg": ..., "key_id": ..., "value": ...}}

    Returns None if XENIA_CONTEXT_SIGNING_KEY is not configured (caller should
    treat this as a warning; the server will reject the missing token).

    The returned dict should be JSON-serialised and passed as clearance_token to
    send_response.  This mirrors sign.py's mint() exactly.
    """
    key_bytes, key_id = _load_key()
    if key_bytes is None:
        return None

    token: dict = {"body": body}
    if extra_fields:
        token.update({k: v for k, v in extra_fields.items() if k not in ("body", _SIG_FIELD)})

    canonical = _canonical_body(token)
    sig_value = _compute_sig(canonical, key_bytes)
    token[_SIG_FIELD] = {
        "alg": _ALG,
        "key_id": key_id or "default",
        "value": sig_value,
    }
    return token


VerifyResult = dict  # {"ok": bool, "reason": str}


def verify_clearance_token(request_body: str, clearance_token_json: str | None) -> VerifyResult:
    """Verify that *clearance_token_json* is a valid signed clearance token for
    *request_body*, using sign.py's exact canonicalization scheme.

    The token is a JSON-encoded dict produced by sign.py's mint() with a "body"
    field and a "sig" envelope.  Verification steps:
      1. Parse JSON.
      2. Check sig envelope shape and algorithm.
      3. Recompute HMAC over canonical(token_dict) and compare (constant-time).
      4. Assert token["body"] == request_body (constant-time on UTF-8).
      5. Reject degraded/unsigned tokens (no bypass allowed).

    Always fails closed:
      - No key configured        -> invalid (cannot verify)
      - Missing/empty token      -> invalid
      - JSON parse error         -> invalid
      - Degraded/unsigned sig    -> invalid (degraded = potential bypass attempt)
      - Algorithm mismatch       -> invalid
      - HMAC mismatch            -> invalid
      - body field mismatch      -> invalid

    Returns {"ok": bool, "reason": str}.

    This function NEVER raises for any input shape or type.  A last-resort
    outermost try/except catches anything the inner guards missed so callers
    always receive a CLEARANCE_INVALID dict, never an exception.
    """
    # Guard request_body type before entering the main logic: a non-string
    # request_body would cause .encode() to raise AttributeError later.
    if not isinstance(request_body, str):
        return {
            "ok": False,
            "reason": (
                f"request_body must be a str, got {type(request_body).__name__!r} — fail closed"
            ),
        }

    try:
        return _verify_clearance_token_inner(request_body, clearance_token_json)
    except Exception as exc:  # last-resort belt over all inner guards
        return {
            "ok": False,
            "reason": f"clearance_token verification error (unexpected): {exc}",
        }


def _verify_clearance_token_inner(
    request_body: str, clearance_token_json: object
) -> VerifyResult:
    """Inner implementation — called only from verify_clearance_token's try block."""
    key_bytes, _ = _load_key()
    if key_bytes is None:
        return {
            "ok": False,
            "reason": (
                "XENIA_CONTEXT_SIGNING_KEY not configured; "
                "cannot verify clearance token — fail closed"
            ),
        }

    # 2a: Accept raw dict (as sign.py mint() returns) OR a JSON string.
    # Check isinstance BEFORE any str() coercion so a raw dict is never
    # accidentally stringified to "{'body':...}" and then rejected.
    if isinstance(clearance_token_json, dict):
        token_dict = clearance_token_json
    else:
        # Treat as string; guard missing/empty before parsing.
        if not clearance_token_json or not str(clearance_token_json).strip():
            return {"ok": False, "reason": "clearance_token missing or empty"}
        raw = str(clearance_token_json).strip()
        try:
            token_dict = json.loads(raw)
        except (json.JSONDecodeError, ValueError) as exc:
            return {"ok": False, "reason": f"clearance_token JSON parse error: {exc}"}

    # Top-level must be a dict regardless of input form.
    if not isinstance(token_dict, dict):
        return {"ok": False, "reason": "clearance_token must be a JSON object (dict)"}

    # Extract sig envelope.
    sig_envelope = token_dict.get(_SIG_FIELD)
    if sig_envelope is None:
        return {
            "ok": False,
            "reason": "clearance_token has no 'sig' field — unsigned token not accepted (fail closed)",
        }

    # 2b: Validate sig is a dict before ANY .get() access — a non-dict sig
    # (string, list, int, …) must fail closed, not crash with AttributeError.
    if not isinstance(sig_envelope, dict):
        return {
            "ok": False,
            "reason": (
                f"clearance_token 'sig' field must be a dict, "
                f"got {type(sig_envelope).__name__!r} — fail closed"
            ),
        }

    # Validate sig field types before access (2b continued).
    sig_alg_raw = sig_envelope.get("alg")
    sig_value_raw = sig_envelope.get("value")
    if not isinstance(sig_alg_raw, str):
        return {"ok": False, "reason": "clearance_token sig.alg is missing or not a string — fail closed"}
    if sig_value_raw is not None and not isinstance(sig_value_raw, str):
        return {"ok": False, "reason": "clearance_token sig.value is not a string — fail closed"}

    # Degraded or null value — reject; this is a potential bypass attempt.
    if sig_envelope.get("degraded") or sig_value_raw is None:
        return {
            "ok": False,
            "reason": (
                "clearance_token is in degraded/unsigned mode (sig.degraded=True or sig.value=null); "
                "degraded tokens are not accepted by the server — fail closed"
            ),
        }

    # Algorithm check.
    if sig_alg_raw != _ALG:
        return {"ok": False, "reason": f"clearance_token unsupported algorithm: {sig_alg_raw!r} (expected HMAC-SHA256)"}

    # Recompute canonical over token_dict (excluding sig) — identical to sign.py.
    # Guard against non-serializable values (sets, circular refs, mixed-type keys,
    # etc.) in a raw dict so json.dumps never propagates an exception out of verify.
    try:
        canonical = _canonical_body(token_dict)
        expected_sig = _compute_sig(canonical, key_bytes)
    except Exception as exc:
        return {
            "ok": False,
            "reason": f"clearance_token canonicalization failed — malformed token dict: {exc}",
        }
    actual_sig: str = sig_value_raw  # already validated as str above

    # Constant-time HMAC comparison (mirrors sign.py line 188).
    try:
        sig_ok = hmac.compare_digest(expected_sig, actual_sig)
    except TypeError:
        return {"ok": False, "reason": "clearance_token sig.value type error in constant-time compare"}

    if not sig_ok:
        return {
            "ok": False,
            "reason": "clearance_token HMAC mismatch — possible tampering or wrong key",
        }

    # Body binding: the token must have been minted for this exact request body.
    token_body = token_dict.get("body", "")
    if not isinstance(token_body, str):
        return {"ok": False, "reason": "clearance_token 'body' field is missing or not a string"}

    # Constant-time string comparison on UTF-8 bytes.
    try:
        body_ok = hmac.compare_digest(
            request_body.encode("utf-8"),
            token_body.encode("utf-8"),
        )
    except TypeError:
        return {"ok": False, "reason": "clearance_token body comparison type error"}

    if not body_ok:
        return {
            "ok": False,
            "reason": (
                "clearance_token body mismatch — token was minted for a different body "
                "(body may have been tampered after signing)"
            ),
        }

    return {"ok": True, "reason": "clearance token valid"}
