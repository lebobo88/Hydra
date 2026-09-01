"""TheEights attestation adapter.

Hydra is one of four consumers of TheEights daemon (per the Phase-6 roadmap).
The daemon exposes MCP tools — `eights.constitution.attest`,
`eights.hydra.envelope.record`, `eights.governance.ceiling.tick`,
`eights.governance.redact_for_squad`, etc. — that record every supervisor-side
event into a shared SQL ledger so cross-consumer audits work.

This module is the Hydra-side caller. It calls those MCP tools **best-effort**:
when the eights-daemon is not reachable via the dispatcher (checked in
``~/.hydra/backends.json`` → ``~/.claude.json`` → ``.mcp.json`` resolution
order), each method no-ops cleanly. This lets Hydra ship the call sites
today and have them light up the moment the daemon is wired without further
code changes here.

Per `AGENTS.md` layering:
  - Hydra emits attestations; eights stores them.
  - On failure, Hydra continues (eights is an audit sink, not a gate).
  - The constitution check itself still runs locally via `immortal_head` —
    `constitution_attest` is the *attestation* (hash+receipt) for the audit
    log, not the authoritative refusal check.
"""
from __future__ import annotations

import logging
import os as _os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

# F34: dedicated short-circuit timeout and circuit-breaker cooldown for
# budget_charge.  budget.charge is ephemeral — stale charges are meaningless
# on daemon recovery so we do NOT spool them; instead we cap the wait and
# use a circuit breaker to avoid unbounded thread accumulation.
_BUDGET_CHARGE_TIMEOUT_DEFAULT = 5.0   # seconds before a charge is abandoned
_BUDGET_CHARGE_COOLDOWN_DEFAULT = 60.0  # seconds breaker stays open after timeout

# P1.2: generalized short-circuit for the OTHER hot eights calls on the
# supervisor critical path — constitution_attest / ceiling_tick / hitl_request.
# These previously called `_dispatch_call` synchronously under `_dispatch_lock`
# with no cap, so a bloated-but-alive eights ledger (slow `call_mcp`, not
# refused) blocked node_intake/dispatch for the full 120s dispatcher tool cap
# (attest + ceiling_tick serialized ≈ 240s at intake). Same circuit-breaker
# shape as the F34 budget_charge guard, keyed per tool. Env-overridable.
_EIGHTS_GUARD_TIMEOUT_DEFAULT = 5.0    # seconds before a guarded call is abandoned
_EIGHTS_GUARD_COOLDOWN_DEFAULT = 60.0  # seconds breaker stays open after a timeout

from ..immortal_head import ConstitutionSnapshot
from .pending_spool import PendingSpool, SpooledCall


# MCP server slug the eights-daemon registers under. The user-scope
# registration in ~/.claude.json keys this as "eights"; Claude Code's /mcp
# reconnect path uses this name. Override per environment via a project-scope
# `.mcp.json` if you mount the daemon on a different name.
EIGHTS_MCP_SERVER = "eights"


# Tools whose payload is durable enough to be worth replaying when the
# daemon recovers. We do NOT spool ephemeral signals (ceiling_tick,
# budget_charge) — those would be stale by the time the daemon is back.
_SPOOLABLE_TOOLS = frozenset({
    "eights.constitution.attest",
    "eights.hydra.envelope.record",
    "eights.governance.hitl.request",
    # E2-17: a terminal-transition resolve MUST survive an offline daemon —
    # otherwise the ticket it was meant to close stays pending forever (the
    # exact zombie class this finding is about). The spooled args carry their
    # own `envelope` (with the capability token), which `replay_pending`
    # preserves because the spooled args override the default envelope. A
    # token minted now may have expired by replay time; the replay then fails
    # and is retried/aged out like any other spool entry.
    "eights.governance.hitl.resolve",
    "eights.evolution.propose",
})

# E2-17: HITL request expiry. `plugins/hydra/skills/hitl-protocol/SKILL.md`
# documents the wait behaviour as "mark the workflow `surfaced` after
# `expires_at` (default 24h)", so 24h is the protocol default here.
# Override with HYDRA_HITL_EXPIRY_HOURS.
_HITL_EXPIRY_HOURS_DEFAULT = 24.0


