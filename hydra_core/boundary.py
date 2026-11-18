"""Squad-boundary enforcement helpers (RBAC, venom gate, envelope redaction).

Pure relocation of logic that previously lived inline in
``dispatcher.MCPStdioDispatcher`` and ``supervisor.build_supervisor``. Behavior
is unchanged; callers reach these functions through module attribute lookup so
tests can patch them. This module imports only the standard library at module
scope; hydra_core modules are loaded lazily inside functions.
"""
from __future__ import annotations

import logging
import os
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

# Keep the historical logger name so log filtering/caplog behavior is unchanged.
logger = logging.getLogger("hydra_core.dispatcher")

REDACT_TEXT_FIELDS: tuple[str, ...] = (
    "objective", "summary", "instructions", "decision",
    "rationale", "risk_assessment", "rollout_plan",
)


def check_tool_rbac(
    server: str,
    tool: str,
    squad_id: str | None,
    *,
    packs: Mapping[str, Any],
    handoffs: Sequence[Mapping[str, Any]],
    strict: bool = False,
    now: datetime | None = None,
) -> str | None:
    """Validate that the squad is authorized to call this tool.

    Returns None if authorized, or a rejection reason string. With
    ``strict=False`` enforcement is skipped (None) when ``squad_id`` is None or
    the squad has no pack; with ``strict=True`` both cases are denied with a
    reason starting with ``RBAC:``.

    RA-3: claude-skill squads are auto-authorized for their own shim tool pair
    ({prefix}.command.list / {prefix}.output.write) on their shim server.
    ``_SKILL_PACK_SHIMS`` is imported lazily to avoid a circular import.
    """
    if squad_id is None:
        if strict:
            return "RBAC: no squad_id supplied; strict mode denies unidentified callers"
        return None
    pack = packs.get(squad_id)
    if pack is None:
        if strict:
            return f"RBAC: unknown squad {squad_id!r}; strict mode denies unregistered squads"
        return None

    # RA-3: auto-authorize each claude-skill squad's own shim tool pair.
    if getattr(pack, "entrypoint", None) == "claude-skill":
        try:
            from .squad_node import _SKILL_PACK_SHIMS  # noqa: PLC0415
            shim = _SKILL_PACK_SHIMS.get(squad_id)
            if shim is not None:
                shim_server = shim["server"]
                shim_prefix = shim["prefix"]
                if server == shim_server and tool in (
                    f"{shim_prefix}.command.list",
                    f"{shim_prefix}.output.write",
                ):
                    return None  # authorized: this squad's own shim pair
        except ImportError:
            pass  # fail-open on import error; fall through to declared-tools check

    declared_tools = getattr(pack, "tools", ())
    tool_key = f"{server}.{tool}" if server else tool
    for t in declared_tools:
        t_name = getattr(t, "name", t) if not isinstance(t, str) else t
        t_server = getattr(t, "mcp_server", None)
        if t_name == tool_key:
            return None
        if t_name == tool and (t_server is None or t_server == server):
            return None
    current = now if now is not None else datetime.now(timezone.utc)
    for h in handoffs:
        if h["squad_id"] != squad_id:
            continue
        if h["expires_at"] and h["expires_at"] < current:
            continue
        if tool_key in h["granted_tools"] or tool in h["granted_tools"]:
            return None
    return (
        f"RBAC: squad {squad_id!r} is not authorized for tool "
        f"{tool!r} on server {server!r}. Declared tools: "
        f"{[getattr(t, 'name', t) for t in declared_tools]}"
    )


