#!/usr/bin/env python3
"""Hydra attended-dispatch audit hook (Hydra#86) -- Claude Code Agent hook.

Stdlib only. Deliberately imports NOTHING from ``hydra_core`` -- this file
must run standalone (``sys.executable -I path/to/hydra_hook.py``) outside
the repo, with no ``hydra_core`` on ``sys.path``. Several of the constants
and small helpers below are therefore hand-mirrored copies of the
equivalent ``hydra_core`` logic (``dispatch_seam.canonical_prompt_text``,
``host_bridge.cursor_path``, the ticket/cursor schema ints, the native-lead
table) rather than imports. A test suite pins each mirrored copy against
the real thing so the two cannot silently drift.

Invocation: ``python hydra_hook.py pre`` or ``python hydra_hook.py post``.
Falls back to the stdin payload's ``hook_event_name`` when argv has no
mode. ``main()`` catches every exception and always returns 0 -- this is
an audit-only hook; it must never block or crash a real Agent dispatch.

Audit mode: this hook never grants or denies a dispatch. It only records
what WOULD have happened (ticket match/deny, prompt match) for later
reconciliation, and optionally emits a stdout ``updatedInput`` payload
when ``HYDRA_HOOK_REPLACE_PROMPT=1`` and the live ticket's pending prompt
disagrees with what the spawn is about to send.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------- #
# Mirrored schema constants (pinned against hydra_core by a test)            #
# --------------------------------------------------------------------------- #

TICKET_SCHEMA: int = 1
CURSOR_SCHEMA: int = 1

# --------------------------------------------------------------------------- #
# Governed-type table                                                        #
# --------------------------------------------------------------------------- #

BARE_GOVERNED: frozenset[str] = frozenset({
    "engineer",
    "judge-cross-vendor",
    "judge-same-vendor",
    "plan-author",
    "plan-critic",
    "plan-scribe",
})

# Mirrors {p.qualified_lead_agent for p in native_packs.NATIVE_PACKS.values()}.
# A test pins this literal against the real import.
NATIVE_LEADS: frozenset[str] = frozenset({
    "hydra:plan-author",
    "executive-suite:boardroom",
    "rlm-creative:calliope",
    "senate:general-counsel",
    "rlm-gaming:the-director",
    "marketbliss:marketing-supervisor",
})


def normalize(t: Any) -> str:
    """Strip one leading ``'hydra:'`` and nothing else."""
    if not isinstance(t, str):
        return ""
    if t.startswith("hydra:"):
        return t[len("hydra:"):]
    return t


_GOVERNED_NORMALIZED: frozenset[str] = frozenset(
    normalize(x) for x in (BARE_GOVERNED | NATIVE_LEADS)
)


def is_governed(t: Any) -> bool:
    return normalize(t) in _GOVERNED_NORMALIZED


# --------------------------------------------------------------------------- #
# Safe-metadata policy                                                       #
# --------------------------------------------------------------------------- #

_SAFE_STR_RE: re.Pattern[str] = re.compile(r"^[A-Za-z0-9._:\-\[\]]+$")


def _safe_num(v: Any) -> int | float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return v
    return None


def _safe_str(v: Any) -> str | None:
    if not isinstance(v, str):
        return None
    if not (1 <= len(v) <= 128):
        return None
    if not _SAFE_STR_RE.match(v):
        return None
    return v


# --------------------------------------------------------------------------- #
# Mirrored dispatch_seam / host_bridge helpers                               #
# --------------------------------------------------------------------------- #

def canonical_prompt_text(action: dict[str, Any]) -> str:
    """Byte-for-byte mirror of ``dispatch_seam.canonical_prompt_text``."""
    parts: list[str] = []
    for key in ("prompt", "instructions", "artifact_text"):
        val = action.get(key)
        if isinstance(val, str):
            parts.append(f"{key}\n{len(val)}\n{val}\n")
    return "".join(parts)


def cursor_path(project_root: str | Path, workflow_id: str, run_id: str) -> Path:
    """Mirror of ``host_bridge.cursor_path``."""
    safe_run = "".join(c for c in str(run_id) if c.isalnum() or c in "-_") or "run"
    return (Path(project_root) / ".hydra" / str(workflow_id)
            / "attended" / f"{safe_run}.json")


# --------------------------------------------------------------------------- #
# Write confinement                                                          #
# --------------------------------------------------------------------------- #

def _write_confined(root: Path, filename: str, text: str) -> None:
    """Append ``text`` to ``<root>/.hydra/<filename>``, refusing the write if
    either ``<root>/.hydra`` itself or the final target resolves (via
    realpath) outside the project root / the ``.hydra`` directory. Never
    raises. ``<root>/.hydra`` is created only after confirming its would-be
    realpath lands inside the project root -- this also holds when
    ``<root>/.hydra`` is (or would resolve through) a symlink or junction to
    an outside directory."""
    try:
        root_real = os.path.normcase(os.path.realpath(str(root)))
        hydra_dir = root / ".hydra"

        try:
            hydra_real_precreate = os.path.normcase(os.path.realpath(str(hydra_dir)))
        except Exception:
            return
        if hydra_real_precreate == root_real:
            return
        try:
            if os.path.commonpath([root_real, hydra_real_precreate]) != root_real:
                return
        except ValueError:
            return

        os.makedirs(str(hydra_dir), exist_ok=True)

        try:
            hydra_real = os.path.normcase(os.path.realpath(str(hydra_dir)))
        except Exception:
            return
        if hydra_real == root_real:
            return
        try:
            if os.path.commonpath([root_real, hydra_real]) != root_real:
                return
        except ValueError:
            return

        target = hydra_dir / filename
        try:
            target_real = os.path.normcase(os.path.realpath(str(target)))
        except Exception:
            return
        if target_real == hydra_real:
            return
        try:
            if os.path.commonpath([hydra_real, target_real]) != hydra_real:
                return
        except ValueError:
            return

        with open(str(target), "a", encoding="utf-8") as f:
            f.write(text)
    except Exception:
        return


def _append_jsonl(root: Path, filename: str, obj: dict[str, Any]) -> None:
    try:
        text = json.dumps(obj, ensure_ascii=False) + "\n"
    except Exception:
        return
    _write_confined(root, filename, text)


# --------------------------------------------------------------------------- #
# Shape recorder                                                             #
# --------------------------------------------------------------------------- #

_SHAPE_KEY_RE: re.Pattern[str] = re.compile(r"^[A-Za-z0-9_]{1,64}$")


def _shape_of(value: Any) -> Any:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            key_out = k if (isinstance(k, str) and _SHAPE_KEY_RE.match(k)) else "<key>"
            if key_out in out:
                continue
            out[key_out] = _shape_of(v)
        return out
    if isinstance(value, list):
        return [_shape_of(v) for v in value]
    if value is None:
        return "NoneType"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    return type(value).__name__


def _maybe_record_shapes(
    root: Path, mode: str, payload: dict[str, Any], environ: dict[str, str],
) -> None:
    if environ.get("HYDRA_HOOK_RECORD_SHAPES") != "1":
        return
    record = {"event": mode, "shape": _shape_of(payload)}
    _append_jsonl(root, "hydra_hook.shapes.jsonl", record)


# --------------------------------------------------------------------------- #
# Ticket lookup                                                              #
# --------------------------------------------------------------------------- #

def _parse_aware_dt(s: Any) -> datetime:
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        raise ValueError("naive datetime")
    return dt


def _validate_ticket(data: Any) -> dict[str, Any] | None:
    if not isinstance(data, dict):
        return None
    if data.get("schema") != TICKET_SCHEMA:
        return None
    if data.get("mode") != "audit":
        return None
    for key in ("workflow_id", "run_id", "call_key", "ticket_id", "agent_type",
                "prompt_sha256"):
        if not isinstance(data.get(key), str):
            return None
    try:
        created = _parse_aware_dt(data["created_at"])
        expires = _parse_aware_dt(data["expires_at"])
    except Exception:
        return None
    return {"raw": data, "created": created, "expires": expires}


def resolve_ticket(
    root: Path, subagent_type: str, now: datetime,
) -> tuple[str, str | None, dict[str, Any] | None, int]:
    """Returns ``(decision, reason, ticket, live_candidates)``.

    ``decision`` is ``'found'`` or ``'would_deny'``; ``reason`` is set for
    ``'would_deny'`` (one of ``expired``, ``type_mismatch``, ``unreadable``,
    ``no_ticket``) and ``None`` for ``'found'``. ``ticket`` is the selected
    ticket's raw dict, or ``None``."""
    norm_target = normalize(subagent_type)
    live_matches: list[tuple[datetime, str, dict[str, Any]]] = []
    has_expired_match = False
    other_type_live = False
    any_invalid = False

    hydra_dir = root / ".hydra"
    try:
        paths = sorted(hydra_dir.glob("*/attended/tickets/*.json"))
    except Exception:
        paths = []

    for p in paths:
        try:
            with open(str(p), "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            any_invalid = True
            continue
        parsed = _validate_ticket(data)
        if parsed is None:
            any_invalid = True
            continue
        raw = parsed["raw"]
        t_norm = normalize(raw.get("agent_type", ""))
        is_live = parsed["expires"] > now
        if t_norm == norm_target:
            if is_live:
                live_matches.append((parsed["created"], raw["ticket_id"], raw))
            else:
                has_expired_match = True
        else:
            if is_live:
                other_type_live = True

    if live_matches:
        best = min(live_matches, key=lambda item: (-item[0].timestamp(), item[1]))
        return "found", None, best[2], len(live_matches)
    if has_expired_match:
        return "would_deny", "expired", None, 0
    if other_type_live:
        return "would_deny", "type_mismatch", None, 0
    if any_invalid:
        return "would_deny", "unreadable", None, 0
    return "would_deny", "no_ticket", None, 0


def _check_prompt(
    root: Path, ticket: dict[str, Any], tool_input: dict[str, Any],
) -> tuple[str, bool | None, bool, dict[str, Any] | None]:
    """Returns ``(prompt_check, prompt_match, would_replace, pending_action)``."""
    try:
        cpath = cursor_path(root, ticket.get("workflow_id", ""), ticket.get("run_id", ""))
        with open(str(cpath), "r", encoding="utf-8") as f:
            cursor = json.load(f)
    except Exception:
        return "cursor_unavailable", None, False, None

    if not isinstance(cursor, dict) or cursor.get("schema") != CURSOR_SCHEMA:
        return "cursor_schema_mismatch", None, False, None

    pending_action = cursor.get("pending_action")
    if not isinstance(pending_action, dict):
        return "cursor_unavailable", None, False, None

    if pending_action.get("call_key") != ticket.get("call_key"):
        return "call_key_mismatch", None, False, None

    try:
        computed = hashlib.sha256(
            canonical_prompt_text(pending_action).encode("utf-8", errors="surrogatepass")
        ).hexdigest()
    except Exception:
        return "cursor_hash_mismatch", None, False, None

    if computed != ticket.get("prompt_sha256"):
        return "cursor_hash_mismatch", None, False, None

    prompt_match = tool_input.get("prompt") == pending_action.get("prompt")
    would_replace = not prompt_match
    return "verified", prompt_match, would_replace, pending_action


# --------------------------------------------------------------------------- #
# Pre / post handlers                                                        #
# --------------------------------------------------------------------------- #

def _handle_pre(
    root: Path, payload: dict[str, Any], tool_input: dict[str, Any],
    subagent_type: str, environ: dict[str, str],
) -> int:
    if not is_governed(subagent_type):
        return 0

    now = datetime.now(timezone.utc)
    decision, reason, ticket, live_candidates = resolve_ticket(root, subagent_type, now)

    prompt_check: str | None = None
    prompt_match: bool | None = None
    would_replace = False
    pending_action: dict[str, Any] | None = None
    if decision == "found" and ticket is not None:
        prompt_check, prompt_match, would_replace, pending_action = _check_prompt(
            root, ticket, tool_input)

    line = {
        "event": "pre",
        "ts": now.isoformat(),
        "tool_use_id": _safe_str(payload.get("tool_use_id")),
        "subagent_type": _safe_str(subagent_type),
        "decision": decision,
        "reason": reason,
        "ticket_id": ticket.get("ticket_id") if ticket else None,
        "workflow_id": ticket.get("workflow_id") if ticket else None,
        "run_id": ticket.get("run_id") if ticket else None,
        "call_key": ticket.get("call_key") if ticket else None,
        "live_candidates": live_candidates,
        "prompt_match": prompt_match,
        "would_replace": would_replace,
        "prompt_check": prompt_check,
    }
    _append_jsonl(root, "hydra_hook.audit.jsonl", line)

    if (environ.get("HYDRA_HOOK_REPLACE_PROMPT") == "1" and would_replace
            and pending_action is not None):
        updated_input = dict(tool_input)
        updated_input["prompt"] = pending_action.get("prompt")
        out = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "updatedInput": updated_input,
            }
        }
        try:
            sys.stdout.write(json.dumps(out))
        except Exception:
            pass
    return 0


