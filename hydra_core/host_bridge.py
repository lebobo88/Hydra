"""Attended (host-bridged) engineering execution.

This is the core of the *attended* execution mode: instead of the headless
``_drive_pp_stage_loop`` driving generate -> judge -> finalize in a detached
subprocess the operator cannot watch, the Claude Code host session drives the
SAME pair-programmer stage protocol IN-CONTEXT, surfacing the generate and judge
steps as visible ``Agent`` subagents it can follow along with.

Design note (supersedes the ``run_host_agent`` trampoline framing in the plan):
``_drive_pp_stage_loop`` is a straight-line function with a broad fail-soft
``except`` that would swallow any mid-loop pause exception (and finalize the run
``aborted``). So we do NOT reuse it. Instead this module is an **explicit
step-state-machine** that persists its progress (the "cursor") to disk between
host round-trips — exactly the resumable plumbing codex's review called for, and
replay-free: each ``step``/``submit`` advances the cursor by exactly one
transition, so every pp ledger call (``record_attempt``/``record_verdict``/
``finalize_*``) happens exactly once.

It calls ONLY tools the engineering squad declares (RBAC-safe via
``squad_id="engineering"``) and reuses the headless loop's governance helpers
(``_build_engineer_prompt``, ``_run_smoke``, ``gate_eligible_judges`` routing,
the finalize-readiness gate, the real-diff judge text) so the attended path and
the headless path enforce the same gates. Budget is NOT charged here — the
caller (the ``hydra step`` / ``submit-host-result`` CLI operating on the
checkpointed ``HydraState``) charges the returned ``cost_usd`` via
``charge_and_gate`` so the 80%/100% tripwires stay authoritative.

Runtime-agnostic: no provider SDK imports. The visible ``engineer`` / judge
subagents are spawned by the host (Claude Code), not here; this module only
sequences the deterministic pp tool calls around them.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Any, Optional, Sequence

import re as _re

from . import telemetry as _telemetry
from .judge_vendor import (
    _base_judge_vendor,
    _judge_vendor_chain,
)
from .proc import run_text
from .strict_json import dumps_strict
from .squad_node import (
    Dispatcher,
    _augment_with_critique,
    _build_engineer_prompt,
    _generate_failure_reason,
    _judge_artifact_text,
    _pp_gate_type,
    _pp_inner,
    _pp_ok,
    _default_rubric_id,
    _resolve_rubric_id,
    _resolve_skill_shim,
    _rubric_md_ex,
    _run_smoke,
    _worktree_dirty_set,
    _worktree_committed_since,
    _git_head_sha,
    coerce_untrusted_cost,
    coerce_untrusted_count,
)

# Cursor schema version — bump on any incompatible shape change so a stale
# on-disk cursor from an older build is rejected loudly rather than misread.
CURSOR_SCHEMA = 1

# Terminal cursor states.
#
# E2-35: ``complete_unpersisted`` is a terminal COMPLETE-shaped outcome whose
# artifact could not be persisted anywhere. It is deliberately NOT spelled
# "complete" so governance/synthesis cannot mistake an artifact-less squad
# return for a durable one, and it is terminal so the driver stops rather than
# re-spawning the pack agent.
_TERMINAL = {"complete", "complete_unpersisted", "surfaced", "aborted"}

_SQ = "engineering"

# --------------------------------------------------------------------------- #
# LV-1: error-payload detection                                               #
# --------------------------------------------------------------------------- #

# Known-good statuses from MCPStdioDispatcher.call_mcp; anything else with an
# "error" key is treated as a failure too.
_CALL_MCP_SUCCESS_STATUSES: frozenset[str] = frozenset(
    {"done", "ok", "complete", "stub", "skipped"}
)


class PPLedgerError(RuntimeError):
    """A pp ledger call failed with a structured error payload.

    ``payload`` carries the original ``call_mcp`` response dict so
    ``_classify_infra_failure`` can key off the STRUCTURE of the rejection
    (``status``, ``gate_error``, ``hitl_required``, ``venom_refused``)
    rather than substring-matching the rendered message. A fail-CLOSED
    rejection whose ``{exc}`` text happens to mention a transport-sounding
    phrase (e.g. "database is locked") must still classify as deterministic
    -- see the venom gate's fail-closed branch in dispatcher.py.
    """

    def __init__(self, message: str, payload: dict[str, Any]):
        super().__init__(message)
        self.payload = payload


def _raise_on_error_payload(resp: Any, tool: str) -> Any:
    """Raise PPLedgerError when a call_mcp response is a structured error dict.

    MCPStdioDispatcher.call_mcp returns error DICTS instead of raising for:
    - RBAC rejections:   {"status":"rejected","error":...}
    - transport/timeout: {"status":"failed","error":...}
    - isError results:   {"status":"failed", "tool":..., "error":...}

    The existing try/except downgrade paths in begin_stage, _apply_generate,
    _apply_judge, and _finalize only catch *raised* exceptions, so they silently
    passed through error dicts, which broke finalize/verdict/attempt tracking.

    Raises PPLedgerError (a RuntimeError subclass carrying the original dict
    as ``.payload`` for structural classification downstream) on:
    - ``status`` in {"rejected","failed","error"}, or
    - ``"error"`` key present and ``status`` not in the known-good set.

    Returns ``resp`` unchanged on a normal response or on a non-dict (callers
    must tolerate both — no change to existing semantics).
    """
    if not isinstance(resp, dict):
        return resp
    status = resp.get("status")
    if status in {"rejected", "failed", "error"}:
        raise PPLedgerError(
            f"pp ledger call {tool!r} returned error payload "
            f"(status={status!r}): {resp.get('error', resp)!r}",
            resp,
        )
    if status not in _CALL_MCP_SUCCESS_STATUSES and "error" in resp:
        raise PPLedgerError(
            f"pp ledger call {tool!r} returned error (status={status!r}): "
            f"{resp['error']!r}",
            resp,
        )
    return resp


# W2-3: markers that positively identify a transport-shaped pp ledger failure
# (timeout, connection drop, lock contention, cold-start race) as opposed to a
# deterministic pp rejection (bad args, schema violation, business-rule
# denial). These are a FALLBACK for exceptions that carry no structured
# payload (see PPLedgerError.payload below, checked first) -- e.g. a raw
# transport exception raised before a call_mcp response dict ever formed.
# Deterministic markers are checked FIRST and win even when a transport word
# also appears in the message, because a rejection's own validation text can
# legitimately contain a word like "connection" — e.g. "connection_id
# invalid". A message that matches neither list is treated as deterministic:
# getting this discrimination wrong in the permissive direction would hide a
# real rejection, so an ambiguous failure must fail the stage rather than
# silently hold it open.
_DETERMINISTIC_FAILURE_MARKERS: tuple[str, ...] = (
    "validation", "invalid_", "schema", "attempt not found",
    "attempt_id not found", "unknown attempt", "rubric not found",
    "duplicate", "already recorded", "not authorized", "rbac",
)
_TRANSPORT_FAILURE_MARKERS: tuple[str, ...] = (
    "timed out", "timeout", "'phase': 'call_tool'", '"phase": "call_tool"',
    "connection", "brokenpipe", "not registered in backends.json",
    "mcp sdk not installed", "sqlite_busy", "database is locked",
    "busy_timeout", "call_tool raised after connect", "econnreset",
    "epipe", "socket", "server not configured",
)
# Structured payload keys that ALWAYS mean "deterministic", regardless of
# what the rendered message text says. A rejection dict (status=="rejected")
# is a positive business/governance decision -- RBAC denial, or the Cerberus
# venom gate's REFUSED / requires_human / fail-CLOSED-internal-error branches
# (dispatcher.py._venom_gate) -- never a retryable transport blip, even when
# the wrapped inner exception's text happens to contain a transport-sounding
# phrase (e.g. a venom gate fail-closed on a locked episodic audit store:
# "venom gate internal error: database is locked"). Only {"status":"failed"}
# is ambiguous enough to fall through to the text markers above.
_DETERMINISTIC_PAYLOAD_KEYS: tuple[str, ...] = (
    "gate_error", "hitl_required", "venom_refused",
)


def _classify_infra_failure(exc: Exception | None) -> str:
    """Classify a pp ledger call failure as "transport" or "deterministic".

    Structural check FIRST: if ``exc`` is a ``PPLedgerError`` carrying the
    original call_mcp response dict, a ``status == "rejected"`` payload (or
    any of ``_DETERMINISTIC_PAYLOAD_KEYS`` present and truthy) is always
    "deterministic" -- no message text is consulted. This is what keeps a
    fail-closed venom-gate rejection from being misclassified as "transport"
    just because its wrapped exception text contains a phrase like "database
    is locked". Only when no such structure is available (a raw exception
    that never became a call_mcp response dict) do we fall back to the
    marker-based text match below.

    Returns "transport" only when the exception text matches a known-good
    transport signal and no deterministic-rejection signal. Any other case --
    including ``exc is None`` -- returns "deterministic" so an unrecognized
    failure shape still fails the stage instead of masking a real rejection.
    """
    if exc is None:
        return "deterministic"
    payload = getattr(exc, "payload", None)
    if isinstance(payload, dict):
        if payload.get("status") == "rejected":
            return "deterministic"
        if any(payload.get(k) for k in _DETERMINISTIC_PAYLOAD_KEYS):
            return "deterministic"
    msg = str(exc).lower()
    if any(m in msg for m in _DETERMINISTIC_FAILURE_MARKERS):
        return "deterministic"
    if any(m in msg for m in _TRANSPORT_FAILURE_MARKERS):
        return "transport"
    return "deterministic"


# --------------------------------------------------------------------------- #
# E2-27: judge_model_id validation / normalization                            #
# --------------------------------------------------------------------------- #
# pp's ``recordVerdict`` hard-pins the model id a judge may report for the
# vendors whose critique CLI is itself pinned (codex, agy). A visible judge
# subagent that reports the model it *thinks* it used (e.g. "gpt-5.1-codex")
# rather than the id pp's critique tool actually served made record_verdict
# throw a validation error; the bridge classified that as deterministic-fatal
# and surfaced an otherwise-PASSING stage with the merge discarded (E2-27).
#
# The host now (a) tells the judge which ids are acceptable via the judge
# host_action, (b) normalizes a mislabeled id to the producer's pinned
# critique model before calling record_verdict (keeping the reported value in
# ``score_json["judge_model_id_reported"]``), and (c) treats a pp pin error as
# host-correctable rather than fatal.

# Suffix the same-vendor Claude judge appends to its producer label (LV-3) so
# pp's generator-identical producer+model check does not reject the verdict.
# It is a LABEL, not a vendor: strip it before looking up model pins.
_SAME_VENDOR_HOST_SUFFIX = "-same-vendor-host"

# Static fallback for pp's pinned critique model ids, used ONLY when the
# ``pp_harness.doctor`` probe is unavailable (B1: doctor's live
# ``judge_capabilities`` is otherwise always authoritative -- see
# ``allowed_judge_model_ids`` below). Mirrors pp's current
# ``JUDGE_MODEL_POLICY`` (daemon/src/config.ts): codex default/escalated
# ``gpt-5.6-terra`` / ``gpt-5.6-sol``; agy default/escalated
# ``gemini-3.8-flash-medium`` / ``gemini-3.1-pro-high``. Keep these in sync
# with pp's ``JUDGE_MODEL_POLICY`` when pp repins -- a stale id here is only a
# fallback-of-last-resort, but a stale one is still wrong.
# ``claude`` is present with an EMPTY tuple on purpose: pp does not pin Claude
# critique models, so any id is acceptable and no normalization applies -- but
# "claude" must still be a KNOWN producer so the same-vendor judge is not
# rejected as unsupported.
_STATIC_JUDGE_MODEL_PINS: dict[str, tuple[str, ...]] = {
    "codex": ("gpt-5.6-terra", "gpt-5.6-sol"),
    "agy": ("gemini-3.8-flash-medium", "gemini-3.1-pro-high"),
    "claude": (),
}

# Substring identifying pp's judge-model pin rejection on record_verdict.
# Such a rejection is host-correctable (re-report the model id) rather than a
# fatal defect in the artifact, so it must NOT surface a passing stage.
_JUDGE_PIN_ERROR_MARKER = "must record judge_model_id"

# --------------------------------------------------------------------------- #
# Hydra#72: judge_model_source / judge_override_reason provenance             #
# --------------------------------------------------------------------------- #
# pp's ``recordVerdict`` (pair-programmer daemon/src/orchestrator/runs.ts:981-
# 1057) enforces THREE independent things on every verdict, verified read-only
# against pp's source for this fix:
#
#   1. ``isAllowedJudgeModel`` (config.ts:311-314, runs.ts:1006-1013): the
#      reported ``judge_model_id`` must be a member of that vendor's
#      ``JUDGE_MODEL_POLICY[vendor].allowed_models`` (config.ts:75-96) --
#      e.g. codex allows {gpt-5.6-terra, gpt-5.6-sol, gpt-5.6-luna}, NOT just
#      the default+escalated pair. This is a MODEL allow-list check,
#      independent of (3) below.
#   2. ``judge_reasoning_effort``, if present, must be one of that vendor's
#      ``allowed_efforts`` (config.ts:47,1016-1022) -- codex: {low, medium,
#      high, xhigh}; agy: {low, medium, high}.
#   3. Provenance (runs.ts:1025-1057): ``judge_model_source`` defaults to
#      "default" when omitted, and "default"/"escalated" are PINS -- the
#      reported ``judge_model_id`` must equal that vendor's pinned
#      default/escalated model exactly (runs.ts:1045-1057) or record_verdict
#      throws. Any OTHER allowed_models member (e.g. codex's "gpt-5.6-luna",
#      or any agy id besides the two pins) can only be recorded via one of
#      the override channels {cli, team_yaml, hydra} (JUDGE_SOURCES_REQUIRING_
#      REASON, runs.ts:887) together with a ``judge_override_reason`` of at
#      least 8 non-whitespace chars (runs.ts:888,1036-1044) -- "hydra" does
#      NOT accept an arbitrary model id, only one already in that vendor's
#      ``allowed_models`` from (1).
#
# Hydra never forwarded judge_model_source/judge_override_reason/
# judge_reasoning_effort, so every verdict implicitly claimed source=
# "default" -- silently correct only when the judge happened to report the
# exact default pin, and a hard rejection (masqueraded as a generic
# "validation" failure -> deterministic-fatal -> revise) for every other
# allowed model, discarding an otherwise-passing stage. ``_judge_verdict_
# provenance`` derives the correct provenance from the SAME pin data
# ``allowed_judge_model_ids`` already sources (doctor probe, falling back to
# ``_STATIC_JUDGE_MODEL_PINS``) -- one helper, not a second copy of the pin
# table -- and ``_is_judge_provenance_error`` routes a residual provenance
# rejection (e.g. pp's live policy has drifted from Hydra's cached/static
# pins) to the same host-correctable path as the E2-27 judge_model_id pin
# error, instead of ever converting it into a revise/fail verdict.

_JUDGE_OVERRIDE_SOURCES = frozenset({"default", "escalated", "cli", "team_yaml", "hydra"})
_JUDGE_SOURCES_REQUIRING_REASON = frozenset({"cli", "team_yaml", "hydra"})
_JUDGE_OVERRIDE_REASON_MIN_CHARS = 8

# Substrings identifying pp's judge-selection PROVENANCE rejection on
# record_verdict (runs.ts:1029-1057) -- distinct from the judge_model_id
# allow-list rejection matched by ``_JUDGE_PIN_ERROR_MARKER`` above. Matched
# together (both substrings must appear) rather than against the full
# rendered sentence, which varies by branch (the "must be one of" source-
# enum error, the override-reason-too-short error, and the source-pins-a-
# different-model error all mention both terms).
_JUDGE_PROVENANCE_ERROR_MARKERS: tuple[str, str] = (
    "judge_model_source", "judge_override_reason",
)


def _judge_default_escalated_ids(
    dispatcher: Dispatcher | None,
) -> dict[str, tuple[str | None, str | None]]:
    """Return ``{vendor: (default_id, escalated_id)}`` from the SAME pin
    source ``allowed_judge_model_ids`` uses (doctor's ``judge_capabilities``,
    falling back to ``_STATIC_JUDGE_MODEL_PINS``) -- never a second copy of
    the pin table. Both sources order their list as [default, escalated,
    ...remaining allow-listed ids] (see ``allowed_judge_model_ids``'s
    docstring), so position 0/1 reliably identify the two pins; a vendor with
    fewer than 2 entries (e.g. claude's empty tuple) reports ``None`` for the
    missing slot(s).
    """
    out: dict[str, tuple[str | None, str | None]] = {}
    for vendor, ids in allowed_judge_model_ids(dispatcher).items():
        out[vendor] = (
            ids[0] if len(ids) > 0 else None,
            ids[1] if len(ids) > 1 else None,
        )
    return out


def _judge_verdict_provenance(
    dispatcher: Dispatcher | None, *, judge_vendor: str, judge_model_id: str,
    result: dict[str, Any], allowed_models: dict[str, list[str]],
) -> dict[str, Any]:
    """Derive the ``judge_model_source``/``judge_override_reason``/
    ``judge_reasoning_effort`` fields to forward on ``record_verdict``.

    Returns a dict to be splatted into the record_verdict payload -- empty
    when pp pins nothing for ``judge_vendor`` (e.g. claude: producers with no
    ``JUDGE_MODEL_POLICY`` entry are unchecked, runs.ts:1006-1007) or when
    ``judge_model_id`` is not in that vendor's ``allowed_models`` at all (no
    provenance field can fix an unlisted model id -- record_verdict's own
    ``isAllowedJudgeModel`` check will reject it; that is the E2-27 pin-error
    path, handled separately).

    Precedence:
      1. A judge result MAY self-report ``judge_model_source`` (and, for the
         three override channels, ``judge_override_reason``) -- honored only
         when internally consistent with ``judge_model_id`` (a "default"
         claim must actually name the default pin; an override claim must
         carry a reason of at least ``_JUDGE_OVERRIDE_REASON_MIN_CHARS``
         chars). An inconsistent/invalid supplied source is never forwarded
         blindly -- it falls through to derivation below.
      2. Otherwise, derive from ``judge_model_id`` against the vendor's pins:
         the default pin -> "default", the escalated pin -> "escalated",
         any other allow-listed id -> "hydra" with a generated reason naming
         the model (pp requires >= 8 chars for any of {cli, team_yaml,
         hydra}; "hydra" is the correct label since this is Hydra's own
         override, not a CLI flag or team_yaml entry).

    ``judge_reasoning_effort`` is forwarded verbatim when the judge result
    supplies one -- pp validates it against that vendor's ``allowed_efforts``
    (config.ts:1016-1022) itself; an invalid effort surfaces as its own
    record_verdict rejection.
    """
    ids = list(allowed_models.get(judge_vendor) or [])
    if not ids:
        return {}
    default_id, escalated_id = ids[0], (ids[1] if len(ids) > 1 else None)

    def _reason_or_none(raw: Any) -> str | None:
        s = str(raw or "").strip()
        return s if len(s) >= _JUDGE_OVERRIDE_REASON_MIN_CHARS else None

    source: str | None = None
    reason: str | None = None
    supplied_source = result.get("judge_model_source")
    if isinstance(supplied_source, str) and supplied_source in _JUDGE_OVERRIDE_SOURCES:
        if supplied_source == "default" and judge_model_id == default_id:
            source = "default"
        elif supplied_source == "escalated" and judge_model_id == escalated_id:
            source = "escalated"
        elif (supplied_source in _JUDGE_SOURCES_REQUIRING_REASON
              and judge_model_id in ids):
            supplied_reason = _reason_or_none(result.get("judge_override_reason"))
            if supplied_reason is not None:
                source, reason = supplied_source, supplied_reason

    if source is None:
        if judge_model_id == default_id:
            source = "default"
        elif escalated_id is not None and judge_model_id == escalated_id:
            source = "escalated"
        elif judge_model_id in ids:
            source = "hydra"
            reason = (
                f"attended judge selected {judge_model_id} from "
                f"allowed_judge_model_ids ({judge_vendor} failover/override)"
            )
        else:
            # Not in pp's allow-list at all -- isAllowedJudgeModel will reject
            # the model id itself; no provenance field can fix that.
            return {}

    out: dict[str, Any] = {"judge_model_source": source}
    if source in _JUDGE_SOURCES_REQUIRING_REASON:
        out["judge_override_reason"] = reason
    effort = result.get("judge_reasoning_effort")
    if isinstance(effort, str) and effort.strip():
        out["judge_reasoning_effort"] = effort.strip()
    return out


def _is_judge_provenance_error(exc: Exception | None) -> bool:
    """True when a record_verdict failure is pp's judge-selection provenance
    rejection (runs.ts:1029-1057) -- a LABEL/provenance problem the host can
    correct by re-deriving/re-reporting the source, not an artifact defect.
    Must be routed to the host-correctable path exactly like
    ``_is_judge_pin_error``, never converted into a revise/fail verdict.
    """
    if exc is None:
        return False
    msg = str(exc).lower()
    if all(m in msg for m in _JUDGE_PROVENANCE_ERROR_MARKERS):
        return True
    payload = getattr(exc, "payload", None)
    if isinstance(payload, dict):
        pmsg = str(payload.get("error", "")).lower()
        if all(m in pmsg for m in _JUDGE_PROVENANCE_ERROR_MARKERS):
            return True
    return False


# Bound on how many times one judge call_key may be bounced back to the host
# for a model-id/producer correction before the bridge stops asking and lets
# the normal (pp-authoritative) path run. Without a bound a host that keeps
# re-reporting the same unsupported producer would livelock the stage.
_MAX_JUDGE_MODEL_CORRECTIONS = 2

# Process-level cache of the doctor probe (fail-soft, refreshed on demand).
_JUDGE_MODEL_PIN_CACHE: dict[str, tuple[str, ...]] | None = None


def _judge_pending_action(*, call_key: str, judge_agent: str, gate_rubric: str,
                          rubric_fallback: bool, required_cross: bool,
                          judge_text: str, rubric_body: str, work_path: Any,
                          allowed_models: dict[str, list[str]],
                          judge_producer: str,
                          failover_note: str = "") -> dict[str, Any]:
    """Build the judge host_action, including B2's selected ``judge_producer``
    (from the authoritative cross-vendor mapping, not pp's raw pool order) and
    that vendor's ``preferred_models``. Shared by the initial judge dispatch
    and by ``_apply_judge``'s engine-side ``judge_tool_failed`` failover so
    both paths hand the host an identically-shaped instruction.
    """
    preferred_models = allowed_models.get(judge_producer, [])
    return {
        "call_key": call_key,
        "agent_type": judge_agent,
        "rubric_id": gate_rubric,
        "rubric_fallback": rubric_fallback,
        "required_cross_vendor": required_cross,
        "artifact_text": judge_text,
        "rubric_md": rubric_body,
        "cwd": work_path,
        "allowed_judge_model_ids": allowed_models,
        # B2: the producer the host MUST use, per pp's authoritative
        # cross-vendor mapping -- not merely a hint among several.
        "judge_producer": judge_producer,
        "preferred_models": preferred_models,
        "instructions": (
            f"Spawn the visible `{judge_agent}` subagent to judge the diff "
            f"against rubric {gate_rubric}"
            + (" (NOTE: no registry served this rubric's body — `rubric_md` "
               "below is the GENERIC fallback text, judge against that)"
               if rubric_fallback else "")
            + f". Use judge_producer={judge_producer!r} "
            f"(preferred_models={preferred_models!r}) per pp's cross-vendor "
            "mapping in judge-cross-vendor.md -- do not substitute a "
            "different vendor unless this host_action tells you to fail "
            "over. If the chosen vendor's CLI is not configured, return "
            "{judge_tool_failed: true, reason, vendor, model: null} and "
            "STOP; do NOT silently fall back to another vendor yourself. "
            + (failover_note + " " if failover_note else "")
            + "Then call submit-host-result with "
            "{call_key, result:{outcome:pass|revise|fail, critique_md, "
            "judge_producer, judge_model_id, score_json, cost_usd}}. "
            "Report judge_model_id exactly as the critique tool returned it; "
            "if the tool named no model, use the pinned id for that producer "
            f"from allowed_judge_model_ids ({allowed_models!r}). Never invent "
            "a model id."),
    }


def allowed_judge_model_ids(dispatcher: Dispatcher | None,
                            *, refresh: bool = False) -> dict[str, list[str]]:
    """Return ``{judge_producer: [acceptable model ids]}``, default id first.

    B1: sourced from ``pp_harness.doctor``'s ``judge_capabilities`` map --
    pp added ``allowed_critique_models`` and ``escalated_critique_model``
    fields specifically so downstream callers stop hard-coding stale static
    pins (see the comment above ``describeJudgeCapabilities`` in pp's
    ``gates.ts``). When doctor reports a vendor, its full allow-list (default
    id first, then escalated, then any remaining allow-listed ids) WINS
    outright for that vendor -- it does not merely get merged in front of the
    static fallback. The static map above is used only when doctor is
    unreachable or reports nothing for a vendor.
    #
    # This also fixes the model-id-index-0 bug: the previous implementation
    # inserted only doctor's DEFAULT ``critique_model`` at the front of the
    # (stale) static bucket and never consulted ``escalated_critique_model``
    # or ``allowed_critique_models`` at all, so a judge legitimately
    # reporting the escalated id (e.g. codex's ``gpt-5.6-sol``) would not be
    # found in the bucket and got silently rewritten down to the default
    # (``gpt-5.6-terra``) by the normalization step in ``_apply_judge``.

    Fail-soft in every direction: a missing dispatcher, an RBAC denial, a
    transport error, or a malformed payload all fall back to the static map.
    An empty list means "this producer is known but pp pins no model for it"
    (claude), which disables normalization rather than rejecting the verdict.
    """
    global _JUDGE_MODEL_PIN_CACHE
    if _JUDGE_MODEL_PIN_CACHE is not None and not refresh:
        return {k: list(v) for k, v in _JUDGE_MODEL_PIN_CACHE.items()}
    pins: dict[str, list[str]] = {
        k: list(v) for k, v in _STATIC_JUDGE_MODEL_PINS.items()
    }
    if dispatcher is not None:
        try:
            caps = _pp_inner(_raise_on_error_payload(
                dispatcher.call_mcp("pp_harness", "doctor", {}, squad_id=_SQ),
                "doctor",
            )).get("judge_capabilities")
            if isinstance(caps, dict):
                for vendor, summary in caps.items():
                    if not isinstance(summary, dict):
                        continue
                    doctor_ids: list[str] = []
                    default_model = (summary.get("critique_model")
                                     or summary.get("default_critique_model"))
                    if default_model:
                        doctor_ids.append(str(default_model))
                    escalated_model = summary.get("escalated_critique_model")
                    if escalated_model and str(escalated_model) not in doctor_ids:
                        doctor_ids.append(str(escalated_model))
                    allow_list = summary.get("allowed_critique_models")
                    if isinstance(allow_list, list):
                        for m in allow_list:
                            if m and str(m) not in doctor_ids:
                                doctor_ids.append(str(m))
                    if doctor_ids:
                        # Doctor is authoritative for this vendor: replace the
                        # static fallback entirely rather than merging, so a
                        # stale static id can never linger in the bucket.
                        pins[str(vendor)] = doctor_ids
                    elif str(vendor) not in pins:
                        # A vendor doctor knows about but pins nothing for
                        # (e.g. claude) -- record it as known with no pin.
                        pins[str(vendor)] = []
        except Exception:  # noqa: BLE001 — never block a stage on a probe
            pass
    _JUDGE_MODEL_PIN_CACHE = {k: tuple(v) for k, v in pins.items()}
    return {k: list(v) for k, v in pins.items()}


def _reset_judge_model_pin_cache() -> None:
    """Test hook: drop the cached doctor probe."""
    global _JUDGE_MODEL_PIN_CACHE
    _JUDGE_MODEL_PIN_CACHE = None


def _is_judge_pin_error(exc: Exception | None) -> bool:
    """True when a record_verdict failure is pp's judge-model pin rejection.

    That rejection is a LABEL problem the host can correct by re-reporting the
    model id -- it says nothing about the artifact -- so it must be routed to
    the host-correctable path instead of ``_classify_infra_failure``'s
    deterministic-fatal default (which matches on "validation").
    """
    if exc is None:
        return False
    if _JUDGE_PIN_ERROR_MARKER in str(exc).lower():
        return True
    payload = getattr(exc, "payload", None)
    if isinstance(payload, dict):
        return _JUDGE_PIN_ERROR_MARKER in str(payload.get("error", "")).lower()
    return False


def _judge_correction_budget_left(cursor: dict[str, Any],
                                  call_key: str | None) -> bool:
    """Whether this judge call_key may still be bounced back for a correction."""
    counts = cursor.get("judge_model_corrections")
    if not isinstance(counts, dict):
        return True
    return int(counts.get(str(call_key), 0)) < _MAX_JUDGE_MODEL_CORRECTIONS


def _record_judge_correction(cursor: dict[str, Any],
                             call_key: str | None) -> int:
    counts = cursor.get("judge_model_corrections")
    if not isinstance(counts, dict):
        counts = {}
        cursor["judge_model_corrections"] = counts
    key = str(call_key)
    counts[key] = int(counts.get(key, 0)) + 1
    return counts[key]


# --------------------------------------------------------------------------- #
# Worktree isolation (write-safety)                                           #
# --------------------------------------------------------------------------- #
# In attended mode the host's visible `engineer` subagent writes code. The
# `hydra-block-direct-write` hook blocks engine-source writes unless the path
# resolves under the project root's worktree root (or HYDRA_PP_STAGE_ACTIVE=1,
# which we must NOT set session-wide). So we isolate the engineer into a linked
# git worktree under `resolve_worktree_root()` (default
# `<AIAPP_BASE>/.hydra-worktrees/<repo_id>/`, overridable via
# `HYDRA_WORKTREE_ROOT`, NOT inside the target repo) — already hook-allowed —
# and merge the result back into the repo on a passing finalize. Keeping the
# worktree outside repo_root avoids two real incidents: a test runner globbing
# `.harness/worktrees/**/tests` alongside the real `tests/` dir, and untracked
# `.hydra/`/`.harness/` state nested inside the target repo aborting a merge.
# This keeps HYDRA_ENFORCE_ROUTING fully on and the host session unable to
# hand-write project source. Fail-soft: if the repo isn't git or worktree
# provisioning fails, fall back to in-place writes.

def _git_timeout_s() -> int:
    """Return the git subprocess timeout in seconds from ``HYDRA_GIT_TIMEOUT_S``
    (default 60). Env: HYDRA_GIT_TIMEOUT_S.

    Fail-soft: any non-integer, missing, or non-positive value returns 60.
    """
    raw = os.environ.get("HYDRA_GIT_TIMEOUT_S")
    try:
        v = int(raw) if raw else 60
    except (TypeError, ValueError):
        v = 60
    return v if v > 0 else 60


def _baseline_timeout_s() -> int:
    """Return the baseline-smoke test-suite timeout in seconds from
    ``HYDRA_BASELINE_TIMEOUT_S`` (default 600). Env: HYDRA_BASELINE_TIMEOUT_S.

    Used for both the pre-change baseline run and the per-failure rerun.
    Raised from the old hardcoded 240 s to give slow suites more headroom.

    Fail-soft: any non-integer, missing, or non-positive value returns 600.
    """
    raw = os.environ.get("HYDRA_BASELINE_TIMEOUT_S")
    try:
        v = int(raw) if raw else 600
    except (TypeError, ValueError):
        v = 600
    return v if v > 0 else 600


def _git(args: list[str], cwd: str | Path, timeout: int | None = None) -> subprocess.CompletedProcess:
    if timeout is None:
        timeout = _git_timeout_s()
    return run_text(["git", *args], cwd=str(cwd), capture_output=True,
                    timeout=timeout, check=False)


def _git_repo_root(path: str | Path) -> str | None:
    try:
        res = _git(["rev-parse", "--show-toplevel"], path)
    except Exception:  # noqa: BLE001
        return None
    return res.stdout.strip() if res.returncode == 0 else None


def _worktree_repo_id(repo_root: str) -> str:
    """Stable per-repo directory-name id derived from ``repo_root``.

    Used to namespace the shared worktree root so attended worktrees for
    different repos never collide when they share ``HYDRA_WORKTREE_ROOT`` /
    ``AIAPP_BASE``. Sanitized the same way ``_provision_worktree`` sanitizes
    ``run_id`` — alnum/-/_ only, never empty.
    """
    name = Path(repo_root).resolve().name if repo_root else ""
    safe = "".join(c for c in name if c.isalnum() or c in "-_")
    return safe or "repo"


def resolve_worktree_root(repo_root: str) -> Path:
    """Resolve the directory under which attended worktrees for ``repo_root``
    are provisioned, namespaced per-repo as ``<root>/<repo_id>``.

    Resolution order (mirrors the ecosystem ``AIAPP_BASE`` convention
    documented in ``docs/PORTABILITY.md``):

      1. ``HYDRA_WORKTREE_ROOT`` env var — explicit override, honored as-is.
      2. ``AIAPP_BASE`` env var — ``<AIAPP_BASE>/.hydra-worktrees``.
      3. Sibling fallback — ``<parent of repo_root>/.hydra-worktrees``. This
         mirrors the convention's own fallback (repo_root is normally a
         direct child of the shared base directory), so a bare checkout with
         no env configured still gets a real sibling location rather than an
         error.

    Moved OUT of ``<repo_root>/.harness/worktrees`` (2026-08): a test runner
    that globs the repo tree from repo_root would otherwise pick up every
    attended stage's worktree copy of ``tests/`` alongside the real one
    (a greenfield project reported 62 tests where 31 existed), and untracked
    ``.hydra/``/``.harness/`` state nested inside the target repo caused a
    real merge abort. The write-block hooks' allow-list is anchored to this
    same env var (see ``plugins/hydra/hooks/hydra-block-direct-write.ps1``),
    so relocating here and there must be kept in lockstep.
    """
    env_override = os.environ.get("HYDRA_WORKTREE_ROOT")
    if env_override:
        base = Path(env_override)
    else:
        aiapp_base = os.environ.get("AIAPP_BASE")
        if aiapp_base:
            base = Path(aiapp_base) / ".hydra-worktrees"
        else:
            base = Path(repo_root).resolve().parent / ".hydra-worktrees"
    return base / _worktree_repo_id(repo_root)


def _provision_worktree(repo_root: str, run_id: str) -> tuple[str, str] | None:
    """Create a linked worktree + branch off HEAD for an attended stage.

    Returns ``(worktree_path, branch)`` or None on any failure (caller falls
    back to in-place). The worktree lives under ``resolve_worktree_root()``
    (default ``<AIAPP_BASE>/.hydra-worktrees/<repo_id>/``, overridable via
    ``HYDRA_WORKTREE_ROOT``) — NOT inside ``repo_root`` — which the write-block
    hook's allow-list resolves the same way so the engineer's writes there
    stay hook-permitted.
    """
    safe = "".join(c for c in str(run_id) if c.isalnum() or c in "-_") or "run"
    branch = f"attended/{safe}"
    wt = resolve_worktree_root(repo_root) / f"attended-{safe}"
    try:
        wt.parent.mkdir(parents=True, exist_ok=True)
        if wt.exists():
            _git(["worktree", "remove", "--force", str(wt)], repo_root)
        # -B resets the branch if a stale one exists from a prior aborted run.
        res = _git(["worktree", "add", "-B", branch, str(wt), "HEAD"], repo_root)
        if res.returncode != 0:
            return None
    except Exception:  # noqa: BLE001
        return None
    return str(wt), branch


_BYPRODUCT_PATTERNS: list[str] = [
    "__pycache__/",
    ".pytest_cache/",
    "*.pyc",
    "node_modules/",
    ".tmp*/",
    ".hydra/",
    ".harness/",
    "*.log",
]


def _write_worktree_gitexcludes(worktree_path: str) -> None:
    """Append byproduct patterns to the worktree-local git exclude file.

    For linked worktrees the ``.git`` entry is a text file pointing at the
    private gitdir; ``git rev-parse --git-path info/exclude`` resolves the
    correct exclude file path regardless of worktree type.  Idempotent — patterns
    already present in the file are not duplicated.  Fail-soft on any error.
    """
    try:
        r = run_text(
            ["git", "-C", worktree_path, "rev-parse", "--git-path", "info/exclude"],
            capture_output=True, timeout=5, check=False,
        )
        if r.returncode != 0 or not r.stdout.strip():
            return
        raw = r.stdout.strip()
        excl = Path(raw) if Path(raw).is_absolute() else Path(worktree_path) / raw
        excl.parent.mkdir(parents=True, exist_ok=True)
        existing = excl.read_text(encoding="utf-8") if excl.exists() else ""
        to_add = [p for p in _BYPRODUCT_PATTERNS if p not in existing]
        if to_add:
            with excl.open("a", encoding="utf-8") as _f:
                _f.write("\n".join(to_add) + "\n")
    except Exception:  # noqa: BLE001 — fail-soft
        pass


def _merge_worktree_back(repo_root: str, worktree_path: str, branch: str) -> dict[str, Any]:
    """Commit the engineer's changes in the worktree and merge them into the
    repo's checked-out branch. Returns a status dict; never raises."""
    out: dict[str, Any] = {"merged": False, "sha": None, "error": None}
    try:
        # Stage + commit any uncommitted work the engineer left in the worktree.
        _write_worktree_gitexcludes(worktree_path)
        st = _git(["status", "--porcelain"], worktree_path)
        if st.stdout.strip():
            _git(["add", "-A"], worktree_path)
            _git(["commit", "-m", f"attended engineering ({branch})",
                  "--no-verify"], worktree_path)
        head = _git(["rev-parse", "HEAD"], worktree_path)
        wt_sha = head.stdout.strip()
        base = _git(["rev-parse", "HEAD"], repo_root).stdout.strip()
        if wt_sha == base:
            out["error"] = "no_changes_to_merge"
            return out
        # Fast-forward / merge the branch into the repo's current branch.
        mres = _git(["merge", "--no-ff", "--no-edit", branch], repo_root)
        if mres.returncode != 0:
            # Abort a conflicted merge so the repo is left clean for the operator.
            _git(["merge", "--abort"], repo_root)
            out["error"] = f"merge_failed: {(mres.stderr or mres.stdout).strip()[:300]}"
            return out
        out["merged"] = True
        out["sha"] = _git(["rev-parse", "HEAD"], repo_root).stdout.strip()
    except Exception as e:  # noqa: BLE001
        out["error"] = f"merge_exception: {e!r}"[:300]
    return out


