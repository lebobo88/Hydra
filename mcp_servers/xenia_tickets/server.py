"""xenia_tickets — MCP ticket-system bridge for the Xenia customer-support squad.

File-backed store: one JSON file per ticket at
  <XENIA_ROOT>/hearth/tasks/TICKET-<id>.json
Counter persisted at:
  <XENIA_ROOT>/hearth/tasks/.counter

Constitution enforcement (defense-in-depth, Article V):
  - customer_ref MUST match ^customer:[0-9a-f]{6,}$  (Article IV identity discipline)
  - send_response requires a SIGNED CLEARANCE TOKEN produced by sign.py's mint()
    scheme (XEN-VHP-2).  Literal-string markers and actor='human' bypass removed.
  - execute_approved reads APPROVAL-<ticket_id>-*.yaml; deny-by-default on any mismatch
  - No monetary execution without a valid, unexpired, action-matching approval artifact
  - Server-side actor allow-list enforced for send_response and execute_approved (XEN-VHP-3)
  - Server-side PII scan with normalization on send_response body (XEN-VHP-1)
  - Approval artifact matching requires exact ticket_id, action, scope and exact
    approval_id stem (XEN-VHP-3 binding fix)

Tools surface as mcp__hydra_gateway__xenia_tickets__* (server name "xenia-tickets" matches
hook matcher mcp__.*ticket.*).
"""
from __future__ import annotations

import html
import json
import os
import re
import sys
import threading
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve()
# Allow `from mcp_servers._pack_shim import ...`
sys.path.insert(0, str(_HERE.parents[2]))

from mcp_servers._pack_shim import resolve_root, run_server  # noqa: E402
from mcp_servers.xenia_tickets.clearance import verify_clearance_token  # noqa: E402

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
STATUS_TRANSITIONS: dict[str, set[str]] = {
    "open":      {"open", "pending", "resolved", "escalated", "closed"},
    "pending":   {"open", "pending", "resolved", "escalated", "closed"},
    "escalated": {"open", "pending", "resolved", "escalated", "closed"},
    "resolved":  {"open", "pending", "resolved", "escalated", "closed"},
    "closed":    set(),   # terminal — no transitions allowed out
}

MUTABLE_FIELDS = {"status", "priority", "intent"}

_counter_lock = threading.Lock()


# ---------------------------------------------------------------------------
# XEN-VHP-3: Server-side actor allow-list
# ---------------------------------------------------------------------------
# Reconciled against heads.yaml (Xenia canonical head registry):
#   - xenia-tickets.send_response is listed ONLY for hermes/escalation-handoff.
#   - xenia-tickets.execute_approved is listed ONLY for hermes/escalation-handoff.
#   - There is NO "human" role in heads.yaml; "human" has been REMOVED from both
#     allow-lists (fix #6).  A human-reviewed response is submitted via hermes.
#
# FIXME(auth): the actor field is self-reported/spoofable by any MCP caller.
# Unforgeable caller identity (e.g. mTLS, signed JWT, session binding) is
# tracked in WS-AUTH.  Until that lands, this allow-list is a defense-in-depth
# speed bump, not a cryptographic guarantee.

_SEND_RESPONSE_ALLOWED_ACTORS: frozenset[str] = frozenset({
    "hermes",
    "escalation-handoff",
    # "human" REMOVED — heads.yaml grants no tool to a "human" role (fix #6).
    # A human reviewer acts through the hermes head.
})

_EXECUTE_APPROVED_ALLOWED_ACTORS: frozenset[str] = frozenset({
    "hermes",
    "escalation-handoff",
    # "human" REMOVED — heads.yaml grants no tool to a "human" role (fix #6).
})


def _check_actor_authz(actor: str, allowed: frozenset[str]) -> dict[str, Any] | None:
    """Return an _err dict if actor is not in allowed, else None.
    FIXME(auth): actor is self-reported — see module note above.
    """
    if actor not in allowed:
        return _err(
            "FORBIDDEN_ACTOR",
            f"Actor {actor!r} is not authorised to call this tool. "
            f"Allowed actors: {sorted(allowed)}. "
            "FIXME(auth): actor field is self-reported; unforgeable identity tracked in WS-AUTH.",
        )
    return None


