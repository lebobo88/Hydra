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

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from ..immortal_head import ConstitutionSnapshot
from .pending_spool import PendingSpool


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

    def _call(self, tool: str, args: dict) -> Optional[dict]:
        if not self.enabled or self.dispatcher is None:
            self._maybe_spool(tool, args, reason="eights_disabled_or_no_dispatcher")
            return None
        call_args = {"envelope": self._eights_envelope(), **args}
        try:
            result = self.dispatcher.call_mcp(self.server, tool, call_args)
        except Exception as exc:  # noqa: BLE001 — fail-soft, spool the payload
            self._maybe_spool(tool, args, reason=f"exception:{type(exc).__name__}")
            return None
        if not isinstance(result, dict):
            self._maybe_spool(tool, args, reason="non_dict_result")
            return None
        if result.get("status") == "failed":
            self._maybe_spool(tool, args, reason="daemon_status_failed")
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

    def replay_pending(self) -> dict[str, int]:
        """Drain the pending-call spool by re-issuing each call to the daemon.

        Called by `node_intake` at the start of every workflow so the spool
        naturally drains the next time eights is healthy. Returns the same
        ``{sent, failed, skipped}`` summary as `PendingSpool.replay` so
        callers can emit a trace event.
        """
        if not self.enabled or self.dispatcher is None:
            return {"sent": 0, "failed": 0, "skipped": 0}

        def _send(tool: str, args: dict[str, Any]) -> Any:
            envelope = self.dispatcher.call_mcp(self.server, tool, args)
            if not isinstance(envelope, dict):
                return None
            if envelope.get("status") == "failed":
                return None
            return envelope

        try:
            return self.spool.replay(_send)
        except Exception:  # noqa: BLE001
            return {"sent": 0, "failed": 0, "skipped": 0}

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
        """Record token/cost spend. The daemon enforces caps; Hydra does not
        gate on this return value — the local BudgetLedger is authoritative
        within a workflow."""
        return self._call("eights.governance.budget.charge", {
            "run_id": str(workflow_id),
            "cost_usd": float(usd),
            "tokens": int(tokens),
        })

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