def _merge_branch_back(repo_root: str, branch: str) -> dict[str, Any]:
    """W2-4: merge a ``preserved_branch`` into the repo's checked-out branch
    WITHOUT a live worktree.

    Used only by the recovery path (`recover_stalled_stage`): the worktree
    that hosted ``branch`` was already removed by ``_finalize``, but
    ``_preserve_non_complete_work`` committed every uncommitted engineer
    change to the branch before that removal, so the branch itself still
    carries the full change set. Unlike ``_merge_worktree_back`` this never
    touches ``worktree_path`` (there isn't one) — it only reads/merges the
    already-committed branch.

    Two DISTINCT no-op shapes exist and must not be collapsed into one
    marker: ``branch_sha == base`` means the branch's tip IS the current
    HEAD (nothing to merge, the branch and HEAD are literally the same
    commit) -- reported as ``no_changes_to_merge``. But a branch that was
    already merged EARLIER (its tip is an ancestor of HEAD, not equal to
    it) does NOT hit that check: ``git merge --no-ff --no-edit <branch>``
    for an already-merged branch prints "Already up to date.", exits 0,
    and creates NO commit -- yet a naive caller that then does
    ``rev-parse HEAD`` unconditionally would report a fabricated "merged"
    sha (actually whatever unrelated commit HEAD already pointed to).  Do
    NOT parse git's "Already up to date." text to detect this -- that
    string is localizable and version-dependent. The only authoritative
    check is comparing commit ids: capture HEAD before the merge attempt
    and compare to HEAD after. If HEAD did not move, git created no
    commit and nothing was merged, reported as ``already_merged`` (with no
    sha) -- a genuinely different situation from ``no_changes_to_merge``
    (branch has nothing new) worth keeping distinguishable for the
    operator. ``out["base"]`` records the pre-merge HEAD whenever a merge
    commit IS created, so ``_revert_merge_commit`` can verify it is
    reverting a commit this call actually produced (mainline parent ==
    base) rather than trusting the reported sha blindly. Never raises."""
    out: dict[str, Any] = {"merged": False, "sha": None, "base": None, "error": None}
    try:
        chk = _git(["rev-parse", "--verify", branch], repo_root)
        if chk.returncode != 0:
            out["error"] = f"branch_not_found: {branch}"
            return out
        branch_sha = chk.stdout.strip()
        pre_head = _git(["rev-parse", "HEAD"], repo_root).stdout.strip()
        if branch_sha == pre_head:
            out["error"] = "no_changes_to_merge"
            return out
        mres = _git(["merge", "--no-ff", "--no-edit", branch], repo_root)
        if mres.returncode != 0:
            _git(["merge", "--abort"], repo_root)
            out["error"] = f"merge_failed: {(mres.stderr or mres.stdout).strip()[:300]}"
            return out
        post_head = _git(["rev-parse", "HEAD"], repo_root).stdout.strip()
        if post_head == pre_head:
            # git exited 0 but HEAD never moved -- "Already up to date.":
            # branch_sha is an ancestor of pre_head, not equal to it, so the
            # fast path above missed it. No commit was created; report the
            # truth instead of a phantom "merged" sha.
            out["error"] = "already_merged"
            return out
        out["merged"] = True
        out["sha"] = post_head
        out["base"] = pre_head
    except Exception as e:  # noqa: BLE001
        out["error"] = f"merge_exception: {e!r}"[:300]
    return out


def _revert_sequencer_git_dir(repo_root: str) -> Path | None:
    """Resolve repo_root's REAL git dir via ``git rev-parse --git-dir``,
    never by assuming ``<repo_root>/.git`` -- that assumption is wrong for a
    linked worktree, where the git dir lives under the main repo's
    ``.git/worktrees/<name>`` instead. Returns None if it cannot be
    resolved (never raises)."""
    try:
        res = _git(["rev-parse", "--git-dir"], repo_root)
        if res.returncode != 0:
            return None
        gd = res.stdout.strip()
        if not gd:
            return None
        path = Path(gd)
        return path if path.is_absolute() else Path(repo_root) / path
    except Exception:  # noqa: BLE001
        return None


def _revert_sequencer_state(repo_root: str) -> str:
    """Inspect repo_root's real git dir for REVERT sequencer state
    (``REVERT_HEAD`` / ``sequencer/todo``). This function is only ever
    called from ``_revert_merge_commit`` immediately after that same
    function's own ``git revert``, so it never needs to distinguish a
    revert sequencer from a cherry-pick one -- it does not check
    ``CHERRY_PICK_HEAD`` and must not claim to. This is the authority on
    whether an abort "worked" -- NOT the abort command's own exit code,
    which is nonzero for two unrelated reasons that must not be conflated:
    (1) a real sequencer was active and the abort itself failed to tear it
    down (state remains, genuinely bad), vs (2) the preceding revert never
    got far enough to create a sequencer at all (e.g. refused upfront over
    a dirty file), so "abort" has nothing to abort and correctly errors
    even though the repo was already clean. Checking exit code alone would
    misreport case (2) as a failed abort.

    Returns one of three states, never just a bool -- collapsing "clean" and
    "could not tell" into one falsy value is exactly the failure shape this
    workstream exists to eliminate (a record claiming a state that was never
    actually verified):

      "active"  -- REVERT_HEAD or sequencer/todo genuinely present on disk.
      "clean"   -- the git dir resolved and neither marker is present.
      "unknown" -- the git dir itself could not be resolved (repo_root
                   inaccessible, git errored, etc). This is NOT the same
                   fact as "clean" -- it means the state could not be
                   inspected at all, and callers must treat it as at least
                   as bad as "active" (fail toward "go look"), never as an
                   assurance of cleanliness.

    Never raises."""
    git_dir = _revert_sequencer_git_dir(repo_root)
    if git_dir is None:
        return "unknown"
    try:
        active = (git_dir / "REVERT_HEAD").exists() or (git_dir / "sequencer" / "todo").exists()
    except Exception:  # noqa: BLE001
        return "unknown"
    return "active" if active else "clean"


def _revert_merge_commit(
    repo_root: str, merge_sha: str, *, expected_base: str,
) -> dict[str, Any]:
    """Undo a merge commit this recovery itself just created, via ``git
    revert`` rather than ``git reset --hard`` -- it adds a new commit instead
    of rewriting history, so it never rewrites or discards any pre-existing
    commit. That guarantee is about commits, not about working-tree/index
    cleanliness: if the abort step below fails to actually clear a real
    sequencer, this leaves the repo sitting mid-revert (conflicted index /
    half-applied working tree), which ``out["error"]`` reports rather than
    hides.

    Provenance guard (the safety invariant this function exists to hold):
    ``HEAD == merge_sha`` alone is NOT sufficient proof that ``merge_sha``
    is a commit this recovery created. It is trivially satisfied by ANY
    commit that happens to be HEAD -- including an unrelated commit an
    operator or another workflow made, if a caller passes a stale/wrong sha
    while HEAD genuinely equals it. That gap is exactly how the live
    incident happened: a caller reported a fabricated "merged" sha for a
    merge that never occurred (see ``_merge_branch_back``'s ``already_merged``
    fix), HEAD legitimately equalled that sha (it was some earlier, real
    commit), and this function faithfully reverted it. When the caller
    passes ``expected_base`` (the HEAD it observed immediately BEFORE
    invoking the merge that supposedly produced ``merge_sha``), this
    function additionally requires ``merge_sha`` to be an actual merge
    commit (>1 parent) whose FIRST (mainline) parent is exactly
    ``expected_base``. A merge commit's first parent is definitionally
    "what HEAD was before this merge ran" -- so this checks the one fact
    that proves the commit was produced by merging INTO ``expected_base``,
    not merely that it happens to be checked out right now. ``expected_base``
    is a required keyword-only argument -- there is no way to call this
    function without supplying it, so the provenance guard can never be
    silently skipped by a caller that simply omits an argument. Every
    in-tree caller supplies it.

    Whether the abort "worked" is judged by the real post-abort repo state
    (``_revert_sequencer_state``), not by the abort command's exit code --
    ``git revert --abort`` legitimately exits nonzero when the preceding
    revert refused before ever starting a sequencer (e.g. a dirty file in
    the way), and that is NOT a failed cleanup, just nothing to clean up.
    ``out["abort_state"]`` carries that check's own three-way result
    (``"active"`` / ``"clean"`` / ``"unknown"``); ``out["abort_failed"]`` is
    True for BOTH ``"active"`` (sequencer genuinely still present) and
    ``"unknown"`` (the git dir couldn't be resolved, so cleanliness could
    not be verified) -- an uninspectable repo is never reported as clean,
    only as a distinctly-worded, equally unmissable warning.

    The "a non-passing recovery must not retain the merged code" property is
    conditional, not unconditional: it holds only when ``HEAD`` still equals
    ``merge_sha`` at entry. If HEAD has moved (something else touched
    repo_root since), the revert is skipped entirely and the merged code
    remains in the repo -- only an error is recorded, nothing is forced
    through.

    Handles both a real merge commit (2 parents, needs ``-m 1`` to pick the
    mainline parent) and a plain single-parent commit defensively, in case a
    future caller passes a fast-forwarded SHA. Never raises."""
    out: dict[str, Any] = {
        "reverted": False, "sha": None, "error": None, "abort_failed": False,
        "abort_state": None,
    }
    try:
        head = _git(["rev-parse", "HEAD"], repo_root).stdout.strip()
        if head != merge_sha:
            out["error"] = (
                f"revert_skipped_head_moved: HEAD={head} expected={merge_sha}")
            return out
        parents = _git(
            ["rev-list", "--parents", "-n", "1", merge_sha], repo_root,
        ).stdout.split()
        is_merge_commit = len(parents) > 2  # [commit, parent1, parent2, ...]
        mainline_parent = parents[1] if len(parents) > 1 else None
        if not is_merge_commit or mainline_parent != expected_base:
            out["error"] = (
                f"revert_refused_provenance_mismatch: merge_sha={merge_sha} "
                f"is_merge_commit={is_merge_commit} mainline_parent="
                f"{mainline_parent!r} expected_base={expected_base!r} -- "
                "this commit's mainline parent does not match the base "
                "this recovery observed before merging, so it cannot be "
                "proven this recovery created it. Refusing to revert; "
                "repo_root is left untouched."
            )
            return out
        revert_cmd = ["revert", "--no-edit"]
        if is_merge_commit:
            revert_cmd += ["-m", "1"]
        revert_cmd.append(merge_sha)
        rres = _git(revert_cmd, repo_root)
        if rres.returncode != 0:
            revert_err = (rres.stderr or rres.stdout).strip()[:300]
            ares = _git(["revert", "--abort"], repo_root)
            seq_state = _revert_sequencer_state(repo_root)
            out["abort_state"] = seq_state
            if seq_state == "active":
                abort_err = (ares.stderr or ares.stdout).strip()[:300]
                out["abort_failed"] = True
                out["error"] = (
                    f"revert_failed: {revert_err}; abort_failed: sequencer "
                    f"state still present after abort (abort rc="
                    f"{ares.returncode}: {abort_err}) -- repo_root is left "
                    "mid-revert (conflicted index / half-applied working "
                    "tree), not cleanly restored; operator must inspect "
                    "repo_root's full state before any retry."
                )
            elif seq_state == "unknown":
                # Fail TOWARD abort_failed here, not away from it: the git
                # dir could not be resolved, so whether a sequencer remains
                # is genuinely unverified -- reporting "clean" would convert
                # ignorance into a false assurance. This is a DISTINCT fact
                # from "found dirty" (seq_state == "active"), so it gets its
                # own marker rather than being folded into the same text.
                abort_err = (ares.stderr or ares.stdout).strip()[:300]
                out["abort_failed"] = True
                out["error"] = (
                    f"revert_failed: {revert_err}; abort_state_unknown: "
                    "could not resolve repo_root's git dir to verify whether "
                    f"a sequencer remains after the abort (abort rc="
                    f"{ares.returncode}: {abort_err}) -- this is NOT a "
                    "confirmation that repo_root is clean, only an inability "
                    "to check; operator must inspect repo_root's full state "
                    "before any retry."
                )
            else:
                # abort's own exit code is irrelevant here: either it
                # cleanly tore down a real sequencer, or there was never one
                # to begin with (revert refused upfront) -- both are
                # verified clean, which is all that matters.
                out["error"] = f"revert_failed: {revert_err}"
            return out
        out["reverted"] = True
        out["sha"] = _git(["rev-parse", "HEAD"], repo_root).stdout.strip()
    except Exception as e:  # noqa: BLE001
        out["error"] = f"revert_exception: {e!r}"[:300]
    return out


def _remove_worktree(repo_root: str, worktree_path: str) -> dict[str, Any]:
    """Remove a worktree checkout and report whether it actually happened.

    Best-effort by design (callers here are cleanup paths, not a place to
    raise), but "best-effort" must not mean "silently untruthful": the
    returned dict tells the caller what really occurred rather than being a
    fire-and-forget ``None``. Checks the git ``CompletedProcess`` returncode
    (a nonzero exit does NOT raise -- `_git` doesn't check=True) AND verifies
    the path is genuinely gone afterwards, rather than trusting either
    signal alone. That before/after observation is the same discipline that
    caught the merge-back no-op elsewhere in this module, where trusting
    git's exit code was precisely the error.

    The returned verdict is deliberately a *disk* verdict, not a *git
    bookkeeping* verdict: ``removed=True`` means "the checkout directory is
    confirmed gone", which is what the operator's original concern (worktree
    disk occupancy) actually asks. It does NOT by itself mean git's own
    ``.git/worktrees/<name>`` registration is deregistered -- on Windows an
    AV scanner or an open handle can let the directory removal race ahead of
    (or independently of) git's admin-dir cleanup, leaving `git worktree
    list` still reporting a path whose directory is already gone. When the
    directory is confirmed gone, this function best-effort re-checks that
    registration and, if it finds this worktree's entry still stale-listed,
    deregisters ONLY this worktree's own ``.git/worktrees/<name>`` admin
    directory (see ``_prune_single_worktree_admin_dir``) -- deliberately NOT
    ``git worktree prune``, which is repo-wide and takes effect immediately
    with no grace period, and would deregister every missing worktree
    registration in the repository, not just this one. Attended stages can
    run concurrently; a repo-wide prune fired from one stage's cleanup could
    deregister another stage's live worktree if its directory read as
    transiently absent (slow filesystem, network mount, mid-write). This
    follow-up is advisory only and never flips the disk-based verdict already
    decided, since it doesn't change whether the disk space was reclaimed.

    Returns ``{"removed": bool, "error": str | None}``. Never raises.
    """
    err: str | None = None
    try:
        res = _git(["worktree", "remove", "--force", worktree_path], repo_root)
        if res.returncode != 0:
            err = (res.stderr or res.stdout or "").strip()[:300]
    except Exception as exc:  # noqa: BLE001
        err = f"exception: {exc!r}"[:300]
    try:
        still_present = Path(worktree_path).exists()
    except Exception:  # noqa: BLE001 — treat an unverifiable path as "not proven gone"
        still_present = True
    if still_present:
        return {"removed": False, "error": err or "git reported success but path still exists"}
    # Path is genuinely gone -- that's a real removal even if git also
    # reported a (now-moot) error, e.g. a racing caller removed it first.
    # Best-effort: if git's own worktree list still shows the (now-gone)
    # path registered -- a stale admin dir -- deregister ONLY this
    # worktree's own admin directory (never a repo-wide `worktree prune`;
    # see docstring). Failure here is swallowed on purpose: this is advisory
    # cleanup, not part of the disk-based verdict.
    try:
        listing = _git(["worktree", "list", "--porcelain"], repo_root).stdout or ""
        normalized = str(Path(worktree_path)).replace("\\", "/")
        still_registered = any(
            line.startswith("worktree ") and line[len("worktree "):].strip().replace("\\", "/") == normalized
            for line in listing.splitlines()
        )
        if still_registered:
            _prune_single_worktree_admin_dir(repo_root, worktree_path)
    except Exception:  # noqa: BLE001 — advisory only, never affects the verdict below
        pass
    return {"removed": True, "error": None}