def hitl_expiry_hours() -> float:
    """Configured HITL expiry window in hours (HYDRA_HITL_EXPIRY_HOURS).

    Falls back to the protocol default on an unset, unparseable or
    non-positive value — an expiry of zero would file already-expired
    requests, which is worse than no expiry at all.
    """
    raw = (_os.environ.get("HYDRA_HITL_EXPIRY_HOURS") or "").strip()
    if not raw:
        return _HITL_EXPIRY_HOURS_DEFAULT
    try:
        hours = float(raw)
    except ValueError:
        return _HITL_EXPIRY_HOURS_DEFAULT
    return hours if hours > 0 else _HITL_EXPIRY_HOURS_DEFAULT


def hitl_expires_at(now: Any = None) -> str:
    """ISO-8601 UTC instant at which a HITL request filed *now* expires."""
    from datetime import datetime, timedelta, timezone
    base = now or datetime.now(timezone.utc)
    stamp = (base + timedelta(hours=hitl_expiry_hours())).replace(microsecond=0)
    return stamp.isoformat().replace("+00:00", "Z")


def _mint_hitl_resolve_token(*, request_id: str, workflow_id: str) -> Optional[dict]:
    """Mint the WS-AUTH operator-capability token `hitl.resolve` requires.

    TheEights' `hitlResolve` fails closed without a token whose capability is
    `hitl.resolve` and whose resource_id is the request id. When
    HYDRA_OPERATOR_KEY is unset the mint degrades (sig.value=None) and the
    daemon rejects it — the resolve then spools like any other failed call.
    """
    try:
        import time as _t
        from ..auth.capability import mint_capability
        now = int(_t.time())
        return mint_capability({
            "v": 1,
            "actor_id": _os.environ.get("HYDRA_OPERATOR_ACTOR") or "hydra.supervisor",
            "actor_kind": "human",
            "capability": "hitl.resolve",
            "resource_id": str(request_id),
            "workflow_id": str(workflow_id or request_id),
            "issued_at": now,
            "exp": now + 900,
        }, now=now)
    except Exception:  # noqa: BLE001 — a mint failure must never block the resolve
        return None

_REPLAY_THREADS: dict[str, threading.Thread] = {}
_REPLAY_THREADS_LOCK = threading.Lock()


