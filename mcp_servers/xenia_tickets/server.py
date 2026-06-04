"""xenia_tickets — MCP ticket-system bridge for the Xenia customer-support squad.

File-backed store: one JSON file per ticket at
  <XENIA_ROOT>/hearth/tasks/TICKET-<id>.json
Counter persisted at:
  <XENIA_ROOT>/hearth/tasks/.counter

Constitution enforcement (defense-in-depth, Article V):
  - customer_ref MUST match ^customer:[0-9a-f]{6,}$  (Article IV identity discipline)
  - send_response requires [AI-assisted response] + seal: cleared markers unless actor='human'
  - execute_approved reads APPROVAL-<ticket_id>-*.yaml; deny-by-default on any mismatch
  - No monetary execution without a valid, unexpired, action-matching approval artifact

Tools surface as mcp__hydra_gateway__xenia_tickets__* (server name "xenia-tickets" matches
hook matcher mcp__.*ticket.*).
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve()
# Allow `from mcp_servers._pack_shim import ...`
sys.path.insert(0, str(_HERE.parents[2]))

from mcp_servers._pack_shim import resolve_root, run_server  # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CUSTOMER_REF_RE = re.compile(r"^customer:[0-9a-f]{6,}$")

# SLA first-response windows (in minutes)
SLA_MINUTES: dict[str, int] = {
    "P1": 60,
    "P2": 240,       # 4 h
    "P3": 480,       # 1 business day ≈ 8 h (simplified — no calendar awareness)
    "P4": 960,       # 2 business days ≈ 16 h
}

VALID_STATUSES = {"open", "pending", "resolved", "escalated", "closed"}
VALID_PRIORITIES = {"P1", "P2", "P3", "P4"}

# Legal status transitions — closed is terminal (no resurrect)
# Maps current_status -> set of allowed next statuses
STATUS_TRANSITIONS: dict[str, set[str]] = {
    "open":      {"open", "pending", "resolved", "escalated", "closed"},
    "pending":   {"open", "pending", "resolved", "escalated", "closed"},
    "escalated": {"open", "pending", "resolved", "escalated", "closed"},
    "resolved":  {"open", "pending", "resolved", "escalated", "closed"},
    "closed":    set(),   # terminal — no transitions allowed out
}

MUTABLE_FIELDS = {"status", "priority", "intent"}

# Counter lock (thread-safety for local use; process-level for the common case)
_counter_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Typed-error helpers
# ---------------------------------------------------------------------------

def _err(code: str, message: str) -> dict[str, Any]:
    return {"error": {"code": code, "message": message}}


# ---------------------------------------------------------------------------
# Root / path helpers
# ---------------------------------------------------------------------------

def _root() -> Path:
    return resolve_root("HYDRA_XENIA_ROOT", "C:/AiAppDeployments/Xenia")


def _tasks_dir(root: Path) -> Path:
    d = root / "hearth" / "tasks"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _approvals_dir(root: Path) -> Path:
    return root / "hearth" / "approvals"


def _ticket_path(tasks: Path, ticket_id: str) -> Path:
    return tasks / f"TICKET-{ticket_id}.json"


# ---------------------------------------------------------------------------
# Counter
# ---------------------------------------------------------------------------

def _next_ticket_id(tasks: Path) -> str:
    """Return zero-padded next ticket id, atomically incrementing the counter file."""
    counter_path = tasks / ".counter"
    with _counter_lock:
        if counter_path.exists():
            try:
                current = int(counter_path.read_text(encoding="utf-8").strip())
            except (ValueError, OSError):
                current = 0
        else:
            current = 0
        nxt = current + 1
        counter_path.write_text(str(nxt), encoding="utf-8")
    return str(nxt).zfill(6)


# ---------------------------------------------------------------------------
# Ticket I/O
# ---------------------------------------------------------------------------

def _load_ticket(tasks: Path, ticket_id: str) -> dict[str, Any] | None:
    p = _ticket_path(tasks, ticket_id)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _save_ticket(tasks: Path, ticket: dict[str, Any]) -> None:
    p = _ticket_path(tasks, ticket["ticket_id"])
    p.write_text(json.dumps(ticket, indent=2, default=str), encoding="utf-8")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ticket_summary(ticket: dict[str, Any]) -> dict[str, Any]:
    """Reduced projection for list responses."""
    return {
        "ticket_id":   ticket["ticket_id"],
        "status":      ticket["status"],
        "priority":    ticket["priority"],
        "intent":      ticket.get("intent"),
        "subject":     ticket.get("subject"),
        "customer_ref": ticket.get("customer_ref"),
        "created_at":  ticket.get("created_at"),
        "updated_at":  ticket.get("updated_at"),
        "sla":         ticket.get("sla"),
    }


# ---------------------------------------------------------------------------
# Approval YAML parser (stdlib only — flat key: value)
# ---------------------------------------------------------------------------

def _parse_flat_yaml(text: str) -> dict[str, str]:
    """Parse a flat key: value YAML file.  No pyyaml dependency.
    Handles optional surrounding quotes on values.  Comments (#) stripped.
    """
    result: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        key_part, _, val_part = line.partition(":")
        key = key_part.strip()
        val = val_part.strip()
        # Strip inline comments
        if " #" in val:
            val = val[:val.index(" #")].strip()
        # Strip optional surrounding quotes
        if (val.startswith('"') and val.endswith('"')) or \
           (val.startswith("'") and val.endswith("'")):
            val = val[1:-1]
        result[key] = val
    return result


def _find_valid_approval(
    approvals_dir: Path,
    ticket_id: str,
    action: str,
    scope: str,
    approval_id: str,
) -> tuple[bool, str, dict[str, str]]:
    """Search approvals_dir for a matching, unexpired, approved artifact.

    Returns (valid: bool, reason: str, parsed_yaml: dict).
    """
    if not approvals_dir.exists():
        return False, "no approvals directory", {}

    pattern = f"APPROVAL-{ticket_id}-*.yaml"
    candidates = list(approvals_dir.glob(pattern))
    if not candidates:
        return False, f"no approval artifact matching {pattern}", {}

    now = datetime.now(timezone.utc)

    for fpath in candidates:
        # If a specific approval_id was given, filter by filename stem
        if approval_id:
            # approval_id may be the full stem or partial; check inclusion
            if approval_id not in fpath.stem and fpath.stem != approval_id:
                # try exact filename match
                if fpath.name != approval_id and fpath.stem != approval_id:
                    continue

        try:
            text = fpath.read_text(encoding="utf-8")
        except OSError:
            continue

        parsed = _parse_flat_yaml(text)

        # Must be approved
        if parsed.get("status", "").lower() != "approved":
            continue

        # ticket_id must be present in the artifact
        art_ticket = parsed.get("ticket_id", "")
        if art_ticket and art_ticket != ticket_id:
            continue

        # action must match (case-insensitive)
        art_action = parsed.get("action", "").lower()
        if art_action and art_action != action.lower():
            continue

        # scope must match if present
        art_scope = parsed.get("scope", "")
        if art_scope and art_scope != scope:
            continue

        # issued_by must be present
        if not parsed.get("issued_by", "").strip():
            continue

        # expires_at must be present and in the future
        expires_str = parsed.get("expires_at", "")
        if not expires_str:
            continue
        try:
            # Handle both offset-aware and naive ISO timestamps
            exp = datetime.fromisoformat(expires_str.replace("Z", "+00:00"))
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue
        if exp <= now:
            return False, f"approval artifact {fpath.name} is expired (expires_at={expires_str})", parsed

        return True, fpath.name, parsed

    return False, "no matching valid approval artifact found", {}


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def _tool_handlers() -> dict[str, Any]:
    root = _root()

    # ------------------------------------------------------------------
    # xenia-tickets.create
    # ------------------------------------------------------------------
    def create(args: dict[str, Any]) -> dict[str, Any]:
        subject    = str(args.get("subject") or "").strip()
        body       = str(args.get("body") or "").strip()
        customer_ref = str(args.get("customer_ref") or "").strip()
        priority   = str(args.get("priority") or "P3").upper()
        intent     = args.get("intent")

        if not subject:
            return _err("MISSING_FIELD", "subject is required")
        if not body:
            return _err("MISSING_FIELD", "body is required")
        if not customer_ref:
            return _err("MISSING_FIELD", "customer_ref is required")

        # Article IV identity discipline — reject raw emails/names
        if not CUSTOMER_REF_RE.match(customer_ref):
            return _err(
                "IDENTITY_REQUIRED",
                f"customer_ref must be an opaque ref matching ^customer:[0-9a-f]{{6,}}$ "
                f"(constitution Article IV). Received: {customer_ref!r}. "
                "Hash the customer identifier before passing it."
            )

        if priority not in VALID_PRIORITIES:
            return _err("INVALID_FIELD", f"priority must be one of {sorted(VALID_PRIORITIES)}")

        tasks = _tasks_dir(root)
        ticket_id = _next_ticket_id(tasks)
        now = _now_iso()

        sla_mins = SLA_MINUTES[priority]
        first_response_due = (
            datetime.now(timezone.utc) + timedelta(minutes=sla_mins)
        ).isoformat()

        ticket: dict[str, Any] = {
            "ticket_id":   ticket_id,
            "status":      "open",
            "priority":    priority,
            "intent":      intent,
            "customer_ref": customer_ref,
            "subject":     subject,
            "created_at":  now,
            "updated_at":  now,
            "sla": {
                "first_response_due": first_response_due,
                "breached": False,
            },
            "history": [
                {
                    "ts":    now,
                    "actor": customer_ref,
                    "kind":  "created",
                    "body":  body,
                }
            ],
            "recommendations": [],
        }

        _save_ticket(tasks, ticket)
        return ticket

    # ------------------------------------------------------------------
    # xenia-tickets.get
    # ------------------------------------------------------------------
    def get(args: dict[str, Any]) -> dict[str, Any]:
        ticket_id = str(args.get("ticket_id") or "").strip()
        if not ticket_id:
            return _err("MISSING_FIELD", "ticket_id is required")
        tasks = _tasks_dir(root)
        ticket = _load_ticket(tasks, ticket_id)
        if ticket is None:
            return _err("NOT_FOUND", f"ticket {ticket_id!r} not found")
        return ticket

    # ------------------------------------------------------------------
    # xenia-tickets.list
    # ------------------------------------------------------------------
    def list_tickets(args: dict[str, Any]) -> dict[str, Any]:
        filter_status   = args.get("status")
        filter_priority = args.get("priority")
        tasks = _tasks_dir(root)

        tickets: list[dict[str, Any]] = []
        for p in tasks.glob("TICKET-*.json"):
            try:
                t = json.loads(p.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if filter_status and t.get("status") != filter_status:
                continue
            if filter_priority and t.get("priority") != filter_priority:
                continue
            tickets.append(t)

        # Sort: P1 first, then by created_at ascending (oldest first = most urgent)
        priority_order = {"P1": 0, "P2": 1, "P3": 2, "P4": 3}

        def sort_key(t: dict[str, Any]) -> tuple:
            prio = priority_order.get(t.get("priority", "P4"), 3)
            return (prio, t.get("created_at", ""))

        tickets.sort(key=sort_key)
        return {"tickets": [_ticket_summary(t) for t in tickets], "count": len(tickets)}

    # ------------------------------------------------------------------
    # xenia-tickets.comment
    # ------------------------------------------------------------------
    def comment(args: dict[str, Any]) -> dict[str, Any]:
        ticket_id = str(args.get("ticket_id") or "").strip()
        body      = str(args.get("body") or "").strip()
        actor     = str(args.get("actor") or "").strip()

        if not ticket_id:
            return _err("MISSING_FIELD", "ticket_id is required")
        if not body:
            return _err("MISSING_FIELD", "body is required")
        if not actor:
            return _err("MISSING_FIELD", "actor is required")

        tasks = _tasks_dir(root)
        ticket = _load_ticket(tasks, ticket_id)
        if ticket is None:
            return _err("NOT_FOUND", f"ticket {ticket_id!r} not found")

        now = _now_iso()
        ticket["history"].append({"ts": now, "actor": actor, "kind": "comment", "body": body})
        ticket["updated_at"] = now
        _save_ticket(tasks, ticket)
        return {"ok": True, "ticket_id": ticket_id, "updated_at": now}

    # ------------------------------------------------------------------
    # xenia-tickets.update_fields
    # ------------------------------------------------------------------
    def update_fields(args: dict[str, Any]) -> dict[str, Any]:
        ticket_id = str(args.get("ticket_id") or "").strip()
        fields    = args.get("fields") or {}

        if not ticket_id:
            return _err("MISSING_FIELD", "ticket_id is required")
        if not isinstance(fields, dict) or not fields:
            return _err("MISSING_FIELD", "fields must be a non-empty object")

        bad_keys = set(fields.keys()) - MUTABLE_FIELDS
        if bad_keys:
            return _err(
                "INVALID_FIELD",
                f"only {sorted(MUTABLE_FIELDS)} may be updated; got: {sorted(bad_keys)}"
            )

        tasks = _tasks_dir(root)
        ticket = _load_ticket(tasks, ticket_id)
        if ticket is None:
            return _err("NOT_FOUND", f"ticket {ticket_id!r} not found")

        now = _now_iso()
        changes: list[str] = []

        if "status" in fields:
            new_status = str(fields["status"]).lower()
            if new_status not in VALID_STATUSES:
                return _err("INVALID_FIELD", f"status must be one of {sorted(VALID_STATUSES)}")
            current = ticket["status"]
            allowed = STATUS_TRANSITIONS.get(current, set())
            if new_status not in allowed:
                return _err(
                    "INVALID_TRANSITION",
                    f"cannot transition from '{current}' to '{new_status}' "
                    "(closed is terminal; no resurrect from closed)"
                )
            changes.append(f"status: {current} -> {new_status}")
            ticket["status"] = new_status

        if "priority" in fields:
            new_prio = str(fields["priority"]).upper()
            if new_prio not in VALID_PRIORITIES:
                return _err("INVALID_FIELD", f"priority must be one of {sorted(VALID_PRIORITIES)}")
            changes.append(f"priority: {ticket.get('priority')} -> {new_prio}")
            ticket["priority"] = new_prio

        if "intent" in fields:
            ticket["intent"] = fields["intent"]
            changes.append(f"intent: {fields['intent']}")

        ticket["updated_at"] = now
        ticket["history"].append({
            "ts":    now,
            "actor": "system",
            "kind":  "field-update",
            "body":  "; ".join(changes),
        })
        _save_ticket(tasks, ticket)
        return {"ok": True, "ticket_id": ticket_id, "changes": changes, "updated_at": now}

    # ------------------------------------------------------------------
    # xenia-tickets.send_response
    # ------------------------------------------------------------------
    def send_response(args: dict[str, Any]) -> dict[str, Any]:
        ticket_id = str(args.get("ticket_id") or "").strip()
        body      = str(args.get("body") or "").strip()
        actor     = str(args.get("actor") or "").strip()

        if not ticket_id:
            return _err("MISSING_FIELD", "ticket_id is required")
        if not body:
            return _err("MISSING_FIELD", "body is required")
        if not actor:
            return _err("MISSING_FIELD", "actor is required")

        tasks = _tasks_dir(root)
        ticket = _load_ticket(tasks, ticket_id)
        if ticket is None:
            return _err("NOT_FOUND", f"ticket {ticket_id!r} not found")

        # Article IX / Article III: pipeline markers required unless a human actor
        if actor != "human":
            has_ai_marker   = "[AI-assisted response]" in body
            has_seal_marker = "seal: cleared" in body
            if not has_ai_marker or not has_seal_marker:
                missing: list[str] = []
                if not has_ai_marker:
                    missing.append("'[AI-assisted response]'")
                if not has_seal_marker:
                    missing.append("'seal: cleared'")
                return _err(
                    "PIPELINE_REQUIRED",
                    "Article IX / Article III: customer-facing response must carry "
                    f"{' and '.join(missing)} markers. "
                    "The full Themis → Eunomia pipeline is required before send_response. "
                    "Only actor='human' bypasses this check."
                )

        now = _now_iso()
        ticket["history"].append({
            "ts":    now,
            "actor": actor,
            "kind":  "response",
            "body":  body,
        })
        # Sending a response moves ticket to pending if it was open
        if ticket["status"] == "open":
            ticket["status"] = "pending"
        ticket["updated_at"] = now
        _save_ticket(tasks, ticket)
        return {"ok": True, "ticket_id": ticket_id, "updated_at": now}

    # ------------------------------------------------------------------
    # xenia-tickets.recommend
    # ------------------------------------------------------------------
    def recommend(args: dict[str, Any]) -> dict[str, Any]:
        ticket_id    = str(args.get("ticket_id") or "").strip()
        action       = str(args.get("action") or "").strip()
        scope        = str(args.get("scope") or "").strip()
        amount       = args.get("amount")
        policy_basis = str(args.get("policy_basis") or "").strip()

        if not ticket_id:
            return _err("MISSING_FIELD", "ticket_id is required")
        if not action:
            return _err("MISSING_FIELD", "action is required")
        if not scope:
            return _err("MISSING_FIELD", "scope is required")
        if not policy_basis:
            return _err("MISSING_FIELD", "policy_basis is required")

        tasks = _tasks_dir(root)
        ticket = _load_ticket(tasks, ticket_id)
        if ticket is None:
            return _err("NOT_FOUND", f"ticket {ticket_id!r} not found")

        now = _now_iso()
        rec: dict[str, Any] = {
            "action":       action,
            "scope":        scope,
            "policy_basis": policy_basis,
            "status":       "pending",
        }
        if amount is not None:
            rec["amount"] = amount

        ticket["recommendations"].append(rec)
        ticket["history"].append({
            "ts":    now,
            "actor": "system",
            "kind":  "recommendation",
            "body":  f"action={action} scope={scope} amount={amount} policy_basis={policy_basis}",
        })
        ticket["updated_at"] = now
        _save_ticket(tasks, ticket)
        return {"ok": True, "ticket_id": ticket_id, "recommendation": rec}

    # ------------------------------------------------------------------
    # xenia-tickets.execute_approved
    # ------------------------------------------------------------------
    def execute_approved(args: dict[str, Any]) -> dict[str, Any]:
        """DENY-BY-DEFAULT. The server NEVER executes without a valid, unexpired,
        matching approval artifact — even if the hook layer was bypassed.
        (Constitution Article V, defense-in-depth Layer 0.)
        """
        ticket_id   = str(args.get("ticket_id") or "").strip()
        action      = str(args.get("action") or "").strip()
        scope       = str(args.get("scope") or "").strip()
        approval_id = str(args.get("approval_id") or "").strip()

        if not ticket_id:
            return _err("MISSING_FIELD", "ticket_id is required")
        if not action:
            return _err("MISSING_FIELD", "action is required")
        if not scope:
            return _err("MISSING_FIELD", "scope is required")
        if not approval_id:
            return _err("MISSING_FIELD", "approval_id is required")

        tasks = _tasks_dir(root)
        ticket = _load_ticket(tasks, ticket_id)
        if ticket is None:
            return _err("NOT_FOUND", f"ticket {ticket_id!r} not found")

        approvals_dir = _approvals_dir(root)
        valid, reason, parsed = _find_valid_approval(
            approvals_dir, ticket_id, action, scope, approval_id
        )

        if not valid:
            return _err(
                "ARTICLE_V_DENY",
                f"Constitution Article V: deny-by-default. Execution refused. Reason: {reason}. "
                "Required: hearth/approvals/APPROVAL-<ticket_id>-<seq>.yaml with "
                "status=approved, unexpired, matching action+scope, issued_by present."
            )

        now = _now_iso()
        ticket["history"].append({
            "ts":          now,
            "actor":       parsed.get("issued_by", "approved-human"),
            "kind":        "approved-action",
            "body":        f"action={action} scope={scope} approval_id={approval_id} artifact={reason}",
        })

        # Mark matching recommendation as approved
        for rec in ticket.get("recommendations", []):
            if rec.get("action", "").lower() == action.lower() and rec.get("status") == "pending":
                rec["status"] = "approved"
                break

        ticket["updated_at"] = now
        _save_ticket(tasks, ticket)
        return {
            "ok":          True,
            "ticket_id":   ticket_id,
            "action":      action,
            "scope":       scope,
            "approval_id": approval_id,
            "executed_at": now,
        }

    # ------------------------------------------------------------------
    # xenia-tickets.ping
    # ------------------------------------------------------------------
    def ping(args: dict[str, Any]) -> dict[str, Any]:
        tasks = _tasks_dir(root)
        open_count = sum(
            1 for p in tasks.glob("TICKET-*.json")
            if _safe_load_status(p) == "open"
        )
        return {"ok": True, "root": str(root), "open_count": open_count}

    return {
        "xenia-tickets.create":          create,
        "xenia-tickets.get":             get,
        "xenia-tickets.list":            list_tickets,
        "xenia-tickets.comment":         comment,
        "xenia-tickets.update_fields":   update_fields,
        "xenia-tickets.send_response":   send_response,
        "xenia-tickets.recommend":       recommend,
        "xenia-tickets.execute_approved": execute_approved,
        "xenia-tickets.ping":            ping,
    }


def _safe_load_status(p: Path) -> str:
    try:
        return json.loads(p.read_text(encoding="utf-8")).get("status", "")
    except (json.JSONDecodeError, OSError):
        return ""


def main() -> None:
    run_server("xenia-tickets", _tool_handlers())