def _is_registered_worktree(repo_root: str, worktree_path: str) -> bool:
    """Return True if ``worktree_path`` currently appears in ``git worktree
    list`` for ``repo_root``.

    Deliberately re-queries git on every call rather than accepting a cached
    listing from the caller: the janitor's orphan-removal path needs the
    answer to be true *at check time, immediately before removal*, not at
    whatever moment an earlier directory scan happened to run. This still
    leaves a narrow gap between this check returning and the caller's
    ``rmtree`` actually running -- see ``_remove_orphan_directory`` for why
    that residual race exists and can't be closed without an atomic
    check-and-delete primitive.

    Fails toward "assume registered" when git itself can't be asked (raised
    exception) -- an unverifiable answer must route the caller to the
    conservative ``git worktree remove`` path, never to the raw-rmtree
    orphan path.
    """
    try:
        listing = _git(["worktree", "list", "--porcelain"], repo_root).stdout or ""
    except Exception:  # noqa: BLE001
        return True
    normalized = str(Path(worktree_path)).replace("\\", "/")
    return any(
        line.startswith("worktree ")
        and line[len("worktree "):].strip().replace("\\", "/") == normalized
        for line in listing.splitlines()
    )


def _remove_orphan_directory(entry: Path, scan_root: Path, repo_root: str) -> dict[str, Any]:
    """Recursively remove ``entry``, a directory git no longer tracks as a
    worktree (deregistered by an earlier ``_finalize`` whose ``git worktree
    remove`` never ran, or ran against an admin dir that had already gone
    stale -- either way, the checkout survived on disk).

    This is a raw ``shutil.rmtree`` -- NOT a git operation -- and unlike
    ``git worktree remove`` it has no safety net of its own, so every guard
    here is re-checked immediately before removal rather than trusted from
    whatever the caller's directory listing found earlier. This is
    best-effort pre-delete validation, not a removal-time guarantee: the
    checks and the ``rmtree`` call below are separate operations, not one
    atomic step, and no atomic primitive is available here (there is no
    "check-and-remove" syscall this can be built on). A registration or
    directory recreation that lands in the gap between the check and the
    ``rmtree`` call is a real, if narrow, residual race -- it is merely much
    less likely to be hit than trusting a listing from an earlier scan
    would be, since the gap is now microseconds instead of however long the
    caller's own work took:

      (a) resolved-path containment under ``scan_root`` (the actual root
          that was scanned to find this entry) -- checked via
          ``Path.relative_to`` on resolved paths, so a symlink or a ``..``
          component cannot walk this call outside the root the sweep was
          told to operate on;
      (b) absent from ``git worktree list`` **at check time** (via
          ``_is_registered_worktree``), not merely absent from whatever
          listing an earlier caller happened to observe -- but another
          process can still re-register or recreate a worktree at this
          path after the check returns and before ``rmtree`` runs.

    The caller (``_sweep_one_root``) is responsible for the cursor-
    terminality gate and the ``attended-<run_id>`` name-shape check before
    ever reaching here; this function only re-verifies the two facts that
    can change between an earlier scan and this removal, and does so as
    close to the removal as this codebase can get without an atomic
    check-and-delete primitive.

    Never deletes a git branch -- exactly like ``_remove_worktree``, this
    only ever touches the checkout directory itself.

    Returns ``{"removed": bool, "error": str | None}``. Never raises.
    """
    try:
        resolved_entry = entry.resolve()
        resolved_root = scan_root.resolve()
    except Exception as exc:  # noqa: BLE001
        return {"removed": False, "error": f"path_resolution_failed: {exc!r}"[:300]}
    try:
        resolved_entry.relative_to(resolved_root)
    except ValueError:
        return {
            "removed": False,
            "error": "containment_check_failed: entry resolves outside the scanned root",
        }
    if _is_registered_worktree(repo_root, str(entry)):
        return {
            "removed": False,
            "error": "orphan_removal_refused: path is a registered git worktree as of removal time",
        }
    try:
        shutil.rmtree(resolved_entry, onerror=_clear_readonly_and_retry)
    except Exception as exc:  # noqa: BLE001
        return {"removed": False, "error": f"rmtree_failed: {exc!r}"[:300]}
    if resolved_entry.exists():
        return {"removed": False, "error": "rmtree reported success but path still exists"}
    return {"removed": True, "error": None}


def _clear_readonly_and_retry(func, path, excinfo) -> None:
    """``shutil.rmtree`` ``onerror`` hook: clear a Windows read-only bit and
    retry the single failing operation once.

    git marks pack/object files (and sometimes whole directories) read-only
    in a checkout -- on Windows that makes ``PermissionError`` the NORMAL
    outcome of a bare ``shutil.rmtree`` against a worktree, not an edge
    case, since ``rmtree`` does not clear the attribute itself before
    deleting. This is the conventional remedy: ``os.chmod(path,
    stat.S_IWRITE)`` clears ONLY the read-only bit -- it cannot grant access
    to a path that is genuinely permission-denied for another reason (an
    open handle, an ACL deny, ...), so a real permission problem still
    fails the retried ``func(path)`` call below, and that exception
    propagates out of ``rmtree`` for ``_remove_orphan_directory``'s own
    try/except to catch and report honestly in ``report["errors"]`` --
    never silently swallowed here.

    Only ever called by ``shutil.rmtree`` from within
    ``_remove_orphan_directory``, i.e. strictly inside a path that has
    already cleared containment, live registration-absence, and (via the
    caller) the terminal-cursor gate -- this hook changes HOW a file already
    cleared for deletion gets deleted, never WHETHER it does.
    """
    exc = excinfo[1] if excinfo else None
    if not isinstance(exc, PermissionError):
        if exc is not None:
            raise exc
        raise OSError(f"rmtree onerror invoked for {func!r} on {path!r} with no exception info")
    try:
        os.chmod(path, stat.S_IWRITE)
    except Exception:  # noqa: BLE001 -- chmod itself failed; surface the ORIGINAL PermissionError
        raise exc
    func(path)  # retry the single failing operation; re-raises on failure, propagating honestly


def _prune_single_worktree_admin_dir(repo_root: str, worktree_path: str) -> None:
    """Best-effort deregister ONLY the admin directory for ``worktree_path``.

    Deliberately does NOT call ``git worktree prune``: that command is
    repo-wide and takes effect immediately with no default grace period
    (verified empirically on Git 2.55.0.windows.3 in a scratch repo) -- it
    deregisters every missing worktree registration in the repository, not
    just the caller's own. Attended stages can run concurrently; a repo-wide
    prune fired from one stage's per-worktree cleanup could deregister
    another stage's live worktree if its directory happened to read as
    transiently absent (slow filesystem, network mount, mid-write).

    Instead, this walks ``<git-common-dir>/worktrees/*`` directly -- each
    admin directory contains a ``gitdir`` file pointing back at
    ``<worktree_path>/.git`` -- finds the single admin dir whose ``gitdir``
    matches ``worktree_path``, and removes only that directory. Every other
    worktree's registration, live or stale, is left completely untouched:
    there is no repo-wide operation for a race to reach.

    Never raises -- the entire body below is wrapped in its own try/except so
    the guarantee holds on its own merits, not merely because the caller
    (``_remove_worktree``) also happens to swallow failures from here. Any
    exception raised by ``_git``, filesystem iteration, or path/stat
    operations is caught and treated as "could not prune" -- silently
    returning, exactly like every other early-return branch in this function.
    This is advisory cleanup only; a raise here must never propagate up into
    the disk-based removal verdict its caller already decided.
    """
    try:
        common = _git(["rev-parse", "--git-common-dir"], repo_root)
        if common.returncode != 0:
            return
        common_dir = Path((common.stdout or "").strip())
        if not common_dir.is_absolute():
            common_dir = Path(repo_root) / common_dir
        worktrees_dir = common_dir / "worktrees"
        if not worktrees_dir.is_dir():
            return
        target = str(Path(worktree_path)).replace("\\", "/").rstrip("/")
        for admin_dir in worktrees_dir.iterdir():
            if not admin_dir.is_dir():
                continue
            gitdir_file = admin_dir / "gitdir"
            if not gitdir_file.is_file():
                continue
            try:
                pointed = gitdir_file.read_text(encoding="utf-8", errors="replace").strip()
            except Exception:  # noqa: BLE001
                continue
            pointed_norm = pointed.replace("\\", "/").rstrip("/")
            if pointed_norm.endswith("/.git"):
                pointed_norm = pointed_norm[: -len("/.git")]
            if pointed_norm == target:
                shutil.rmtree(admin_dir, ignore_errors=True)
                return
    except Exception:  # noqa: BLE001 -- earn the "Never raises" guarantee for real
        return


# --------------------------------------------------------------------------- #
# Attended-worktree janitor                                                   #
# --------------------------------------------------------------------------- #
# `_finalize` already removes a stage's worktree on every code path it reaches
# (pass, surfaced, aborted). This janitor exists for the case it never
# reaches: a killed session, a crashed host process, or any other exit mid
# await_generate/await_judge that leaves the worktree (and its registered git
# branch) on disk with nothing left to remove it. It is intentionally
# separate from pp's `janitor.ts` (mtime-staleness based) — an attended run
# can legitimately sit paused on HITL for days, and mtime staleness cannot
# tell that apart from a truly abandoned worktree; only the Hydra cursor's
# state can. pp's janitor must skip `attended/*` branches/worktrees entirely
# rather than gain attended-awareness itself (kept out of this module's
# scope; see docs/PORTABILITY.md-adjacent worktree relocation notes).
#
# Safety invariants (both required, neither may be relaxed by a caller):
#   - NEVER remove a worktree whose cursor is non-terminal (state not in
#     `_TERMINAL`), including a worktree with NO discoverable cursor at all —
#     "no cursor found" is treated as "cannot prove terminal", not as "safe
#     to remove". A worktree observed mid-provision (cursor not yet written)
#     must never be swept out from under `begin_stage`.
#   - NEVER delete a git branch. A preserved `attended/<run_id>` branch is the
#     only remaining copy of surfaced/non-landed work; only the linked
#     worktree checkout is removed, exactly like `_remove_worktree` does
#     everywhere else in this module.

def _find_attended_cursor(project_root: str, run_id: str) -> Path | None:
    """Locate the cursor file for ``run_id`` under ``<project_root>/.hydra/``.

    The cursor path is keyed by ``(workflow_id, run_id)`` and the janitor only
    knows ``run_id`` (parsed from the worktree dirname), so this globs across
    every workflow_id directory. Returns None if no match (or more than one
    ambiguous match) is found — ambiguity is treated the same as "no cursor"
    by the caller (fail toward not-removing).
    """
    root = Path(project_root) / ".hydra"
    if not root.is_dir():
        return None
    safe_run = "".join(c for c in str(run_id) if c.isalnum() or c in "-_") or "run"
    matches = sorted(root.glob(f"*/attended/{safe_run}.json"))
    if len(matches) != 1:
        return None
    return matches[0]


def _legacy_worktree_root(repo_root: str) -> Path:
    """Pre-relocation worktree location: ``<repo_root>/.harness/worktrees``.

    WS3 moved WHERE NEW attended worktrees are provisioned (see
    ``resolve_worktree_root``'s docstring) but did not migrate worktrees that
    already existed there — so this is still real, reclaimable residue on
    any repo whose attended runs predate that move. The janitor must scan
    both locations or the whole point of item 2 (reclaiming that residue) is
    unmet.
    """
    return Path(repo_root) / ".harness" / "worktrees"


def _sweep_one_root(
        wt_root: Path, root_label: str, repo_root: str, project_root: str,
        dry_run: bool, report: dict[str, Any], seen: set[str]) -> None:
    """Scan a single worktree root for ``attended-*`` directories and apply
    the sweep decision to each, appending to the shared ``report``. Shared by
    both the current (``resolve_worktree_root``) and legacy
    (``_legacy_worktree_root``) locations so the decision logic — and every
    safety invariant it enforces — is applied identically to both; a legacy
    entry is not a fast path that skips any check the current-root entries
    get. ``seen`` (a set of resolved absolute paths) is threaded across both
    calls so a path reachable from both roots (e.g. via a symlink or an
    env-var override that happens to coincide with the legacy path) is never
    processed twice.
    """
    if not wt_root.is_dir():
        return
    for entry in sorted(wt_root.iterdir()):
        if not entry.is_dir() or not entry.name.startswith("attended-"):
            continue
        try:
            resolved = str(entry.resolve())
        except Exception:  # noqa: BLE001 — unresolvable path still dedupes on its raw str
            resolved = str(entry)
        if resolved in seen:
            continue
        seen.add(resolved)
        run_id = entry.name[len("attended-"):]
        state: str | None = None
        try:
            cursor_file = _find_attended_cursor(project_root, run_id)
            if cursor_file is None:
                report["skipped"].append({
                    "worktree": str(entry), "run_id": run_id,
                    "reason": "no_cursor_found", "root": root_label,
                })
                continue
            cursor = load_cursor(cursor_file)
            state = cursor.get("state")
            if state not in _TERMINAL:
                report["skipped"].append({
                    "worktree": str(entry), "run_id": run_id,
                    "reason": f"non_terminal_state:{state}", "root": root_label,
                })
                continue
            if dry_run:
                report["removed"].append({
                    "worktree": str(entry), "run_id": run_id, "state": state,
                    "root": root_label, "dry_run": True,
                })
                continue
            # A worktree git still tracks goes through `git worktree remove`
            # (the conservative, git-aware path). A directory that has
            # already fallen out of `git worktree list` -- e.g. `_finalize`
            # deregistered it but the checkout directory itself survived --
            # is exactly the residue `git worktree remove` cannot touch
            # (it fails with "is not a working tree"); that case routes to
            # the raw-rmtree orphan path, which re-verifies containment and
            # re-checks registration at removal time. See
            # `_remove_orphan_directory`'s docstring for the safety gates.
            if _is_registered_worktree(repo_root, str(entry)):
                result = _remove_worktree(repo_root, str(entry))
            else:
                result = _remove_orphan_directory(entry, wt_root, repo_root)
            if result.get("removed"):
                report["removed"].append({
                    "worktree": str(entry), "run_id": run_id, "state": state,
                    "root": root_label,
                })
            else:
                # git refused (locked worktree, permission error, ...), the
                # orphan path's own guards refused, or the path is still on
                # disk after the attempt -- report that honestly rather than
                # claiming a clean sweep. See `_remove_worktree`'s and
                # `_remove_orphan_directory`'s docstrings for why the exit
                # code alone is not trusted here. Carries `cursor_state`
                # through even on the error path -- the dry-run preview
                # already proved this cursor was read successfully and
                # terminal, so an operator investigating a failed removal
                # should not have to re-derive that by hand.
                report["errors"].append({
                    "worktree": str(entry), "run_id": run_id,
                    "error": result.get("error") or "removal_failed",
                    "root": root_label, "cursor_state": state,
                })
        except Exception as exc:  # noqa: BLE001 — one bad entry must not abort the sweep
            report["errors"].append({
                "worktree": str(entry), "run_id": run_id, "error": str(exc)[:300],
                "root": root_label, "cursor_state": state,
            })


def sweep_stale_worktrees(
        repo_root: str, project_root: str | None = None,
        dry_run: bool = False) -> dict[str, Any]:
    """Remove attended worktrees whose cursor has reached a terminal state;
    skip (never remove) anything whose cursor is non-terminal or missing.

    Scans BOTH the current worktree root (``resolve_worktree_root(repo_root)``)
    AND the pre-relocation legacy root (``<repo_root>/.harness/worktrees``,
    see ``_legacy_worktree_root``) for ``attended-*`` directories (the same
    naming ``_provision_worktree`` creates), looks up each one's cursor via
    ``_find_attended_cursor``, and removes only the ones proven terminal.
    Never deletes a branch. Never raises — per-entry failures are collected
    in the returned report rather than aborting the sweep. A missing legacy
    directory is the normal post-migration state, not an error -- it is
    silently skipped exactly like a missing current root already was.

    Both roots are scanned with the IDENTICAL decision logic (see
    ``_sweep_one_root``): the cursor-terminality gate, fail-toward-keeping on
    a corrupt/unreadable/missing cursor, never-delete-a-branch, and per-entry
    error reporting all apply the same way to a legacy entry as to a current
    one. Paths seen via both roots are deduplicated so nothing is processed
    twice.

    ``dry_run=True`` (the CLI operator entry point's default) runs the exact
    same terminality decision for every entry but never calls
    ``_remove_worktree`` — a candidate that would be removed is still
    appended to ``report["removed"]``, tagged ``"dry_run": True``, so callers
    get an honest preview without touching disk. ``dry_run=False`` (this
    function's own default, preserved for existing callers) behaves exactly
    as before.

    Returns ``{"removed": [...], "skipped": [...], "errors": [...]}`` where
    each entry is ``{"worktree": path, "run_id": ..., "root": "current" |
    "legacy", "reason"/"state"/"error": ...}`` — ``root`` lets an operator
    tell current-location entries apart from reclaimed legacy residue.
    """
    report: dict[str, Any] = {"removed": [], "skipped": [], "errors": []}
    root = project_root or repo_root
    seen: set[str] = set()
    _sweep_one_root(resolve_worktree_root(repo_root), "current", repo_root, root,
                    dry_run, report, seen)
    _sweep_one_root(_legacy_worktree_root(repo_root), "legacy", repo_root, root,
                    dry_run, report, seen)
    return report


# --------------------------------------------------------------------------- #
# Stage-active sentinel (Marker 2 for the PreToolUse write-enforcement hooks) #
# --------------------------------------------------------------------------- #
# The hooks (hydra-block-direct-write.ps1 / hydra-block-bash-writes.ps1 /
# hydra-block-direct-pp.ps1) only honor a bare HYDRA_PP_STAGE_ACTIVE=1 when
# this sentinel file exists — the old fallback of enumerating any attended-*
# worktree directory ("Marker 1") was retired because stale worktrees
# accumulate and make that check permanently true. This module is the ONLY
# writer/clearer, so the sentinel's presence is a true run-scoped signal: it
# exists exactly while begin_stage..._finalize/abort_stage spans one attended
# stage, on the same project the hooks check.

def _stage_active_sentinel_path(project_root: str | Path) -> Path:
    return Path(project_root) / ".harness" / "stage-active"


def _write_stage_active_sentinel(project_root: str | Path) -> None:
    """Write the sentinel at stage start. Fail-soft: any I/O error is
    swallowed so a write hiccup never blocks the attended stage — worst case
    the hooks fall back to full enforcement (fail-closed, not fail-open)."""
    try:
        sentinel = _stage_active_sentinel_path(project_root)
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        sentinel.write_text("1", encoding="utf-8")
    except Exception:  # noqa: BLE001 — fail-soft
        pass


def _clear_stage_active_sentinel(project_root: str | Path) -> None:
    """Remove the sentinel at finalize/abort so a later, unrelated session
    doesn't inherit a stale bypass. Fail-soft."""
    try:
        sentinel = _stage_active_sentinel_path(project_root)
        if sentinel.exists():
            sentinel.unlink()
    except Exception:  # noqa: BLE001 — fail-soft
        pass


# Hydra#71 follow-up: the first cut copied entire ``build/`` and ``.harness/``
# TREES via unbounded ``**/`` globs. On a C++ repo (the case that surfaced
# #71) ``build/`` is a multi-gigabyte output tree, not evidence -- copying it
# whole on every non-complete finalize would silently balloon disk usage
# without limit. This policy copies only small, text-shaped report files
# (never binaries/object files/build trees) and enforces hard size caps.
_EVIDENCE_ALLOWED_EXTS: frozenset[str] = frozenset({".log", ".txt", ".json", ".xml"})
# Directory names never descended into, anywhere in the tree: vendor/build
# caches that can each independently be enormous and carry no run evidence.
_EVIDENCE_EXCLUDED_DIR_NAMES: frozenset[str] = frozenset({
    ".git", "node_modules", ".venv", "venv", "__pycache__",
})
_EVIDENCE_PER_FILE_MAX_BYTES = 20 * 1024 * 1024  # 20 MB


def _evidence_max_total_bytes() -> int:
    """Total evidence-copy budget per preserved run, ``HYDRA_EVIDENCE_MAX_BYTES``
    (default 200 MB). Any non-integer/negative override falls back to the
    default rather than disabling the cap."""
    raw = os.environ.get("HYDRA_EVIDENCE_MAX_BYTES")
    if raw is None:
        return 200 * 1024 * 1024
    try:
        val = int(raw)
    except (TypeError, ValueError):
        return 200 * 1024 * 1024
    return val if val >= 0 else 200 * 1024 * 1024


def _evidence_within_build_logs_or_testing(dir_parts: Sequence[str]) -> bool:
    """True when ``dir_parts`` (the directory components of a candidate
    file's path, relative to the worktree root, lower-cased) descend from a
    ``build`` directory into a ``logs`` or ``Testing`` (ctest) subdirectory --
    the only evidence this policy takes FROM a build tree. A file directly
    under ``build/`` (or under any other build subdirectory) never qualifies:
    that is build output, not a log/report."""
    try:
        build_idx = dir_parts.index("build")
    except ValueError:
        return False
    remainder = dir_parts[build_idx + 1:]
    return "logs" in remainder or "testing" in remainder


def _evidence_candidate_files(src: Path) -> list[Path]:
    """Walk ``src`` and return every file eligible for evidence preservation.

    Eligible:
      - anything under ``.harness/`` (small run metadata Hydra itself writes,
        regardless of extension), and
      - ``.log``/``.txt``/``.json``/``.xml`` report files that are either
        OUTSIDE any ``build/`` tree, or inside a ``build/**/logs`` or
        ``build/**/Testing`` (ctest) subtree.

    Never eligible: anything else under ``build/`` (binaries, object files,
    build-system caches) and anything under an excluded vendor/cache
    directory (``_EVIDENCE_EXCLUDED_DIR_NAMES``) or a NESTED git checkout
    (a directory containing its own ``.git`` — a separate worktree/repo, not
    this run's evidence) at any depth.
    """
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(src):
        d = Path(dirpath)
        for name in list(dirnames):
            if name in _EVIDENCE_EXCLUDED_DIR_NAMES:
                dirnames.remove(name)
                continue
            child = d / name
            if child != src and (child / ".git").exists():
                dirnames.remove(name)
        for fname in filenames:
            f = d / fname
            try:
                rel_parts = f.relative_to(src).parts
            except ValueError:  # pragma: no cover — defensive, path is under src
                continue
            dir_parts_lower = [p.lower() for p in rel_parts[:-1]]
            if dir_parts_lower and dir_parts_lower[0] == ".harness":
                out.append(f)
                continue
            if f.suffix.lower() not in _EVIDENCE_ALLOWED_EXTS:
                continue
            if "build" in dir_parts_lower and not _evidence_within_build_logs_or_testing(
                    dir_parts_lower):
                continue
            out.append(f)
    return out


def _preserve_worktree_evidence(cursor: dict[str, Any], worktree_path: str,
                                run_id: str) -> str | None:
    """Copy bounded, text-shaped build/log/``.harness`` evidence out of
    ``worktree_path`` before ``_remove_worktree`` deletes it on a
    non-complete outcome.

    Hydra#71: ``_BYPRODUCT_PATTERNS`` deliberately excludes ``.harness/`` and
    ``*.log`` from the worktree-local git excludes so
    ``_preserve_non_complete_work``'s ``git add -A`` + commit never picks them
    up -- they are build/log byproducts, not source. But that also means a
    genuine generate-failure/judge-fail/smoke-fail worktree removal silently
    destroyed them, taking the only on-disk record of what actually happened
    with it.

    Bounded (Hydra#71 follow-up): only report-shaped files
    (``_evidence_candidate_files`` — never build binaries/object files/build
    trees, never vendor/build-cache dirs, never a nested checkout) are
    candidates; a per-file cap (``_EVIDENCE_PER_FILE_MAX_BYTES``, 20 MB) and a
    total-per-run cap (``HYDRA_EVIDENCE_MAX_BYTES``, default 200 MB) bound the
    copy. Any candidate declined by either cap is recorded — never silently
    dropped — in ``manifest.json`` alongside the copy, with its path, size,
    and the reason it was skipped.

    Retention: this function only COPIES (the worktree removal below is what
    reclaims the source disk); nothing currently deletes OLD entries under
    ``.hydra-preserved-evidence/`` itself -- it is intentionally excluded from
    ``sweep_stale_worktrees`` (``_sweep_one_root`` only matches ``attended-*``
    worktree directory names), since a preserved evidence bundle is exactly
    the kind of record an operator investigating a surfaced run must be able
    to find AFTER the worktree itself is gone, so it must never be swept on
    the same terminal-cursor signal that reclaims worktrees. Until an
    explicit operator-facing retention command exists, `.hydra-preserved-
    evidence/` is bounded per-run by the caps above but grows without bound
    ACROSS runs; an operator (or a future age-based janitor pass explicitly
    scoped to this directory, not `sweep_stale_worktrees`) should periodically
    reclaim it by run age.

    Fail-soft: never raises: any error, or nothing found to preserve, returns
    ``None`` without touching ``cursor`` or blocking finalize. On success sets
    ``cursor["preserved_evidence_path"]`` and returns it.
    """
    try:
        src = Path(worktree_path)
        if not src.is_dir():
            return None
        candidates = _evidence_candidate_files(src)
        if not candidates:
            return None
        dest_root = (Path(worktree_path).parent
                    / ".hydra-preserved-evidence" / str(run_id))
        max_total = _evidence_max_total_bytes()
        per_file_max = _EVIDENCE_PER_FILE_MAX_BYTES
        copied_total = 0
        copied_manifest: list[dict[str, Any]] = []
        skipped_manifest: list[dict[str, Any]] = []
        for f in sorted(candidates):
            rel = f.relative_to(src)
            try:
                size = f.stat().st_size
            except OSError as exc:  # noqa: BLE001 — record and move on
                skipped_manifest.append({
                    "path": str(rel), "size": None,
                    "reason": f"stat_failed: {exc!r}"[:200],
                })
                continue
            if size > per_file_max:
                skipped_manifest.append({
                    "path": str(rel), "size": size,
                    "reason": f"exceeds_per_file_cap_bytes={per_file_max}",
                })
                continue
            if copied_total + size > max_total:
                skipped_manifest.append({
                    "path": str(rel), "size": size,
                    "reason": f"exceeds_total_cap_bytes={max_total}",
                })
                continue
            dest = dest_root / rel
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, dest)
            except Exception as exc:  # noqa: BLE001 — best-effort per-item
                skipped_manifest.append({
                    "path": str(rel), "size": size,
                    "reason": f"copy_failed: {exc!r}"[:200],
                })
                continue
            copied_total += size
            copied_manifest.append({"path": str(rel), "size": size})
        if not copied_manifest and not skipped_manifest:
            return None
        dest_root.mkdir(parents=True, exist_ok=True)
        manifest = {
            "run_id": run_id,
            "source_worktree": str(src),
            "max_per_file_bytes": per_file_max,
            "max_total_bytes": max_total,
            "total_bytes_copied": copied_total,
            "copied": copied_manifest,
            "skipped": skipped_manifest,
        }
        try:
            (dest_root / "manifest.json").write_text(
                dumps_strict(manifest, label="preserved-evidence manifest",
                            indent=2, default=str),
                encoding="utf-8",
            )
        except Exception as exc:  # noqa: BLE001 — the copies themselves still stand
            _trace(cursor, "attended.evidence_manifest_write_failed",
                   {"run_id": run_id, "error": str(exc)[:200]})
        cursor["preserved_evidence_path"] = str(dest_root)
        _trace(cursor, "attended.evidence_preserved", {
            "run_id": run_id, "path": str(dest_root),
            "copied_count": len(copied_manifest),
            "skipped_count": len(skipped_manifest),
            "total_bytes_copied": copied_total,
        })
        return str(dest_root)
    except Exception as exc:  # noqa: BLE001 — never block finalize
        _trace(cursor, "attended.evidence_preserve_failed",
               {"run_id": run_id, "error": str(exc)[:200]})
        return None


