"""Attended dispatch seam (W1.2 step B, Hydra#85) -- audit mode.

One place where an attended host_action crosses from the engine to the host,
AFTER host_bridge has already redacted it at build time (step A, 642c2b2):

* :func:`budget_gate` -- the in-graph budget check, applied only when a NEW
  attended stage would open (never touches state, never files a HITL);
* :func:`prepare_host_action` -- for ``squad`` actions only, appends the
  squad's tool-scope directive to the prompt (idempotent: a prompt that
  already carries the directive is left unchanged); ``engineer`` and
  ``judge`` actions are returned byte-identical. This module does its own
  redaction of NOTHING -- the directive text itself is passed once through
  ``host_bridge._outbound_text`` before it is appended, and that is the
  only text this module ever adds to a host-visible action;
* :func:`write_dispatch_ticket` -- records an auditable ticket (hashes only,
  never raw prompt/instructions/artifact text) under
  ``<project_root>/.hydra/<workflow_id>/attended/tickets/``.

Audit mode: nothing here refuses a dispatch except the budget gate; a ticket
write failure is fail-soft -- the host_action is still returned, with
``dispatch_ticket`` set to ``None``. Runtime-agnostic: no provider SDK
imports.

CONTRACT BOUNDARY. The fail-soft contract below covers exactly:

(a) :func:`write_dispatch_ticket` and everything it calls;
(b) :func:`safe_emit`;
(c) ``cli._apply_dispatch_seam``, from its first statement to its return;
(d) the two ``attended.budget_blocked`` emit sites in ``cli.py``.

It does NOT cover cursor reads, cursor saves or telemetry done by the
surrounding step, poll, self-heal or submit code before or after
``_apply_dispatch_seam`` is called -- in particular the open-cursor
RECOVERY re-serve lookup in ``cli._cmd_attended_step`` (its
``Path.exists``/``load_cursor`` step). That code keeps its behaviour at
base ``ebf9376`` -- a fault there is reported via :func:`safe_emit` and
answered with the dedicated ``dispatch_seam_cursor_unreadable`` result
rather than silently falling through to ``begin_squad_stage`` -- and is
out of scope for the fail-soft guarantee itself (a follow-up issue tracks
unifying the two).

FAIL-SOFT CONTRACT (R3-tail-style property, Hydra#85 cross-vendor retry 2,
tightened cross-vendor retry 3). No exception raised by file I/O, an ``os``
call (``makedirs``/``open``/``link``/``remove``/``path.exists``), a
telemetry ``emit``, JSON encode or decode, text encoding (``str.encode``),
hashing, datetime arithmetic, or env parsing -- while on an audit-mode path
-- may escape to the CLI caller (``hydra_core.cli``). "Audit-mode path"
means: :func:`write_dispatch_ticket` and everything it calls (including
:func:`ticket_path`'s identity hash and the FIRST :func:`_check_reuse` call
-- both now INSIDE the function's single outer guard, not ahead of it; see
cross-vendor retry 3 findings 1-2); :func:`prepare_host_action` and
everything it calls; ``cli._apply_dispatch_seam`` (now wrapped end to end
in a single outer ``try/except Exception``, per the contract boundary
above) and the two ``attended.budget_blocked`` emit sites in ``cli.py``.

In :func:`write_dispatch_ticket` the ONLY code outside the single outer
``try/except Exception`` is the validation of ``workflow_id`` (a pure
regex match -- deliberate exception E1 below) and the unpacking of
``call_key``/``agent_type``/``workflow_id``/``run_id`` out of the caller's
arguments, none of which can raise. Everything else -- the identity hash,
the ticket path, both reuse checks, the clock, the uuid, the write/publish/
cleanup sequence and its own temp-file cleanup retries -- runs inside that
one guard. A cleanup fault that persists after both attempts (``os.remove``
on the temp file) never nulls a response that ``os.link`` already
published, but it is reported through :func:`safe_emit` as
``dispatch.ticket_tmp_cleanup_failed`` (exception type name and the temp
file's bare name only -- no path) rather than swallowed silently.

Three defined results:

1. A ticket problem (invalid identity, a write/publish/cleanup fault, an
   unreadable or mismatched existing file) -- ``dispatch_ticket`` is
   ``None``; ``host_action`` is still returned unchanged.
2. A cursor-save failure, but only when the prepared action actually
   differs from what is on disk (the squad tool-scope directive changed
   it) -- exactly
   ``{"ok": False, "status": "dispatch_seam_persist_failed",
   "cursor_path": <path>, "retryable": True}``, even when the telemetry
   emit for this case ALSO fails. The SAME result applies when the action
   changed but ``cli.py`` cannot even VERIFY the on-disk cursor (its
   ``Path.exists`` check itself raises): an unverifiable cursor must never
   be left disagreeing with a changed, ticketed action, so a changed
   action is served (and ticketed) ONLY when it truly agrees with the
   cursor -- either because the cursor was read and successfully updated,
   or because the action is provably unchanged from what the cursor
   already holds (a ``prepare_host_action`` fault serves the ORIGINAL
   action, which by construction still agrees).
3. A telemetry failure (anywhere on an audit-mode path) never changes the
   response -- it is silently absorbed by :func:`safe_emit`, the one
   guarded helper every emit on these paths goes through.

DELIBERATE EXCEPTIONS (the only raises this module condones on an
audit-mode path):

* E1 -- an invalid ``workflow_id`` (``ticket_path`` raising ``ValueError``):
  :func:`write_dispatch_ticket` returns ``None`` with NO
  ``dispatch.ticket_write_failed`` telemetry, because emitting would itself
  join the unsanitised ``workflow_id`` into a trace path. Tested by
  ``TestM1TicketIdentity.test_invalid_workflow_id_writes_no_file_anywhere``
  and ``TestM7FailSoftProperty::test_e1_invalid_workflow_id_no_telemetry``.
* E2 -- the budget gate's ``budget_blocked`` refusal (:func:`budget_gate`
  returning non-``None``) is the ONE intended refusal in this module;
  exceptions from ``governance.should_block_for_budget`` or the caller's
  ``state`` object are caller-state errors, out of scope for this contract.
  Tested by ``TestM7FailSoftProperty::test_e2_budget_gate_state_error_is_out_of_scope``.
* E3 -- programming errors in the caller's own arguments (a non-``dict``
  ``host_action`` passed directly to :func:`write_dispatch_ticket`, or a
  ``kind`` outside ``{"squad", "engineer", "judge"}``) are out of scope and
  may raise. Tested by
  ``TestM7FailSoftProperty::test_e3_programming_error_in_callers_argument_may_raise``.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from . import governance
from .strict_json import dumps_strict
from .telemetry import emit
from .tool_scope import build_tool_scope_directive

if TYPE_CHECKING:  # pragma: no cover
    from .squad_loader import SquadPack
    from .state import HydraState

TICKET_SCHEMA: int = 1
TICKET_TTL_ENV: str = "HYDRA_DISPATCH_TICKET_TTL_S"
DISPATCH_TICKET_TTL_SECONDS: int = 3600

# Upper bound for an env-supplied TTL override: 10 years. Anything beyond
# this is rejected (falls back to the default) rather than handed to
# `timedelta`/`datetime` arithmetic, which raises `OverflowError` for an
# astronomically large `seconds=` value -- e.g.
# `HYDRA_DISPATCH_TICKET_TTL_S=999999999999999999999` parses fine as a
# positive `int` but is not a usable TTL.
_TICKET_TTL_MAX_SECONDS: int = 10 * 365 * 24 * 3600

# Cursor states whose pending_action is a judge action (await_smoke and
# stalled_infra keep the judge's pending_action/call_key while they wait).
JUDGE_PENDING_STATES: frozenset[str] = frozenset(
    {"await_judge", "stalled_infra", "await_smoke"})

Kind = Literal["squad", "engineer", "judge"]

# Same rule as ``hydra_core.cli._WORKFLOW_ID_RE`` (the attended CLI entry
# points); duplicated here because importing ``cli`` from this module would
# be a cycle. A test pins the two patterns together.
WORKFLOW_ID_RE: re.Pattern[str] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9\-_]{0,63}$")


def ticket_ttl_seconds() -> int:
    """TTL in seconds: the env override when it parses as a positive int AND
    is representable in `timedelta`/`datetime` arithmetic (<=
    :data:`_TICKET_TTL_MAX_SECONDS`), else :data:`DISPATCH_TICKET_TTL_SECONDS`.

    A value like ``999999999999999999999`` parses fine as a positive
    ``int`` but raises ``OverflowError`` once used as
    ``timedelta(seconds=...)`` -- that arithmetic happens at the ticket's
    ``created_at``/``expires_at`` computation, outside any fail-soft
    handler, so dispatch would crash instead of degrading. Reject it here,
    before it ever reaches that arithmetic."""
    raw = os.environ.get(TICKET_TTL_ENV)
    if raw is None:
        return DISPATCH_TICKET_TTL_SECONDS
    try:
        val = int(str(raw).strip())
    except (ValueError, AttributeError):
        return DISPATCH_TICKET_TTL_SECONDS
    if val <= 0 or val > _TICKET_TTL_MAX_SECONDS:
        return DISPATCH_TICKET_TTL_SECONDS
    return val


def _expires_at(created: datetime, ttl_seconds: int) -> datetime:
    """``created + timedelta(seconds=ttl_seconds)``, defended a second time:
    even if ``ttl_seconds`` were ever something ``ticket_ttl_seconds``'s own
    bound missed, ``timedelta``/``datetime`` arithmetic raising
    ``OverflowError`` or ``ValueError`` here falls back to the default TTL
    rather than escaping `write_dispatch_ticket` -- audit mode is fail-soft
    end to end, not just at the env-parsing boundary.

    The fallback add is defended a THIRD time: if ``created`` itself is
    already close enough to ``datetime.max`` that even the default 3600s
    TTL overflows, fall back to ``created`` unchanged -- an already-expired
    ticket (fail closed) rather than an exception escaping this helper."""
    try:
        return created + timedelta(seconds=ttl_seconds)
    except (OverflowError, ValueError):
        try:
            return created + timedelta(seconds=DISPATCH_TICKET_TTL_SECONDS)
        except (OverflowError, ValueError):
            return created


def budget_gate(state: "HydraState") -> dict[str, Any] | None:
    """None unless the workflow budget is exhausted. Pure: files no HITL and
    mutates nothing. The caller adds ``task_id`` / ``squad_slug``."""
    if not governance.should_block_for_budget(state):
        return None
    return {
        "ok": False,
        "status": "budget_blocked",
        "workflow_id": str(state.workflow_id),
        "spent_usd": float(state.budget.spent_usd),
        "budget_usd": float(state.budget.budget_usd),
    }


# --------------------------------------------------------------------------- #
# Ticket identity                                                            #
# --------------------------------------------------------------------------- #

def _safe(value: str) -> str:
    """Same character rule as ``host_bridge.cursor_path``'s sanitiser: keep
    alnum, '-' and '_'. Copied here (not imported) per the W1.2 contract that
    this module never imports ``host_bridge`` at module scope."""
    return "".join(c for c in str(value) if c.isalnum() or c in "-_")


def _safe_run(run_id: str) -> str:
    return _safe(run_id) or "run"


def _safe_call(call_key: str) -> str:
    return _safe(call_key) or "call"


def _length_prefixed(s: str) -> bytes:
    # errors="surrogatepass": a lone surrogate in `workflow_id`/`run_id`/
    # `call_key` (W1.2 fail-soft property, Hydra#85 cross-vendor retry 2)
    # must not raise `UnicodeEncodeError` here -- that exception subclasses
    # `ValueError`, which `ticket_path` otherwise catches ONLY for a
    # genuinely invalid `workflow_id`, so an unguarded plain `.encode("utf-8")`
    # would misreport a lone surrogate in `run_id`/`call_key` as an invalid
    # `workflow_id` (and silently drop its telemetry -- see E1). surrogatepass
    # is lossless (unlike "replace") and deterministic/injective over `str`,
    # so the ticket identity stays exact for the one input an auditor would
    # most want recorded precisely.
    b = str(s).encode("utf-8", errors="surrogatepass")
    return str(len(b)).encode("ascii") + b":" + b


def compute_ticket_id(workflow_id: str, run_id: str, call_key: str) -> str:
    """``sha256(lp(workflow_id) + lp(run_id) + lp(call_key))``, the full 64
    hex characters. ``run_id``/``call_key`` may be empty strings; identity
    still comes from the hash, not from the sanitised filename prefixes."""
    h = hashlib.sha256()
    h.update(_length_prefixed(workflow_id))
    h.update(_length_prefixed(run_id))
    h.update(_length_prefixed(call_key))
    return h.hexdigest()


def ticket_path(
    project_root: str | Path, workflow_id: str, run_id: str, call_key: str,
) -> Path:
    """Ticket file path; refuses (``ValueError``) a workflow_id the attended
    CLI entry points would refuse, so it can never traverse out of
    ``.hydra``. The sanitised ``run_id``/``call_key`` prefixes are for
    readability only and carry no identity -- the hash does."""
    if not WORKFLOW_ID_RE.match(str(workflow_id)):
        raise ValueError(f"invalid workflow_id {str(workflow_id)!r}")
    tid = compute_ticket_id(str(workflow_id), str(run_id), str(call_key))
    rp = _safe_run(run_id)[:40]
    cp = _safe_call(call_key)[:40]
    return (Path(project_root) / ".hydra" / str(workflow_id) / "attended"
            / "tickets" / f"{rp}.{cp}.{tid[:32]}.json")


def _head_for(kind: Kind, agent_type: str) -> str:
    """Squad: the text after the first ':' (plugin prefix stripped), e.g.
    'hydra:plan-author' -> 'plan-author'; unchanged when there is no ':'.
    Engineer/judge: agent_type unchanged."""
    if kind == "squad" and ":" in agent_type:
        return agent_type.split(":", 1)[1]
    return agent_type


def canonical_prompt_text(host_action: dict[str, Any]) -> str:
    """Canonical concatenation of everything the host reads as its task:
    ``prompt``, ``instructions`` and ``artifact_text`` (those present), each
    framed with its name and length so the join is unambiguous. This is the
    text ``prompt_sha256`` is computed over -- never persisted itself."""
    parts: list[str] = []
    for key in ("prompt", "instructions", "artifact_text"):
        val = host_action.get(key)
        if isinstance(val, str):
            parts.append(f"{key}\n{len(val)}\n{val}\n")
    return "".join(parts)


# --------------------------------------------------------------------------- #
# Cursor persistence wrapper                                                 #
# --------------------------------------------------------------------------- #

def _persist_cursor(path: str | Path, cursor: dict[str, Any]) -> None:
    """Seam-level wrapper over ``host_bridge.save_cursor``. Tests patch THIS
    function, not ``host_bridge.save_cursor`` directly, because
    ``begin_squad_stage``/``begin_stage`` also call ``save_cursor`` and must
    not be disturbed by a seam-only failure injection."""
    from . import host_bridge
    host_bridge.save_cursor(path, cursor)


# --------------------------------------------------------------------------- #
# Directive pass                                                             #
# --------------------------------------------------------------------------- #

def prepare_host_action(
    host_action: dict[str, Any], *, kind: Kind, pack: "SquadPack | None",
) -> tuple[dict[str, Any], bool]:
    """Shallow copy of ``host_action``; for ``kind='squad'`` only, appends the
    pack's tool-scope directive to ``prompt`` (idempotent -- a prompt that
    already ends with the (redacted) directive is left unchanged). ``engineer``
    and ``judge`` actions come back byte-identical. Returns ``(action,
    changed)``; the input is never mutated."""
    out = dict(host_action)
    if kind != "squad":
        return out, False
    # `pack.tools` is a SquadPack dataclass field, but attended-path test
    # fixtures and any stub pack predate this seam calling
    # `build_tool_scope_directive` at all (fact (b): before W1.2 step B the
    # directive was only ever built on the engine/headless squad_node path)
    # and may carry no `tools` attribute whatsoever -- `getattr` rather than
    # a bare `pack.tools` access, so such a pack is treated the same as "no
    # tools declared" (empty directive) instead of raising.
    if pack is None or not getattr(pack, "tools", None):
        return out, False
    directive = build_tool_scope_directive(pack)
    if not directive:
        return out, False
    prompt = out.get("prompt")
    if not isinstance(prompt, str):
        return out, False
    from . import host_bridge
    red = host_bridge._outbound_text(directive, kind="squad")
    suffix = "\n\n" + red
    if prompt.endswith(suffix):
        return out, False
    out["prompt"] = prompt + suffix
    return out, True


# --------------------------------------------------------------------------- #
# Dispatch ticket                                                            #
# --------------------------------------------------------------------------- #

def _check_reuse(path: Path, workflow_id: str, run_id: str, call_key: str) -> str:
    """'' when ``path`` does not exist (proceed to write); 'ok' when it exists
    and its stored identity matches exactly; else a fail-soft reason
    ('identity_mismatch' or 'existing_unreadable')."""
    if not os.path.exists(_long_path(path)):
        return ""
    try:
        with open(_long_path(path), "r", encoding="utf-8") as f:
            data = json.loads(f.read())
    except Exception:  # noqa: BLE001
        return "existing_unreadable"
    if not isinstance(data, dict):
        return "existing_unreadable"
    if (str(data.get("workflow_id")) == workflow_id
            and str(data.get("run_id")) == run_id
            and str(data.get("call_key")) == call_key):
        return "ok"
    return "identity_mismatch"


_DRIVE_ABSOLUTE_RE: re.Pattern[str] = re.compile(r"^[A-Za-z]:[\\/]")


def _extended_length(s: str) -> str:
    """Pure string transform (no filesystem access, no platform check --
    the caller is responsible for both): the extended-length (``\\\\?\\``)
    form of a Windows absolute path.

    * Already-prefixed (``\\\\?\\...``) input is returned unchanged
      (idempotent).
    * A UNC path (``\\\\server\\share\\...``) becomes
      ``\\\\?\\UNC\\server\\share\\...`` -- the distinct extended-length UNC
      form -- never ``\\\\?\\\\\\server\\share\\...``, which is what naively
      prepending ``\\\\?\\`` to a UNC path produces and which Windows does
      not accept.
    * A drive-absolute path (``C:\\...``) becomes ``\\\\?\\C:\\...``.
    * Anything else (a relative path) is returned unchanged -- this helper
      does not resolve relative input to absolute; the caller passes an
      already-absolute path when the extended-length form is actually
      needed."""
    if s.startswith("\\\\?\\"):
        return s
    if s.startswith("\\\\"):
        return "\\\\?\\UNC\\" + s.lstrip("\\")
    if _DRIVE_ABSOLUTE_RE.match(s):
        return "\\\\?\\" + s
    return s


def _long_path(p: str | Path) -> str:
    """Extended-length form of an absolute path on Windows (bypasses the
    260-char ``MAX_PATH`` that ``os.link``/``open`` otherwise hit for a
    ticket path nested a few directories deep under a long project root --
    the 40+40+32-character ticket filename this module's identity scheme
    produces is exactly the kind of name that trips it). A no-op on POSIX
    and a no-op for relative input; see :func:`_extended_length` for the
    Windows string transform itself, which is kept as a separate pure
    function so it can be unit-tested without a real NT filesystem."""
    s = str(p)
    if os.name != "nt":
        return s
    return _extended_length(s)


def safe_emit(
    project_root: str | Path, workflow_id: str, event: str, payload: dict[str, Any],
) -> None:
    """The single guarded telemetry call for the whole dispatch seam --
    every emit in this module, plus (by explicit import) ``cli.py``'s
    ``_apply_dispatch_seam`` and its two ``attended.budget_blocked`` sites,
    goes through this function rather than calling ``telemetry.emit``
    directly. Catches every exception the underlying ``emit`` can raise
    (trace-path construction, ``open``/``write`` on the trace file, JSON
    encoding of ``payload``) and never raises.

    This is the ONLY silent ``except Exception: pass`` the fail-soft
    contract allows: a failing emit cannot itself report its own failure.
    Every OTHER caller on an audit-mode path that can still emit a failure
    event is expected to route it through here, not to swallow its own
    fault with a private bare catch."""
    try:
        emit(Path(project_root), workflow_id, event, payload)
    except Exception:  # noqa: BLE001 -- the one sanctioned silent catch; see docstring
        pass


def _emit_write_failed(
    project_root: str | Path, workflow_id: str, call_key: str, agent_type: str,
    kind: str, reason: str,
) -> None:
    safe_emit(project_root, workflow_id, "dispatch.ticket_write_failed",
              {"call_key": call_key, "ticket_kind": kind, "agent_type": agent_type,
               "reason": reason})


def write_dispatch_ticket(
    project_root: str | Path,
    workflow_id: str,
    run_id: str,
    host_action: dict[str, Any],
    *,
    kind: Kind,
    squad_slug: str | None,
    budget_usd: float | None,
    budget_remaining_usd: float | None,
    now: datetime | None = None,
) -> Path | None:
    """Atomically write the dispatch ticket for an already-prepared
    ``host_action``; returns its path, or ``None`` on a (fail-soft) write
    error or an identity mismatch against an existing file at the same path.

    First creation is published via an EXCLUSIVE link from a fully-written
    temp file (``os.link``, which fails with ``FileExistsError`` if the
    target already exists on both Windows and POSIX) rather than a
    check-then-``os.replace`` sequence, so two concurrent writers for the
    same (workflow_id, run_id, call_key) can never clobber one another: the
    loser's temp file is discarded and its call falls into the reuse rule
    against whatever the winner published.

    The reuse rule: a second write for the same identity returns the
    existing ticket unchanged (no rewrite, no new ``dispatch.ticket_written``
    event). A file at the computed path whose stored identity differs, or
    that cannot be read/parsed, is left byte-unchanged and NOT returned --
    this is reported as a fail-soft ``dispatch.ticket_write_failed`` event
    with reason ``'identity_mismatch'`` or ``'existing_unreadable'``.

    The ticket never holds raw, pre-redaction or artifact text -- only
    hashes."""
    call_key = str(host_action.get("call_key") or "")
    agent_type = str(host_action.get("agent_type") or "")
    workflow_id_s = str(workflow_id)
    run_id_s = str(run_id)

    # E1 -- the ONLY code in this function outside the single outer
    # try/except below. A pure regex match: it cannot itself raise (unlike
    # `ticket_path`, which also computes the identity hash -- see the
    # "misclassified" note just below). Checking the workflow_id on its own
    # here, BEFORE anything that touches a hash/file/clock, is what keeps
    # E1 ("invalid workflow_id, no telemetry") distinct from every other
    # exception that can happen while preparing or writing a ticket: those
    # all go through the guard and get a reported `dispatch_ticket_write_failed`
    # (cross-vendor retry 2 finding 1 -- `ticket_path` used to be the ONLY
    # guard, catching just `ValueError`, so a `RuntimeError` out of
    # `hashlib.sha256` inside `compute_ticket_id` escaped all the way to the
    # CLI caller).
    if not WORKFLOW_ID_RE.match(workflow_id_s):
        return None

    # Everything else -- building the path (which hashes the identity),
    # the FIRST reuse check (including its `os.path.exists` call), the
    # ticket-id/hash computation, the temp name, datetime arithmetic, and
    # the write/publish/cleanup sequence -- is inside ONE outer try: any
    # exception reaching the handler below gives a fail-soft null ticket
    # (items 1/3/4/5/7-fallback/8/9/2 of the W1.2 fault inventory;
    # Hydra#85 cross-vendor retry 2).
    tmp: Path | None = None
    tmp_created = False
    try:
        path = ticket_path(project_root, workflow_id_s, run_id_s, call_key)

        reuse = _check_reuse(path, workflow_id_s, run_id_s, call_key)
        if reuse == "ok":
            return path
        if reuse:
            _emit_write_failed(project_root, workflow_id_s, call_key, agent_type,
                                kind, reuse)
            return None

        tid = compute_ticket_id(workflow_id_s, run_id_s, call_key)
        created = now or datetime.now(timezone.utc)
        artifact = host_action.get("artifact_text")
        ticket: dict[str, Any] = {
            "schema": TICKET_SCHEMA,
            "mode": "audit",
            "workflow_id": workflow_id_s,
            "run_id": run_id_s,
            "call_key": call_key,
            "ticket_id": tid,
            "kind": kind,
            "agent_type": agent_type,
            "squad_slug": squad_slug,
            "head": _head_for(kind, agent_type),
            # errors="surrogatepass" -- see `_length_prefixed`: a lone
            # surrogate in the prompt/instructions/artifact_text must not
            # raise here either; the ticket is still written with a
            # deterministic hash over the exact (lossless) bytes.
            "prompt_sha256": hashlib.sha256(
                canonical_prompt_text(host_action).encode(
                    "utf-8", errors="surrogatepass")).hexdigest(),
            "budget_usd": budget_usd,
            "budget_remaining_usd": budget_remaining_usd,
            "created_at": created.isoformat(),
            "expires_at": _expires_at(created, ticket_ttl_seconds()).isoformat(),
        }
        if kind == "judge":
            ticket["artifact_sha256"] = (
                hashlib.sha256(
                    artifact.encode("utf-8", errors="surrogatepass")).hexdigest()
                if isinstance(artifact, str) else None)

        # Short temp name in the same dir: keeps the temp path no longer than
        # the final path, which still matters on Windows even with the
        # extended-length prefix below (some win32 calls underneath
        # `os.link`/`open` are themselves happier with a shorter source).
        tmp = path.with_name(f".{uuid.uuid4().hex[:8]}.tmp")
        try:
            os.makedirs(_long_path(path.parent), exist_ok=True)
            # `tmp_created = True` goes right before `open`, not after: a
            # failure during the write/flush/close that follows (e.g. an
            # ENOSPC on `f.write`) must still reach the `finally` below and
            # remove whatever partial bytes `open` left on disk -- cleanup
            # must not depend on the write having succeeded.
            tmp_created = True
            with open(_long_path(tmp), "w", encoding="utf-8") as f:
                f.write(dumps_strict(ticket, label="dispatch ticket", indent=2))
            published = False
            try:
                os.link(_long_path(tmp), _long_path(path))
                published = True
            except FileExistsError:
                # Race: a concurrent writer published between our existence
                # check above and now. Never fall back to check-then-
                # os.replace over it -- resolve against whatever actually
                # landed first.
                published = False
        finally:
            # Runs on EVERY exit from the block above -- successful publish,
            # a caught FileExistsError race, or an uncaught exception from
            # `os.makedirs`/`open`/`f.write`/`os.link` alike. No partial temp
            # file is ever left behind, regardless of which step failed --
            # EXCEPT the one case where the cleanup call itself keeps
            # failing (see below): that row trades "no temp file" for "never
            # null a response that `os.link` already published."
            #
            # Up to two attempts, and the broad `except Exception` (not just
            # `OSError`) is deliberate: a non-OSError from this cleanup
            # (e.g. a RuntimeError injected by a faulty monitoring hook)
            # must not propagate past this `finally` and reach the outer
            # `except` below -- which would null a response that `os.link`
            # may have ALREADY published as the final file. A one-shot
            # transient fault (first attempt raises, second succeeds) still
            # leaves no temp file; a persistent fault leaves the temp file
            # behind but never raises and never nulls an already-published
            # ticket.
            if tmp_created and tmp is not None:
                for _cleanup_attempt in range(2):
                    try:
                        os.remove(_long_path(tmp))
                        break
                    except Exception as _cleanup_exc:  # noqa: BLE001 -- see comment above
                        if _cleanup_attempt == 1:
                            # Persistent cleanup fault: give up on removing
                            # the temp file, but this is NOT a silent
                            # swallow -- a failure event can still be
                            # reported even though the response itself
                            # (whatever `published` ends up being) must
                            # not be nulled by it. No path beyond the file
                            # name: the temp file's own name carries no
                            # secret, but the full path could join other
                            # directory contents into a trace.
                            safe_emit(
                                project_root, workflow_id_s,
                                "dispatch.ticket_tmp_cleanup_failed",
                                {"call_key": call_key, "ticket_kind": kind,
                                 "agent_type": agent_type,
                                 "tmp_name": tmp.name,
                                 "error": type(_cleanup_exc).__name__})
        if not published:
            reuse2 = _check_reuse(path, workflow_id_s, run_id_s, call_key)
            if reuse2 == "ok":
                return path
            _emit_write_failed(project_root, workflow_id_s, call_key, agent_type,
                                kind, reuse2 or "existing_unreadable")
            return None

        # `safe_emit` absorbs its own failure (see its docstring); this
        # success-path telemetry call must be no less fail-soft than
        # `_emit_write_failed` -- the ticket itself already published
        # successfully by this point, so a trace-write failure here (e.g.
        # a disk-full `open('a')` in `telemetry._write`) must not turn a
        # successful dispatch into an uncaught exception. Kept INSIDE the
        # single outer try (cross-vendor retry 3, requirement D): this
        # function has exactly one statement outside the guard (the E1
        # workflow_id check above), and a static test pins that.
        safe_emit(project_root, workflow_id_s, "dispatch.ticket_written",
                  {"call_key": call_key, "run_id": run_id_s, "ticket_id": tid,
                   "ticket_kind": kind, "agent_type": agent_type})
        return path
    except Exception as exc:  # noqa: BLE001 -- audit mode is fail-soft
        _emit_write_failed(project_root, workflow_id_s, call_key, agent_type,
                            kind, type(exc).__name__)
        return None