def venom_gate(
    *,
    server: str | None = None,
    tool: str | None = None,
    args: Any = None,
    cmd: Any = None,
    call_mcp: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Run the Cerberus venom gate over a runtime action. Returns a rejection
    envelope when the action is REFUSED or REQUIRES HUMAN approval, else None.
    No registered venom matching the signature -> None (fast common path).
    Fail-CLOSED on an explicit VenomRefused / requires_human AND on a
    gate-internal error (a matched venom whose gate errored must not slip
    through). Only a missing venom MODULE (ImportError) fails open."""
    try:
        from .venom import gate_runtime_action, VenomRefused
    except Exception:  # noqa: BLE001 — venom module optional
        return None
    # F35: inject AgentSmith cross-check when HYDRA_VENOM_CROSS_CHECK=1.
    _xc_fn = None
    if os.environ.get("HYDRA_VENOM_CROSS_CHECK") == "1" and call_mcp is not None:
        _call_mcp = call_mcp

        def _agentsmith_cross_check(cap: str, xargs: Any) -> Optional[dict]:
            try:
                result = _call_mcp(
                    "agentsmith",
                    "agentsmith.hydra.venom_cross_check",
                    {"capability": cap, "context": xargs if isinstance(xargs, dict) else {}},
                )
                if isinstance(result, dict):
                    inner = result.get("result", result)
                    if isinstance(inner, dict):
                        return inner
            except Exception:  # noqa: BLE001 — fail-open on transport error
                pass
            return None

        _xc_fn = _agentsmith_cross_check
    try:
        verdicts = gate_runtime_action(
            server=server, tool=tool, args=args, cmd=cmd, raise_on_refuse=True,
            cross_check_fn=_xc_fn,
        )
    except VenomRefused as vr:
        # Blocked pending human review: surface so the supervisor routes a
        # constitution_breach HITL.
        logger.warning("Cerberus blocked %s (HITL): %s", vr.capability, "; ".join(vr.reasons))
        return {"status": "rejected", "error": str(vr),
                "venom_refused": True, "hitl_required": True,
                "capability": vr.capability, "reasons": list(vr.reasons),
                "audit_key": vr.audit_key}
    except Exception as exc:  # noqa: BLE001
        # FAIL-CLOSED: reached only when a venom signature already matched and
        # the gate itself errored. Block + route to HITL.
        logger.warning("venom gate internal error (fail-CLOSED → HITL): %r", exc)
        return {"status": "rejected",
                "error": f"venom gate internal error: {exc}",
                "hitl_required": True, "gate_error": True}
    # Defensive: PASS-with-requires_human also blocks autonomously.
    hil = [v for v in verdicts if getattr(v, "requires_human", False)]
    if hil:
        action = ".".join(c for c in (server, tool) if c) or "subprocess"
        logger.warning("Cerberus requires human approval for venom-class action %s", action)
        return {"status": "rejected",
                "error": "venom-class action requires human approval",
                "hitl_required": True,
                "audit_keys": [v.audit_key for v in hil]}
    return None


def redact_envelope_text_fields(
    env_dict: dict[str, Any],
    *,
    redact_fn: Callable[[str], str],
) -> dict[str, Any]:
    """Shallow copy of ``env_dict`` with only the string text fields redacted."""
    redacted = dict(env_dict)
    for text_field in REDACT_TEXT_FIELDS:
        if text_field in redacted and isinstance(redacted[text_field], str):
            redacted[text_field] = redact_fn(redacted[text_field])
    return redacted


def validate_and_redact_envelope(
    env_dict: dict[str, Any],
    *,
    direction: str,
    squad_id: str | None,
    trace_root: Path,
    validate_fn: Callable[[dict[str, Any]], Any],
    redact_fn: Callable[[str], str],
    emit_fn: Callable[..., Any],
) -> dict[str, Any]:
    """Validate envelope schema and redact text at a squad boundary.

    Invalid envelopes emit ``envelope_validation_failed`` and re-raise.
    """
    try:
        validate_fn(env_dict)
    except (ValueError, Exception) as exc:
        emit_fn(trace_root, "boundary", "envelope_validation_failed", {
            "envelope_id": env_dict.get("id"),
            "envelope_type": env_dict.get("type"),
            "direction": direction,
            "squad_id": squad_id,
            "error": str(exc),
        })
        raise

    redacted = redact_envelope_text_fields(env_dict, redact_fn=redact_fn)

    emit_fn(trace_root, env_dict.get("workflow_id", "boundary"),
            "squad_boundary_crossing", {
                "envelope_id": env_dict.get("id"),
                "envelope_type": env_dict.get("type"),
                "direction": direction,
                "squad_id": squad_id,
            })
    return redacted