def _preserve_non_complete_work(cursor: dict[str, Any], worktree_path: str,
                                branch: str, run_id: str,
                                final_status: str = "surfaced") -> None:
    """Commit any uncommitted engineer changes to the attended branch before the
    worktree is removed on a non-complete outcome (MU12).

    Fail-soft: any exception or nonzero git exit emits ``attended.preserve_failed``
    and returns without changing the finalize outcome or cursor state machine.
    Skips silently when the worktree has no changes (nothing to preserve).

    ``final_status`` is interpolated into the commit message so the branch log
    shows the actual outcome (e.g. "surfaced") rather than a hardcoded string.

    On success sets ``cursor["preserved_branch"]`` so the caller and the step
    result exposed to the operator carry the branch name for pickup.
    """
    try:
        _write_worktree_gitexcludes(worktree_path)
        st = _git(["status", "--porcelain"], worktree_path)
        if not (st.stdout or "").strip():
            return  # nothing to preserve — skip silently
        _git(["add", "-A"], worktree_path)
        res = _git(
            ["-c", "user.name=hydra-attended",
             "-c", "user.email=hydra-attended@local",
             "commit",
             "-m", f"attended engineering ({final_status}): {run_id} — preserved for operator pickup",
             "--no-verify"],
            worktree_path,
        )
        if res.returncode != 0:
            raise RuntimeError((res.stderr or res.stdout or "").strip()[:200])
        cursor["preserved_branch"] = branch
        _trace(cursor, "attended.preserved", {"branch": branch, "run_id": run_id})
    except Exception as exc:  # noqa: BLE001
        _trace(cursor, "attended.preserve_failed",
               {"run_id": run_id, "error": str(exc)[:200]})


# --------------------------------------------------------------------------- #
# Cursor persistence                                                          #
# --------------------------------------------------------------------------- #

def cursor_path(project_root: str | Path, workflow_id: str, run_id: str) -> Path:
    """Sidecar path for an attended stage's cursor.

    Lives under ``<project>/.hydra/<workflow_id>/attended/<run_id>.json`` — the
    ``.hydra`` tree is already the per-workflow scratch/trace area.
    """
    safe_run = "".join(c for c in str(run_id) if c.isalnum() or c in "-_") or "run"
    return (Path(project_root) / ".hydra" / str(workflow_id)
            / "attended" / f"{safe_run}.json")


def load_cursor(path: str | Path) -> dict[str, Any]:
    """Load a cursor; raise FileNotFoundError if absent, ValueError if stale."""
    p = Path(path)
    data = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema") != CURSOR_SCHEMA:
        raise ValueError(f"attended cursor schema mismatch at {p} "
                         f"(want {CURSOR_SCHEMA}, got {data.get('schema')!r})")
    return data