def _usage_fields(tool_response: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if not isinstance(tool_response, dict):
        out.update({
            "total_tokens": None, "input_tokens": None, "output_tokens": None,
            "cache_creation_input_tokens": None, "cache_read_input_tokens": None,
            "total_duration_ms": None, "total_tool_use_count": None,
            "resolved_model": None,
            "status": None, "agent_id": None,
            "usage_status": "usage_unavailable",
            "reason": "no_usage_in_response",
        })
        return out

    status_raw = tool_response.get("status")
    usage = tool_response.get("usage")

    if status_raw == "async_launched":
        usage_status, reason = "usage_unavailable", "async_launched"
    elif isinstance(usage, dict):
        usage_status, reason = "recorded", None
    else:
        usage_status, reason = "usage_unavailable", "no_usage_in_response"

    if usage_status == "usage_unavailable":
        out.update({
            "total_tokens": None, "input_tokens": None, "output_tokens": None,
            "cache_creation_input_tokens": None, "cache_read_input_tokens": None,
            "total_duration_ms": None, "total_tool_use_count": None,
            "resolved_model": None,
        })
    else:
        u = usage if isinstance(usage, dict) else {}
        out.update({
            "total_tokens": _safe_num(tool_response.get("totalTokens")),
            "input_tokens": _safe_num(u.get("input_tokens")),
            "output_tokens": _safe_num(u.get("output_tokens")),
            "cache_creation_input_tokens": _safe_num(u.get("cache_creation_input_tokens")),
            "cache_read_input_tokens": _safe_num(u.get("cache_read_input_tokens")),
            "total_duration_ms": _safe_num(tool_response.get("totalDurationMs")),
            "total_tool_use_count": _safe_num(tool_response.get("totalToolUseCount")),
            "resolved_model": _safe_str(tool_response.get("resolvedModel")),
        })

    out["status"] = _safe_str(status_raw)
    out["agent_id"] = _safe_str(tool_response.get("agentId"))
    out["usage_status"] = usage_status
    if reason is not None:
        out["reason"] = reason
    return out


def _handle_post(
    root: Path, payload: dict[str, Any], tool_input: dict[str, Any],
    subagent_type: str, environ: dict[str, str],
) -> int:
    if not is_governed(subagent_type):
        return 0

    now = datetime.now(timezone.utc)
    decision, reason, ticket, live_candidates = resolve_ticket(root, subagent_type, now)
    del decision, reason, live_candidates  # post audit line does not carry these

    tool_response = payload.get("tool_response")
    usage_line = _usage_fields(tool_response)

    line: dict[str, Any] = {
        "event": "post",
        "ts": now.isoformat(),
        "usage_scope": "final_request_only",
        "tool_use_id": _safe_str(payload.get("tool_use_id")),
        "ticket_id": ticket.get("ticket_id") if ticket else None,
    }
    line.update(usage_line)
    _append_jsonl(root, "hydra_hook.audit.jsonl", line)
    return 0


# --------------------------------------------------------------------------- #
# Entry point                                                                #
# --------------------------------------------------------------------------- #

def _parse_payload(raw: str) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    return data


def _mode_from_argv_or_payload(argv: list[str], payload: dict[str, Any]) -> str | None:
    if len(argv) > 1 and argv[1] in ("pre", "post"):
        return argv[1]
    event = payload.get("hook_event_name")
    if event == "PreToolUse":
        return "pre"
    if event == "PostToolUse":
        return "post"
    return None


def main(
    argv: list[str] | None = None,
    stdin_text: str | None = None,
    environ: dict[str, str] | None = None,
) -> int:
    try:
        if argv is None:
            argv = sys.argv
        if environ is None:
            environ = os.environ  # type: ignore[assignment]
        if stdin_text is None:
            stdin_text = sys.stdin.read()

        payload = _parse_payload(stdin_text)
        if payload is None:
            return 0

        mode = _mode_from_argv_or_payload(argv, payload)
        if mode not in ("pre", "post"):
            return 0

        root_raw = environ.get("CLAUDE_PROJECT_DIR")
        if not root_raw:
            return 0
        root = Path(root_raw)

        tool_name = payload.get("tool_name")
        if tool_name is not None and tool_name != "Agent":
            return 0

        tool_input = payload.get("tool_input")
        _maybe_record_shapes(root, mode, payload, environ)  # type: ignore[arg-type]

        if not isinstance(tool_input, dict):
            return 0

        subagent_type = tool_input.get("subagent_type")
        if not isinstance(subagent_type, str) or not subagent_type:
            return 0

        if mode == "pre":
            return _handle_pre(root, payload, tool_input, subagent_type, environ)  # type: ignore[arg-type]
        return _handle_post(root, payload, tool_input, subagent_type, environ)  # type: ignore[arg-type]
    except Exception:
        return 0


if __name__ == "__main__":
    sys.exit(main())