@dataclass
class EightsAttestor:
    """Best-effort wrapper over the eights-daemon MCP tools.

    `dispatcher` must expose `.call_mcp(server, tool, args) -> dict`. When the
    eights-daemon is not registered, calls return None and the supervisor
    proceeds normally.

    B8: durable payloads (see ``_SPOOLABLE_TOOLS``) are spooled to a local
    JSON queue on failure; `replay_pending` drains that queue when the
    daemon recovers. ``workflow_id`` is captured at construction so spool
    entries carry the workflow that surfaced the lesson.
    """
    dispatcher: Any | None = None
    server: str = EIGHTS_MCP_SERVER
    enabled: bool = True
    workflow_id: Optional[str] = None
    spool: PendingSpool = field(default_factory=PendingSpool)
    _dispatch_lock: threading.RLock = field(
        default_factory=threading.RLock,
        init=False,
        repr=False,
    )
    # F34 circuit-breaker state: monotonic timestamp after which budget_charge
    # may attempt again.  0.0 = breaker closed (normal path).
    _budget_charge_breaker_until: float = field(
        default=0.0,
        init=False,
        repr=False,
    )
    # F34 concurrency gate: BoundedSemaphore(1) ensures at most ONE
    # budget_charge thread is in-flight at a time, so a wedged call_mcp
    # cannot accumulate parked threads behind _dispatch_lock.
    _budget_charge_semaphore: threading.BoundedSemaphore = field(
        default_factory=lambda: threading.BoundedSemaphore(1),
        init=False,
        repr=False,
    )
    # F34 round-3: dedicated lock for race-safe read/write of _budget_charge_breaker_until.
    # Tiny critical sections — only the timestamp reads/writes, not the whole
    # budget_charge body — so contention is negligible.
    _budget_charge_breaker_lock: threading.Lock = field(
        default_factory=threading.Lock,
        init=False,
        repr=False,
    )
    # P1.2: generalized F34 breaker state for constitution_attest / ceiling_tick
    # / hitl_request. Keyed by a guard name so each tool gets its own breaker
    # timestamp and its own single-permit concurrency gate. One lock guards both
    # dicts (tiny critical sections — dict get/set only, not the call body).
    _eights_guard_lock: threading.Lock = field(
        default_factory=threading.Lock,
        init=False,
        repr=False,
    )
    _eights_guard_breaker_until: dict[str, float] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )
    _eights_guard_semaphores: dict[str, threading.BoundedSemaphore] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )

    @staticmethod
    def _is_success_envelope(result: Any) -> bool:
        if not isinstance(result, dict):
            return False
        status = result.get("status")
        if status is None:
            return True
        return status in {"done", "ok", "complete"}

    @staticmethod
    def _is_advisory_constitution_rejection(tool: str, result: Any) -> bool:
        if tool != "eights.constitution.attest" or not isinstance(result, dict):
            return False
        if result.get("status") != "rejected":
            return False
        if result.get("hitl_required") is True:
            return True
        err = str(result.get("error") or "").lower()
        return "operator capability" in err or "capability token" in err

    @staticmethod
    def _failure_reason(result: Any) -> str:
        if not isinstance(result, dict):
            return "daemon_unavailable"
        status = str(result.get("status") or "daemon_unavailable")
        if result.get("hitl_required") is True:
            return f"{status}:hitl_required"
        err = str(result.get("error") or "").strip()
        if err:
            return f"{status}:{err[:120]}"
        return status

    def _eights_envelope(self, *, workflow_id: Optional[str] = None) -> dict[str, Any]:
        """Build a TheEights-compatible envelope from workflow context.

        Every TheEights MCP tool (except identity.* and audit.*) requires
        this envelope for audit lineage. Fields match the Zod schema in
        TheEights/daemon/src/schemas/envelope.ts. ``workflow_id`` overrides the
        instance default for a single call, so callers that share one attestor
        across workflows don't race on ``self.workflow_id``.
        """
        return {
            "tenant_id": "local",
            "actor_id": "hydra.supervisor",
            "project_id": "Hydra",
            "domain": "orchestration",
            "scope": [],
            "trace_id": str(workflow_id or self.workflow_id or "no-workflow"),
        }

    def _dispatch_call(self, tool: str, args: dict[str, Any]) -> Any:
        with self._dispatch_lock:
            return self.dispatcher.call_mcp(self.server, tool, args)

    def _call(self, tool: str, args: dict) -> Optional[dict]:
        if not self.enabled or self.dispatcher is None:
            self._maybe_spool(tool, args, reason="eights_disabled_or_no_dispatcher")
            return None
        call_args = {"envelope": self._eights_envelope(), **args}
        try:
            result = self._dispatch_call(tool, call_args)
        except Exception as exc:  # noqa: BLE001 — fail-soft, spool the payload
            self._maybe_spool(tool, args, reason=f"exception:{type(exc).__name__}")
            return None
        if self._is_advisory_constitution_rejection(tool, result):
            return None
        if not self._is_success_envelope(result):
            self._maybe_spool(tool, args, reason=self._failure_reason(result))
            return None
        inner = result.get("result", result)
        return inner if isinstance(inner, dict) else None

    def _maybe_spool(self, tool: str, args: dict, *, reason: str) -> None:
        """Persist a durable failed payload to the spool. No-op for ephemeral
        tools (ticks/charges) so we don't bloat the spool with stale signals."""
        if tool not in _SPOOLABLE_TOOLS:
            return
        try:
            self.spool.spool(
                tool=tool,
                args=dict(args or {}),
                workflow_id=self.workflow_id,
                reason=reason,
            )
        except Exception:  # noqa: BLE001 — spool write must never crash dispatch
            pass

    def _guarded_call(self, tool: str, args: dict, *, guard_key: str) -> Optional[dict]:
        """Short-circuit wrapper over ``_call`` for the hot audit/ephemeral
        eights tools on the supervisor critical path (constitution_attest,
        ceiling_tick, hitl_request).

        Same F34 circuit-breaker shape as ``budget_charge`` — a per-``guard_key``
        breaker timestamp plus a single-permit concurrency gate — generalized so
        each tool has its own state. A bloated-but-alive eights daemon (slow
        ``call_mcp``, not refused) can therefore no longer block node_intake /
        dispatch for the full 120s dispatcher cap: the call is abandoned after a
        short timeout and the breaker opens for a cooldown.

        Return value matches the degrade-open contract callers already handle:
        ``None`` when the daemon is unreachable, the permit is busy, the breaker
        is open, or the worker is abandoned on timeout. Durable payloads
        (``_SPOOLABLE_TOOLS`` — attest / hitl / envelope.record) are spooled on
        the breaker-open and in-flight-skip paths (where ``_call`` is not invoked
        at all) so they still replay when the daemon recovers; on the timeout
        path the worker still owns the call and spools it itself, so we don't
        double-spool. Ephemeral tools (ceiling_tick) are never spooled.
        """
        if not self.enabled or self.dispatcher is None:
            # Preserve _call's disabled-path spooling semantics.
            return self._call(tool, args)

        import time as _time

        timeout_s = float(_os.environ.get(
            "HYDRA_EIGHTS_GUARD_TIMEOUT_S", str(_EIGHTS_GUARD_TIMEOUT_DEFAULT)
        ))
        cooldown_s = float(_os.environ.get(
            "HYDRA_EIGHTS_GUARD_COOLDOWN_S", str(_EIGHTS_GUARD_COOLDOWN_DEFAULT)
        ))

        # Read breaker timestamp + fetch-or-create this key's permit under lock.
        with self._eights_guard_lock:
            breaker_until = self._eights_guard_breaker_until.get(guard_key, 0.0)
            sem = self._eights_guard_semaphores.get(guard_key)
            if sem is None:
                sem = threading.BoundedSemaphore(1)
                self._eights_guard_semaphores[guard_key] = sem

        # Guard 1: circuit breaker — during cooldown, skip immediately.
        if _time.monotonic() < breaker_until:
            self._maybe_spool(tool, args, reason="eights_guard_breaker_open")
            return None

        # Guard 2: concurrency gate — at most ONE guarded worker per key. A
        # healthy overlap simply skips THIS call (breaker NOT tripped).
        if not sem.acquire(blocking=False):
            self._maybe_spool(tool, args, reason="eights_guard_inflight")
            return None

        result_box: list[Optional[dict]] = [None]

        def _worker() -> None:
            # Owns the permit for its full lifetime; releases in finally on
            # success/exception but NOT on abandonment. A wedged call_mcp never
            # reaches finally, so the permit stays held and guard 2 rejects
            # subsequent calls instead of stacking threads behind _dispatch_lock.
            try:
                result_box[0] = self._call(tool, args)
            finally:
                sem.release()

        t = threading.Thread(target=_worker, daemon=True,
                             name=f"hydra-eights-{guard_key}")
        t.start()
        t.join(timeout=timeout_s)

        if t.is_alive():
            with self._eights_guard_lock:
                self._eights_guard_breaker_until[guard_key] = (
                    _time.monotonic() + cooldown_s
                )
            logger.debug(
                "eights guard %s: timed out after %.1fs — breaker tripped for "
                "%.0fs; supervisor proceeds (eights is an audit sink, not a gate)",
                guard_key, timeout_s, cooldown_s,
            )
            # Do NOT spool here — the abandoned worker still owns the call and
            # will spool on its own if/when call_mcp finally returns a failure
            # (spooling here too would double-queue a durable payload).

        return result_box[0]

    def replay_pending(
        self,
        *,
        max_replays: int | None = None,
        max_attempts: int = 5,
        max_age_hours: float = 24.0,
    ) -> dict[str, int]:
        """Drain the pending-call spool by re-issuing each call to the daemon.

        Called by `node_intake` at the start of every workflow so the spool
        naturally drains the next time eights is healthy. Returns the same
        ``{sent, failed, skipped}`` summary as `PendingSpool.replay` so
        callers can emit a trace event.
        """
        if not self.enabled or self.dispatcher is None:
            return {"sent": 0, "failed": 0, "skipped": 0}

        def _send(spooled_call: SpooledCall) -> Any:
            args = {
                "envelope": self._eights_envelope(workflow_id=spooled_call.workflow_id),
                **dict(spooled_call.args or {}),
            }
            envelope = self._dispatch_call(spooled_call.tool, args)
            if self._is_advisory_constitution_rejection(spooled_call.tool, envelope):
                return {"status": "done", "advisory_degraded": True}
            if not self._is_success_envelope(envelope):
                return None
            return envelope

        try:
            return self.spool.replay(
                _send,
                max_replays=max_replays,
                max_attempts=max_attempts,
                max_age_hours=max_age_hours,
            )
        except Exception:  # noqa: BLE001
            return {"sent": 0, "failed": 0, "skipped": 0}

    def replay_pending_async(
        self,
        *,
        max_replays: int | None = None,
        max_attempts: int = 5,
        max_age_hours: float = 24.0,
        on_complete: Callable[[dict[str, int]], None] | None = None,
    ) -> bool:
        """Kick off a single-flight background replay for this spool root.

        Intake uses this so a slow eights drain never stalls the workflow.
        At most one replay worker runs per spool root inside this process.
        """
        if not self.enabled or self.dispatcher is None:
            return False
        spool_key = str(self.spool.root.resolve(strict=False))

        def _worker() -> None:
            summary = self.replay_pending(
                max_replays=max_replays,
                max_attempts=max_attempts,
                max_age_hours=max_age_hours,
            )
            if on_complete is not None:
                try:
                    on_complete(summary)
                except Exception:  # noqa: BLE001 — replay completion must stay fail-soft
                    pass
            with _REPLAY_THREADS_LOCK:
                current = _REPLAY_THREADS.get(spool_key)
                if current is threading.current_thread():
                    _REPLAY_THREADS.pop(spool_key, None)

        with _REPLAY_THREADS_LOCK:
            current = _REPLAY_THREADS.get(spool_key)
            if current is not None and current.is_alive():
                return False
            thread = threading.Thread(
                target=_worker,
                name=f"hydra-eights-replay-{Path(spool_key).name}",
                daemon=True,
            )
            _REPLAY_THREADS[spool_key] = thread
        thread.start()
        return True

    def pending_count(self) -> int:
        try:
            return self.spool.count()
        except Exception:  # noqa: BLE001
            return 0

    # ---------- constitution ----------

    def constitution_attest(
        self,
        snapshot: ConstitutionSnapshot,
    ) -> Optional[dict]:
        """Record a constitution attestation. Returns the receipt dict, or
        None when the daemon is unreachable.

        Receipt shape (eights-daemon contract):
            {"hash": "sha256:...", "version": "...", "receipt": "uuid"}
        """
        return self._guarded_call("eights.constitution.attest", {
            "consumer": "hydra",
        }, guard_key="constitution_attest")

    # ---------- envelope lineage ----------

    def envelope_record(self, envelope: dict) -> Optional[dict]:
        """Record an envelope emission for cross-consumer audit. Idempotent —
        the daemon dedupes by envelope id."""
        if not isinstance(envelope, dict) or not envelope.get("id"):
            return None
        return self._call("eights.hydra.envelope.record", {
            "hydra_envelope": {
                "id": str(envelope.get("id")),
                "type": envelope.get("type"),
                "workflow_id": str(envelope.get("workflow_id", "")),
                "origin_squad": envelope.get("origin_squad", "hydra"),
                "target_squad": envelope.get("target_squad"),
                "parent_id": str(envelope.get("parent_id") or "") or None,
            },
        })

    # ---------- memory federation ----------

    def memory_search(
        self,
        query: str,
        *,
        top_k: int = 10,
        types: Optional[list[str]] = None,
        scopes: Optional[list[str]] = None,
        fusion: str = "hybrid",
        workflow_id: Optional[str] = None,
    ) -> Optional[Any]:
        """Federated hybrid memory search via TheEights ``eights.memory.search``.

        Returns the daemon's hit payload (a list of hits, or a dict wrapping
        them — eights returns ``engine.search(...)`` directly, so the shape is
        not always a dict, which is why this does NOT go through ``_call``).
        Returns ``None`` when eights is disabled/unreachable so callers can fall
        back to local search. ``workflow_id`` stamps the audit envelope per
        call (no shared-state race). The envelope is added here, same as
        ``_call``."""
        if not self.enabled or self.dispatcher is None or not (query or "").strip():
            return None
        args: dict[str, Any] = {
            "envelope": self._eights_envelope(workflow_id=workflow_id),
            "query": query,
            "top_k": int(top_k),
            "fusion": fusion,
        }
        if types:
            args["types"] = types
        if scopes:
            args["scopes"] = scopes
        try:
            result = self.dispatcher.call_mcp(self.server, "eights.memory.search", args)
        except Exception:  # noqa: BLE001 — fail-soft; caller falls back to local
            return None
        if not isinstance(result, dict) or result.get("status") == "failed":
            return None
        return result.get("result", result)

    # ---------- governance ----------

    def ceiling_tick(self, *, workflow_id: str, node: str) -> Optional[dict]:
        """Bump the loop-ceiling counter in the shared ledger so cross-consumer
        loops are caught (e.g., engineering + executive ping-ponging)."""
        return self._guarded_call("eights.governance.ceiling.tick", {
            "run_id": str(workflow_id),
            "kind": "iteration",
        }, guard_key="ceiling_tick")

    def budget_charge(
        self,
        *,
        workflow_id: str,
        usd: float,
        tokens: int,
        vendor: str = "",
        purpose: str = "",
    ) -> Optional[dict]:
        """Record token/cost spend — non-blocking on the hot path (F34).

        The daemon enforces caps; Hydra does not gate on this return value —
        the local BudgetLedger is authoritative within a workflow.

        Design (F34 round-2 circuit-breaker):

        Problem: the naive thread+join approach still holds ``_dispatch_lock``
        in the background worker.  A wedged ``call_mcp`` holds that lock
        indefinitely; each subsequent timed-out call stacks another abandoned
        thread behind the lock (unbounded accumulation).

        Solution — two guards + one lock:

        1. **BoundedSemaphore(1)** — at most ONE budget_charge thread may be
           in-flight at a time.  A healthy overlapping call (two concurrent
           charges while eights is fine) simply skips THIS call only — the
           breaker is NOT tripped, so the next solo call proceeds normally.
           The breaker is opened ONLY on evidence of a real wedge (worker
           thread still alive after ``timeout_s`` join).

        2. **Circuit breaker (time-based)** — after the first timeout the
           breaker is opened for ``HYDRA_BUDGET_CHARGE_COOLDOWN_S`` (default
           60 s).  During cooldown, calls return immediately (no thread, no
           lock contention).  After cooldown the breaker resets and one new
           attempt is allowed.

        3. **Breaker lock** — ``_budget_charge_breaker_lock`` guards all reads
           and writes of ``_budget_charge_breaker_until`` so concurrent calls
           from multiple threads cannot race on the timestamp.

        Both guards together ensure: at most ONE budget_charge worker is ever
        in-flight, so at most 1 thread ever holds/waits on ``_dispatch_lock``.
        While a wedged worker holds the permit, the breaker rejects every new
        call (breaker spares them the join wait), so a sustained eights wedge
        produces NO further threads — the permit re-arms the gate only when the
        wedged worker's ``call_mcp`` finally returns and releases it.
        """
        if not self.enabled or self.dispatcher is None:
            return None

        import time as _time

        timeout_s = float(_os.environ.get(
            "HYDRA_BUDGET_CHARGE_TIMEOUT_S", str(_BUDGET_CHARGE_TIMEOUT_DEFAULT)
        ))
        cooldown_s = float(_os.environ.get(
            "HYDRA_BUDGET_CHARGE_COOLDOWN_S", str(_BUDGET_CHARGE_COOLDOWN_DEFAULT)
        ))

        # Guard 1: circuit breaker check (lock-protected read).
        with self._budget_charge_breaker_lock:
            breaker_until = self._budget_charge_breaker_until
        now = _time.monotonic()
        if now < breaker_until:
            logger.debug(
                "budget_charge: circuit breaker open (%.1fs remaining) — skip",
                breaker_until - now,
            )
            return None

        # Guard 2: concurrency gate.  Exactly one thread in-flight at a time.
        # Non-blocking acquire: if another budget_charge thread is already
        # in-flight, skip THIS CALL ONLY — the breaker is NOT tripped here
        # because a healthy concurrent overlap is not evidence of a wedge.
        # The breaker opens ONLY when the worker thread actually times out (below).
        if not self._budget_charge_semaphore.acquire(blocking=False):
            logger.debug(
                "budget_charge: previous charge still in-flight — "
                "skipping this call (healthy overlap; breaker NOT tripped)",
            )
            return None

        result_box: list[Optional[dict]] = [None]

        def _charge_worker() -> None:
            # The worker OWNS the semaphore permit for its full lifetime and
            # releases it in `finally` — on success OR exception, but NOT on
            # abandonment.  A wedged call_mcp never reaches finally, so the
            # permit stays held: the concurrency gate above then rejects the
            # next call (guard 2 skip path) instead of spawning a second thread
            # that would park behind the wedged one.  This is what makes the
            # documented invariant — at most ONE in-flight worker, hence at
            # most 1 thread waiting on _dispatch_lock — actually hold.
            try:
                result_box[0] = self._call("eights.governance.budget.charge", {
                    "run_id": str(workflow_id),
                    "cost_usd": float(usd),
                    "tokens": int(tokens),
                })
            finally:
                self._budget_charge_semaphore.release()

        t = threading.Thread(
            target=_charge_worker,
            daemon=True,
            name="hydra-budget-charge",
        )
        t.start()
        t.join(timeout=timeout_s)

        # NOTE: the main thread does NOT release the semaphore.  On a fast /
        # failed charge the worker's `finally` already released it; on a
        # timeout the worker is abandoned still holding the permit (by design),
        # which keeps the gate closed until the daemon recovers.

        if t.is_alive():
            with self._budget_charge_breaker_lock:
                self._budget_charge_breaker_until = _time.monotonic() + cooldown_s
            logger.debug(
                "budget_charge: timed out after %.1fs — breaker tripped "
                "for %.0fs; local BudgetLedger remains authoritative",
                timeout_s, cooldown_s,
            )

        return result_box[0]

    def hitl_request(self, hitl_envelope: dict, *, gate_node: str = "") -> Optional[dict]:
        """Enqueue a HITL request to the shared ledger so the operator UI
        can show pending requests across consumers.

        Campaign mesh-console-unification C2 (2026-06-05): emits the frozen
        hydra_gate contract so AgentMesh can federate Hydra gates with the
        TheEights hitl_queue and dedupe by workflow_id + gate_node:
          run_id = workflow_id; kind = "hydra_gate"
          payload = { hitl_id, workflow_id, reason, summary, options[],
                      default_option, gate_node, expires_at }
        """
        wf = str(hitl_envelope.get("workflow_id", ""))
        return self._guarded_call("eights.governance.hitl.request", {
            "run_id": wf,
            "kind": "hydra_gate",
            "payload": {
                "hitl_id": str(hitl_envelope.get("id", "")),
                "workflow_id": wf,
                "reason": hitl_envelope.get("reason", "operator_review"),
                "summary": hitl_envelope.get("summary"),
                "options": list(hitl_envelope.get("options") or []),
                "default_option": hitl_envelope.get("default_option"),
                "gate_node": gate_node or "unspecified",
                # E2-17: never file an immortal request. An unset expires_at is
                # what left 858 pending zombies in the shared ledger.
                "expires_at": hitl_envelope.get("expires_at") or hitl_expires_at(),
            },
        }, guard_key="hitl_request")

    def hitl_list(
        self,
        *,
        status: str = "pending",
        kind: Optional[str] = "hydra_gate",
    ) -> Optional[list[dict]]:
        """List HITL rows from the shared ledger (E2-17).

        Returns the rows, or ``None`` when the daemon did not service the call
        (unreachable / disabled) so callers can distinguish "no backlog" from
        "backlog unknown". ``kind`` filters to Hydra-filed gates by default;
        pass ``None`` for every consumer's rows.

        Not routed through ``_call``: hitl.list returns a JSON *array*, which
        ``_call`` would discard as a non-dict result. It is also read-only, so
        there is nothing to spool on failure.
        """
        if not self.enabled or self.dispatcher is None:
            return None
        args = {"envelope": self._eights_envelope(), "status": status}
        try:
            result = self._dispatch_call("eights.governance.hitl.list", args)
        except Exception:  # noqa: BLE001 — read-only probe, never raises upward
            return None
        if not self._is_success_envelope(result):
            return None
        inner = result.get("result", result) if isinstance(result, dict) else None
        if isinstance(inner, dict):
            inner = inner.get("rows") or inner.get("requests")
        if not isinstance(inner, list):
            return None
        rows = [r for r in inner if isinstance(r, dict)]
        if kind:
            rows = [r for r in rows if r.get("kind") == kind]
        return rows

    def hitl_resolve(
        self,
        *,
        request_id: str,
        decision: str = "rejected",
        note: str = "",
        workflow_id: Optional[str] = None,
    ) -> Optional[dict]:
        """Resolve one pending HITL request in the shared ledger (E2-17).

        Fail-soft and spooled like every other durable eights write: a
        rejected or unreachable call returns None and replays on drain.
        """
        rid = str(request_id or "").strip()
        if not rid:
            return None
        envelope = self._eights_envelope(workflow_id=workflow_id)
        token = _mint_hitl_resolve_token(
            request_id=rid, workflow_id=str(workflow_id or ""),
        )
        if token is not None:
            envelope["capability_token"] = token
        return self._call("eights.governance.hitl.resolve", {
            "envelope": envelope,
            "request_id": rid,
            "decision": decision,
            "note": note,
        })

    # ---------- redaction ----------

    def redact_for_squad(
        self,
        *,
        text: str,
        from_squad: str,
        to_squad: str,
        allow_pii: bool = False,
    ) -> Optional[str]:
        """Daemon-side redaction. When unavailable, callers should fall back
        to `governance.redact_for_squad_boundary` (already in place).

        Returns the redacted text, or None when the daemon didn't service the
        call (so the caller knows to use the local fallback).
        """
        out = self._call("eights.governance.redact_for_squad", {
            "target_squad": to_squad,
            "payload": {"text": text, "from_squad": from_squad, "allow_pii": allow_pii},
        })
        if isinstance(out, dict) and isinstance(out.get("redacted"), str):
            return out["redacted"]
        return None

    # ---------- prompts ----------

    def prompt_get(self, *, slug: str) -> Optional[str]:
        """Fetch a registered prompt (system prompt for a squad/agent)."""
        out = self._call("eights.prompt.get", {"rid": slug})
        if isinstance(out, dict) and isinstance(out.get("text"), str):
            return out["text"]
        return None

    # ---------- evolution (F36: procedural risk routing) ----------

    def evolution_register(
        self,
        *,
        resource_kind: str,
        resource_id: str,
        body: str,
        summary: str = "",
    ) -> Optional[dict]:
        """Register a resource in the eights evolution ledger before proposing.

        Required for medium/high-risk procedural kinds so TheEights can track
        the resource's lifecycle. Idempotent — re-registering with the same
        resource_id is a no-op in the daemon.
        """
        return self._call("eights.evolution.register", {
            "resource_kind": resource_kind,
            "resource_id": resource_id,
            "body": body,
            "summary": summary or resource_kind,
        })

    def evolution_propose(
        self,
        *,
        resource_id: str,
        summary: str,
        body: str,
        proposed_by: str = "hydra.procedural",
        workflow_id: Optional[str] = None,
    ) -> Optional[dict]:
        """Propose an evolution update for a resource. Returns the proposal dict
        (including ``proposal_id`` and ``status``) or None when the daemon is
        unreachable. A None return means the caller must treat the update as
        pending (fail-soft for medium risk) or rejected (fail-closed for high)."""
        return self._call("eights.evolution.propose", {
            "resource_id": resource_id,
            "summary": summary,
            "body": body,
            "proposed_by": proposed_by,
            "run_id": str(workflow_id or self.workflow_id or "procedural"),
        })

    def evolution_commit(
        self,
        *,
        resource_id: str,
        proposal_id: str,
    ) -> Optional[dict]:
        """Commit an approved evolution proposal. Returns the commit receipt or
        None when the daemon is unreachable."""
        return self._call("eights.evolution.commit", {
            "resource_id": resource_id,
            "proposal_id": proposal_id,
        })