# ---------------------------------------------------------------------------
# XEN-VHP-1: Server-side PII scan with normalization
# ---------------------------------------------------------------------------
# Before scanning we apply:
#   1. HTML-unescaping  (e.g. &lt; -> <, &#64; -> @)
#   2. NFKC unicode normalization (compatibility decomposition, full-width digits etc.)
#   3. For SSN / credit-card patterns we also scan a separator-stripped variant
#      (remove '.', '-', ' ') so dotted/spaced forms are caught.
#
# These run server-side regardless of hook state.  The hook is the redaction
# path; the server is the last-resort block gate.

_PII_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # Email address — run on HTML-unescaped + NFKC-normalised text
    (
        "EMAIL",
        re.compile(
            r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b",
        ),
    ),
    # US Social Security Number (ddd-dd-dddd or ddd.dd.dddd or condensed dddddddddd)
    # Separator-stripped variant handles 123.45.6789 and 123 45 6789
    (
        "US_SSN",
        re.compile(
            r"\b(?!000|666|9\d{2})\d{3}[-\s.]?(?!00)\d{2}[-\s.]?(?!0000)\d{4}\b",
        ),
    ),
    # Credit card — 13-19 contiguous or space/dash/dot-grouped digits.
    # Separator-stripped variant handles 4111.1111.1111.1111 etc.
    (
        "CREDIT_CARD",
        re.compile(
            r"\b(?:\d[ \-.]?){12,18}\d\b",
        ),
    ),
    # US/Canada/international phone
    (
        "PHONE",
        re.compile(
            r"(?<!\d)"
            r"(?:\+?1[\s.\-]?)?"
            r"(?:\(\d{3}\)|\d{3})"
            r"[\s.\-]?"
            r"\d{3}"
            r"[\s.\-]?"
            r"\d{4}"
            r"(?!\d)",
        ),
    ),
]


def _normalize_for_pii(text: str) -> str:
    """HTML-unescape and NFKC-normalize for PII scanning."""
    unescaped = html.unescape(text)
    return unicodedata.normalize("NFKC", unescaped)


def _scan_pii(text: str) -> list[str]:
    """Return a deduplicated list of PII category names found in *text*.

    Normalizes the input (HTML-unescape, NFKC) before scanning.  For SSN and
    credit-card patterns also scans a separator-stripped variant so dotted/spaced
    forms (123.45.6789, 4111.1111.1111.1111) are caught.
    """
    normalized = _normalize_for_pii(text)
    # Separator-stripped variant for digit-only pattern evasion.
    # Strip: whitespace, dot, hyphen, forward-slash, and zero-width Unicode
    # characters (U+200B ZWSP, U+200C ZWNJ, U+200D ZWJ, U+FEFF BOM/ZWNBSP)
    # so that 123/45/6789 and zero-width-separated digits are caught.
    stripped = re.sub(r"[\s.\-/​‌‍﻿]", "", normalized)

    found: list[str] = []
    seen: set[str] = set()

    for category, pattern in _PII_PATTERNS:
        if category in seen:
            continue
        # Primary scan on normalized text
        if pattern.search(normalized):
            found.append(category)
            seen.add(category)
            continue
        # Secondary scan on separator-stripped text for digit-heavy patterns
        if category in ("US_SSN", "CREDIT_CARD") and pattern.search(stripped):
            found.append(category)
            seen.add(category)

    return found


# ---------------------------------------------------------------------------
# XEN-VHP-4: Money / commitment lexicon
# ---------------------------------------------------------------------------
# NOTE: Regex is intentionally over-inclusive (false-positive tolerant) because
# a miss means sending an unreviewed money commitment to a customer.
#
# IMPORTANT — regex is NOT airtight:
#   - Adversarial phrasing ("We can provide a monetary adjustment of five hundred
#     US dollars") may evade word-boundary patterns.
#   - Robust commitment detection requires SIGNED STRUCTURED COMMITMENT METADATA
#     (a separate workflow step producing a commitment object, not free text).
#   - Tracked follow-up: implement structured commitment signing in the
#     Eunomia/Hermes pipeline so commitments are structurally identified, not
#     inferred from response text.  This regex layer is defense-in-depth only.

