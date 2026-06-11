"""hydra_core/auth/capability.py

Operator-capability token format for Hydra.

Canonical format is byte-identical to Xenia's tools/context_token/sign.py so
that one token is verifiable by both systems when they share the same key:

    canonical = json.dumps({all fields except "sig"}, sort_keys=True,
                            separators=(",",":"), ensure_ascii=True).encode("utf-8")
    sig_value = base64url-nopad(HMAC-SHA256(key, canonical))
    envelope:  token["sig"] = {"alg": "HMAC-SHA256", "key_id": <id>,
                                "value": <b64url-nopad>}
    degraded:  token["sig"] = {"alg": "HMAC-SHA256", "key_id": <id>,
                                "value": None, "degraded": True}

Environment variables
---------------------
HYDRA_OPERATOR_KEY     : hex-encoded (preferred) or UTF-8 key material.
                         DISTINCT from XENIA_CONTEXT_SIGNING_KEY.
HYDRA_OPERATOR_KEY_ID  : optional key identifier (default "default").

Key material is NEVER written to the token, the repo, or any log.

Stdlib-only (hashlib, hmac, json, base64, os, time).
No provider SDK imports — keeps hydra_core runtime-agnostic.

Degraded-mode approval posture (WS-AUTH foundation run)
---------------------------------------------------------
When HYDRA_OPERATOR_KEY is unset, mint_for_approval / apply_approval still
produce a token (degraded, sig.value=None). The approval itself is NOT
blocked — that is the WS-AUTH foundation posture: we instrument first,
enforce in consumers later (TheEights/Xenia in runs B/C). The caller (CLI
resume, hitl-gate) logs a warning so operators know the token carries no
cryptographic proof. Downstream verify_capability and
verify_operator_capability reject degraded tokens fail-closed; consumers that
call those functions will refuse to honour the gate. In run A (this run),
consumers are not yet wired, so the workflow proceeds normally.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import time
from typing import Any

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SIG_FIELD = "sig"
_ALG = "HMAC-SHA256"

# Required payload fields for a capability token.
_REQUIRED_FIELDS = frozenset({
    "v",
    "actor_id",
    "actor_kind",
    "capability",
})


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _load_operator_key() -> tuple[bytes | None, str]:
    """Return (key_bytes, key_id).

    key_bytes is None when HYDRA_OPERATOR_KEY is unset (degraded mode).
    Accepts hex-encoded key (preferred — allows arbitrary bytes) or plain
    UTF-8, matching Xenia sign.py _load_key behaviour.
    The key is NEVER embedded in any return value.
    """
    raw = os.environ.get("HYDRA_OPERATOR_KEY", "")
    if not raw:
        return None, "default"
    key_id = os.environ.get("HYDRA_OPERATOR_KEY_ID", "default")
    try:
        key_bytes = bytes.fromhex(raw)
    except ValueError:
        key_bytes = raw.encode("utf-8")
    return key_bytes, key_id


def _canonical_body(token_dict: dict) -> bytes:
    """Produce stable canonical-JSON bytes, excluding the 'sig' field.

    Stability guarantees (mirror Xenia sign.py _canonical_body):
    - json.dumps with sort_keys=True  -> deterministic key order
    - separators=(',', ':')           -> no whitespace variation
    - ensure_ascii=True (default)     -> no encoding ambiguity
    """
    body = {k: v for k, v in token_dict.items() if k != _SIG_FIELD}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def _compute_sig(canonical: bytes, key_bytes: bytes) -> str:
    """Return base64url-encoded HMAC-SHA256 digest (no padding)."""
    digest = hmac.new(key_bytes, canonical, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _is_valid_exp(value: Any) -> bool:
    """Return True iff value is EXACTLY int (not bool, not float, not str,
    not any int subclass).

    We use `type(x) is int` (exact type check) rather than isinstance so that:
    - bool is rejected  (type(True) is bool, not int)
    - int subclasses are rejected (hostile subclasses with overridden __eq__
      or __ge__ would otherwise bypass the >= expiry comparison)
    - float, str, float('inf') are rejected
    Only the exact builtin `int` passes.
    """
    return type(value) is int


# ---------------------------------------------------------------------------
# Public API — mint
# ---------------------------------------------------------------------------

def mint_capability(payload: dict, *, now: int | None = None) -> dict:
    """Sign *payload* and return a new dict with a 'sig' envelope appended.

    Payload fields
    --------------
    Required: v, actor_id, actor_kind, capability
    Optional: resource_id, workflow_id, issued_at, exp, ttl_seconds

    Timing / expiry (strict)
    ------------------------
    exp MUST be present in the resulting token (WS-AUTH strict-expiry rule).
    Resolution order:
      1. Caller passes explicit ``exp`` (int) — used verbatim.
      2. Caller passes ``ttl_seconds`` — exp = issued_at + ttl_seconds.
      3. Neither — exp = issued_at + 900 (15-min default).
    ``ttl_seconds`` is consumed internally and NOT written to the token.
    ``issued_at`` is set to *now* if absent.

    The caller's dict is never mutated.

    Degraded mode
    -------------
    When HYDRA_OPERATOR_KEY is unset the token is returned with
    sig.degraded=True so downstream callers detect the situation without
    blocking. See module docstring for the WS-AUTH foundation posture.

    Raises
    ------
    ValueError  — if any required field is missing.
    TypeError   — if payload is not a dict.
    """
    if not isinstance(payload, dict):
        raise TypeError(f"payload must be a dict, got {type(payload).__name__}")

    missing = _REQUIRED_FIELDS - set(payload)
    if missing:
        raise ValueError(f"mint_capability: missing required fields: {sorted(missing)}")

    # Work on a copy; never mutate the caller's dict.
    result: dict[str, Any] = {k: v for k, v in payload.items() if k != _SIG_FIELD}

    # Strip internal helper field — not part of the token wire format.
    ttl = result.pop("ttl_seconds", None)

    ts_now = now if now is not None else int(time.time())
    if "issued_at" not in result:
        result["issued_at"] = ts_now

    # Strict expiry: exp MUST be present and EXACTLY int.
    # If the caller supplied an explicit exp, enforce exact-int type now
    # (before signing) so a bool/float/str exp is rejected at mint time rather
    # than silently embedded in a token that verify will later reject.
    if "exp" in result:
        if not _is_valid_exp(result["exp"]):
            raise TypeError(
                f"exp must be an exact int (type int, not bool/float/str/subclass), "
                f"got {type(result['exp']).__name__}"
            )
    else:
        if ttl is not None:
            result["exp"] = result["issued_at"] + int(ttl)
        else:
            result["exp"] = result["issued_at"] + 900  # 15-min default

    key_bytes, key_id = _load_operator_key()

    if key_bytes is None:
        result[_SIG_FIELD] = {
            "alg": _ALG,
            "key_id": key_id,
            "value": None,
            "degraded": True,
        }
        return result

    canonical = _canonical_body(result)
    sig_value = _compute_sig(canonical, key_bytes)
    result[_SIG_FIELD] = {
        "alg": _ALG,
        "key_id": key_id,
        "value": sig_value,
    }
    return result


# ---------------------------------------------------------------------------
# Public API — verify_capability (lower-level; expected_workflow_id /
# expected_resource_id are optional — use verify_operator_capability for
# strict enforcement in gated consumers)
# ---------------------------------------------------------------------------

def verify_capability(
    token: Any,
    *,
    expected_capability: str,
    expected_actor_kind: str | None = None,
    expected_workflow_id: str | None = None,
    expected_resource_id: str | None = None,
    now: int | None = None,
) -> dict:
    """Verify a capability token.

    Returns
    -------
    dict with keys:
      valid      : bool
      reason     : str
      actor_id   : str | None
      actor_kind : str | None

    Fail-closed on every error path including:
    - Non-dict / malformed token
    - No operator key configured
    - Degraded token (sig.value is None)
    - sig not a dict
    - sig.alg != "HMAC-SHA256"
    - sig.value not a str
    - Constant-time HMAC mismatch
    - Expired: now >= exp (strict >=)
    - exp missing or not a plain int (not bool/float/str/Infinity)
    - token.capability != expected_capability
    - token.actor_kind != expected_actor_kind (when provided)
    - token.workflow_id != expected_workflow_id (when provided)
    - token.resource_id != expected_resource_id (when provided)
    - Canonicalization error

    NEVER raises for any input shape. All exceptions are caught at the
    outermost level and returned as {"valid": False, "reason": "..."}.
    """
    try:
        return _verify_capability_inner(
            token,
            expected_capability=expected_capability,
            expected_actor_kind=expected_actor_kind,
            expected_workflow_id=expected_workflow_id,
            expected_resource_id=expected_resource_id,
            now=now,
        )
    except BaseException as exc:  # noqa: BLE001
        # Re-raise genuine interrupts; fail closed on all other BaseExceptions
        # (including hostile subclasses whose __iter__/keys/items raises
        # BaseException during json.dumps traversal).
        # STATIC reason string only — never interpolate exc or type(exc).__name__
        # (a hostile metaclass could make __name__ raise).
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        return {"valid": False, "reason": "verification error",
                "actor_id": None, "actor_kind": None}


def _verify_capability_inner(
    token: Any,
    *,
    expected_capability: str,
    expected_actor_kind: str | None,
    expected_workflow_id: str | None,
    expected_resource_id: str | None,
    now: int | None,
) -> dict:
    def _fail(reason: str, actor_id: str | None = None, actor_kind: str | None = None) -> dict:
        return {"valid": False, "reason": reason, "actor_id": actor_id, "actor_kind": actor_kind}

    # Exact-type guard: require type(token) is dict (not a subclass).
    # dict subclasses can override .get/.items to inject arbitrary behaviour;
    # rejecting them up front removes the subclass-method-injection vector
    # entirely, before any token field is touched.
    if type(token) is not dict:
        return _fail("token is not a plain dict")

    # Blanket normalization: json round-trip strips ALL subclasses (str, int,
    # dict, list, bool) to plain builtins in one pass.  The C JSON encoder uses
    # the string VALUE for str subclasses (never calls __repr__/__eq__/__bool__);
    # non-serializable objects raise TypeError, caught here.  After this line
    # every value in token is a plain builtin — no overridden dunder can run
    # during subsequent comparisons, truth-tests, or format strings.
    # Note: json round-trip of plain types is identity, so the canonical signing
    # bytes and the HMAC verification are unchanged for legitimate tokens.
    try:
        token = json.loads(json.dumps(token))
    except Exception:
        return _fail("token is not plain-JSON-serializable")

    actor_id = token.get("actor_id")
    actor_kind = token.get("actor_kind")

    # Belt-and-suspenders exact str-type guards (after normalization these
    # are also safety nets for missing or wrong-type fields, not subclass blocks).
    if type(actor_id) is not str:
        return _fail("actor_id is not a plain str", actor_id, actor_kind)
    if type(actor_kind) is not str:
        return _fail("actor_kind is not a plain str", actor_id, actor_kind)

    # Structural check: sig must be present and EXACTLY a dict.
    sig_envelope = token.get(_SIG_FIELD)
    if sig_envelope is None:
        return _fail("missing sig envelope", actor_id, actor_kind)
    if type(sig_envelope) is not dict:
        return _fail("sig is not a plain dict", actor_id, actor_kind)

    # Degraded token: fail closed.  After normalization sig_envelope["degraded"]
    # is a plain bool or absent — use `is True` for exact match (not truthiness).
    if sig_envelope.get("degraded") is True or sig_envelope.get("value") is None:
        return _fail("degraded token: no key was configured at mint time", actor_id, actor_kind)

    # Algorithm: exact str-type guard before any comparison.
    alg = sig_envelope.get("alg", "")
    if type(alg) is not str:
        return _fail("sig.alg is not a plain str", actor_id, actor_kind)
    if alg != _ALG:
        return _fail("unsupported algorithm (not HMAC-SHA256)", actor_id, actor_kind)

    # sig.value must be EXACTLY a str (base64url) — exact-type guard prevents
    # str-subclass compare_digest bypass.
    sig_value = sig_envelope.get("value")
    if type(sig_value) is not str:
        return _fail("sig.value is not a plain str", actor_id, actor_kind)

    # We need the operator key to verify a non-degraded token.
    key_bytes, _key_id = _load_operator_key()
    if key_bytes is None:
        return _fail("no operator key configured for verification", actor_id, actor_kind)

    # Constant-time HMAC verification.
    try:
        canonical = _canonical_body(token)
        expected_sig = _compute_sig(canonical, key_bytes)
    except Exception as exc:  # noqa: BLE001
        return _fail(f"canonicalization error: {exc}", actor_id, actor_kind)

    if not hmac.compare_digest(expected_sig, sig_value):
        return _fail("signature mismatch (possible tampering)", actor_id, actor_kind)

    # Strict expiry: exp MUST be present and a plain int (not bool/float/str).
    exp = token.get("exp")
    if exp is None:
        return _fail("exp field missing", actor_id, actor_kind)
    if not _is_valid_exp(exp):
        return _fail(f"exp is not a valid integer (got {type(exp).__name__})", actor_id, actor_kind)

    ts_now = now if now is not None else int(time.time())
    if ts_now >= exp:  # strict >=: expired at the boundary instant
        return _fail("token expired", actor_id, actor_kind)

    # Capability check: exact str-type guard before comparison.
    token_capability = token.get("capability")
    if type(token_capability) is not str:
        return _fail("capability is not a plain str", actor_id, actor_kind)
    if token_capability != expected_capability:
        return _fail("capability mismatch", actor_id, actor_kind)

    # Optional actor kind check.
    if expected_actor_kind is not None and actor_kind != expected_actor_kind:
        return _fail("actor_kind mismatch", actor_id, actor_kind)

    # Optional workflow_id binding (fix #2 — prevent cross-workflow replay).
    if expected_workflow_id is not None:
        token_wf = token.get("workflow_id")
        if type(token_wf) is not str:
            return _fail("workflow_id is not a plain str", actor_id, actor_kind)
        if token_wf != expected_workflow_id:
            return _fail("workflow_id mismatch", actor_id, actor_kind)

    # Optional resource_id binding.
    if expected_resource_id is not None:
        token_res = token.get("resource_id")
        if type(token_res) is not str:
            return _fail("resource_id is not a plain str", actor_id, actor_kind)
        if token_res != expected_resource_id:
            return _fail("resource_id mismatch", actor_id, actor_kind)

    return {"valid": True, "reason": "signature valid", "actor_id": actor_id, "actor_kind": actor_kind}


# ---------------------------------------------------------------------------
# Public API — verify_operator_capability (strict; for gated consumers)
# ---------------------------------------------------------------------------

def verify_operator_capability(
    token: Any,
    *,
    expected_capability: str,
    expected_workflow_id: str,
    expected_resource_id: str,
    now: int | None = None,
) -> dict:
    """Strict operator-capability verifier for gated consumers (runs B/C).

    Unlike verify_capability, this function unconditionally requires:
      - token.v == 1
      - token.actor_id is a non-empty str
      - token.actor_kind == "human"
      - token.capability == expected_capability
      - token.workflow_id == expected_workflow_id  (replay-prevention)
      - token.resource_id == expected_resource_id  (replay-prevention)
      - valid, non-expired HMAC-SHA256 signature (now >= exp is expired)

    There are no opt-out parameters for the workflow/resource binding.
    The expected_workflow_id and expected_resource_id are required positional
    kwargs — callers must supply them explicitly to prevent accidental omission.

    Returns
    -------
    dict with keys: valid, reason, actor_id, actor_kind  (same schema as
    verify_capability so callers can use either function interchangeably).

    NEVER raises for any input shape.
    """
    try:
        return _verify_operator_capability_inner(
            token,
            expected_capability=expected_capability,
            expected_workflow_id=expected_workflow_id,
            expected_resource_id=expected_resource_id,
            now=now,
        )
    except BaseException as exc:  # noqa: BLE001
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        return {"valid": False, "reason": "verification error",
                "actor_id": None, "actor_kind": None}


def _verify_operator_capability_inner(
    token: Any,
    *,
    expected_capability: str,
    expected_workflow_id: str,
    expected_resource_id: str,
    now: int | None,
) -> dict:
    def _fail(reason: str, actor_id: str | None = None, actor_kind: str | None = None) -> dict:
        return {"valid": False, "reason": reason, "actor_id": actor_id, "actor_kind": actor_kind}

    # Exact-type guard (same as _verify_capability_inner).
    if type(token) is not dict:
        return _fail("token is not a plain dict")

    # Blanket normalization (same rationale as _verify_capability_inner).
    # Normalizing here as well so the operator-specific checks below (v, actor_id
    # sentinel) also operate on plain builtins, not subclasses.
    try:
        token = json.loads(json.dumps(token))
    except Exception:
        return _fail("token is not plain-JSON-serializable")

    actor_id = token.get("actor_id")
    actor_kind = token.get("actor_kind")

    # v must be EXACTLY int 1 (type(True) is bool, so True == 1 does NOT pass).
    # After normalization True stays bool (JSON true -> Python True), so the
    # type(v) is not int guard remains effective.
    v = token.get("v")
    if type(v) is not int or v != 1:
        return _fail(f"token.v must be exactly int 1, got type {type(v).__name__}", actor_id, actor_kind)

    # actor_id must be EXACTLY a non-empty str, and must NOT be the sentinel
    # "unknown" (which is the fallback when operator identity could not be
    # resolved). Issuing a valid human capability for an unidentified operator
    # would allow any approval to be self-authorized without a real identity.
    # Use type(x) is str (exact) — not isinstance — so that str subclasses
    # with hostile __eq__/strip are rejected before those methods are called.
    _REJECTED_ACTOR_IDS = {"", "unknown"}
    if type(actor_id) is not str:
        return _fail(
            f"actor_id is not a plain str (got {type(actor_id).__name__})",
            actor_id, actor_kind,
        )
    if actor_id.strip() in _REJECTED_ACTOR_IDS:
        return _fail("actor_id must be a non-empty str and not a sentinel value", actor_id, actor_kind)

    # actor_kind MUST be EXACTLY str "human" — exact-type guard first.
    if type(actor_kind) is not str or actor_kind != "human":
        return _fail("actor_kind must be 'human'", actor_id, actor_kind)

    # Delegate to the inner lower-level verifier with all bindings required.
    return _verify_capability_inner(
        token,
        expected_capability=expected_capability,
        expected_actor_kind="human",
        expected_workflow_id=expected_workflow_id,
        expected_resource_id=expected_resource_id,
        now=now,
    )


# ---------------------------------------------------------------------------
# Gate-mint integration
# ---------------------------------------------------------------------------

def mint_for_approval(
    *,
    workflow_id: str,
    pending_hitl: dict,
    operator: str,
    ttl_seconds: int = 900,
) -> dict:
    """Mint an operator-capability token for a HITL approval event.

    Derives the capability name from ``pending_hitl`` in this priority order:
    1. pending_hitl["capability"]      — explicit capability field
    2. pending_hitl["gate_node"]        — e.g. "approval", "judge_per_squad"
    3. pending_hitl["reason"]           — e.g. "high_risk", "policy_breach"
    4. "hitl_approve"                   — generic fallback

    The resource_id is derived from:
    1. pending_hitl["resource_id"]
    2. pending_hitl["proposal_id"]
    3. pending_hitl["workflow_id"]
    4. workflow_id argument

    When HYDRA_OPERATOR_KEY is unset, returns a degraded token (sig.value=None).
    The approval is NOT blocked — see module docstring for posture rationale.
    Callers (cli.py _cmd_resume_locked) log a warning in that case.

    Parameters
    ----------
    workflow_id:
        Workflow ID string (typically str(state.workflow_id)).
    pending_hitl:
        The pending_hitl dict from HydraState.
    operator:
        Identity of the human operator approving (e.g. email or username).
    ttl_seconds:
        Token lifetime in seconds (default 900 = 15 min).

    Returns
    -------
    Signed capability token dict (or degraded if no key configured).
    """
    capability = (
        pending_hitl.get("capability")
        or pending_hitl.get("gate_node")
        or pending_hitl.get("reason")
        or "hitl_approve"
    )
    resource_id = (
        pending_hitl.get("resource_id")
        or pending_hitl.get("proposal_id")
        or pending_hitl.get("workflow_id")
        or workflow_id
    )

    ts_now = int(time.time())
    payload: dict[str, Any] = {
        "v": 1,
        "actor_id": operator,
        "actor_kind": "human",
        "capability": str(capability),
        "resource_id": str(resource_id) if resource_id is not None else None,
        "workflow_id": str(workflow_id),
        "issued_at": ts_now,
        "exp": ts_now + ttl_seconds,
    }
    return mint_capability(payload, now=ts_now)


# ---------------------------------------------------------------------------
# Approval-path seam
# ---------------------------------------------------------------------------

def apply_approval(state: Any, operator: str, *, ttl_seconds: int = 900) -> Any:
    """Mint an operator-capability token for the current HITL gate and stash
    it on *state.operator_capability*.

    This is the canonical Python seam invoked by:
      - hydra_core/cli.py _cmd_resume_locked on action="approve"
      - The hydra-hitl-gate agent on /hydra:approve

    Side-effect-free beyond mutating state — no network calls, no MCP, no LLM.

    Degraded-mode behaviour: when HYDRA_OPERATOR_KEY is unset the token is
    degraded (sig.value=None). This function still succeeds and stores the
    degraded token; the caller is responsible for warning the operator (cli.py
    does this). The approval is not blocked in this run (foundation posture).

    Parameters
    ----------
    state:
        HydraState instance.  Must have ``workflow_id`` and ``pending_hitl``
        attributes.  The ``operator_capability`` field will be set on return.
    operator:
        Identity string of the approving operator.
    ttl_seconds:
        Lifetime for the minted token (default 900 s = 15 min).

    Returns
    -------
    The same *state* object, mutated in place, for convenience.
    """
    pending = getattr(state, "pending_hitl", None) or {}
    token = mint_for_approval(
        workflow_id=str(getattr(state, "workflow_id", "")),
        pending_hitl=pending if isinstance(pending, dict) else {},
        operator=operator,
        ttl_seconds=ttl_seconds,
    )
    state.operator_capability = token
    return state
