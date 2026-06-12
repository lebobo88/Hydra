"""WS-AUTH Phase 2 — Trusted mint helper for PreToolUse hook dispatch.

Called by the Xenia PreToolUse hook (.ps1 / .sh) via:

    python -m mcp_servers.xenia_tickets.mint_for_tool \\
        --tool-name xenia-tickets.send_response \\
        --ticket-id 000123

Exits 0 and prints a compact JSON capability token dict to stdout on success.
Exits 1 and prints NOTHING to stdout (error on stderr) on failure — the hook
treats a non-0 exit or empty stdout as "no token" and blocks the call (fail-closed).

SECURITY INVARIANTS
-------------------
1. actor_id embedded in the token comes ONLY from os.environ["CLAUDE_HOOK_AGENT_NAME"].
   There is NO actor_id parameter, NO CLAUDE_AGENT_NAME fallback, NO CLI argument
   for identity.  If CLAUDE_HOOK_AGENT_NAME is unset or empty -> return None.

2. HYDRA_OPERATOR_KEY is NEVER written to stdout, stderr, or any log.  If it is
   unset -> return None (fail-closed).  The hook gets no token and must exit 2.
   Degraded tokens are NEVER emitted; the absence of the key is treated as a
   hard failure, not a soft degraded path.

3. mint_token_for_tool() has NO actor_id parameter.  The function signature is:
       mint_token_for_tool(*, tool_name: str, ticket_id: str) -> dict | None
   This makes it impossible for a caller to supply an identity.

4. Log lines contain ONLY fixed error codes and exception type names.  No
   caller-supplied values (tool_name, agent name, ticket_id) are interpolated
   into log output — an attacker cannot exfiltrate data through error logs.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

# Ensure repo root is on path (this file lives at
# mcp_servers/xenia_tickets/mint_for_tool.py -> root is two levels up).
_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parents[2]))

from mcp_servers.xenia_tickets.server import mint_caller_capability  # noqa: E402

_log = logging.getLogger(__name__)

# Exact allow-list of canonical Xenia ticket tool names -> capability strings.
#
# Fix A: ONLY tools that identify the xenia-tickets MCP server segment are
# accepted.  Bare names like "send_response" and foreign prefixes like
# "mcp__evil__x_send_response" are rejected — they do not name the xenia-tickets
# server.  The gateway forms use the "xenia-tickets" server slug (dot or
# underscore separator depending on gateway registration).
#
# A foreign tool whose name merely ends in "_send_response" or "send_response"
# will NOT match this table and will receive UNKNOWN_TOOL -> None (fail-closed).
_TOOL_TO_CAPABILITY: dict[str, str] = {
    # Primary dot-separated MCP forms (Claude Code native tool name).
    "xenia-tickets.send_response":    "xenia.send_response",
    "xenia-tickets.execute_approved": "xenia.execute_approved",
    # Hydra gateway forms: mcp__xenia-tickets__send_response (lowercased by gateway).
    "mcp__xenia-tickets__send_response":    "xenia.send_response",
    "mcp__xenia-tickets__execute_approved": "xenia.execute_approved",
    # Underscore-separator variants (some gateway registrations replace hyphens).
    "xenia_tickets.send_response":    "xenia.send_response",
    "xenia_tickets.execute_approved": "xenia.execute_approved",
    "mcp__xenia_tickets__send_response":    "xenia.send_response",
    "mcp__xenia_tickets__execute_approved": "xenia.execute_approved",
}


def mint_token_for_tool(
    *,
    tool_name: str,
    ticket_id: str,
) -> dict | None:
    """Mint a caller-capability token for *tool_name* bound to *ticket_id*.

    Identity (actor_id) is read EXCLUSIVELY from os.environ["CLAUDE_HOOK_AGENT_NAME"].
    There is NO actor_id parameter.  This is intentional: the function signature
    itself enforces that callers cannot supply an identity.

    Returns the token dict on success, None on any failure.
    The caller is responsible for exiting non-zero when None is returned.

    Failure paths (all return None — hook must exit 2):
      - CLAUDE_HOOK_AGENT_NAME unset or empty
      - tool_name not in _TOOL_TO_CAPABILITY
      - ticket_id empty/whitespace
      - HYDRA_OPERATOR_KEY unset/empty (key absent = fail-closed; no degraded token)
      - mint_caller_capability raises (logged as type name only, no detail)
      - mint_caller_capability returns a degraded token (sig.degraded is True or
        sig.value is None) — this is treated as a failure, not a soft path
    """
    # -----------------------------------------------------------------------
    # 1. Resolve trusted actor identity — ONLY from CLAUDE_HOOK_AGENT_NAME.
    #    No CLAUDE_AGENT_NAME fallback.  No parameter override.
    # -----------------------------------------------------------------------
    actor_id = os.environ.get("CLAUDE_HOOK_AGENT_NAME", "").strip()
    if not actor_id:
        print(
            "WS-AUTH-PHASE2-ERROR: IDENTITY_UNSET "
            "CLAUDE_HOOK_AGENT_NAME is unset or empty. "
            "Fail-closed: no token will be minted.",
            file=sys.stderr,
        )
        return None

    # -----------------------------------------------------------------------
    # 2. Resolve capability from tool name (table lookup only, no inference).
    #    Fix 8: log only a fixed code, NOT the raw tool_name value.
    # -----------------------------------------------------------------------
    capability = _TOOL_TO_CAPABILITY.get(tool_name)
    if capability is None:
        print(
            "WS-AUTH-PHASE2-ERROR: UNKNOWN_TOOL "
            "not in capability table. Fail-closed.",
            file=sys.stderr,
        )
        return None

    # -----------------------------------------------------------------------
    # 3. Validate ticket_id — must be a non-empty string.
    # -----------------------------------------------------------------------
    if not ticket_id or not ticket_id.strip():
        print(
            "WS-AUTH-PHASE2-ERROR: EMPTY_TICKET_ID "
            "ticket_id is empty. Fail-closed: no token minted.",
            file=sys.stderr,
        )
        return None
    ticket_id = ticket_id.strip()

    # -----------------------------------------------------------------------
    # 4. Require signing key — ABSENT KEY = FAIL CLOSED (Fix 6).
    #    Do NOT mint a degraded token.  The hook must block (exit 2) if
    #    no token is returned; a degraded token would reach the server and
    #    still be rejected, but it leaks a trust-looking envelope.
    # -----------------------------------------------------------------------
    if not os.environ.get("HYDRA_OPERATOR_KEY", "").strip():
        print(
            "WS-AUTH-PHASE2-ERROR: KEY_ABSENT "
            "HYDRA_OPERATOR_KEY not configured. "
            "Fail-closed: no token minted.",
            file=sys.stderr,
        )
        return None

    # -----------------------------------------------------------------------
    # 5. Mint.  actor_id is from the TRUSTED framework env — set above.
    #    Fresh jti auto-generated per call (single-use enforcement).
    # -----------------------------------------------------------------------
    try:
        token = mint_caller_capability(
            actor_id=actor_id,    # TRUSTED env identity exclusively
            capability=capability,
            ticket_id=ticket_id,
            ttl_seconds=120,      # short-lived: hook-to-server single use
        )
    except Exception as exc:  # noqa: BLE001
        # Log exception TYPE only — never the string representation, which
        # could contain key material or other sensitive details.
        print(
            f"WS-AUTH-PHASE2-ERROR: MINT_EXCEPTION type={type(exc).__name__} "
            "Fail-closed.",
            file=sys.stderr,
        )
        return None

    # -----------------------------------------------------------------------
    # 6. Reject degraded tokens (Fix 6).
    #    mint_caller_capability returns a degraded token when the key is
    #    absent or invalid.  We guard at step 4, but double-check here for
    #    defence-in-depth (e.g. if the key was cleared between steps).
    # -----------------------------------------------------------------------
    sig = token.get("sig", {})
    if sig.get("degraded") is True or sig.get("value") is None:
        print(
            "WS-AUTH-PHASE2-ERROR: DEGRADED_TOKEN "
            "Minted token is degraded (no valid signature). "
            "Fail-closed: discarding token.",
            file=sys.stderr,
        )
        return None

    return token


def _main() -> None:
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr,
                        format="%(levelname)s: %(message)s")

    parser = argparse.ArgumentParser(
        description=(
            "WS-AUTH Phase 2: mint a caller-capability token from the trusted "
            "framework identity (CLAUDE_HOOK_AGENT_NAME) for a Xenia tool call. "
            "Identity comes ONLY from the environment — there is no --actor-id flag."
        ),
    )
    parser.add_argument(
        "--tool-name", required=True,
        help="MCP tool name, e.g. 'xenia-tickets.send_response'",
    )
    parser.add_argument(
        "--ticket-id", required=True,
        help="Ticket ID the token is bound to (resource_id + workflow_id).",
    )
    args = parser.parse_args()

    token = mint_token_for_tool(
        tool_name=args.tool_name,
        ticket_id=args.ticket_id,
        # No actor_id: identity always from CLAUDE_HOOK_AGENT_NAME env.
    )
    if token is None:
        # Error already written to stderr above.
        sys.exit(1)

    # Write the token JSON object to stdout — the hook reads this.
    # Compact separators: no extra whitespace, unambiguous single-line output.
    # This is the ONLY thing written to stdout.
    sys.stdout.write(json.dumps(token, separators=(",", ":")) + "\n")
    sys.stdout.flush()
    sys.exit(0)


if __name__ == "__main__":
    _main()