_MONEY_PATTERN = re.compile(
    r"\b("
    # Explicit action verbs
    r"refund|credit|reimburse|reimbursement|compensation|compensate|"
    r"discount|waive|waiver|remit|remittance|"
    r"goodwill|adjustment|"
    # Phrasing — "we will/we'll pay/send you/issue"
    r"we will pay|we'll pay|we are paying|"
    r"we will send you|we'll send you|"
    r"you will receive|you'll receive|"
    r"pay you|paying you|"
    r"send you"
    r")\b"
    r"|"
    # Currency amounts: $N, USD N, N dollars/euros/bucks/cents
    r"\$\s*\d"
    r"|USD\s*\d"
    r"|\d+\s*(?:dollars?|cents?|euros?|bucks?|USD)",
    re.IGNORECASE,
)


def _is_money_commitment(text: str) -> bool:
    """Return True if the text contains money/commitment language.

    See module-level note: this regex is defense-in-depth only; structured
    commitment metadata is the correct long-term solution.
    """
    return bool(_MONEY_PATTERN.search(text))


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

    Security-relevant keys (SECURITY_KEYS below) are duplicate-detected: if the
    same key appears more than once in the document the value is set to the
    sentinel _DUPLICATE_KEY_SENTINEL so that callers can fail-closed rather than
    silently last-winning to a potentially attacker-controlled value.
    """
    SECURITY_KEYS = {"status", "expires_at", "issued_by", "action", "scope", "ticket_id"}
    _DUPLICATE_KEY_SENTINEL = "__DUPLICATE_KEY__"

    result: dict[str, str] = {}
    seen_keys: set[str] = set()
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        key_part, _, val_part = line.partition(":")
        key = key_part.strip()
        val = val_part.strip()
        if " #" in val:
            val = val[:val.index(" #")].strip()
        if (val.startswith('"') and val.endswith('"')) or \
           (val.startswith("'") and val.endswith("'")):
            val = val[1:-1]
        if key in SECURITY_KEYS and key in seen_keys:
            result[key] = _DUPLICATE_KEY_SENTINEL
        else:
            result[key] = val
        seen_keys.add(key)
    return result


def _find_valid_approval(
    approvals_dir: Path,
    ticket_id: str,
    action: str,
    scope: str,
    approval_id: str,
) -> tuple[bool, str, dict[str, str]]:
    """Search approvals_dir for a matching, unexpired, approved artifact.

    XEN-VHP-3 binding fix (issue #3):
      - ticket_id: EXACT non-empty match required (artifact missing ticket_id -> rejected)
      - action:    EXACT case-insensitive match, non-empty required
      - scope:     EXACT match, non-empty required
      - approval_id: EXACT stem match (no substring/inclusion matching)

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
        # EXACT approval_id stem match — stem only, no filename-with-extension
        # acceptance (fix #3b: tighten to stem so the id is exact and unambiguous).
        if approval_id:
            if fpath.stem != approval_id:
                continue

        try:
            text = fpath.read_text(encoding="utf-8")
        except OSError:
            continue

        parsed = _parse_flat_yaml(text)

        # Status: exact "approved" required
        raw_status = parsed.get("status", "")
        if raw_status != "approved":
            continue

        # ticket_id: must be present and EXACTLY match (fix #3 — no missing allowed)
        art_ticket = parsed.get("ticket_id", "")
        if not art_ticket or art_ticket == "__DUPLICATE_KEY__":
            continue  # missing ticket_id in artifact -> rejected
        if art_ticket != ticket_id:
            continue

        # action: must be present and EXACTLY match (case-sensitive) (fix #3a)
        art_action = parsed.get("action", "")
        if not art_action or art_action == "__DUPLICATE_KEY__":
            continue  # missing action -> rejected
        if art_action != action:
            continue

        # scope: must be present and EXACTLY match (fix #3 — no missing allowed)
        art_scope = parsed.get("scope", "")
        if not art_scope or art_scope == "__DUPLICATE_KEY__":
            continue  # missing scope -> rejected
        if art_scope != scope:
            continue

        # issued_by: must be present, non-empty, not sentinel
        issued_by = parsed.get("issued_by", "")
        if issued_by == "__DUPLICATE_KEY__" or not issued_by.strip():
            continue

        # expires_at: must be present, parseable, and in the future
        expires_str = parsed.get("expires_at", "")
        if not expires_str or expires_str == "__DUPLICATE_KEY__":
            continue
        try:
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
        subject      = str(args.get("subject") or "").strip()
        body         = str(args.get("body") or "").strip()
        customer_ref = str(args.get("customer_ref") or "").strip()
        priority     = str(args.get("priority") or "P3").upper()
        intent       = args.get("intent")

        if not subject:
            return _err("MISSING_FIELD", "subject is required")
        if not body:
            return _err("MISSING_FIELD", "body is required")
        if not customer_ref:
            return _err("MISSING_FIELD", "customer_ref is required")

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
            "ticket_id":    ticket_id,
            "status":       "open",
            "priority":     priority,
            "intent":       intent,
            "customer_ref": customer_ref,
            "subject":      subject,
            "created_at":   now,
            "updated_at":   now,
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
    #
    # Enforcement order (fail-closed at each step):
    #   1. Field validation + ticket lookup
    #   2. VHP-3: actor authz — actor MUST be non-empty, MUST be on allow-list
    #   3. VHP-2: clearance token (signed dict from sign.py) — HMAC + body binding
    #   4. VHP-1: PII scan (normalized)
    #   5. VHP-4: money/commitment -> approval required
    #   6. Append + save
    # ------------------------------------------------------------------
    def send_response(args: dict[str, Any]) -> dict[str, Any]:
        ticket_id       = str(args.get("ticket_id") or "").strip()
        body            = str(args.get("body") or "").strip()
        actor           = str(args.get("actor") or "").strip()
        clearance_token = args.get("clearance_token")  # JSON string or dict
        approval_id     = str(args.get("approval_id") or "").strip()

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

        # ---- VHP-3: actor authorization (unconditional) ----
        # FIXME(auth): actor is self-reported; unforgeable identity tracked in WS-AUTH.
        authz_err = _check_actor_authz(actor, _SEND_RESPONSE_ALLOWED_ACTORS)
        if authz_err:
            return authz_err

        # ---- VHP-2: signed clearance token ----
        # Token is a JSON-encoded dict produced by sign.py's mint() containing
        # a "body" field and a "sig" envelope.  Verification:
        #   - Parse JSON, check sig.alg == HMAC-SHA256
        #   - Recompute HMAC over canonical(token_minus_sig) — same as sign.py
        #   - Assert token["body"] == this request body (constant-time)
        # Degraded/unsigned tokens REJECTED (no bypass).
        # actor='human' bypass REMOVED (fix #2, #6).
        # FIXME(auth): actor is still self-reported; unforgeable identity in WS-AUTH.
        clearance_result = verify_clearance_token(body, clearance_token)
        if not clearance_result["ok"]:
            return _err(
                "CLEARANCE_INVALID",
                f"send_response blocked: clearance token invalid. "
                f"Reason: {clearance_result['reason']}. "
                "The Themis -> Eunomia pipeline must issue a signed clearance token "
                "(sign.py mint() dict with body field + sig envelope) "
                "before send_response is called. "
                "Degraded/unsigned tokens are not accepted. "
                "actor='human' bypass has been removed (XEN-VHP-2).",
            )

        # ---- VHP-1: server-side PII scan (normalized) ----
        pii_categories = _scan_pii(body)
        if pii_categories:
            return _err(
                "PII_DETECTED",
                f"send_response blocked: unredacted PII detected in body. "
                f"Categories: {pii_categories}. "
                "Redact all PII before sending (hook redaction must run first; "
                "this server-side check is a last-resort gate, not a substitute).",
            )

        # ---- VHP-4: money/commitment requires approval artifact ----
        if _is_money_commitment(body):
            approvals_dir = _approvals_dir(root)
            valid, reason, _parsed = _find_valid_approval(
                approvals_dir,
                ticket_id=ticket_id,
                action="send_response",
                scope="monetary",
                approval_id=approval_id,
            )
            if not valid:
                return _err(
                    "APPROVAL_REQUIRED",
                    f"send_response blocked: body contains monetary/commitment language "
                    f"but no valid approval artifact found. Reason: {reason}. "
                    "Required: hearth/approvals/APPROVAL-<ticket_id>-*.yaml with "
                    "status=approved, action=send_response, scope=monetary, unexpired, "
                    "issued_by present, ticket_id present. Pass approval_id in args.",
                )

        # ---- All checks passed: append and save ----
        now = _now_iso()
        ticket["history"].append({
            "ts":    now,
            "actor": actor,
            "kind":  "response",
            "body":  body,
        })
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
    #
    # Enforcement order:
    #   1. Field validation + ticket lookup
    #   2. VHP-3: actor authz — actor REQUIRED, non-empty, MUST be on allow-list
    #             (unconditional — fix #1; no optional path)
    #   3. Article V: approval artifact check (deny-by-default)
    # ------------------------------------------------------------------
    def execute_approved(args: dict[str, Any]) -> dict[str, Any]:
        """DENY-BY-DEFAULT. The server NEVER executes without a valid, unexpired,
        matching approval artifact — even if the hook layer was bypassed.
        (Constitution Article V, defense-in-depth Layer 0.)

        actor is REQUIRED and MUST be on the allow-list (fix #1).
        """
        ticket_id   = str(args.get("ticket_id") or "").strip()
        action      = str(args.get("action") or "").strip()
        scope       = str(args.get("scope") or "").strip()
        approval_id = str(args.get("approval_id") or "").strip()
        actor       = str(args.get("actor") or "").strip()

        if not ticket_id:
            return _err("MISSING_FIELD", "ticket_id is required")
        if not action:
            return _err("MISSING_FIELD", "action is required")
        if not scope:
            return _err("MISSING_FIELD", "scope is required")
        if not approval_id:
            return _err("MISSING_FIELD", "approval_id is required")

        # ---- VHP-3: actor REQUIRED and authorization UNCONDITIONAL (fix #1) ----
        # actor is never optional for execute_approved — there must always be an
        # identified, allow-listed caller.  No path may skip this check.
        # FIXME(auth): actor is self-reported; unforgeable identity tracked in WS-AUTH.
        if not actor:
            return _err(
                "MISSING_FIELD",
                "actor is required for execute_approved; "
                "all executions must have an identified, allow-listed caller.",
            )
        authz_err = _check_actor_authz(actor, _EXECUTE_APPROVED_ALLOWED_ACTORS)
        if authz_err:
            return authz_err

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
                "status=approved, unexpired, matching action+scope, issued_by present, "
                "ticket_id present (exact match)."
            )

        now = _now_iso()
        ticket["history"].append({
            "ts":          now,
            "actor":       parsed.get("issued_by", "approved-human"),
            "kind":        "approved-action",
            "body":        f"action={action} scope={scope} approval_id={approval_id} artifact={reason}",
        })

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
        "xenia-tickets.create":           create,
        "xenia-tickets.get":              get,
        "xenia-tickets.list":             list_tickets,
        "xenia-tickets.comment":          comment,
        "xenia-tickets.update_fields":    update_fields,
        "xenia-tickets.send_response":    send_response,
        "xenia-tickets.recommend":        recommend,
        "xenia-tickets.execute_approved": execute_approved,
        "xenia-tickets.ping":             ping,
    }


def _safe_load_status(p: Path) -> str:
    try:
        return json.loads(p.read_text(encoding="utf-8")).get("status", "")
    except (json.JSONDecodeError, OSError):
        return ""


def main() -> None:
    run_server("xenia-tickets", _tool_handlers())
