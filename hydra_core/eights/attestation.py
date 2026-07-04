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

# F34: dedicated short-circuit timeout for budget_charge so a wedged TheEights
# daemon costs the hot path at most this many seconds (not the 120s default).
# budget.charge is ephemeral — stale charges are meaningless on daemon recovery
# so we do NOT spool them; we just cap the wait and abandon.
_BUDGET_CHARGE_TIMEOUT_DEFAULT = 5.0

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
    "eights.evolution.propose",
})

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
        return self._call("eights.constitution.attest", {
            "consumer": "hydra",
        })

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
        return self._call("eights.governance.ceiling.tick", {
            "run_id": str(workflow_id),
            "kind": "iteration",
        })

    def budget_charge(
        self,
        *,
        workflow_id: str,
        usd: float,
        tokens: int,
        vendor: str = "",
        purpose: str = "",
    ) -> Optional[dict]:
        """Record token/cost spend with a dedicated short timeout (F34).

        The daemon enforces caps; Hydra does not gate on this return value —
        the local BudgetLedger is authoritative within a workflow.

        Design (F34): budget.charge is ephemeral — stale charges carry no
        value once the daemon recovers, so we do NOT spool them (see
        ``_SPOOLABLE_TOOLS``). Instead we cap the hot-path wait with a
        daemon thread + join(timeout). A wedged TheEights costs at most
        ``HYDRA_BUDGET_CHARGE_TIMEOUT_S`` seconds (default 5 s) per call
        site, not the 120 s dispatcher default. After the cap the thread is
        abandoned (daemon thread, reclaimed on process exit).
        """
        if not self.enabled or self.dispatcher is None:
            return None

        timeout_s = float(
            _os.environ.get("HYDRA_BUDGET_CHARGE_TIMEOUT_S",
                            str(_BUDGET_CHARGE_TIMEOUT_DEFAULT))
        )
        result_box: list[Optional[dict]] = [None]

        def _charge_worker() -> None:
            result_box[0] = self._call("eights.governance.budget.charge", {
                "run_id": str(workflow_id),
                "cost_usd": float(usd),
                "tokens": int(tokens),
            })

        t = threading.Thread(
            target=_charge_worker,
            daemon=True,
            name="hydra-budget-charge",
        )
        t.start()
        t.join(timeout=timeout_s)
        if t.is_alive():
            logger.debug(
                "budget_charge: eights did not respond within %.1fs — "
                "abandoning (charge will be lost; local BudgetLedger "
                "remains authoritative)",
                timeout_s,
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
        return self._call("eights.governance.hitl.request", {
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
                "expires_at": hitl_envelope.get("expires_at"),
            },
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