def save_cursor(path: str | Path, cursor: dict[str, Any]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    # Atomic-ish write: temp + replace so a crash mid-write never leaves a
    # truncated cursor that would wedge the workflow.
    tmp = p.with_suffix(p.suffix + ".tmp")
    # Strict: the engine reads this back via `load_cursor` to drive the
    # attended stage state machine (budget/cost fields, verdict scores,
    # retry counts). A non-finite value silently swapped to `null` here
    # is not a display defect -- it is a stage-machine decision made on a
    # wrong value (the same "budget comparison fails open on NaN" class of
    # bug `strict_json.reject_non_finite` guards at the CLI boundary).
    # Refuse rather than persist a poisoned cursor.
    tmp.write_text(
        dumps_strict(cursor, label=f"attended cursor at {p}", indent=2, default=str),
        encoding="utf-8",
    )
    os.replace(tmp, p)


# --------------------------------------------------------------------------- #
# Rider (a) — smoke baseline helpers                                         #
# --------------------------------------------------------------------------- #

def _parse_failing_tests(output: str) -> set[str]:
    """Parse failing test IDs from pytest --tb=no -q output.

    Matches lines like: ``FAILED tests/test_foo.py::test_bar - reason``
    Returns a set of bare test IDs (no trailing reason).
    """
    failing: set[str] = set()
    for line in output.splitlines():
        s = line.strip()
        if s.startswith("FAILED "):
            # Strip "FAILED " prefix and any trailing " - reason" suffix.
            test_id = s[7:].split(" - ")[0].strip()
            if test_id:
                failing.add(test_id)
    return failing


def _capture_baseline_failures(
        project_path: str, repo_root: str | None = None) -> list[str]:
    """Run pytest before engineer changes; return sorted list of failing test IDs.

    Called at begin_stage time (fresh worktree = no engineer changes) so
    environment-specific failures (path-sensitive tests that always fail
    inside a worktree) are captured as the baseline.  A later smoke-fail
    is treated as clean if current failures ⊆ baseline.

    GAP-a2 (Fix 3): tries repo_root first when provided, since an attended
    worktree (resolved via ``resolve_worktree_root()``, outside repo_root)
    does NOT have a tests/ directory of its own — the tests live in the repo
    root.  Without repo_root, falls
    back to project_path then project_path.parent (less reliable for worktrees,
    which may be several levels deep under the repo root).
    Fail-soft: any exception returns an empty list (no baseline → smoke
    failures are NOT excused, which is the safe default).
    """
    import json as _json
    import sys as _sys
    # Build candidate list: prefer repo_root > project_path > parent
    candidates: list[str] = []
    if repo_root and repo_root != project_path:
        candidates.append(repo_root)
    candidates.append(project_path)
    if not repo_root:
        # Legacy fallback: try parent (unreliable for deep worktrees but better
        # than nothing when repo_root is unknown).
        parent = str(Path(project_path).parent)
        if parent and parent != project_path:
            candidates.append(parent)

    # MU17: the baseline only depends on the tree at branch point (HEAD of the
    # anchor repo), and the full-suite run costs minutes — enough to blow the
    # attended step budget on large repos. Cache completed baselines per
    # (anchor, HEAD sha) under <anchor>/.harness/baseline/<sha>.json so only
    # the first stage after a new commit pays the suite cost.
    # Completed baselines are cached as <sha>.json (a JSON list of failing tests).
    # Timeouts are now cached as a degraded marker: <sha>.timeout.json containing
    # {"timeout_s": <value>}.  On entry, if the marker exists for the current HEAD
    # sha, the function returns [] immediately without re-running the suite — raise
    # HYDRA_BASELINE_TIMEOUT_S to give the suite more budget.  This prevents
    # re-paying an already-too-slow suite on every stage (which would double the
    # damage and blow the step budget) while keeping the safe default: no baseline
    # → smoke failures are not excused.  Do NOT try the next candidate on timeout —
    # re-running is always just as slow.
    _cache_anchor = candidates[0] if candidates else project_path
    _cache_file: Path | None = None
    _timeout_marker: Path | None = None
    try:
        _sha = _git(["rev-parse", "HEAD"], _cache_anchor).stdout.strip()
        if _sha:
            _cache_file = (Path(_cache_anchor) / ".harness" / "baseline"
                           / f"{_sha}.json")
            _timeout_marker = (Path(_cache_anchor) / ".harness" / "baseline"
                               / f"{_sha}.timeout.json")
            if _cache_file.is_file():
                cached = _json.loads(_cache_file.read_text(encoding="utf-8"))
                if isinstance(cached, list):
                    return sorted(str(t) for t in cached)
            # If a timeout marker exists the suite already exceeded its budget at
            # this commit.  Skip silently; operator can delete the marker or raise
            # HYDRA_BASELINE_TIMEOUT_S.
            if _timeout_marker.is_file():
                import logging as _logging
                _logging.getLogger(__name__).info(
                    "baseline timeout marker found for sha=%s — skipping suite "
                    "(baseline degraded; raise HYDRA_BASELINE_TIMEOUT_S to retry)",
                    _sha,
                )
                return []
    except Exception:  # noqa: BLE001 — cache read is best-effort
        _cache_file = None
        _timeout_marker = None

    for cwd in candidates:
        tests_dir = Path(cwd) / "tests"
        if not tests_dir.is_dir():
            continue
        try:
            res = run_text(
                [
                    _sys.executable, "-m", "pytest",
                    "tests/", "--no-header", "-q", "--tb=no",
                ],
                cwd=cwd,
                capture_output=True,
                check=False,
                timeout=_baseline_timeout_s(),
            )
            failing = sorted(_parse_failing_tests(res.stdout + "\n" + res.stderr))
            # Cache the completed result (empty list is a valid baseline) so
            # subsequent stages at the same HEAD skip the suite entirely.
            if _cache_file is not None:
                try:
                    _cache_file.parent.mkdir(parents=True, exist_ok=True)
                    # Strict: read back by `_json.loads` above and used to
                    # skip re-running the whole baseline suite; `failing` is
                    # a list of test-id strings so this can never actually
                    # trip, but guarding it keeps the enforcement test from
                    # needing an allow-list entry for a genuinely read-back
                    # value.
                    _cache_file.write_text(
                        dumps_strict(failing, label=f"baseline cache for {_sha}"),
                        encoding="utf-8",
                    )
                except Exception:  # noqa: BLE001 — cache write is best-effort
                    pass
            # Return the first successful (or empty) result — empty is valid
            # (all tests pass in this env = no baseline needed).
            return failing
        except subprocess.TimeoutExpired:
            # MU17 degraded-marker: write <sha>.timeout.json so subsequent calls
            # at the same HEAD return [] immediately without re-running the suite.
            # No baseline → smoke failures are not excused, the safe default.
            # Do NOT try the next candidate — the suite is too slow regardless of
            # which cwd we use.
            if _timeout_marker is not None:
                try:
                    _timeout_marker.parent.mkdir(parents=True, exist_ok=True)
                    _timeout_marker.write_text(
                        _json.dumps({"timeout_s": _baseline_timeout_s()}),
                        encoding="utf-8",
                    )
                except Exception:  # noqa: BLE001 — marker write is best-effort
                    pass
            return []
        except Exception:  # noqa: BLE001 — baseline failure is non-fatal
            continue
    return []


# GAP-h: heuristic — a regex for file-path-like tokens in critique text.
_PATH_TOKEN_RE = _re.compile(
    r'[A-Za-z0-9_][A-Za-z0-9_./-]*\.[a-zA-Z]{1,10}'
)


def _has_real_file_ref(critique_md: str, work_path: str) -> bool:
    """Return True if critique_md contains at least one path-like token that
    resolves to an existing file under work_path.  Heuristic — warn-only."""
    for match in _PATH_TOKEN_RE.finditer(critique_md):
        token = match.group().replace("\\", "/").lstrip("/")
        try:
            if (Path(work_path) / token).exists():
                return True
        except Exception:  # noqa: BLE001
            pass
    return False


# --------------------------------------------------------------------------- #
# Step results                                                                #
# --------------------------------------------------------------------------- #

def _host_action(cursor: dict[str, Any]) -> dict[str, Any] | None:
    return cursor.get("pending_action")


def _step_result(cursor: dict[str, Any], cursor_file: str | Path) -> dict[str, Any]:
    """Project a cursor into the result dict the CLI/MCP layer returns to the host."""
    state = cursor.get("state")
    if state in _TERMINAL:
        status = state
    else:
        status = "awaiting_host"
    res: dict[str, Any] = {
        "status": status,
        "workflow_id": cursor.get("workflow_id"),
        "run_id": cursor.get("run_id"),
        "stage_id": cursor.get("stage_id"),
        "task_id": cursor.get("task_id"),
        "squad_slug": cursor.get("squad_slug"),
        "state": state,
        "cursor_path": str(cursor_file),
        "cost_usd": float(cursor.get("cost_usd") or 0.0),
        "tokens_in": int(cursor.get("tokens_in") or 0),
        "tokens_out": int(cursor.get("tokens_out") or 0),
        # B8: cost provenance for the stage as a whole — the CLI's
        # charge_and_gate call reads "cost_source" to tag the ledger entry so
        # an unreporting host counts as estimated/unmeasured, never free.
        # Defaults to "measured" (pre-existing behaviour) when the stage never
        # ran any accrual (e.g. still awaiting the host).
        "cost_source": cursor.get("cost_source") or "measured",
        "unmeasured_count": int(cursor.get("unmeasured_count") or 0),
        # Fix (mixed-provenance estimated_usd): the per-component estimated
        # dollars accrued across this stage's calls, so a charging site can
        # credit budget.estimated_usd with only this figure instead of
        # inferring it from the collapsed "cost_source" label.
        "estimated_cost_usd": float(cursor.get("estimated_cost_usd") or 0.0),
    }
    if status == "awaiting_host":
        res["host_action"] = _host_action(cursor)
        if state == "stalled_infra":
            # W2-3: surface the hold + reason on the non-terminal path too, not
            # just on a terminal outcome — an operator/recovery caller needs
            # to see this without the stage having been finalized.
            res["stalled_infra"] = True
            if cursor.get("error"):
                res["error"] = cursor["error"]
    if state in _TERMINAL:
        res["final_status"] = cursor.get("final_status") or state
        res["stage_outcome"] = cursor.get("outcome")
        res["smoke_status"] = cursor.get("smoke_status")
        res["changed_paths"] = cursor.get("changed_paths") or []
        if cursor.get("merge") is not None:
            res["merge"] = cursor["merge"]
        if cursor.get("error"):
            res["error"] = cursor["error"]
        # MU12: expose preserved_branch so the operator knows where to find
        # the work when the stage surfaces without completing.
        if cursor.get("preserved_branch"):
            res["preserved_branch"] = cursor["preserved_branch"]
        # Hydra#71: untracked build/log/.harness evidence excluded from the
        # branch commit (see ``_BYPRODUCT_PATTERNS``) but copied out before
        # ``_remove_worktree`` deletes the checkout on a non-complete outcome.
        if cursor.get("preserved_evidence_path"):
            res["preserved_evidence_path"] = cursor["preserved_evidence_path"]
        # Rider (b): expose charged flag so _cmd_attended_submit can skip
        # duplicate budget charges on a retried submit-host-result call.
        res["already_charged"] = bool(cursor.get("charged", False))
        # Hydra#69 round 6 defect 2: the call_key that actually produced this
        # cursor's terminal transition, persisted on the cursor itself (see
        # `submit_host_result`'s post-transition stamp below). A caller
        # derives its checkpoint-reconciliation/charge identity from THIS
        # value, never from its own possibly-stale/different args.call_key.
        # Absent (None) on a legacy cursor written before this field existed,
        # or one terminated outside `submit_host_result` (operator abort,
        # stalled-stage recovery) -- callers fold that into a fixed "legacy"
        # identity so every future retry, regardless of which call_key it
        # carries, converges on the SAME reconciliation/charge identity
        # instead of growing a new one per call_key.
        res["terminal_call_key"] = cursor.get("terminal_call_key")
        if cursor.get("emitted_envelopes"):
            res["emitted_envelopes"] = cursor["emitted_envelopes"]
            res["emitted_envelope_count"] = len(cursor["emitted_envelopes"])
        # E2-34: an emitted envelope that failed schema validation is
        # outstanding work, not a completed stage. Report it on every read of
        # a terminal cursor and override the reported status so the host
        # cannot mistake the run for fully complete.
        if cursor.get("rejected_envelopes"):
            res["rejected_envelopes"] = cursor["rejected_envelopes"]
            res["status"] = "envelopes_rejected"
        if cursor.get("artifact_text"):
            res["artifact_text"] = cursor["artifact_text"]
        # E2-35: surface where (or whether) the squad artifact landed so the
        # CLI does not re-attempt a native persist over a ref that already
        # exists, and so an operator sees an unpersisted result as such.
        for key in ("artifact_ref", "artifact_persisted_via",
                    "artifact_persist_error", "artifact_persist_warning"):
            if cursor.get(key) is not None:
                res[key] = cursor[key]
    return res


def step_result(cursor: dict[str, Any], cursor_file: str | Path) -> dict[str, Any]:
    """Public wrapper over ``_step_result`` (Hydra#70): ``hydra_core.cli``'s
    ``step`` command needs to project an ALREADY-LOADED cursor (one it polled
    for an in-flight ``await_smoke`` job) without re-deriving the module's
    private helper name."""
    return _step_result(cursor, cursor_file)


def _trace(cursor: dict[str, Any], kind: str, payload: dict[str, Any]) -> None:
    wf = cursor.get("workflow_id")
    if not wf:
        return
    try:
        _telemetry.emit(Path(cursor["project_path"]), wf, kind,
                        {"run_id": cursor.get("run_id"), **payload})
    except Exception:  # noqa: BLE001 — never crash the driver on a trace write
        pass


# B8: priority order used to resolve one stage's overall cost provenance
# across its several accrual calls (generate, judge, squad-result) —
# "estimated" wins over "measured" wins over "unmeasured", because a single
# estimated call anywhere in the stage means the stage's total cost_usd is no
# longer purely measured money, and a single measured call means the stage is
# not wholly unmeasured.
_SOURCE_RANK = {"unmeasured": 0, "measured": 1, "estimated": 2}


def _merge_cost_source(cursor: dict[str, Any], new_source: str) -> None:
    current = cursor.get("cost_source")
    if current is None or _SOURCE_RANK[new_source] > _SOURCE_RANK[current]:
        cursor["cost_source"] = new_source


def _priced_cost(
    cursor: dict[str, Any],
    result: dict[str, Any],
    *,
    label: str,
    model_hint: str | None = None,
) -> tuple[float, str]:
    """B8: resolve a host result's dollar cost + provenance ``source``.

    Returns ``(cost_usd, source)`` where ``source`` is one of
    ``"measured"`` (the host reported ``cost_usd`` directly — the pre-existing
    behaviour, unchanged), ``"estimated"`` (the host reported no cost but DID
    report ``tokens_in``/``tokens_out`` and a ``model``, so
    ``hydra_core.pricing.price_call`` prices it), or ``"unmeasured"`` (neither
    a cost nor usable tokens+model were reported — this call contributes
    $0.0 and a trace event is emitted; it never blocks the stage).

    Also folds the resolved source into ``cursor["cost_source"]`` — priority
    estimated > measured > unmeasured across the whole stage's accrual calls
    (generate + judge + squad-result) — and bumps ``cursor["unmeasured_count"]``
    on an unmeasured call so a caller charging the STAGE's total cost once
    (``_cmd_attended_submit`` / recover-stalled-stage) can pick the right
    ``source`` for ``charge_and_gate``.

    Fix (mixed-provenance ``estimated_usd``): a stage can mix a measured call
    (e.g. generate) with an estimated call (e.g. judge). ``cost_source``
    necessarily collapses to a single winning label for the whole stage
    (see ``_merge_cost_source``), so it cannot tell the charging site how much
    of the stage's *total* was actually estimated money. This function
    separately accumulates ``cursor["estimated_cost_usd"]`` by ONLY the
    dollar amount resolved on this call's estimated branch, so a caller can
    credit ``budget.estimated_usd`` with just that figure instead of the
    whole (mixed) stage total.

    Cross-vendor judge finding (follow-up round, HIGH): this is the
    ATTENDED path's exact counterpart of the headless drive loop's vendor
    cost exposure -- ``result`` is an untrusted HOST result (the same JSON
    file ``cli.py``'s ``_cmd_attended_submit`` reads), and this line
    ``return float(reported), "measured"`` had no finiteness check at all,
    let alone one applied AFTER coercion: a host reporting ``cost_usd:
    "NaN"`` (a string -- ordinary, valid JSON, just a hostile value) was
    cast to a real non-finite float and forcibly labeled ``"measured"``,
    poisoning ``cursor["cost_usd"]`` (and, via `_cmd_attended_submit`'s
    later `charge_and_gate`, `state.budget.spent_usd`) permanently. Routed
    through ``coerce_untrusted_cost`` -- coerce first, check finiteness on
    the coerced value -- exactly as the headless vendor paths now do. A
    reported-but-rejected cost falls through to the same token-based
    estimate (or ``"unmeasured"``) branch a MISSING cost already used, so a
    still-priceable call is not needlessly downgraded to $0.
    """
    reported = result.get("cost_usd")
    if reported is not None:
        cost, source = coerce_untrusted_cost(reported)
        if source == "measured":
            _merge_cost_source(cursor, "measured")
            return cost, "measured"
        # Reported but rejected (non-finite after coercion, or unparseable)
        # -- fall through to the token-based estimate below exactly as a
        # MISSING cost field already does; never trust the raw value.

    tokens_in = coerce_untrusted_count(result.get("tokens_in"))
    tokens_out = coerce_untrusted_count(result.get("tokens_out"))
    model = str(result.get("model") or model_hint or cursor.get("model_tier") or "")
    if (tokens_in or tokens_out) and model:
        from .pricing import price_call
        priced = price_call(model, tokens_in, tokens_out)
        # Cross-vendor judge finding (follow-up round, HIGH -- rule fix):
        # `price_call` now enforces the ONE seam behind "measured only on
        # positive evidence" for estimates -- `None` means pricing did not
        # happen (unknown model, broken/negative rate, non-finite total);
        # any OTHER return is a genuinely-priced, trustworthy number
        # (including a real `0.0`). This `is not None` check is therefore
        # already correct and needs no per-call re-derivation here -- it
        # was only ever wrong when `price_call` itself could floor a
        # broken input into a plausible-looking `0.0` (fixed at the source).
        if priced is not None:
            _merge_cost_source(cursor, "estimated")
            cursor["estimated_cost_usd"] = (
                float(cursor.get("estimated_cost_usd") or 0.0) + priced
            )
            return priced, "estimated"

    _merge_cost_source(cursor, "unmeasured")
    cursor["unmeasured_count"] = int(cursor.get("unmeasured_count") or 0) + 1
    _trace(cursor, "attended.cost_unmeasured", {
        "stage_id": cursor.get("stage_id"), "call": label, "model": model or None,
    })
    return 0.0, "unmeasured"


# --------------------------------------------------------------------------- #
# Driver                                                                       #
# --------------------------------------------------------------------------- #

def begin_stage(
    dispatcher: Dispatcher,
    *,
    workflow_id: str,
    run_id: str,
    project_path: str,
    request_text: str,
    model_tier: str | None = None,
    judge_rubric_id: str | None = None,
    project_root: str | Path | None = None,
    task_id: str | None = None,
    isolate: bool = True,
    hydra_context_block: str | None = None,
    # B9: the real pp gate_type for this stage's triggering envelope (see
    # squad_node._gate_type_for_envelope / _ENVELOPE_TYPE_TO_KIND). The
    # caller passes the actual envelope type (PRD/ARCH_RFC/DEV_TASK/HANDOFF)
    # it resolved for this task, or None when no better signal exists — this
    # falls through to the documented code_style DEFAULT via `_pp_gate_type`.
    gate_type: str | None = None,
) -> dict[str, Any]:
    """Open an attended code stage and pause for the first host action (the
    ``engineer`` generation). ``run_id`` must already exist (the caller runs
    ``start_run`` / has a scaffolded run). Returns an ``awaiting_host`` step
    result whose ``host_action`` tells the host to spawn the visible
    ``engineer`` subagent.

    ``isolate`` (default True): provision a linked git worktree under
    ``.harness/worktrees/`` for the engineer to write into (write-safe under the
    ``hydra-block-direct-write`` hook), merged back on a passing finalize. Falls
    back to in-place writes when the target isn't a git repo.
    """
    # Browser isolation parity with the headless driver (PP-BV-ISO).
    os.environ.setdefault("PP_BROWSER_ENGINE", "playwright")
    # B9: resolve the real gate_type ONCE here and persist it on the cursor
    # (below) so the later submit-host-result step reads the SAME value
    # instead of re-deriving (and potentially drifting from) a second
    # hardcoded literal. E2-25: still defaults to the code rubric (NOT the
    # spec rubric `rfc-2119-normative`, which pp's gates.ts maps only to
    # gate_type="spec") when the caller carries no better signal — it used to
    # default to that spec rubric unconditionally, and whose body no registry
    # served for the unversioned id, so every code stage was silently judged
    # on a generic one-liner while the ledger recorded the spec rubric's name.
    gate_type = _pp_gate_type("code", gate_type)
    judge_rubric_id = judge_rubric_id or _default_rubric_id(gate_type)
    cm = dispatcher.call_mcp

    st = _raise_on_error_payload(
        cm("pp_harness", "start_stage",
           {"run_id": run_id, "kind": "code", "gate_type": "code"},
           squad_id=_SQ),
        "start_stage",
    )
    stage_id = _pp_inner(st).get("stage_id")
    if not stage_id:
        raise RuntimeError(f"start_stage returned no stage_id: {st!r}")

    # Write-safety: isolate the engineer into a hook-allowed worktree.
    work_path = project_path
    worktree_path: str | None = None
    branch: str | None = None
    repo_root: str | None = None
    if isolate:
        repo_root = _git_repo_root(project_path)
        if repo_root:
            prov = _provision_worktree(repo_root, run_id)
            if prov is not None:
                worktree_path, branch = prov
                work_path = worktree_path

    base_prompt = _build_engineer_prompt(request_text, work_path)
    # 7b: prepend hydra_context_block (workflow/envelope metadata from start_run)
    # so the engineer subagent carries full Hydra routing context. Empty/None →
    # identical to previous behavior.
    if hydra_context_block:
        base_prompt = f"{hydra_context_block}\n\n{base_prompt}"
    # Rider (a): capture baseline failures before the engineer touches anything.
    # The worktree is a linked git worktree (same commits as the repo root); any
    # failures here are environment-specific rather than regressions introduced
    # by the engineer's changes.  Pass repo_root so pytest runs from the right
    # directory (worktrees don't carry their own tests/ dir).
    baseline_failures = _capture_baseline_failures(work_path, repo_root=repo_root)
    root = project_root or project_path
    cursor: dict[str, Any] = {
        "schema": CURSOR_SCHEMA,
        "workflow_id": workflow_id,
        "run_id": run_id,
        "stage_id": stage_id,
        "task_id": task_id,
        "project_path": project_path,
        "project_root": str(root),
        "work_path": work_path,
        "worktree_path": worktree_path,
        "branch": branch,
        "repo_root": repo_root,
        "request_text": request_text,
        # Persisted so the Reflexion retry prompt can prepend it (7b fix).
        "hydra_context_block": hydra_context_block or "",
        "model_tier": model_tier,
        "judge_rubric_id": judge_rubric_id,
        # B9: persisted so the later submit-host-result step (a separate host
        # round-trip, possibly a fresh process) uses the SAME gate_type this
        # stage opened with instead of re-deriving it.
        "gate_type": gate_type,
        "state": "await_generate",
        "pre_dirty": sorted(_worktree_dirty_set(work_path)),
        # Hydra#71: base commit for THIS generate attempt's commit-aware
        # attribution (``_worktree_committed_since``). Re-stamped on the
        # Reflexion×1 retry transition below so a retry attributes only its
        # own new commits, not the first attempt's.
        "generate_base_sha": _git_head_sha(work_path),
        "baseline_failures": baseline_failures,
        "producer": "claude",
        "generate_index": 0,   # GAP-f: tracks Reflexion×1 — 0=first attempt, 1=retry
        "reflexion_critique": "",
        "attempt_id": None,
        "cost_usd": 0.0,
        "tokens_in": 0,
        "tokens_out": 0,
        "changed_paths": [],
        "outcome": None,
        "smoke_status": "skipped",
        "smoke_reason": "",
        "final_status": None,
        "error": None,
        "pending_action": {
            "call_key": "generate-0",
            "agent_type": "engineer",
            "cwd": work_path,
            "isolated_worktree": bool(worktree_path),
            "prompt": base_prompt,
            "instructions": (
                "Spawn the visible `engineer` subagent in cwd to implement the "
                "request, editing files directly. Then call submit-host-result "
                "with {call_key, result:{text, cost_usd, tokens_in, tokens_out, "
                "model}} where `text` summarizes the change."),
        },
    }
    cfile = cursor_path(root, workflow_id, run_id)
    save_cursor(cfile, cursor)
    # Marker 2: write the run-scoped sentinel the write-enforcement hooks
    # check. Cleared in _finalize / abort_stage.
    _write_stage_active_sentinel(root)
    _trace(cursor, "attended.stage_started", {"stage_id": stage_id})
    return _step_result(cursor, cfile)


def _apply_generate(dispatcher: Dispatcher, cursor: dict[str, Any],
                    result: dict[str, Any], *,
                    workflow_terminal: bool = False) -> None:
    """await_generate -> await_judge (or terminal on generate failure).

    The host's ``engineer`` subagent already wrote files in cwd; ``result`` is
    its summary + spend. We attribute the run-scoped diff, archive + record the
    attempt, route the judge via pp's ``gate_eligible_judges``, and stage the
    judge host-action.

    ``workflow_terminal``: threaded through to ``_finalize`` on the
    generate-failure terminal paths below -- see ``submit_host_result``'s
    docstring. A generate FAILURE never reaches the merge branch of
    ``_finalize`` (``passed=False`` there unconditionally), so this only
    affects the ``merge`` error label reported to the operator, not any
    dispatch/re-entry behaviour.
    """
    cm = dispatcher.call_mcp
    work_path = cursor.get("work_path") or cursor["project_path"]
    stage_id = cursor["stage_id"]
    run_id = cursor["run_id"]
    producer = cursor["producer"]

    gen_text = str(result.get("text") or "")
    # B8: a host result that omits cost_usd is no longer free — if it reports
    # tokens+model, price it (source="estimated"); otherwise it is
    # source="unmeasured" ($0.0, logged, non-blocking).
    _gen_cost, _gen_source = _priced_cost(cursor, result, label="generate")
    cursor["cost_usd"] = float(cursor["cost_usd"]) + _gen_cost
    cursor["tokens_in"] = int(cursor["tokens_in"]) + coerce_untrusted_count(result.get("tokens_in"))
    cursor["tokens_out"] = int(cursor["tokens_out"]) + coerce_untrusted_count(result.get("tokens_out"))

    # Hydra#71: commit-aware attribution. The attended host engineer commits
    # its work (unlike the headless drive loop, which the harness commits on
    # its behalf later), so the uncommitted-dirty-set delta alone is empty for
    # the normal case and silently attributes nothing. Union in the branch's
    # own commit history since this attempt's recorded base -- captured at
    # begin_stage / the Reflexion retry transition, per generate attempt.
    pre_dirty = set(cursor.get("pre_dirty") or [])
    dirty_changed = _worktree_dirty_set(work_path) - pre_dirty
    committed_changed = _worktree_committed_since(
        work_path, cursor.get("generate_base_sha"))
    run_changed = dirty_changed | committed_changed
    cursor["changed_paths"] = sorted(set(cursor.get("changed_paths") or []) | run_changed)
    wrote_changes = bool(run_changed)

    # Hydra#71: on the attended path a host result is a structured payload,
    # not free-form CLI narration -- never marker-classify its prose summary
    # (``apply_text_markers=False``). Only hard signals (an explicit
    # failure-shaped ``gen`` dict, or truly empty output with nothing
    # attributed to this run) still fail the stage.
    gen_fail = _generate_failure_reason(
        {"status": "done", "result": result}, gen_text, wrote_changes,
        apply_text_markers=False)

    model_id = str(result.get("model") or cursor.get("model_tier") or f"{producer}-default")

    # GAP-f: generate_index tracks the Reflexion×1 retry (0=first, 1=Reflexion).
    gen_idx = cursor.get("generate_index", 0)

    if gen_fail:
        cursor["error"] = gen_fail
        cursor["outcome"] = "error"
        try:
            _raise_on_error_payload(
                cm("pp_harness", "archive_artifact", {
                    "run_id": run_id,
                    "relative_path": f"code/{producer}-attempt-{gen_idx}.failed.md",
                    "bytes": f"GENERATE FAILED: {gen_fail}\n\n{gen_text or '(no output)'}",
                    "stage_id": stage_id, "kind": "code", "encoding": "utf8",
                }, squad_id=_SQ),
                "archive_artifact",
            )
        except Exception:  # noqa: BLE001
            pass
        try:
            att = _raise_on_error_payload(
                cm("pp_harness", "record_attempt", {
                    "stage_id": stage_id, "producer": producer, "model_id": model_id,
                    "agent_type": "engineer",   # F29
                    "tokens_in": coerce_untrusted_count(result.get("tokens_in")),
                    "tokens_out": coerce_untrusted_count(result.get("tokens_out")),
                    "cost_usd": _gen_cost,
                    "status": "error", "retry_index": gen_idx,
                    "notes": {"candidate_index": 1},
                }, squad_id=_SQ),
                "record_attempt",
            )
            cursor["attempt_id"] = _pp_inner(att).get("attempt_id")
        except Exception:  # noqa: BLE001
            pass
        # Generation failed; finalize as surfaced (no judge).
        _finalize(dispatcher, cursor, passed=False, gen_failed=True,
                 workflow_terminal=workflow_terminal)
        return

    # Successful generate: archive the producer summary + record the attempt.
    try:
        _raise_on_error_payload(
            cm("pp_harness", "archive_artifact", {
                "run_id": run_id,
                "relative_path": f"code/{producer}-attempt-{gen_idx}.md",
                "bytes": gen_text or "(no summary returned)",
                "stage_id": stage_id, "kind": "code", "encoding": "utf8",
            }, squad_id=_SQ),
            "archive_artifact",
        )
    except Exception:  # noqa: BLE001
        pass
    # Finding 1: wrap record_attempt in try/except — an RPC failure here must
    # surface cleanly, not crash submit_host_result and orphan the stage.
    # LV-1: _raise_on_error_payload converts error dicts into a RuntimeError
    # (PPLedgerError) so the existing except clause fires for payload-level
    # failures too -- and it preserves the original dict as ``.payload``,
    # which is what venom-gate classification downstream reads to tell a
    # structural rejection from a transport-shaped failure.
    # The pp schema accepts agent_type as an optional top-level string; strict
    # mode only rejects the literal 'general-purpose', not 'engineer'.
    try:
        att = _raise_on_error_payload(
            cm("pp_harness", "record_attempt", {
                "stage_id": stage_id, "producer": producer, "model_id": model_id,
                "agent_type": "engineer",   # F29 — accepted optional; strict rejects 'general-purpose'
                "tokens_in": coerce_untrusted_count(result.get("tokens_in")),
                "tokens_out": coerce_untrusted_count(result.get("tokens_out")),
                "cost_usd": _gen_cost,
                "status": "ok", "retry_index": gen_idx,
                "notes": {"candidate_index": 1},
            }, squad_id=_SQ),
            "record_attempt",
        )
        cursor["attempt_id"] = _pp_inner(att).get("attempt_id")
    except Exception as _ra_exc:  # noqa: BLE001
        # record_attempt RPC failed — surface the stage immediately rather than
        # crashing. The engineer's work is generated but cannot be tracked.
        cursor["error"] = f"record_attempt RPC failed: {_ra_exc!r}"
        _finalize(dispatcher, cursor, passed=False, gen_failed=False,
                 workflow_terminal=workflow_terminal)
        return

    # Judge routing — honour pp's gate_eligible_judges (cross- vs same-vendor)
    # exactly like the headless loop, so the host spawns the right judge agent.
    # B9: reuse the SAME gate_type this stage opened with (persisted on the
    # cursor by begin_stage) rather than re-deriving a hardcoded literal here
    # — the two must never drift from each other.
    gate_type_used = cursor.get("gate_type") or _pp_gate_type("code", "code_style")
    gate_dec: dict[str, Any] = {}
    try:
        gate_dec = _pp_inner(_raise_on_error_payload(
            cm("pp_harness", "gate_eligible_judges", {
                "gate_type": gate_type_used,
                "generator_producer": producer,
                "prompt_keywords": cursor["request_text"][:1000],
            }, squad_id=_SQ),
            "gate_eligible_judges",
        ))
    except Exception:  # noqa: BLE001
        gate_dec = {}
    required_cross = bool(gate_dec.get("required_cross_vendor", True))
    # B2: pull pp's own closing (cross-vendor) lane so its preferred_producers
    # feed the vendor-selection mapping below instead of being discarded.
    allowed_judges_list = gate_dec.get("allowed_judges") or []
    _closing_lane = next(
        (j for j in allowed_judges_list if isinstance(j, dict) and j.get("closing")),
        None) or {}
    _pp_preferred_producers = _closing_lane.get("preferred_producers") or []
    # E2-25: pp and Hydra both key rubrics by an immutable `@N`; a bare base id
    # resolves to the highest registered version before it reaches the judge or
    # the ledger. An id nothing registers is traced, not silently accepted.
    requested_rubric = str(gate_dec.get("rubric_id") or cursor["judge_rubric_id"])
    gate_rubric = _resolve_rubric_id(
        dispatcher, requested_rubric,
        lambda base: _trace(cursor, "attended.rubric_unresolved", {
            "stage_id": stage_id, "requested": base}))
    # producer is "claude": a sanctioned same-vendor Claude judge is allowed only
    # when cross-vendor is NOT required; otherwise the host must spawn the
    # cross-vendor judge (codex/agy critique).
    judge_agent = "judge-cross-vendor" if required_cross else "judge-same-vendor"
    # B2: select the judge PRODUCER, not just the agent type. For a
    # cross-vendor gate the mapping is authoritative (judge-cross-vendor.md);
    # for a same-vendor gate the judge is the generator's own vendor by
    # definition. The chain's remaining entries are engine-side failover
    # targets for `judge_tool_failed` (see `_apply_judge`).
    if required_cross:
        judge_vendor_chain = _judge_vendor_chain(
            producer, gate_type_used, _pp_preferred_producers)
    else:
        judge_vendor_chain = [_base_judge_vendor(producer)]
    judge_producer_selected = judge_vendor_chain[0]
    rubric_body, rubric_fallback = _rubric_md_ex(gate_rubric, dispatcher)
    if rubric_fallback:
        # The judge is about to see the GENERIC rubric, not the named one. Say so
        # here, in the host_action, and in the verdict metadata — the ledger must
        # never carry a rubric id whose body was not the one applied.
        _trace(cursor, "attended.rubric_fallback", {
            "stage_id": stage_id, "requested": requested_rubric,
            "effective": gate_rubric,
        })
    judge_text = _judge_artifact_text(
        work_path, sorted(run_changed), gen_text)

    cursor["gate_rubric"] = gate_rubric
    cursor["gate_rubric_fallback"] = rubric_fallback
    cursor["required_cross"] = required_cross
    # B2: the engine-owned vendor failover chain + cursor into it. Failover on
    # `judge_tool_failed` NEVER happens agent-side (pp's judge-cross-vendor
    # spec forbids it -- it can land a same-vendor judge and trip F31); it is
    # driven from here, in `_apply_judge`.
    cursor["judge_vendor_chain"] = judge_vendor_chain
    cursor["judge_vendor_chain_idx"] = 0
    cursor["state"] = "await_judge"
    # LV-8: scope the call_key with run_id + stage_id, not just the generate
    # index. This value round-trips verbatim as pp's record_verdict
    # idempotency_token (see _apply_judge below); an unscoped "judge-0"/
    # "judge-1" collides across every stage/run in the shared pp daemon DB,
    # so a second stage's genuine first verdict call silently short-circuits
    # into the *first* stage's verdict row via pp's unscoped idempotency
    # lookup. Scoping keeps the retry-safety property (same stage + same
    # gen_idx replay => same token => same idempotent short-circuit) while
    # eliminating cross-stage/cross-run collisions.
    #
    # Retry-fix follow-up: also fold in attempt_id (set on the cursor a few
    # lines above by record_attempt). run_id+stage_id+gen_idx is already
    # unique for the lifetime of this stage's logical verdict slot -- pp's
    # own findVerdictByIdempotencyToken/resolveIdempotentVerdict is what
    # actually enforces exactly-once, not this token's uniqueness. But pp's
    # guard error message itself tells callers to scope idempotency tokens by
    # attempt, and this token didn't. Including attempt_id makes token<->
    # attempt 1:1 by construction rather than by an argument spanning three
    # call sites (this one, the finalize_stage fallback below, and
    # recover_stalled_stage's replay) that a future refactor could silently
    # invalidate. attempt_id is stable across a stalled-infra re-drive and
    # across recover_stalled_stage (both replay the ORIGINAL captured
    # call_key/payload rather than rebuilding it from cursor state), so this
    # doesn't break retry-safety: the same judge call re-driven on the same
    # attempt still produces the same token.
    judge_call_key = f"judge-{run_id}-{stage_id}-{cursor['attempt_id']}-{gen_idx}"
    # E2-27: hand the host the model ids pp will accept for each judge
    # producer, so the judge subagent reports a pinned id instead of guessing
    # one that record_verdict then rejects.
    allowed_models = allowed_judge_model_ids(dispatcher)
    cursor["pending_action"] = _judge_pending_action(
        call_key=judge_call_key, judge_agent=judge_agent, gate_rubric=gate_rubric,
        rubric_fallback=rubric_fallback, required_cross=required_cross,
        judge_text=judge_text, rubric_body=rubric_body, work_path=work_path,
        allowed_models=allowed_models, judge_producer=judge_producer_selected,
    )
    _trace(cursor, "attended.attempt_recorded", {
        "stage_id": stage_id, "attempt_id": cursor["attempt_id"],
        "producer": producer, "judge_agent": judge_agent,
        "required_cross_vendor": required_cross, "wrote_changes": wrote_changes,
        "generate_index": gen_idx,
    })


def _attended_smoke_mode() -> str:
    """``HYDRA_ATTENDED_SMOKE_MODE`` — ``"async"`` (default) or ``"sync"``.

    Hydra#70: the MCP route (and the default CLI) always runs the smoke as a
    detached, tracked job (see ``hydra_core.smoke_job``) so a smoke that runs
    past ``HYDRA_SUBMIT_TIMEOUT_S`` is never orphaned when the submitting
    process is killed. ``sync`` is an explicit escape hatch for callers not
    bounded by that call budget (a test fixture, or a bespoke direct-CLI
    driver) that want the old inline behaviour.
    """
    raw = (os.environ.get("HYDRA_ATTENDED_SMOKE_MODE") or "async").strip().lower()
    return "sync" if raw == "sync" else "async"


def _apply_smoke_baseline_excuse(cursor: dict[str, Any], work_path: str,
                                 smoke_status: str, smoke_reason: str) -> tuple[str, str]:
    """GAP-a2 / Rider (a): compare a `fail` smoke against the baseline
    failures captured before the engineer's change. If every currently-
    failing test was ALREADY failing before the change, the smoke failure is
    not attributable to this change — excuse it (Finding 6: bounded set).

    Split out of ``_apply_judge`` (Hydra#70) so both the sync inline path and
    the async job-completion path apply the SAME excuse logic."""
    if smoke_status != "fail":
        return smoke_status, smoke_reason
    _captured_baseline = list(cursor.get("baseline_failures") or [])
    _env_bl_raw = os.environ.get("HYDRA_SMOKE_BASELINE_TESTS", "")
    _env_allowlist: set[str] | None = (
        {t.strip() for t in _env_bl_raw.split(",") if t.strip()}
        if _env_bl_raw else None
    )
    _captured_set = set(_captured_baseline)
    if _env_allowlist is not None:
        _excusable = (_captured_set & _env_allowlist) if _captured_set else _env_allowlist
    else:
        _excusable = _captured_set

    if not _excusable:
        return smoke_status, smoke_reason

    _max_excuse = int(os.environ.get("HYDRA_SMOKE_BASELINE_MAX", "10"))
    if len(_excusable) > _max_excuse:
        _trace(cursor, "attended.smoke.baseline_too_broad", {
            "stage_id": cursor.get("stage_id"),
            "excusable_count": len(_excusable), "max": _max_excuse,
        })
        smoke_reason = (
            f"smoke: baseline too broad ({len(_excusable)} excusable "
            f"tests > HYDRA_SMOKE_BASELINE_MAX={_max_excuse}); "
            "treating as real failure"
        )
        return smoke_status, smoke_reason

    import sys as _sys
    try:
        _reruns = run_text(
            [_sys.executable, "-m", "pytest", "tests/", "--no-header", "-q", "--tb=no"],
            cwd=work_path, capture_output=True, check=False,
            timeout=_baseline_timeout_s(),
        )
        _current_failing = _parse_failing_tests(_reruns.stdout + "\n" + _reruns.stderr)
    except Exception:  # noqa: BLE001
        _current_failing = set()
    _excused = _current_failing & _excusable
    _new_failures = _current_failing - _excusable
    if _current_failing or _excused:
        _trace(cursor, "attended.smoke.baseline_excuse_decision", {
            "stage_id": cursor.get("stage_id"),
            "current_failing": sorted(_current_failing),
            "excused": sorted(_excused),
            "new_failures": sorted(_new_failures),
            "excusable_set_size": len(_excusable),
        })
    if not _new_failures:
        smoke_status = "pass"
        smoke_reason = (
            f"smoke: {len(_current_failing)} failure(s) all pre-existed in "
            f"baseline ({len(_excused)} excused); treated as pass"
        )
    return smoke_status, smoke_reason


def _apply_smoke_and_finalize(dispatcher: Dispatcher, cursor: dict[str, Any], *,
                              cursor_file: "str | Path | None", call_key: str | None,
                              work_path: str, smoke_status: str, smoke_reason: str,
                              workflow_terminal: bool) -> None:
    """The common tail shared by every smoke-result source (sync inline run,
    cached ``smoke_result_for`` replay, and the async job's completed
    result): apply the baseline excuse, record the pp smoke status exactly
    once, persist the idempotency marker, honour the finalize-readiness
    gate, and finalize the stage.

    Hydra#70: split out of ``_apply_judge`` so the async job-completion path
    (``poll_smoke_job``) and the sync path converge on IDENTICAL gating
    instead of two implementations drifting apart.
    """
    smoke_status, smoke_reason = _apply_smoke_baseline_excuse(
        cursor, work_path, smoke_status, smoke_reason)
    try:
        _raise_on_error_payload(
            dispatcher.call_mcp("pp_harness", "record_smoke_status", {
                "stage_id": cursor["stage_id"], "candidate_index": 1,
                "status": smoke_status,
                "reason": (smoke_reason or "attended drive smoke")[:300],
            }, squad_id=_SQ),
            "record_smoke_status",
        )
    except Exception:  # noqa: BLE001
        pass
    # Fix-1b: persist smoke outcome before _finalize so a timeout between
    # here and the outer save_cursor does not restart the smoke on retry.
    if call_key is not None and cursor_file is not None:
        cursor["smoke_result_for"] = {
            "call_key": call_key, "status": smoke_status, "reason": smoke_reason,
        }
        save_cursor(cursor_file, cursor)
    passed = smoke_status == "pass"
    cursor["smoke_status"] = smoke_status
    cursor["smoke_reason"] = smoke_reason
    cursor.pop("smoke_job", None)

    # Honour pp's finalize-readiness gate (same auto-resolved deferrals as the
    # headless loop).
    if passed:
        try:
            rd = _pp_inner(_raise_on_error_payload(
                dispatcher.call_mcp("pp_harness", "get_stage_finalize_readiness",
                   {"stage_id": cursor["stage_id"]}, squad_id=_SQ),
                "get_stage_finalize_readiness",
            ))
        except Exception:  # noqa: BLE001
            rd = {}
        if rd.get("can_pass") is False:
            na = rd.get("next_action") or "not_ready"
            _auto_resolved = {"run_artifact_validate", "run_tdd_pre_check",
                              "run_tdd_post_check", "record_smoke_or_assertion"}
            if na not in _auto_resolved:
                passed = False
                cursor["outcome"] = "surfaced"
                cursor["error"] = f"pp readiness: not ready (next_action={na})"

    _finalize(dispatcher, cursor, passed=passed, gen_failed=False,
             workflow_terminal=workflow_terminal)


def poll_smoke_job(dispatcher: Dispatcher, cursor: dict[str, Any], *,
                   cursor_file: "str | Path | None",
                   workflow_terminal: bool = False) -> bool:
    """Poll an ``await_smoke`` cursor's detached job (Hydra#70).

    Returns ``True`` if the job is still running and before its deadline
    (cursor left unchanged — caller should report "still pending" without
    blocking). Returns ``False`` once the cursor has ADVANCED (finalized,
    via ``_apply_smoke_and_finalize``) — either because the job completed,
    or because it was judged lost (deadline passed / process vanished with
    no result), which is always classified as an infra failure so a lost
    job can never wedge the cursor in ``await_smoke`` forever.
    """
    from . import smoke_job as _smoke_job
    job = cursor.get("smoke_job") or {}
    work_path = cursor.get("work_path") or cursor.get("project_path")
    call_key = job.get("call_key") or (cursor.get("pending_action") or {}).get("call_key")
    if not job:
        _apply_smoke_and_finalize(
            dispatcher, cursor, cursor_file=cursor_file, call_key=call_key,
            work_path=work_path, smoke_status="infra_error",
            smoke_reason="await_smoke cursor has no smoke_job recorded",
            workflow_terminal=workflow_terminal)
        return False
    result = _smoke_job.poll_job(job)
    if result is None:
        return True
    _trace(cursor, "attended.smoke_job_polled", {
        "stage_id": cursor.get("stage_id"), "call_key": call_key,
        "status": result.get("status"),
    })
    _apply_smoke_and_finalize(
        dispatcher, cursor, cursor_file=cursor_file, call_key=call_key,
        work_path=work_path, smoke_status=str(result.get("status") or "infra_error"),
        smoke_reason=str(result.get("reason") or ""),
        workflow_terminal=workflow_terminal)
    return False


def _apply_judge(dispatcher: Dispatcher, cursor: dict[str, Any],
                 result: dict[str, Any],
                 *, cursor_file: "str | Path | None" = None,
                 call_key: str | None = None,
                 workflow_terminal: bool = False) -> dict[str, Any] | None:
    """await_judge -> terminal (or back to await_generate for Reflexion x1).

    ``workflow_terminal`` (round 6 gap fix): see ``submit_host_result``'s
    docstring. Threaded through to every ``_finalize`` call below so a
    PASSING verdict on a stale cursor still records the ledger verdict
    (already happened via ``record_verdict`` above, before ``_finalize`` is
    ever reached) but never merges the candidate worktree back into the
    repo -- merging is itself the "continue a terminal workflow" act this
    fix refuses.

    F26+M8: a failed record_verdict/finalize_stage on a pass outcome downgrades
    the stage to surfaced (never proceeds to finalize_run complete).
    F31: required_cross_vendor but judge was same-vendor (degraded) → downgrade.
    GAP-f: Reflexion×1 — on the first revise, transition back to await_generate
    with call_key='generate-1' and the critique embedded in the prompt.
    GAP-h: warn-telemetry when critique_md cites no existing worktree file.
    GAP-a2: lazy baseline fallback from HYDRA_SMOKE_BASELINE_TESTS env var.
    Fix-1b: idempotency markers (verdict_recorded_for / smoke_result_for) written
    mid-function so a retried submit after a timeout never double-records in the
    pp ledger or restarts the ~28-min smoke.
    E2-27: an unsupported judge_producer, or a pp judge-model pin rejection on
    record_verdict, is HOST-CORRECTABLE -- the cursor stays in ``await_judge``
    with its pending_action (and call_key) untouched and this function returns
    a ``{"ok": False, "retryable": True, ...}`` dict for the caller to relay,
    instead of surfacing a passing stage.

    B2: a judge that could not reach its assigned vendor's CLI reports
    ``{judge_tool_failed: true, reason, vendor, model: null}`` per
    judge-cross-vendor.md and MUST NOT silently fall back to a different
    vendor itself (that can land a same-vendor judge and trip F31's
    degraded-downgrade undetected). Failover instead happens HERE, engine
    side: the next entry in ``cursor["judge_vendor_chain"]`` is selected and
    the SAME pending_action/call_key is reissued pointing at it.

    Returns ``None`` on a normal transition, or the host-correctable error
    dict described above.
    """
    # B2: engine-side judge-vendor failover. Checked before anything else is
    # accrued/recorded -- a failed judge tool call produced no verdict, so
    # nothing needs to be undone, only the pending_action needs to move on to
    # the next vendor in the chain.
    if result.get("judge_tool_failed"):
        chain = list(cursor.get("judge_vendor_chain") or [])
        idx = int(cursor.get("judge_vendor_chain_idx") or 0)
        stage_id = cursor.get("stage_id")
        failed_vendor = chain[idx] if idx < len(chain) else result.get("vendor")
        next_idx = idx + 1
        if next_idx < len(chain):
            next_vendor = chain[next_idx]
            cursor["judge_vendor_chain_idx"] = next_idx
            pending = dict(cursor.get("pending_action") or {})
            allowed_models = allowed_judge_model_ids(dispatcher)
            pending["judge_producer"] = next_vendor
            pending["preferred_models"] = allowed_models.get(next_vendor, [])
            pending["allowed_judge_model_ids"] = allowed_models
            pending["instructions"] = (
                f"FAILOVER: judge_producer {failed_vendor!r} reported "
                f"judge_tool_failed ({result.get('reason')!r}). Spawn the "
                f"visible judge subagent AGAIN for the SAME call_key, this "
                f"time with judge_producer={next_vendor!r} "
                f"(preferred_models={allowed_models.get(next_vendor, [])!r}). "
                "Then call submit-host-result with "
                "{call_key, result:{outcome:pass|revise|fail, critique_md, "
                "judge_producer, judge_model_id, score_json, cost_usd}}."
            )
            cursor["pending_action"] = pending
            _trace(cursor, "attended.judge_vendor_failover", {
                "stage_id": stage_id, "call_key": call_key,
                "from_producer": failed_vendor, "to_producer": next_vendor,
                "reason": result.get("reason"),
            })
            if cursor_file is not None:
                save_cursor(cursor_file, cursor)
            return {
                "ok": False,
                "retryable": True,
                "error": (
                    f"judge vendor {failed_vendor!r} reported judge_tool_failed "
                    f"({result.get('reason')!r}); resubmit the SAME call_key "
                    f"with judge_producer={next_vendor!r}"),
                "judge_producer": next_vendor,
            }
        # No further vendors to try — this is a genuine infra failure, not a
        # code defect, so it must surface immediately rather than loop.
        cursor["error"] = (
            f"all judge vendors exhausted after judge_tool_failed from "
            f"{failed_vendor!r}: {result.get('reason')}")
        _trace(cursor, "attended.judge_vendor_failover_exhausted", {
            "stage_id": stage_id, "call_key": call_key,
            "chain": chain, "reason": result.get("reason"),
        })
        _finalize(dispatcher, cursor, passed=False, gen_failed=False,
                 workflow_terminal=workflow_terminal)
        return None

    cm = dispatcher.call_mcp
    producer = cursor["producer"]
    attempt_id = cursor.get("attempt_id")
    gate_rubric = cursor.get("gate_rubric") or cursor["judge_rubric_id"]
    required_cross = bool(cursor.get("required_cross"))
    gen_idx = cursor.get("generate_index", 0)   # GAP-f: 0=first attempt, 1=retry
    work_path = cursor.get("work_path") or cursor["project_path"]

    # E2-27: validate the judge's self-reported producer BEFORE anything is
    # accrued or written. An unsupported producer means the host cannot build
    # a valid record_verdict payload, and pp does not police non-{codex,agy}
    # vendors -- so a bogus label would otherwise land a bogus vendor in the
    # ledger. Bounce it back as host-correctable: the cursor is left exactly
    # as it was (state "await_judge"/"stalled_infra", same pending_action,
    # same call_key), so a corrected resubmit re-enters here cleanly.
    allowed_models = allowed_judge_model_ids(dispatcher)
    _reported_producer = str(result.get("judge_producer")
                             or ("codex" if required_cross else "claude"))
    _reported_vendor = _base_judge_vendor(_reported_producer)
    if _reported_vendor not in allowed_models:
        if _judge_correction_budget_left(cursor, call_key):
            n = _record_judge_correction(cursor, call_key)
            _trace(cursor, "attended.judge_producer_unsupported", {
                "stage_id": cursor.get("stage_id"), "call_key": call_key,
                "judge_producer": _reported_producer,
                "correction_attempt": n,
                "allowed_judge_model_ids": allowed_models,
            })
            if cursor_file is not None:
                save_cursor(cursor_file, cursor)
            return {
                "ok": False,
                "retryable": True,
                "error": (
                    f"judge_producer {_reported_producer!r} is not a supported "
                    f"judge vendor; resubmit the SAME call_key with a "
                    f"judge_producer in {sorted(allowed_models)} and a "
                    f"judge_model_id from allowed_judge_model_ids"),
                "allowed_judge_model_ids": allowed_models,
            }
        # Correction budget exhausted — stop bouncing and let the normal path
        # run so the stage reaches a decision instead of livelocking.
        _trace(cursor, "attended.judge_producer_unsupported_accepted", {
            "stage_id": cursor.get("stage_id"), "call_key": call_key,
            "judge_producer": _reported_producer,
            "reason": "correction budget exhausted; proceeding to pp",
        })

    # W2-3: guard cost/token accrual against double-counting when a
    # transport-shaped record_verdict failure holds the cursor open
    # (state="stalled_infra") and the SAME judge result is resubmitted under
    # the SAME call_key to re-drive the stage. Without this guard a re-drive
    # would add the judge's cost_usd/tokens a second time.
    _judge_cost_applied = (call_key is not None
                           and cursor.get("judge_cost_applied_for") == call_key)
    if not _judge_cost_applied:
        # The double-counting guard above only activates when `call_key` is
        # not None -- it relies on real judge submissions always carrying one
        # (enforced by call topology: every host_action the driver hands out
        # for a judge step sets pending_action["call_key"]). Make that
        # structural rather than incidental: surface it in trace if it's ever
        # violated, instead of silently accruing cost with no re-drive guard.
        if call_key is None:
            _trace(cursor, "attended.judge_cost_no_call_key", {
                "stage_id": cursor.get("stage_id"),
                "warning": ("submit_verdict called with call_key=None; the "
                            "judge_cost_applied_for double-counting guard "
                            "cannot protect this accrual on a re-drive"),
            })
        # B8: same treat-missing-cost-as-priceable-or-unmeasured policy as the
        # generate accrual above; hint the judge's own model field
        # (judge_model_id) since a judge result's model lives there, not in
        # the generic "model" key.
        _judge_cost, _judge_cost_source = _priced_cost(
            cursor, result, label="judge",
            model_hint=str(result.get("judge_model_id") or "") or None,
        )
        cursor["cost_usd"] = float(cursor["cost_usd"]) + _judge_cost
        cursor["tokens_in"] = int(cursor["tokens_in"]) + coerce_untrusted_count(result.get("tokens_in"))
        cursor["tokens_out"] = int(cursor["tokens_out"]) + coerce_untrusted_count(result.get("tokens_out"))
        if call_key is not None:
            cursor["judge_cost_applied_for"] = call_key

    outcome = result.get("outcome") or result.get("verdict") or "revise"
    if outcome not in {"pass", "revise", "fail"}:
        outcome = "revise"
    critique_md = str(result.get("critique_md") or result.get("critique") or "")
    judge_producer = str(result.get("judge_producer")
                         or ("codex" if required_cross else "claude"))
    cross_vendor = judge_producer != producer
    degraded = required_cross and not cross_vendor
    # LV-3 defense-in-depth: when same-vendor judging is allowed (not
    # required_cross) and the judge producer is identical to the generator,
    # relabel with a "-same-vendor-host" suffix before record_verdict.  pp's
    # recordVerdict rejects generator-identical producer+model pairs; the
    # suffix keeps the model id honest while making the ledger entry
    # distinguishable.  cross_vendor is NOT recomputed — it was False (same
    # vendor) and stays False; score_json._judge_tier="same_vendor" is correct.
    if not required_cross and judge_producer == producer:
        judge_producer = f"{producer}-same-vendor-host"

    score_json = dict(result.get("score_json") or result.get("score") or {})
    score_json["_cross_vendor"] = cross_vendor
    score_json["_judge_tier"] = "cross_vendor" if required_cross else "same_vendor"
    score_json["_attended"] = True
    if degraded:
        score_json["_judge_degraded"] = True
    # E2-25: the effective rubric travels with the verdict. `_rubric_fallback`
    # marks a verdict whose named rubric body was NOT the one the judge applied,
    # so an auditor reading the pp ledger can tell the two cases apart.
    score_json["_rubric_id"] = gate_rubric
    if cursor.get("gate_rubric_fallback"):
        score_json["_rubric_fallback"] = True

    # E2-27: normalize the reported judge_model_id against pp's pins BEFORE
    # record_verdict. pp pins codex/agy critique models; a judge that names
    # the model it believes it used ("gpt-5.1-codex") rather than the id the
    # critique tool served made record_verdict throw, which used to discard a
    # passing stage. Normalize to the vendor's pinned critique model and keep
    # the judge's own claim in score_json for audit. Vendors pp does not pin
    # (claude -> empty list) are passed through untouched.
    _judge_vendor = _base_judge_vendor(judge_producer)
    _reported_model = str(result.get("judge_model_id")
                          or result.get("model") or "").strip()
    judge_model_id = _reported_model or f"{judge_producer}-default"
    _vendor_pins = allowed_models.get(_judge_vendor) or []
    if _vendor_pins and judge_model_id not in _vendor_pins:
        _pinned = _vendor_pins[0]
        score_json["judge_model_id_reported"] = _reported_model or None
        _trace(cursor, "attended.judge_model_id_normalized", {
            "stage_id": cursor.get("stage_id"), "call_key": call_key,
            "judge_producer": judge_producer,
            "reported": _reported_model or None,
            "normalized_to": _pinned,
            "allowed_judge_model_ids": _vendor_pins,
        })
        judge_model_id = _pinned

    # Hydra#72: derive judge_model_source/judge_override_reason/
    # judge_reasoning_effort BEFORE record_verdict -- see the module-level
    # comment above ``_judge_verdict_provenance`` for the pp contract this
    # forwards against (runs.ts:1029-1057).
    _verdict_provenance = _judge_verdict_provenance(
        dispatcher, judge_vendor=_judge_vendor, judge_model_id=judge_model_id,
        result=result, allowed_models=allowed_models,
    )

    # Finding 2: track whether the outcome change is an infra failure (F31 /
    # F26+M8) vs a genuine artifact defect.  Infra failures must surface
    # immediately — Reflexion is reserved for code defects the engineer can fix.
    _infra_downgrade = False

    # F31: required cross-vendor but got same-vendor judge → downgrade pass to surfaced.
    if degraded and outcome == "pass":
        outcome = "revise"   # treat as revise so non-pass path runs
        _infra_downgrade = True
        cursor["error"] = ("required_cross_vendor=true but judge was same-vendor "
                           "(degraded); stage downgraded to surfaced")

    cursor["outcome"] = outcome

    # Fix-1b: idempotency — skip record_verdict if a prior attempt for this exact
    # call_key already succeeded and we persisted the marker.  A submit timeout
    # that kills mid-_run_smoke (before the outer save_cursor at line ~1172) would
    # otherwise cause a retry to double-write the pp verdict ledger.
    _record_verdict_ok = True
    _record_verdict_exc: Exception | None = None
    _verdict_already_recorded = (call_key is not None
                                  and cursor.get("verdict_recorded_for") == call_key)
    if _verdict_already_recorded:
        _trace(cursor, "attended.verdict_skip_idempotent", {
            "stage_id": cursor.get("stage_id"),
            "call_key": call_key,
            "reason": "verdict_recorded_for marker matches — skipping duplicate record_verdict",
        })
    elif attempt_id:
        # W2-4: persist the exact record_verdict payload BEFORE the call so a
        # stage stranded by a transport-shaped failure that ends up needing
        # the `/hydra:resume --action recover-stalled-stage` path (e.g. an
        # older cursor from before the stalled_infra hold existed) can replay
        # this call verbatim instead of needing the judge's raw result
        # reconstructed from scratch.
        _verdict_payload = {
            "attempt_id": attempt_id,
            "judge_producer": judge_producer,
            "judge_model_id": judge_model_id,
            "outcome": outcome if outcome in {"pass", "revise", "fail"} else "revise",
            "critique_md": critique_md[:4000],
            "score_json": score_json,
            "rubric_id": gate_rubric,
            # Hydra#72: forward the derived judge-selection provenance so
            # record_verdict does not implicitly treat every verdict as
            # source="default" (see ``_judge_verdict_provenance``).
            **_verdict_provenance,
            # W2-3: the attended call_key doubles as pp's idempotency token. A
            # re-drive after a stalled_infra hold resubmits the same call_key,
            # so pp's recordVerdict returns the original verdict_id instead of
            # inserting a duplicate row -- exactly-once even across a
            # transport-shaped retry (or the W2-4 recovery replay).
            **({"idempotency_token": call_key} if call_key else {}),
        }
        cursor["pending_verdict_payload"] = _verdict_payload
        if cursor_file is not None:
            save_cursor(cursor_file, cursor)
        # F26+M8: capture record_verdict success; a failure on a pass outcome downgrades.
        # LV-1: _raise_on_error_payload converts error dicts (rejected/failed) into
        # a RuntimeError (PPLedgerError) so the existing except fires for
        # payload-level errors too -- and it preserves the original dict as
        # ``.payload``, which is what venom-gate classification downstream
        # reads to tell a structural rejection from a transport-shaped failure.
        try:
            _raise_on_error_payload(
                cm("pp_harness", "record_verdict", _verdict_payload, squad_id=_SQ),
                "record_verdict",
            )
            # Persist marker before _run_smoke so a timeout mid-smoke leaves the
            # cursor in a state where a retry can skip this call.
            if call_key is not None and cursor_file is not None:
                cursor["verdict_recorded_for"] = call_key
                save_cursor(cursor_file, cursor)
        except Exception as exc:  # noqa: BLE001
            # W2-2: capture the failure reason instead of discarding it. This
            # exact swallow is what forced a manual forensic reconstruction of
            # the first stalled-verdict incident -- the ledger had no verdict
            # row and no trace explaining why.
            _record_verdict_ok = False
            _record_verdict_exc = exc

    # E2-27 / Hydra#72: pp's judge-model pin rejection AND pp's judge-
    # selection provenance rejection (judge_model_source/judge_override_
    # reason -- runs.ts:1029-1057) are both LABEL problems, not an artifact
    # defect. `_classify_infra_failure` calls either "deterministic" (the
    # text matches the "validation" marker), which used to surface a PASSING
    # stage and discard the merge. Route both to the host-correctable path
    # instead: nothing about the cursor state or pending_action changes, so a
    # resubmit under the SAME call_key with a corrected judge_model_id (or,
    # for Hydra#72, after ``_judge_verdict_provenance`` re-derives on the
    # next call) re-enters here and retries record_verdict (no verdict row
    # was written, so `verdict_recorded_for` is unset and there is nothing to
    # double-write).
    _is_pin_error = _is_judge_pin_error(_record_verdict_exc)
    _is_provenance_error = _is_judge_provenance_error(_record_verdict_exc)
    if not _record_verdict_ok and (_is_pin_error or _is_provenance_error):
        _pin_reason = str(_record_verdict_exc)
        if _judge_correction_budget_left(cursor, call_key):
            n = _record_judge_correction(cursor, call_key)
            _trace_event = ("attended.judge_provenance_rejected" if _is_provenance_error
                             else "attended.judge_model_id_pin_rejected")
            _trace(cursor, _trace_event, {
                "stage_id": cursor.get("stage_id"), "call_key": call_key,
                "attempt_id": attempt_id,
                "judge_producer": judge_producer,
                "judge_model_id": judge_model_id,
                "correction_attempt": n,
                "reason": _pin_reason,
                "allowed_judge_model_ids": allowed_models,
            })
            if cursor_file is not None:
                save_cursor(cursor_file, cursor)
            _err_prefix = ("pp rejected the judge selection provenance: "
                           if _is_provenance_error else
                           "pp rejected the judge model id: ")
            return {
                "ok": False,
                "retryable": True,
                "error": (
                    _err_prefix + _pin_reason +
                    " — resubmit the SAME call_key with a judge_model_id from "
                    "allowed_judge_model_ids"),
                "allowed_judge_model_ids": allowed_models,
            }
        _trace(cursor, "attended.judge_model_id_pin_unrecoverable", {
            "stage_id": cursor.get("stage_id"), "call_key": call_key,
            "reason": _pin_reason,
            "correction_budget": _MAX_JUDGE_MODEL_CORRECTIONS,
        })

    if outcome == "pass" and not _record_verdict_ok:
        _rv_reason = str(_record_verdict_exc) if _record_verdict_exc is not None else "unknown error"
        _rv_kind = _classify_infra_failure(_record_verdict_exc)
        cursor["error"] = (cursor.get("error") or "") + \
            f" record_verdict RPC failed ({_rv_kind}): {_rv_reason}"
        _trace(cursor, "attended.verdict_rpc_failed", {
            "stage_id": cursor.get("stage_id"), "tool": "record_verdict",
            "call_key": call_key, "attempt_id": attempt_id,
            "reason": _rv_reason, "kind": _rv_kind,
        })
        if _rv_kind == "transport":
            # W2-3: hold the cursor open instead of downgrading the outcome
            # and finalizing. pending_action is left untouched (still the
            # judge's call_key), so a re-issued submit_host_result carrying
            # the SAME judge result re-enters this function and retries
            # record_verdict via the idempotency_token above. The worktree,
            # pp attempt row, and any smoke result are NOT touched here, so
            # they remain available to the recovery path (W2-4) or a manual
            # retry.
            cursor["state"] = "stalled_infra"
            _trace(cursor, "attended.stalled_infra", {
                "stage_id": cursor.get("stage_id"), "call_key": call_key,
                "attempt_id": attempt_id, "reason": _rv_reason,
            })
            return
        # Deterministic pp rejection (or an ambiguous failure we could not
        # positively classify as transport -- err toward failing the stage
        # rather than silently masking a real rejection): keep today's
        # behavior of downgrading to revise/surfaced.
        outcome = "revise"
        _infra_downgrade = True
        cursor["outcome"] = "revise"

    _trace(cursor, "attended.verdict", {
        "stage_id": cursor["stage_id"], "rubric_id": gate_rubric,
        "rubric_fallback": bool(cursor.get("gate_rubric_fallback")),
        "attempt_id": attempt_id, "producer": producer,
        "judge_producer": judge_producer, "outcome": outcome,
        "cross_vendor": cross_vendor, "generate_index": gen_idx,
        "degraded": degraded,
    })

    # GAP-h: warn when critique references no existing worktree file.
    if critique_md and not _has_real_file_ref(critique_md, work_path):
        _trace(cursor, "attended.judge.suspicious_critique", {
            "stage_id": cursor["stage_id"],
            "warning": "critique_md contains no path token matching an existing worktree file",
            "judge_producer": judge_producer,
            "critique_head": critique_md[:200],
        })

    # GAP-f: Reflexion×1 — on first revise (gen_idx==0), transition back to
    # await_generate with an augmented prompt that embeds the critique.
    # Finding 2: skip Reflexion for infra failures (F31 degraded judge, F26+M8
    # record_verdict RPC error) — retrying the engineer cannot fix an infra
    # problem and wastes a generation slot.
    if outcome == "revise" and gen_idx == 0 and not _infra_downgrade:
        cursor["generate_index"] = 1
        cursor["reflexion_critique"] = critique_md
        # Hydra#71: re-stamp the attribution base to the current HEAD (attempt
        # 0's commits, already folded into cursor["changed_paths"]) so the
        # retry's own commit-aware attribution only picks up ITS new commits,
        # not attempt 0's again.
        cursor["generate_base_sha"] = _git_head_sha(work_path)
        cursor["pre_dirty"] = sorted(_worktree_dirty_set(work_path))
        aug_prompt = _augment_with_critique(cursor["request_text"], critique_md)
        # 7b fix: re-prepend the hydra_context_block exactly once so the retry
        # prompt mirrors the initial generate-0 prompt structure.  The block was
        # stored in the cursor at begin_stage; an empty string is a no-op.
        _hcb = cursor.get("hydra_context_block", "")
        if _hcb:
            aug_prompt = f"{_hcb}\n\n{aug_prompt}"
        cursor["state"] = "await_generate"
        cursor["pending_action"] = {
            "call_key": "generate-1",
            "agent_type": "engineer",
            "cwd": work_path,
            "isolated_worktree": bool(cursor.get("worktree_path")),
            "prompt": aug_prompt,
            "retry_index": 1,
            "instructions": (
                "Spawn the visible `engineer` subagent to revise the implementation "
                "addressing the critique embedded in the prompt. Then call "
                "submit-host-result with {call_key, result:{text, cost_usd, "
                "tokens_in, tokens_out, model}}."),
        }
        # Fix-1b: clear idempotency markers when transitioning to a new generate
        # cycle so the next judge (judge-1) records its own verdict freshly.
        cursor.pop("verdict_recorded_for", None)
        cursor.pop("smoke_result_for", None)
        _trace(cursor, "attended.reflexion", {
            "stage_id": cursor["stage_id"], "generate_index": 1,
        })
        return  # Don't finalize — wait for generate-1

    # PP-VG-5: a code stage may finalize 'complete' only with a real smoke result.
    if outcome == "pass" and attempt_id:
        # Fix-1b: if the smoke already completed for this call_key (persisted before
        # a prior submit timed out inside _finalize), reuse the result without
        # re-running the ~28-min test suite or double-calling record_smoke_status.
        _cached_smoke = cursor.get("smoke_result_for") or {}
        _smoke_from_cache = (call_key is not None
                             and _cached_smoke.get("call_key") == call_key)
        if _smoke_from_cache:
            _trace(cursor, "attended.smoke_skip_idempotent", {
                "stage_id": cursor.get("stage_id"),
                "call_key": call_key,
                "smoke_status": _cached_smoke.get("status"),
                "reason": "smoke_result_for marker matches — reusing persisted smoke outcome",
            })
            _apply_smoke_and_finalize(
                dispatcher, cursor, cursor_file=cursor_file, call_key=call_key,
                work_path=work_path,
                smoke_status=str(_cached_smoke.get("status") or "skipped"),
                smoke_reason=str(_cached_smoke.get("reason") or ""),
                workflow_terminal=workflow_terminal)
            return None
        # Hydra#70: the smoke runs SYNCHRONOUSLY inside this single MCP call
        # by default only under HYDRA_ATTENDED_SMOKE_MODE=sync (test
        # fixtures / a caller not bounded by the MCP submit-call budget).
        # The default (and the sole mode on the MCP route -- see
        # `mcp_servers/hydra_control/server.py`'s HYDRA_SUBMIT_TIMEOUT_S vs
        # HYDRA_SMOKE_TIMEOUT_S mismatch this fixes) is "async": start a
        # DETACHED, TRACKED job (`hydra_core.smoke_job`) that survives this
        # process's own death, and return promptly with state="await_smoke"
        # so the host polls via `hydra.workflow.step` / a same-call_key
        # resubmit instead of blocking the MCP call past its own timeout.
        # An async job needs a cursor_file to derive its result/log paths and
        # to persist cursor["smoke_job"] for a later poll -- a caller with no
        # cursor_file (defensive/legacy) cannot use the async path at all, so
        # it degrades to sync rather than losing the smoke job's location.
        if _attended_smoke_mode() == "sync" or cursor_file is None:
            smoke_status, smoke_reason = _run_smoke(
                dispatcher, project_path=work_path, stage_id=cursor["stage_id"])
            _apply_smoke_and_finalize(
                dispatcher, cursor, cursor_file=cursor_file, call_key=call_key,
                work_path=work_path, smoke_status=smoke_status,
                smoke_reason=smoke_reason, workflow_terminal=workflow_terminal)
            return None
        from . import smoke_job as _smoke_job
        job = _smoke_job.start_job(
            cursor_file, project_path=work_path, stage_id=cursor["stage_id"],
            call_key=call_key)
        cursor["smoke_job"] = job
        cursor["state"] = "await_smoke"
        # W2-3-shaped: keep the SAME judge call_key as pending_action.call_key
        # so a re-issued submit_host_result under that call_key re-enters the
        # "await_smoke" branch in `submit_host_result` as a POLL, never a
        # duplicate record_verdict/record_attempt.
        cursor["pending_action"] = {
            "call_key": call_key,
            "action": "poll_smoke",
            "poll": True,
            "instructions": (
                "The verdict is already recorded. Smoke is running as a "
                "detached background job — there is no agent to spawn. "
                "Call hydra.workflow.step(workflow_id) again after a short "
                "delay to poll for completion (a same-call_key "
                "submit-host-result resubmit also works as a poll)."
            ),
        }
        if cursor_file is not None:
            save_cursor(cursor_file, cursor)
        _trace(cursor, "attended.smoke_job_started", {
            "stage_id": cursor.get("stage_id"), "call_key": call_key,
            "pid": job.get("pid"), "deadline": job.get("deadline"),
        })
        return None
    else:
        cursor["smoke_status"] = "skipped"
        cursor["smoke_reason"] = ""
        _finalize(dispatcher, cursor, passed=False, gen_failed=False,
                 workflow_terminal=workflow_terminal)


def _finalize(dispatcher: Dispatcher, cursor: dict[str, Any], *,
              passed: bool, gen_failed: bool,
              workflow_terminal: bool = False) -> None:
    """Finalize the stage + run and set the terminal cursor state. Mirrors the
    headless loop's downgrade-honouring finalize_run handling.

    F26+M8: a finalize_stage RPC failure on a passing stage downgrades to
    surfaced — we never proceed to finalize_run 'complete' with an un-recorded
    stage.
    Finding 4: worktree merge happens BEFORE finalize_run so a merge failure
    can downgrade finalize_run to 'surfaced' truthfully (previously the run
    was finalized 'complete' and only the cursor reflected the merge failure).
    F30: abort/error reason is included in summary_md (FinalizeRunSchema strips
    standalone `reason` / `project_path` keys).

    Round 6 gap fix: ``workflow_terminal=True`` means the caller already
    determined -- from the authoritative HydraState checkpoint -- that this
    workflow has a durable ``terminal_resolution``. ``finalize_stage`` above
    still runs unconditionally (pp ledger bookkeeping for the already-
    incurred attempt/verdict, exactly once); the merge-back below is what
    gets refused: it is the one side effect that would actually CONTINUE a
    terminal workflow (landing code in the target repo). The branch is
    preserved (committed, never merged) for manual operator pickup instead.
    """
    cm = dispatcher.call_mcp
    stage_id = cursor["stage_id"]
    run_id = cursor["run_id"]
    attempt_id = cursor.get("attempt_id")

    # F26+M8: capture finalize_stage success; failure on pass → downgrade.
    # LV-1: _raise_on_error_payload converts error dicts into a RuntimeError
    # (PPLedgerError) so the existing except fires for payload-level
    # rejections/failures too -- and it preserves the original dict as
    # ``.payload``, which is what venom-gate classification downstream reads
    # to tell a structural rejection from a transport-shaped failure.
    _finalize_stage_ok = True
    try:
        _raise_on_error_payload(
            cm("pp_harness", "finalize_stage", {
                "stage_id": stage_id,
                "status": "passed" if passed else "surfaced",
                **({"winner_attempt_id": attempt_id} if (passed and attempt_id) else {}),
            }, squad_id=_SQ),
            "finalize_stage",
        )
    except Exception:  # noqa: BLE001
        _finalize_stage_ok = False
    if passed and not _finalize_stage_ok:
        passed = False
        cursor["outcome"] = "surfaced"
        cursor["error"] = (cursor.get("error") or "") + \
            " finalize_stage RPC failed; stage downgraded to surfaced"

    # Finding 4: merge the worktree BEFORE calling finalize_run so a merge
    # failure can truthfully downgrade the run to 'surfaced'.  Previously the
    # order was finalize_run(complete) → merge → cursor surfaced, which left the
    # pp ledger claiming 'complete' while no code actually landed.
    worktree_path = cursor.get("worktree_path")
    repo_root = cursor.get("repo_root")
    branch = cursor.get("branch")
    if worktree_path and repo_root and branch:
        if passed and not workflow_terminal:
            merge = _merge_worktree_back(repo_root, worktree_path, branch)
            cursor["merge"] = merge
            if not merge.get("merged"):
                # Merge failed — surface the run so the operator knows code
                # did not land, and pass that truth to finalize_run below.
                # NEW: downgrade cursor['outcome'] so step_result / summary
                # report 'pass_unlanded' rather than 'pass' on a surfaced run.
                passed = False
                cursor["outcome"] = "pass_unlanded"
                cursor["error"] = (cursor.get("error") or "") + \
                    f" merge-back failed: {merge.get('error')}"
                # MU12: _merge_worktree_back already committed any uncommitted
                # work to the branch before attempting the merge — advertise the
                # branch so the operator can pick it up without a new commit.
                cursor["preserved_branch"] = branch
                _trace(cursor, "attended.preserved",
                       {"branch": branch, "run_id": run_id,
                        "via": "merge_helper_commit"})
        else:
            # Round 6 gap fix: a workflow-terminal finalize is refused the
            # merge unconditionally, even though this attempt/verdict itself
            # PASSED -- landing code now would continue a workflow the
            # operator already aborted/rejected elsewhere. Reported error is
            # distinct from the ordinary "never even attempted a merge"
            # non-complete case so the operator can tell the two apart.
            if workflow_terminal:
                cursor["merge"] = {"merged": False, "error": "workflow_terminal"}
                if passed:
                    passed = False
                    cursor["outcome"] = "workflow_terminal"
            else:
                cursor["merge"] = {"merged": False, "error": "discarded_non_complete"}
            # MU12: commit any engineer changes to the attended branch BEFORE
            # removing the worktree so the operator can pick them up.  The
            # complete path is handled by _merge_worktree_back above; this
            # preserves work on non-complete outcomes (smoke-fail, judge-fail,
            # generate-fail, or a workflow-terminal refusal).
            _preserve_non_complete_work(
                cursor, worktree_path, branch, run_id,
                final_status=("workflow_terminal" if workflow_terminal else "surfaced"))
            # Hydra#71: the branch commit above never carries the excluded
            # build/log/.harness byproducts (see _BYPRODUCT_PATTERNS) — copy
            # them out before the worktree directory is deleted below, or
            # this generate/judge/smoke-fail's only on-disk evidence is lost.
            _preserve_worktree_evidence(cursor, worktree_path, run_id)
        _remove_worktree(repo_root, worktree_path)

    # F30: build summary_md that embeds any error/abort reason.
    if gen_failed:
        summary = f"Attended drive: generate failed -- {cursor.get('error')}"
    elif passed:
        summary = (f"Attended drive: stage_outcome=pass; "
                   f"smoke={cursor.get('smoke_status')}.")
    else:
        extra = f" :: {cursor['error']}" if cursor.get("error") else ""
        summary = (f"Attended drive: stage_outcome={cursor.get('outcome')}; "
                   f"smoke={cursor.get('smoke_status')}{extra}.")

    fin = cm("pp_harness", "finalize_run", {
        "run_id": run_id,
        "status": "complete" if passed else "surfaced",
        "summary_md": summary,
    }, squad_id=_SQ)
    fin_inner = _pp_inner(fin)
    fin_status = fin_inner.get("effective_status") or fin_inner.get("status")
    fin_downgraded = bool(fin_inner.get("downgraded"))
    if passed and _pp_ok(fin) and not fin_downgraded \
            and fin_status not in {"surfaced", "failed", "aborted", "blocked"}:
        cursor["final_status"] = "complete"
        cursor["state"] = "complete"
    else:
        cursor["final_status"] = "surfaced"
        cursor["state"] = "surfaced"

    cursor["pending_action"] = None
    cursor["finalized"] = True
    # Marker 2: clear the run-scoped sentinel now that the stage is terminal —
    # a later, unrelated session must not inherit a stale bypass.
    _clear_stage_active_sentinel(cursor.get("project_root") or cursor["project_path"])
    # Rider (b): initialise the charged flag to False. _cmd_attended_submit sets
    # it to True after the first budget charge so retried submit calls don't
    # double-charge (the already_charged field in _step_result exposes this flag).
    cursor.setdefault("charged", False)
    _trace(cursor, "attended.finalized", {
        "stage_id": stage_id, "final_status": cursor["final_status"],
        "smoke_status": cursor.get("smoke_status"), "cost_usd": cursor.get("cost_usd"),
        "merged": (cursor.get("merge") or {}).get("merged"),
    })


def _field_required_marker(field: Any) -> str:
    """Return "required" / "optional" for a pydantic v2 ``FieldInfo``."""
    try:
        return "required" if field.is_required() else "optional"
    except Exception:  # noqa: BLE001 — defensive; never let doc-gen crash a prompt
        return "optional"


def _field_type_label(field: Any) -> str:
    """Render a `FieldInfo.annotation` as a readable type label.

    A parameterised generic (e.g. `list[PlanStep]`) has `__name__ == "list"`
    on the CPython versions this repo supports -- using it unguarded (as an
    earlier revision did) silently drops the type argument for every such
    field, `steps` included. `typing.get_args`/`get_origin` recover the
    argument(s) so `steps` renders as `list[PlanStep]` from the schema
    itself, the same as every other field, rather than needing a
    hand-written special case.
    """
    import typing

    ann = getattr(field, "annotation", None)
    origin = typing.get_origin(ann)
    if origin is not None:
        args = typing.get_args(ann)
        if args:
            arg_labels = ", ".join(getattr(a, "__name__", str(a)) for a in args)
            origin_label = getattr(origin, "__name__", str(origin))
            return f"{origin_label}[{arg_labels}]"
    label = getattr(ann, "__name__", None)
    return label or str(ann)


def _plan_envelope_schema_doc() -> str:
    """D1: render the ``## Required output: PLAN envelope`` prompt section
    straight from the live pydantic models (`hydra_core.schemas.Plan` /
    `PlanStep`) so the plan-author prompt can never drift from the schema the
    validator actually enforces -- the root cause of "every first draft was
    rejected" (authors emitted step fields id/title/success that don't exist
    on `PlanStep`).

    Uses ``model_fields`` (schema introspection), never a hand-copied field
    list.
    """
    from . import schemas as _schemas

    plan_fields = _schemas.Plan.model_fields
    step_fields = _schemas.PlanStep.model_fields
    allowed_types = sorted(_schemas.SCHEMA_REGISTRY.keys())

    lines = ["## Required output: PLAN envelope", ""]
    lines.append(
        "Return exactly one PLAN envelope (type=\"PLAN\") inside the submit "
        "result's `emitted_envelopes` list. The field list below is generated "
        "at runtime from `hydra_core.schemas.Plan` / `PlanStep` -- it cannot "
        "drift from what the validator accepts."
    )
    lines.append("")
    lines.append("### Plan fields (including inherited envelope fields)")
    for name, field in plan_fields.items():
        marker = _field_required_marker(field)
        suffix = (
            " -- see \"PlanStep fields\" below" if name == "steps" else ""
        )
        lines.append(f"- `{name}` ({_field_type_label(field)}, {marker}){suffix}")
    lines.append("")
    lines.append("### PlanStep fields (each entry in `steps`)")
    for name, field in step_fields.items():
        lines.append(
            f"- `{name}` ({_field_type_label(field)}, {_field_required_marker(field)})"
        )
    lines.append("")
    lines.append(
        "### Allowed PlanStep.envelope_type values\n"
        + ", ".join(allowed_types)
    )
    lines.append("")
    # Hydra#69 round 5 defect 5 (LOW): the field-list rendering above is
    # useful prose, but it is a SUMMARY derived from `model_fields` -- it
    # drops constraints (min/max length, enum bounds, `$defs` nesting) that
    # only the generated JSON Schema actually carries. Emit
    # `Plan.model_json_schema()` verbatim (compact JSON, no manual field
    # re-description) alongside the prose list so an author has both a
    # human-readable summary AND the exact machine contract the validator
    # enforces, in the SAME generated-from-the-model fashion as the summary
    # above -- never a hand-copied schema that could drift from
    # `hydra_core.schemas.Plan`.
    plan_schema = _schemas.Plan.model_json_schema()
    lines.append("### Plan JSON Schema (generated from hydra_core.schemas.Plan)")
    # Static schema data (no operator-controlled floats can reach this), but
    # `dumps_strict` is still the right choice over a bare `json.dumps`: it
    # refuses a non-finite float / circular reference outright rather than
    # emitting invalid RFC 8259 JSON, and keeps this call inside the same
    # enforced boundary every other serialization site in this module uses
    # (see `tests/test_json_dumps_enforcement.py`). `sort_keys=True` makes
    # the embedded schema deterministic across pydantic dict-ordering
    # variance, not just compact.
    lines.append(dumps_strict(
        plan_schema, label="plan_schema", separators=(",", ":"), sort_keys=True,
    ))
    return "\n".join(lines)


def _build_squad_prompt(
    *,
    workflow_id: str,
    task_id: str,
    squad_slug: str,
    request_text: str,
    goal: str | None = None,
    envelope_id: str | None = None,
    upstream_refs: Sequence[str] | None = None,
    budget_usd: float | None = None,
    budget_remaining_usd: float | None = None,
    risk: str | None = None,
    priority: str | None = None,
    acceptance_criteria: Sequence[str] | None = None,
    plan_revision: int | None = None,
    plan_critique: str | None = None,
    supersedes_plan_envelope_id: str | None = None,
) -> str:
    """E2-28: build the non-engineering squad host_action prompt.

    Mirrors the engineering path's ``## Hydra context`` block so a squad leg is
    reproducible from the trace alone: workflow/task identity, the root goal,
    the task description, and the governing constraints.

    ``upstream_refs`` carries MemoryRef handles / envelope ids of prior completed
    work ONLY — never raw upstream artifact content, which must not cross a squad
    boundary un-redacted (AGENTS.md hard rule 3).

    D1 (Hydra#69 part 3): ``plan_revision`` / ``plan_critique`` /
    ``supersedes_plan_envelope_id`` are ONLY consumed when
    ``squad_slug == "planning"`` -- every other squad's prompt is byte-for-byte
    unchanged by their presence (they default to ``None`` and are simply never
    read outside the planning branch below).
    """
    _none = "(none)"
    refs = [str(r).strip() for r in (upstream_refs or []) if str(r).strip()]
    crit = [str(c).strip() for c in (acceptance_criteria or []) if str(c).strip()]

    if budget_usd is None and budget_remaining_usd is None:
        budget_line = "unknown"
    elif budget_usd is None:
        budget_line = f"{budget_remaining_usd:.2f}"
    elif budget_remaining_usd is None:
        budget_line = f"of {budget_usd:.2f} total"
    else:
        budget_line = f"{budget_remaining_usd:.2f} remaining of {budget_usd:.2f} total"

    lines = [
        "## Hydra context",
        f"workflow_id: {workflow_id}",
        f"task_id: {task_id}",
        f"squad: {squad_slug}",
        f"envelope_id: {envelope_id or _none}",
        f"upstream_refs: {', '.join(refs) if refs else _none}",
        "",
        "## Goal",
        (goal or "").strip() or _none,
        "",
        "## Task",
        (request_text or "").strip() or _none,
        "",
        "## Constraints",
        (f"budget_usd: {budget_line}, risk: {risk or 'unknown'}, "
         f"priority: {priority or 'unknown'}"),
        "acceptance_criteria: " + (_none if not crit else ""),
    ]
    lines.extend(f"- {c}" for c in crit)

    # D1 (Hydra#69 part 3): planning-only section, schema-generated.
    if squad_slug == "planning":
        lines.append("")
        lines.append(_plan_envelope_schema_doc())
        lines.append("")
        _expected_revision = int(plan_revision) if plan_revision else 1
        lines.append(f"expected plan_revision: {_expected_revision}")
        if _expected_revision > 1:
            lines.append(
                f"supersedes: {supersedes_plan_envelope_id or _none} "
                "(the prior plan envelope id -- set `Plan.supersedes` to this value)"
            )
            lines.append("")
            lines.append("### Prior revision critique")
            lines.append((plan_critique or "").strip() or _none)

    return "\n".join(lines)


def begin_squad_stage(
    *,
    workflow_id: str,
    task_id: str,
    squad_slug: str,
    entrypoint: str,
    lead_agent: str,
    pack_cwd: str,
    request_text: str,
    project_root: str | Path,
    goal: str | None = None,
    envelope_id: str | None = None,
    upstream_refs: Sequence[str] | None = None,
    budget_usd: float | None = None,
    budget_remaining_usd: float | None = None,
    risk: str | None = None,
    priority: str | None = None,
    acceptance_criteria: Sequence[str] | None = None,
    action_extras: dict[str, Any] | None = None,
    attempt: int = 0,
    plan_revision: int | None = None,
    plan_critique: str | None = None,
    supersedes_plan_envelope_id: str | None = None,
) -> dict[str, Any]:
    """Create a lightweight cursor for an attended non-engineering squad task
    (claude-skill or agent-impersonation entrypoint).

    No pp stage is opened; no worktree isolation is needed (these squads produce
    documents, not engine code). The cursor lives at
    ``cursor_path(project_root, workflow_id, task_id)`` so the submit-host-result
    CLI can find it by passing ``--run-id <task_id>``.

    Returns an ``awaiting_host`` step result whose ``host_action`` tells the host
    to spawn the visible pack agent subagent in ``pack_cwd``.

    E2-28: ``host_action.prompt`` is the full context-bearing prompt built by
    ``_build_squad_prompt``; the bare planner task label stays available as
    ``host_action.task_description``.

    Hydra#69 defect C: ``attempt`` (default 0, unchanged for every existing
    caller) is folded into ``call_key`` so a re-issued cursor for the SAME
    task_id (e.g. a ``planning`` task whose PLAN was rejected and the task
    stays open) never reuses the prior attempt's call_key -- a late/duplicate
    submit under the stale key is refused by `submit_host_result`'s call_key
    match instead of silently matching the new cursor.
    """
    call_key = f"squad-{task_id}-{int(attempt)}"
    prompt = _build_squad_prompt(
        workflow_id=workflow_id,
        task_id=task_id,
        squad_slug=squad_slug,
        request_text=request_text,
        goal=goal,
        envelope_id=envelope_id,
        upstream_refs=upstream_refs,
        budget_usd=budget_usd,
        budget_remaining_usd=budget_remaining_usd,
        risk=risk,
        priority=priority,
        acceptance_criteria=acceptance_criteria,
        plan_revision=plan_revision,
        plan_critique=plan_critique,
        supersedes_plan_envelope_id=supersedes_plan_envelope_id,
    )
    cursor: dict[str, Any] = {
        "schema": CURSOR_SCHEMA,
        "kind": "squad",
        "workflow_id": workflow_id,
        "task_id": task_id,
        "run_id": task_id,   # mirrors engineering cursor shape for CLI compatibility
        "squad_slug": squad_slug,
        "entrypoint": entrypoint,
        "project_path": pack_cwd,
        "request_text": request_text,
        "state": "await_squad_agent",
        "cost_usd": 0.0,
        "tokens_in": 0,
        "tokens_out": 0,
        "final_status": None,
        "error": None,
        "finalized": False,
        "attempt": int(attempt),
        "pending_action": {
            "call_key": call_key,
            "agent_type": lead_agent,
            "cwd": pack_cwd,
            "prompt": prompt,
            "task_description": request_text,
            "instructions": "Run the pack agent and submit the artifact.",
        },
    }
    # E2-31: claude-skill packs that are not plugin-loadable carry the pack's
    # slash command + MCP tool scope so the host can drive them via
    # general-purpose; `lead_agent_file` is informational.
    if action_extras:
        cursor["pending_action"].update(
            {k: v for k, v in action_extras.items() if k != "agent_type"})
    cfile = cursor_path(project_root, workflow_id, task_id)
    save_cursor(cfile, cursor)
    _trace(cursor, "attended.squad_stage_started", {
        "squad_slug": squad_slug,
        "entrypoint": entrypoint,
        "task_id": task_id,
    })
    return _step_result(cursor, cfile)


def _has_native_pack(slug: str) -> bool:
    """True when the squad owns a registered Claude Code plugin pack."""
    if not slug:
        return False
    try:
        from .native_packs import native_pack
        native_pack(slug)
        return True
    except Exception:  # noqa: BLE001 — unregistered slug or registry error
        return False


def _persist_attended_squad_artifact(
    dispatcher: Dispatcher,
    cursor: dict[str, Any],
    *,
    cursor_file: str | Path,
) -> tuple[dict[str, Any] | None, str | None, list[str]]:
    """Persist a non-native squad's attended artifact (E2-35).

    The CLI persists native-pack results through the pack's declared output
    root; a squad with no ``NATIVE_PACKS`` entry (customer-support via the
    xenia shim, for instance) had NO persist path at all, so its result came
    back ``complete`` with an ``artifact_persist_error`` and no ``artifact_ref``
    and its work never reached memory or synthesis.

    Ladder, both best-effort:

    1. the generic attended store (``<project>/.hydra/<wf>/attended/artifacts``),
       which needs no MCP round-trip and is therefore the reliable leg;
    2. when the squad has a claude-skill shim, ``<prefix>.output.write`` as
       well, so the pack's own on-disk store stays in sync.

    Returns ``(artifact_ref | None, via | None, errors)``. The caller downgrades
    the outcome to ``complete_unpersisted`` only when BOTH legs failed.
    """
    text = str(cursor.get("artifact_text") or "")
    slug = str(cursor.get("squad_slug") or "")
    wf = str(cursor.get("workflow_id") or "")
    raw_task = str(cursor.get("task_id") or cursor.get("run_id") or "task")
    task_id = "".join(c for c in raw_task if c.isalnum() or c in "-_") or "task"
    errors: list[str] = []
    via: list[str] = []
    ref: dict[str, Any] | None = None

    try:
        from .artifact_store import write_attended_artifact
        # `cursor["project_path"]` is the PACK cwd for squad cursors, not the
        # Hydra project root, so derive the root from the cursor sidecar path
        # (`<project>/.hydra/<wf>/attended/<run>.json`) instead.
        project_root = Path(cursor_file).resolve().parents[3]
        mref = write_attended_artifact(project_root, wf, f"{task_id}.md", text)
        ref = mref.model_dump(mode="json")
        via.append("generic")
    except Exception as exc:  # noqa: BLE001 — fail-soft; the shim leg may still land
        errors.append(f"generic store: {exc}")

    shim = _resolve_skill_shim(slug) if slug else None
    if shim:
        tool = f"{shim['prefix']}.output.write"
        try:
            args: dict[str, Any] = {
                shim["path_key"]: "attended",
                "topic": f"attended {raw_task}"[:80],
                "content": text,
            }
            if shim["server"] == "rlm_creative":
                args.update({"domain": "creative", "scopes": ["team:garland-crew"]})
            resp = dispatcher.call_mcp(shim["server"], tool, args, squad_id=slug)
            _raise_on_error_payload(resp, tool)
            via.append("shim")
            rel = resp.get("relative") if isinstance(resp, dict) else None
            if ref is None and rel:
                from .schemas import MemoryRef
                ref = MemoryRef(
                    tier="episodic",
                    key=f"{shim['prefix']}:output:{rel}",
                    summary=str(rel),
                ).model_dump(mode="json")
        except Exception as exc:  # noqa: BLE001 — pack store is best-effort
            errors.append(f"{tool}: {exc}")

    return ref, ("+".join(via) or None), errors


def _apply_squad_result(
    dispatcher: Dispatcher,
    cursor: dict[str, Any],
    result: dict[str, Any],
    *,
    cursor_file: str | Path,
) -> None:
    """await_squad_agent → terminal.

    Accumulate spend from the host agent's result and mark the cursor complete.
    There are no pp ledger calls (this is not an engineering stage). The CLI
    (``_cmd_attended_submit``) charges the returned cost_usd through the
    authoritative HydraState budget path after this returns.
    """
    # B8: same treat-missing-cost-as-priceable-or-unmeasured policy as the
    # engineering generate/judge accruals above.
    _squad_cost, _squad_cost_source = _priced_cost(cursor, result, label="squad_result")
    cursor["cost_usd"] = float(cursor.get("cost_usd") or 0.0) + _squad_cost
    cursor["tokens_in"] = (int(cursor.get("tokens_in") or 0)
                           + coerce_untrusted_count(result.get("tokens_in")))
    cursor["tokens_out"] = (int(cursor.get("tokens_out") or 0)
                            + coerce_untrusted_count(result.get("tokens_out")))
    cursor["artifact_text"] = str(result.get("text") or result.get("artifact") or "")
    # Native pack results may delegate typed work to another squad.  Keep the
    # raw list in the cursor so the CLI can validate/redact/ingest it under the
    # workflow lock; never silently discard a DEV_TASK or CREATIVE_BRIEF.
    emitted = result.get("emitted_envelopes", result.get("envelopes", []))
    cursor["emitted_envelopes"] = emitted if isinstance(emitted, list) else []

    # E2-35: a squad with no native pack still gets its artifact persisted --
    # generically, and (when it has one) through its claude-skill shim too.
    # Native packs are left to the CLI's `write_native_artifact` path, which
    # already returns a `<plugin>:output:...` ref for them.
    slug = str(cursor.get("squad_slug") or "")
    final_status = "complete"
    if cursor["artifact_text"] and not _has_native_pack(slug):
        ref, via, errors = _persist_attended_squad_artifact(
            dispatcher, cursor, cursor_file=cursor_file)
        if ref is not None:
            cursor["artifact_ref"] = ref
            cursor["artifact_persisted_via"] = via
            _trace(cursor, "attended.skill_artifact_persisted", {
                "squad": slug, "memory_ref": ref.get("key"), "via": via,
            })
            if errors:
                # One leg landed; keep the other's failure visible without
                # downgrading a result that IS durably persisted.
                cursor["artifact_persist_warning"] = "; ".join(errors)
        else:
            final_status = "complete_unpersisted"
            cursor["artifact_persist_error"] = (
                "; ".join(errors) or "no attended artifact persist path succeeded")
            _trace(cursor, "attended.skill_artifact_persist_failed", {
                "squad": slug, "error": cursor["artifact_persist_error"],
            })

    cursor["final_status"] = final_status
    cursor["state"] = final_status
    cursor["pending_action"] = None
    cursor["finalized"] = True
    _trace(cursor, "attended.squad_result_applied", {
        "task_id": cursor.get("task_id"),
        "squad_slug": cursor.get("squad_slug"),
        "cost_usd": cursor.get("cost_usd"),
        "final_status": final_status,
        "emitted_envelope_count": len(cursor["emitted_envelopes"]),
    })


def recover_stalled_stage(dispatcher: Dispatcher, *,
                          cursor_file: str | Path,
                          workflow_terminal: bool = False) -> dict[str, Any]:
    """W2-4: sanctioned recovery for an engineering stage stranded by a
    transport-shaped pp-ledger failure.

    Reachable ONLY via ``hydra resume --action recover-stalled-stage`` — never
    a parallel CLI verb. Governance is explicit that a paused/stranded
    workflow resumes only through approve/resume, so this is exposed as a
    resume action rather than a standalone command (see ``_cmd_resume_locked``
    in cli.py).

    Handles two cursor shapes:

    - ``state == "stalled_infra"`` (the W2-3 hold): the isolated worktree is
      still on disk and the pp attempt is still open. Only ``record_verdict``
      was skipped; everything downstream (smoke, merge, finalize) reuses the
      existing ``_finalize`` machinery unchanged, so recovery for this shape
      exercises the SAME code path a normal pass finalize does.
    - ``state == "surfaced"`` (an older cursor stranded by the pre-fix
      downgrade-then-finalize behavior): the worktree is already gone, but
      the branch that hosted the stage's work (``cursor["branch"]``, set once
      at worktree creation and never cleared) still carries every commit the
      engineer made. If there was ALSO uncommitted work when the worktree was
      torn down, ``_preserve_non_complete_work`` committed it and recorded
      ``cursor["preserved_branch"]``; that is preferred when present. When
      the engineer committed everything cleanly, ``preserved_branch`` is
      never set (there was nothing uncommitted to preserve) even though the
      branch is just as recoverable, so recovery falls back to
      ``cursor["branch"]`` after confirming it still exists in the repo --
      refusing recovery in that case would invert the incentive (the tidier
      the engineer, the less recoverable the stage). Recovery re-issues
      record_verdict, merges the resolved branch directly via
      ``_merge_branch_back``, and (best-effort) re-finalizes. Which branch
      was used, and whether it came from ``preserved_branch`` or the
      fallback, is recorded on the cursor (``recovery_branch``,
      ``recovery_branch_source``) and in the trace
      (``attended.recovery.branch_resolved``) so an operator can tell what
      was actually merged.

    Idempotent: replays ``record_verdict`` with the payload's
    ``idempotency_token`` (== the original judge call_key), so pp returns the
    already-recorded verdict_id on a repeat call instead of a duplicate row.
    Never double-charges: this function does not touch budget at all — the
    caller reads ``already_charged`` off the returned step result exactly as
    ``submit_host_result`` callers do and only calls ``charge_and_gate`` /
    ``mark_charged`` when it is False, so a stage that was already charged (the
    only way that can happen for the pre-fix "surfaced" shape, since its
    original submit charged on the downgraded outcome before this fix existed)
    is never charged a second time.

    Round 6 gap fix: ``workflow_terminal=True`` means the caller already
    determined -- from the authoritative HydraState checkpoint -- that this
    workflow has a durable ``terminal_resolution``. Recovery MUST still be
    able to reconcile the pp ledger for cost/verdict bookkeeping that already
    happened (exactly once, same as ``submit_host_result``), but it must
    never CONTINUE a terminal workflow by landing code: for the
    ``stalled_infra`` shape this threads straight into ``_finalize``, which
    already refuses the worktree merge-back when ``workflow_terminal`` is
    set; for the ``surfaced`` shape it refuses to call ``_merge_branch_back``
    at all (the merge itself is the one side effect that lands preserved
    work into the target repo) and instead reports
    ``merge={"merged": False, "error": "workflow_terminal"}`` while the
    branch stays preserved (untouched, uncommitted-nothing-lost) for manual
    operator pickup, exactly like the existing "merge failed" branch already
    does for other merge refusals.
    """
    cm = dispatcher.call_mcp
    cursor = load_cursor(cursor_file)
    if cursor.get("kind") not in (None, "engineering"):
        return {"ok": False, "error": "recovery only supports engineering stage cursors"}
    state = cursor.get("state")
    if state == "await_smoke":
        # Hydra#70: a prior recovery call already started the async smoke
        # job (or `_apply_judge` did, on the normal path). Re-invoking
        # recovery here is a POLL, not a fresh recovery attempt.
        poll_smoke_job(dispatcher, cursor, cursor_file=cursor_file,
                      workflow_terminal=workflow_terminal)
        save_cursor(cursor_file, cursor)
        out = _step_result(cursor, cursor_file)
        out["ok"] = True
        return out
    if state not in ("stalled_infra", "surfaced"):
        return {"ok": False, "error": f"cursor state {state!r} is not recoverable"}

    # Resolve which branch to recover from. `preserved_branch` is set only by
    # `_preserve_non_complete_work`/the merge-failure path, which run when
    # there was UNCOMMITTED work to rescue -- an engineer who committed
    # everything cleanly leaves it unset even though `cursor["branch"]` (set
    # once at worktree creation and never cleared) still names a real branch
    # with every commit. Refusing recovery in that case inverts the
    # incentive (the tidier the engineer, the less recoverable the stage), so
    # fall back to `cursor["branch"]` -- but only after confirming the branch
    # actually exists; a stale/garbage-collected branch name must still
    # refuse cleanly rather than hand a bogus ref to git merge downstream.
    recovery_branch: str | None = None
    recovery_branch_source: str | None = None
    if state == "surfaced":
        preserved = cursor.get("preserved_branch")
        if preserved:
            recovery_branch = preserved
            recovery_branch_source = "preserved_branch"
        else:
            fallback = cursor.get("branch")
            fallback_repo_root = cursor.get("repo_root") or cursor.get("project_path")
            if fallback and fallback_repo_root:
                chk = _git(["rev-parse", "--verify", fallback], fallback_repo_root)
                if chk.returncode == 0:
                    recovery_branch = fallback
                    recovery_branch_source = "branch_fallback"
        if not recovery_branch:
            return {"ok": False, "error": (
                "surfaced cursor has no preserved_branch and no recoverable "
                "branch (cursor['branch'] is absent or no longer exists in "
                "the repo) to recover from")}
        cursor["recovery_branch"] = recovery_branch
        cursor["recovery_branch_source"] = recovery_branch_source
        _trace(cursor, "attended.recovery.branch_resolved", {
            "branch": recovery_branch, "source": recovery_branch_source,
        })

    stage_id = cursor.get("stage_id")
    attempt_id = cursor.get("attempt_id")
    payload = cursor.get("pending_verdict_payload")

    # Step 1: re-issue record_verdict if it was never recorded. Idempotent via
    # the payload's idempotency_token (see the comment where it is built).
    if not cursor.get("verdict_recorded_for"):
        if not (payload and attempt_id):
            return {"ok": False, "error": (
                "no pending_verdict_payload captured on this cursor -- cannot "
                "safely reconstruct the verdict. This cursor predates the "
                "W2-4 payload capture; it needs a manual pp-side replay.")}
        try:
            _raise_on_error_payload(
                cm("pp_harness", "record_verdict", payload, squad_id=_SQ),
                "record_verdict",
            )
        except Exception as exc:  # noqa: BLE001
            _trace(cursor, "attended.recovery.verdict_failed", {
                "stage_id": stage_id, "attempt_id": attempt_id, "reason": str(exc),
            })
            return {"ok": False, "error": f"record_verdict recovery failed: {exc}"}
        cursor["verdict_recorded_for"] = payload.get("idempotency_token") or "recovery"
        _trace(cursor, "attended.recovery.verdict_recorded", {
            "stage_id": stage_id, "attempt_id": attempt_id,
        })
        save_cursor(cursor_file, cursor)

    outcome = (payload.get("outcome") if payload else None) or cursor.get("outcome")

    if state == "stalled_infra":
        # The worktree + pp attempt are exactly as they were when the stage
        # stalled -- everything past record_verdict is the SAME code the
        # normal (non-stranded) path runs, so reuse it verbatim instead of
        # re-implementing smoke/merge/finalize here.
        #
        # Hydra#70: the verdict was JUST (re-)recorded above in this same
        # call, so route the smoke through the same detached-job mechanism
        # `_apply_judge` uses -- recovery is itself invoked through the CLI
        # under its own bounded timeout (`_cmd_resume_locked`), so blocking
        # here on a long smoke reproduces the exact orphaned-tree bug this
        # fix closes. `sync` mode (tests / a bespoke driver) still runs
        # inline for a prompt recovery result.
        if outcome == "pass" and attempt_id and _attended_smoke_mode() != "sync":
            work_path = cursor.get("work_path") or cursor["project_path"]
            from . import smoke_job as _smoke_job
            _recovery_call_key = (cursor.get("pending_action") or {}).get(
                "call_key") or f"recovery-{stage_id}"
            job = _smoke_job.start_job(
                cursor_file, project_path=work_path, stage_id=stage_id,
                call_key=_recovery_call_key)
            cursor["smoke_job"] = job
            cursor["state"] = "await_smoke"
            cursor["pending_action"] = {
                "call_key": _recovery_call_key, "action": "poll_smoke",
                "poll": True,
                "instructions": (
                    "Recovery re-recorded the verdict and started the smoke "
                    "as a detached job. Poll via hydra.workflow.step "
                    "(or resubmit recover-stalled-stage) until it completes."
                ),
            }
            _trace(cursor, "attended.recovery.smoke_job_started", {
                "stage_id": stage_id, "pid": job.get("pid"),
            })
            save_cursor(cursor_file, cursor)
            out = _step_result(cursor, cursor_file)
            out["ok"] = True
            return out
        passed = False
        if outcome == "pass" and attempt_id:
            work_path = cursor.get("work_path") or cursor["project_path"]
            smoke_status, smoke_reason = _run_smoke(
                dispatcher, project_path=work_path, stage_id=stage_id)
            cursor["smoke_status"] = smoke_status
            cursor["smoke_reason"] = smoke_reason
            try:
                _raise_on_error_payload(cm("pp_harness", "record_smoke_status", {
                    "stage_id": stage_id, "candidate_index": 1,
                    "status": smoke_status,
                    "reason": (smoke_reason or "recovery smoke")[:300],
                }, squad_id=_SQ), "record_smoke_status")
            except Exception:  # noqa: BLE001
                pass
            passed = smoke_status == "pass"
        _trace(cursor, "attended.recovery.resuming_finalize", {
            "stage_id": stage_id, "outcome": outcome, "passed": passed,
            "workflow_terminal": workflow_terminal,
        })
        _finalize(dispatcher, cursor, passed=passed, gen_failed=False,
                 workflow_terminal=workflow_terminal)
        save_cursor(cursor_file, cursor)
        out = _step_result(cursor, cursor_file)
        out["ok"] = True
        if workflow_terminal:
            out["workflow_terminal"] = True
        return out

    # state == "surfaced": worktree is gone; merge directly from the
    # resolved branch (preserved_branch, or the branch_fallback resolved
    # above), then best-effort re-finalize.
    repo_root = cursor.get("repo_root") or cursor.get("project_path")
    branch = recovery_branch
    if workflow_terminal:
        # Round 6 gap fix: never call `_merge_branch_back` once the workflow
        # is terminal -- that call is the one side effect here that would
        # actually land preserved work into `repo_root`. The branch (already
        # resolved above, either `preserved_branch` or the existing
        # `cursor["branch"]`) stays exactly as it was -- nothing further to
        # preserve, since recovery never touched it -- for manual operator
        # pickup instead.
        merge = {"merged": False, "error": "workflow_terminal"}
    else:
        merge = _merge_branch_back(repo_root, branch)
    cursor["merge"] = merge
    _trace(cursor, "attended.recovery.merge", {
        "stage_id": stage_id, "branch": branch, "merged": merge.get("merged"),
        "error": merge.get("error"), "workflow_terminal": workflow_terminal,
    })
    if not merge.get("merged"):
        save_cursor(cursor_file, cursor)
        out = _step_result(cursor, cursor_file)
        out["ok"] = False
        if workflow_terminal:
            out["ok"] = True
            out["workflow_terminal"] = True
            out["error"] = (
                "recovery refused to merge: workflow is terminal "
                f"(branch {branch!r} preserved in {repo_root} for manual "
                "operator pickup)"
            )
        elif merge.get("error") == "already_merged":
            # State-shaped, not failure-shaped: the branch's work is
            # ALREADY present in repo_root (git reported "Already up to
            # date." -- no new commit was needed or created). That is not
            # "recovery failed to land the work"; it is "there was nothing
            # left for recovery to land". Still ok=False -- recovery itself
            # did not run smoke/re-finalize here, so the caller must not
            # treat this as a completed pass -- but the wording must not
            # read as "work missing" when the opposite is true.
            out["error"] = (
                "recovery found no merge to perform: the branch's work is "
                "already present in repo_root (already_merged) -- no new "
                "merge commit was needed or created"
            )
        else:
            out["error"] = f"recovery merge failed: {merge.get('error')}"
        return out

    if outcome == "pass" and cursor.get("smoke_status") not in ("pass", "fail"):
        smoke_status, smoke_reason = _run_smoke(
            dispatcher, project_path=repo_root, stage_id=stage_id)
        cursor["smoke_status"] = smoke_status
        cursor["smoke_reason"] = smoke_reason
        try:
            _raise_on_error_payload(cm("pp_harness", "record_smoke_status", {
                "stage_id": stage_id, "candidate_index": 1,
                "status": smoke_status,
                "reason": (smoke_reason or "recovery smoke (post-merge)")[:300],
            }, squad_id=_SQ), "record_smoke_status")
        except Exception:  # noqa: BLE001
            pass

    # The original (pre-fix) submit already called finalize_stage/finalize_run
    # with status="surfaced" once, leaving the pp run-level record permanently
    # "surfaced" even though this recovery may now find the stage passing.
    # Re-finalizing must be explicit and best-effort: report the outcome
    # honestly rather than silently claiming "complete" on the cursor while
    # pp's ledger still disagrees. finalizeRun in pp's daemon
    # (daemon/src/orchestrator/runs.ts) has no already-finalized guard -- it
    # unconditionally re-runs the full finalize procedure (gates, DB write,
    # master-plan patch) against whatever the stage rows say right now, so a
    # second call is safe and is exactly what reconciles the two ledgers.
    passed = outcome == "pass" and cursor.get("smoke_status") == "pass"

    # The merge above ran before the outcome was known (justified: in this
    # legacy "surfaced" shape the worktree is already gone, so repo_root is
    # the only place smoke can inspect the code). Now that the outcome IS
    # known, a non-passing recovery must not silently retain the merged
    # code -- that is the exact divergence class (repo has code no system of
    # record acknowledges) this workstream exists to close, in the more
    # dangerous direction of failing code landing quietly. Revert the merge
    # commit this recovery itself created; if the revert itself fails, make
    # the landed-but-unacknowledged state unmissable instead of pretending a
    # clean revert happened.
    revert: dict[str, Any] | None = None
    if not passed and merge.get("merged") and merge.get("sha"):
        revert = _revert_merge_commit(
            repo_root, merge["sha"], expected_base=merge.get("base"))
        cursor["merge"]["reverted"] = bool(revert.get("reverted"))
        if revert.get("reverted"):
            cursor["merge"]["revert_sha"] = revert.get("sha")
        else:
            cursor["merge"]["revert_error"] = revert.get("error")
            cursor["merge"]["abort_failed"] = bool(revert.get("abort_failed"))
            cursor["merge"]["abort_state"] = revert.get("abort_state")
            if revert.get("abort_state") == "unknown":
                cursor["error"] = (cursor.get("error") or "") + (
                    f"; recovery merge {merge['sha']} landed in {repo_root} on "
                    f"branch checked out there, outcome={outcome!r} "
                    f"smoke={cursor.get('smoke_status')!r} did not pass, the "
                    f"automatic revert failed, and repo_root's post-abort "
                    f"state could NOT be verified ({revert.get('error')}) -- "
                    "code is MERGED INTO THE REPO, UNACKNOWLEDGED by the pp "
                    "ledger, and whether repo_root is clean or mid-revert is "
                    "UNKNOWN (not confirmed clean); operator must inspect "
                    "repo_root's full state before any retry."
                )
            elif revert.get("abort_failed"):
                cursor["error"] = (cursor.get("error") or "") + (
                    f"; recovery merge {merge['sha']} landed in {repo_root} on "
                    f"branch checked out there, outcome={outcome!r} "
                    f"smoke={cursor.get('smoke_status')!r} did not pass, the "
                    f"automatic revert failed AND its abort also failed "
                    f"({revert.get('error')}) -- code is MERGED INTO THE REPO, "
                    "UNACKNOWLEDGED by the pp ledger, and repo_root is left "
                    "mid-revert (not cleanly restored); operator must inspect "
                    "repo_root's full state (conflicted index / half-applied "
                    "working tree) before any retry, not just revert manually."
                )
            else:
                cursor["error"] = (cursor.get("error") or "") + (
                    f"; recovery merge {merge['sha']} landed in {repo_root} on "
                    f"branch checked out there, but outcome={outcome!r} "
                    f"smoke={cursor.get('smoke_status')!r} did not pass and the "
                    f"automatic revert failed ({revert.get('error')}) -- code is "
                    "MERGED INTO THE REPO but UNACKNOWLEDGED by the pp ledger; "
                    "repo_root itself was cleanly restored (abort succeeded); "
                    "operator must inspect repo_root and revert manually."
                )
        _trace(cursor, "attended.recovery.merge_reverted", {
            "stage_id": stage_id, "branch": branch,
            "merge_sha": merge.get("sha"), "reverted": revert.get("reverted"),
            "revert_error": revert.get("error"),
            "abort_failed": bool(revert.get("abort_failed")),
            "abort_state": revert.get("abort_state"),
        })

    try:
        _raise_on_error_payload(cm("pp_harness", "finalize_stage", {
            "stage_id": stage_id,
            "status": "passed" if passed else "surfaced",
            **({"winner_attempt_id": attempt_id} if (passed and attempt_id) else {}),
        }, squad_id=_SQ), "finalize_stage")
        fin_stage_ok = True
    except Exception as exc:  # noqa: BLE001
        fin_stage_ok = False
        cursor["error"] = (cursor.get("error") or "") + f"; recovery finalize_stage failed: {exc}"
    if passed and not fin_stage_ok:
        passed = False

    # PP-VG-7 ordering: the stage row must already reflect "passed" (done
    # above) BEFORE finalize_run(complete) is requested, or pp's
    # surfaced-stages gate silently downgrades the run back to "surfaced" and
    # undoes this reconciliation. Replay finalize_run so the run-level record
    # matches reality instead of staying permanently stuck on the original
    # pre-fix "surfaced" write.
    summary = (f"Attended recovery: stage_outcome={outcome}; "
               f"smoke={cursor.get('smoke_status')}; "
               f"finalize_stage_ok={fin_stage_ok}.")
    if revert is not None:
        if revert.get("reverted"):
            summary += f" Merge {merge.get('sha')} reverted ({revert.get('sha')})."
        elif revert.get("abort_state") == "unknown":
            summary += (
                f" WARNING: merge {merge.get('sha')} landed in {repo_root}, the "
                f"automatic revert FAILED, and repo_root's post-abort state "
                f"could NOT be verified ({revert.get('error')}) -- code is "
                "merged, this stage did not pass, and whether repo_root is "
                "clean or mid-revert is UNKNOWN (not confirmed clean); "
                "operator must inspect repo_root's full state before any "
                "retry."
            )
        elif revert.get("abort_failed"):
            summary += (
                f" WARNING: merge {merge.get('sha')} landed in {repo_root}, the "
                f"automatic revert FAILED AND its abort also FAILED "
                f"({revert.get('error')}) -- code is merged, this stage did "
                "not pass, and repo_root is left mid-revert (conflicted "
                "index / half-applied working tree, not cleanly restored); "
                "operator must inspect repo_root's full state before any "
                "retry."
            )
        else:
            summary += (
                f" WARNING: merge {merge.get('sha')} landed in {repo_root} and "
                f"the automatic revert FAILED ({revert.get('error')}) -- code "
                "is merged but this stage did not pass; repo_root itself was "
                "cleanly restored (abort succeeded); operator must revert "
                "manually."
            )
    fin = cm("pp_harness", "finalize_run", {
        "run_id": cursor["run_id"],
        "status": "complete" if passed else "surfaced",
        "summary_md": summary,
    }, squad_id=_SQ)
    fin_inner = _pp_inner(fin)
    fin_status = fin_inner.get("effective_status") or fin_inner.get("status")
    fin_downgraded = bool(fin_inner.get("downgraded"))
    fin_run_ok = _pp_ok(fin)
    if not fin_run_ok:
        cursor["error"] = (cursor.get("error") or "") + \
            f"; recovery finalize_run did not report success: {fin!r}"

    # Never claim "complete" on the cursor unless pp's run-level record
    # actually agrees -- if finalize_run failed outright, or pp itself
    # downgraded (VG-7), or returned anything other than "complete", record
    # what pp actually holds instead of a divergent cursor claim. Mirrors
    # _finalize's downgrade-honouring check above.
    if passed and fin_run_ok and not fin_downgraded \
            and fin_status not in {"surfaced", "failed", "aborted", "blocked"}:
        cursor["final_status"] = "complete"
    else:
        cursor["final_status"] = "surfaced"
        if fin_downgraded:
            cursor["error"] = (cursor.get("error") or "") + \
                "; finalize_run downgraded complete->surfaced (PP-VG-7)"
    cursor["state"] = cursor["final_status"]
    cursor["pending_action"] = None
    cursor["finalized"] = True
    cursor.setdefault("charged", False)
    _trace(cursor, "attended.recovery.finalized", {
        "stage_id": stage_id, "final_status": cursor["final_status"],
        "merged": bool(merge.get("merged")),
        "merge_reverted": bool(revert.get("reverted")) if revert is not None else None,
        "finalize_stage_ok": fin_stage_ok,
        "finalize_run_ok": fin_run_ok, "finalize_run_status": fin_status,
        "finalize_run_downgraded": fin_downgraded,
    })
    save_cursor(cursor_file, cursor)
    out = _step_result(cursor, cursor_file)
    out["ok"] = True
    return out


def submit_host_result(
    dispatcher: Dispatcher,
    *,
    cursor_file: str | Path,
    call_key: str,
    result: dict[str, Any],
    workflow_terminal: bool = False,
) -> dict[str, Any]:
    """Feed a host subagent's result back in and advance the cursor by exactly
    one transition. Idempotent on a stale/duplicate ``call_key`` (returns the
    current step result without re-applying), so a retried submit never
    double-records in the pp ledger.

    Handles both ``kind="engineering"`` (the default pp stage flow) and the
    lightweight ``kind="squad"`` cursors created by ``begin_squad_stage`` for
    non-engineering tasks (claude-skill / agent-impersonation).

    ``workflow_terminal`` (round 6 gap fix): True when the CALLER already
    determined -- from the authoritative HydraState checkpoint, BEFORE this
    function runs -- that the workflow has a durable ``terminal_resolution``
    (see ``hydra_core.state.workflow_terminal_resolution``). Threaded through
    to ``_apply_generate``/``_apply_judge`` -> ``_finalize`` so a passing
    finalize on a stale cursor still records the already-incurred pp ledger
    attempt/verdict (real spend already happened) but refuses to merge the
    candidate worktree into the repo -- merging would continue a terminal
    workflow. Never itself re-reads the checkpoint; this module has no
    supervisor/graph access, only the cursor sidecar.
    """
    cursor = load_cursor(cursor_file)
    state = cursor.get("state")
    if state in _TERMINAL:
        # Hydra#69 round 6 defect 2: a terminal cursor only ever returns its
        # cached result to the call_key that actually produced the terminal
        # transition. A different call_key (stale, or belonging to another
        # cursor's caller entirely) is refused structurally -- it must never
        # be treated as an idempotent re-submit and re-billed by the caller.
        # `terminal_call_key` is unset on a legacy cursor written before this
        # field existed, or one terminated outside this function (operator
        # abort, stalled-stage recovery); such a cursor accepts any call_key
        # here (already fully charged/settled by definition of being on
        # disk), matching the migration policy: never re-charge it.
        _terminal_key = cursor.get("terminal_call_key")
        if _terminal_key is not None and call_key != _terminal_key:
            out = _step_result(cursor, cursor_file)
            out["ignored"] = (
                f"call_key {call_key!r} != terminal call identity "
                f"{_terminal_key!r}"
            )
            out["error_code"] = "stale_call_key"
            return out
        return _step_result(cursor, cursor_file)

    pending = cursor.get("pending_action") or {}
    expected_key = pending.get("call_key")
    if call_key != expected_key:
        # Duplicate / out-of-order submit — do not re-apply (exactly-once).
        out = _step_result(cursor, cursor_file)
        out["ignored"] = f"call_key {call_key!r} != expected {expected_key!r}"
        # Hydra#69 defect C: a squad cursor's call_key carries the attempt
        # number (``squad-{task_id}-{attempt}``, see begin_squad_stage). A
        # mismatch on a squad-shaped call_key is very likely a stale
        # response from an EARLIER (rejected) attempt racing a freshly
        # re-issued cursor -- flag it structurally so a caller can
        # distinguish "stale attempt" from any other call_key mismatch
        # instead of parsing the free-text `ignored` string.
        if (isinstance(call_key, str) and isinstance(expected_key, str)
                and call_key.rsplit("-", 1)[:-1] == expected_key.rsplit("-", 1)[:-1]
                and call_key != expected_key):
            out["stale_attempt"] = True
            out["error_code"] = "stale_attempt"
        return out

    if state == "await_generate":
        _apply_generate(dispatcher, cursor, result, workflow_terminal=workflow_terminal)
    elif state in ("await_judge", "stalled_infra"):
        # W2-3: "stalled_infra" is a non-terminal hold state entered when a
        # transport-shaped record_verdict failure would otherwise have been
        # downgraded + finalized. Its pending_action.call_key is left
        # unchanged from the original judge step, so a re-issued
        # submit_host_result carrying the same call_key/result re-enters
        # _apply_judge here and retries record_verdict via the
        # idempotency_token — exactly-once even across the re-drive.
        # E2-27: a host-correctable judge label problem (unsupported producer,
        # or pp's judge-model pin rejection) comes back as a retryable error
        # dict. The cursor is unchanged and still holds this judge's
        # pending_action/call_key, so the host corrects the label and
        # resubmits under the SAME call_key rather than losing the stage.
        _judge_err = _apply_judge(dispatcher, cursor, result,
                                  cursor_file=cursor_file, call_key=call_key,
                                  workflow_terminal=workflow_terminal)
        if _judge_err is not None:
            save_cursor(cursor_file, cursor)
            out = _step_result(cursor, cursor_file)
            out.update(_judge_err)
            return out
    elif state == "await_smoke":
        # Hydra#70: the verdict is already recorded (see `_apply_judge`); the
        # smoke is running as a detached job. A resubmit under the SAME
        # call_key (the judge's -- pending_action.call_key was left
        # unchanged when this state was entered) is treated as a POLL, never
        # a duplicate record_verdict/record_attempt. `result` (the judge
        # payload the host resubmitted) is intentionally ignored here.
        still_pending = poll_smoke_job(dispatcher, cursor, cursor_file=cursor_file,
                                       workflow_terminal=workflow_terminal)
        if still_pending:
            save_cursor(cursor_file, cursor)
            return _step_result(cursor, cursor_file)
    elif state == "await_squad_agent":
        # Lightweight non-engineering squad flow — no pp protocol calls needed.
        _apply_squad_result(dispatcher, cursor, result, cursor_file=cursor_file)
    else:  # pragma: no cover — defensive
        cursor["state"] = "aborted"
        cursor["final_status"] = "aborted"
        cursor["error"] = f"unknown attended state {state!r}"

    # Hydra#69 round 6 defect 2: stamp the call_key that produced THIS
    # transition as the cursor's trusted terminal call identity, the instant
    # the cursor first goes terminal. `call_key` here has already been
    # validated == `pending.get("call_key")` above, so this is exactly the
    # call that drove the transition -- never a caller-supplied value taken
    # on faith. `setdefault` so a cursor that was already terminal before
    # this call (e.g. the stalled_infra retry path above, which returns
    # before reaching here) never has its original identity overwritten.
    if cursor.get("state") in _TERMINAL:
        cursor.setdefault("terminal_call_key", call_key)

    save_cursor(cursor_file, cursor)
    return _step_result(cursor, cursor_file)


def mark_charged(cursor_file: str | Path) -> None:
    """Mark a terminal cursor as budget-charged (rider b idempotency guard).

    Called by _cmd_attended_submit immediately after charging the HydraState
    budget ledger. Subsequent submit calls that see ``already_charged=True``
    in the step result skip the charge, preventing double-billing on retried
    submit-host-result invocations.  Fail-soft: any I/O or schema error is
    silently ignored so a storage hiccup never blocks the calling workflow.
    """
    try:
        cursor = load_cursor(cursor_file)
        if cursor.get("state") in _TERMINAL:
            cursor["charged"] = True
            save_cursor(cursor_file, cursor)
    except Exception:  # noqa: BLE001 — never crash the caller on persist failure
        pass


def record_rejected_envelopes(cursor_file: str | Path,
                              rejected: list[dict[str, Any]]) -> None:
    """E2-34: park schema-rejected emitted envelopes on the terminal cursor.

    The engineering task itself completed; what failed is the delegation it
    emitted. Persisting the rejections here (rather than only returning them
    once) means a later ``step`` / finalize read still sees the outstanding
    work, so a dropped DEV_TASK cannot disappear between calls. Fail-soft: a
    storage hiccup never blocks the calling workflow.
    """
    if not rejected:
        return
    try:
        cursor = load_cursor(cursor_file)
        cursor["rejected_envelopes"] = list(rejected)
        save_cursor(cursor_file, cursor)
        _trace(cursor, "attended.envelopes_rejected", {
            "task_id": cursor.get("task_id"),
            "squad_slug": cursor.get("squad_slug"),
            "rejected_count": len(rejected),
        })
    except Exception:  # noqa: BLE001 — never crash the caller on persist failure
        pass


def abort_stage(dispatcher: Dispatcher, *, cursor_file: str | Path,
                reason: str = "operator_abort") -> dict[str, Any]:
    """Best-effort abort: finalize the pp run ``aborted`` to release the lock and
    mark the cursor terminal. Never raises."""
    try:
        cursor = load_cursor(cursor_file)
    except Exception as e:  # noqa: BLE001
        return {"status": "aborted", "error": f"cursor_unreadable: {e}"}
    if cursor.get("state") in _TERMINAL:
        return _step_result(cursor, cursor_file)
    try:
        # F30: FinalizeRunSchema strips 'reason'/'project_path' — embed reason
        # in summary_md so it is never silently dropped.
        dispatcher.call_mcp("pp_harness", "finalize_run", {
            "run_id": cursor["run_id"], "status": "aborted",
            "summary_md": f"attended_abort: {reason}",
        }, squad_id=_SQ)
    except Exception:  # noqa: BLE001
        pass
    # Discard the isolated worktree (no merge on abort).
    worktree_path = cursor.get("worktree_path")
    repo_root = cursor.get("repo_root")
    if worktree_path and repo_root:
        _remove_worktree(repo_root, worktree_path)
        cursor["merge"] = {"merged": False, "error": "discarded_abort"}
    # Marker 2: clear the run-scoped sentinel on abort too, not just a clean
    # finalize — an aborted stage must not leave a stale bypass behind.
    _clear_stage_active_sentinel(cursor.get("project_root") or cursor.get("project_path"))
    cursor["state"] = "aborted"
    cursor["final_status"] = "aborted"
    cursor["error"] = reason
    cursor["pending_action"] = None
    save_cursor(cursor_file, cursor)
    return _step_result(cursor, cursor_file)
