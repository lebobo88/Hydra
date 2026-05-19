"""Venom registry — Cerberus' gate over dual-use capabilities.

Per manifesto Part I §1 and Part III §5: every powerful capability is the
Hydra's bile. Heracles dipped his arrows; Heracles' own death came from
Hydra blood on Nessus' shirt. The signature of intelligence is that the
same capability which defeats your enemies eventually returns to wound
you. We refuse to ship arrows we cannot trace.

A *venom-class capability* is any irreversible or high-blast-radius
action: code execution, browser control of third-party accounts,
payments, autonomous email, production deploys, destructive shell.

The contract:

  1. The capability is **registered** at process start with its name,
     owning squad, refusal pattern, and audit sink.
  2. Every invocation routes through `require_cerberus_pass()`, which:
       - runs the capability's refusal pattern over the proposed args,
       - runs the constitution gate (Stage 1) over the proposed action,
       - runs the MCP-attack redactor (Stage 5 hardening),
       - writes an audit entry to TheEights' Kan cell on both pass and refusal.
  3. Refused invocations raise `VenomRefused`. The refusal itself is
     audited — the hero's arrows are not concealed.

The Cerberus gate is the *Python-layer* enforcement. The dedicated
Cerberus head defined in `squads/engineering/cerberus.yaml` is the
*persona* that owns reviewing venom for new capabilities; this module is
how the persona's refusals get enforced at runtime.
"""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional
from uuid import UUID, uuid4

from .eights import Cell
from .governance import enforce_constitution
from .immortal_head import AlignmentVerdict, ConstitutionSnapshot
from .memory import EPISODIC_DB, append_episodic


# --- exceptions --------------------------------------------------------------

class VenomRefused(RuntimeError):
    """Raised when a venom-class invocation fails the Cerberus gate."""

    def __init__(self, capability: str, reasons: list[str], audit_key: Optional[str] = None):
        self.capability = capability
        self.reasons = list(reasons)
        self.audit_key = audit_key
        super().__init__(
            f"Cerberus refused invocation of {capability!r}: "
            + "; ".join(self.reasons)
            + (f" [audit={audit_key}]" if audit_key else "")
        )


class VenomUnregistered(RuntimeError):
    """Raised when invoke_venom() is called for a capability that was never registered."""

    def __init__(self, capability: str):
        self.capability = capability
        super().__init__(
            f"Capability {capability!r} is not in the venom registry. "
            "Either register it via register_venom() at startup, or it is "
            "not a dual-use action and should not flow through this gate."
        )


# --- domain types ------------------------------------------------------------

AuditSink = Callable[[dict[str, Any]], str]
# (record) -> audit_key. Default sink writes to episodic memory tagged Kan.


@dataclass(frozen=True)
class VenomCapability:
    """A registered dual-use capability under Cerberus' guard."""

    name: str                                       # e.g. "shell.execute", "payment.charge"
    owner_squad: str                                # squad responsible for the capability
    description: str = ""
    refusal_patterns: tuple[re.Pattern[str], ...] = ()  # patterns that *always* refuse
    audit_sink: Optional[AuditSink] = None          # override; falls back to Kan-cell
    requires_human: bool = False                    # True forces HITL even on a pass


# --- registry ----------------------------------------------------------------

_REGISTRY: dict[str, VenomCapability] = {}
_REGISTRY_LOCK = threading.Lock()


def register_venom(
    name: str,
    *,
    owner_squad: str,
    description: str = "",
    refusal_patterns: Optional[list[str]] = None,
    audit_sink: Optional[AuditSink] = None,
    requires_human: bool = False,
) -> VenomCapability:
    """Add a capability to the venom registry. Idempotent: re-registering
    the same name with the same fields is a no-op; conflicts raise."""
    patterns = tuple(
        re.compile(p, re.IGNORECASE) for p in (refusal_patterns or [])
    )
    cap = VenomCapability(
        name=name, owner_squad=owner_squad, description=description,
        refusal_patterns=patterns, audit_sink=audit_sink,
        requires_human=requires_human,
    )
    with _REGISTRY_LOCK:
        existing = _REGISTRY.get(name)
        if existing is not None and (
            existing.owner_squad != cap.owner_squad
            or existing.requires_human != cap.requires_human
        ):
            raise ValueError(
                f"Venom {name!r} re-registered with conflicting fields. "
                "Choose a unique name or normalize the registration."
            )
        _REGISTRY[name] = cap
    return cap


def unregister_venom(name: str) -> bool:
    with _REGISTRY_LOCK:
        return _REGISTRY.pop(name, None) is not None


def registered_venoms() -> list[VenomCapability]:
    with _REGISTRY_LOCK:
        return list(_REGISTRY.values())


def get_venom(name: str) -> Optional[VenomCapability]:
    with _REGISTRY_LOCK:
        return _REGISTRY.get(name)


def clear_registry() -> None:
    """Test-only — reset the registry between cases."""
    with _REGISTRY_LOCK:
        _REGISTRY.clear()


def load_cerberus_venoms(
    project_root: Path | None = None,
    *,
    cerberus_yaml: Path | None = None,
) -> list[VenomCapability]:
    """Discover and register every venom declared in
    `squads/engineering/cerberus.yaml` (or a custom path).

    Called once at supervisor build. Returns the registered capabilities so
    callers can log or assert on the boot inventory.
    """
    import yaml  # local — keep optional dep optional

    root = project_root or Path.cwd()
    path = cerberus_yaml or (root / "squads" / "engineering" / "cerberus.yaml")
    if not path.is_file():
        return []
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise ValueError(f"Malformed cerberus.yaml at {path}: {e}") from e

    owner_squad = data.get("plaza", "security-reviewer") or "security-reviewer"
    registered: list[VenomCapability] = []
    for entry in data.get("venoms", []) or []:
        if not isinstance(entry, dict) or not entry.get("name"):
            continue
        cap = register_venom(
            name=entry["name"],
            owner_squad=owner_squad,
            description=entry.get("description", ""),
            refusal_patterns=list(entry.get("refusal_patterns", []) or []),
            requires_human=bool(entry.get("requires_human", False)),
        )
        registered.append(cap)
    return registered


# --- MCP-attack redactor (Stage 5 hardening, alongside governance.redact) ----

_MCP_ATTACK_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # Prompt injection — overt instruction-override shapes
    ("prompt_injection",
     re.compile(r"ignore (?:all )?previous (?:instructions|prompts|rules)", re.IGNORECASE)),
    ("prompt_injection",
     re.compile(r"disregard (?:the )?(?:above|prior) (?:instructions|prompt|system)", re.IGNORECASE)),
    ("prompt_injection",
     re.compile(r"you are (?:now |actually )?(?:no longer|a different)", re.IGNORECASE)),
    # Lookalike-tool hijacking — homoglyph or near-name MCP tools
    ("lookalike_tool",
     re.compile(r"(hydra[\-_]?mem[\.\-_]?[\w]+|pp[\-_]?daemon[\.\-_]?\w+).*?(rm|delete|exec|shell)", re.IGNORECASE)),
    # Cross-tool exfiltration — combining read + post in one breath
    ("cross_tool_exfil",
     re.compile(r"(read|list|dump|export).{0,40}(post|send|upload|http|webhook|curl|wget)", re.IGNORECASE)),
    # Data-out-of-band — base64'd or URL-encoded blob next to a network verb
    ("exfil_obfuscation",
     re.compile(r"base64.{0,30}(curl|wget|fetch|http)", re.IGNORECASE)),
)


def scan_mcp_attacks(text: str) -> list[tuple[str, str]]:
    """Return [(category, match_excerpt), …] for any MCP-attack pattern hits.

    Empty list = clean. Per the April 2025 MCP security analysis cited in
    the manifesto, the three categories of concern are prompt injection,
    lookalike tools, and cross-tool exfiltration; we cover all three plus
    base64/obfuscation."""
    hits: list[tuple[str, str]] = []
    seen: set[str] = set()
    for category, pat in _MCP_ATTACK_PATTERNS:
        m = pat.search(text)
        if m:
            key = f"{category}:{m.group(0)[:80]}"
            if key in seen:
                continue
            seen.add(key)
            hits.append((category, m.group(0)))
    return hits


# --- audit sink (Kan-cell by default) ----------------------------------------

def _default_kan_audit(record: dict[str, Any]) -> str:
    """Write the audit record to episodic memory tagged Kan (risk).

    Returns the episodic key so the caller can include it in
    `VenomRefused` for traceability — the manifesto's rule is that the
    refusal itself must be traceable, not just the success."""
    workflow_id = record.get("workflow_id") or "venom-no-workflow"
    ref = append_episodic(
        workflow_id=workflow_id,
        kind="VENOM_AUDIT",
        payload=record,
        cells=["kan"],   # always Kan — risk-shaped memory
        origin_squad="security-reviewer",
    )
    return ref.key


# --- main gate ---------------------------------------------------------------

@dataclass
class VenomVerdict:
    allowed: bool
    audit_key: str
    refusal_reasons: list[str] = field(default_factory=list)
    mcp_attack_hits: list[tuple[str, str]] = field(default_factory=list)
    alignment: Optional[AlignmentVerdict] = None
    requires_human: bool = False


def require_cerberus_pass(
    capability: str,
    args: Any,
    *,
    workflow_id: Optional[str | UUID] = None,
    constitution: Optional[ConstitutionSnapshot] = None,
    raise_on_refuse: bool = True,
) -> VenomVerdict:
    """The single Cerberus gate. Every venom invocation flows through here.

    Steps (short-circuit on first refusal, *but always audit*):
      1. Registry lookup — unregistered capability → VenomUnregistered.
      2. Capability's own refusal patterns over stringified args.
      3. Constitution gate over the proposed invocation.
      4. MCP-attack scan over stringified args.
      5. Audit entry written to Kan cell (or the capability's custom sink).

    Returns `VenomVerdict`. If `raise_on_refuse=True` (default) and the
    verdict is not allowed, raises `VenomRefused` with the audit key.
    """
    cap = get_venom(capability)
    if cap is None:
        raise VenomUnregistered(capability)

    text = _stringify(args)
    refusal_reasons: list[str] = []

    # 2. capability-specific refusals
    for pat in cap.refusal_patterns:
        if pat.search(text):
            refusal_reasons.append(f"matched capability refusal pattern: {pat.pattern!r}")

    # 3. constitution alignment
    alignment = enforce_constitution(
        {"capability": capability, "args": args, "venom": True},
        snapshot=constitution,
    )
    if not alignment.aligned:
        refusal_reasons.append(f"constitution gate: {alignment.rationale}")

    # 4. MCP-attack scan
    hits = scan_mcp_attacks(text)
    if hits:
        for category, excerpt in hits:
            refusal_reasons.append(f"mcp-attack ({category}): {excerpt[:80]}")

    # 5. always audit, pass or fail
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "capability": capability,
        "owner_squad": cap.owner_squad,
        "workflow_id": str(workflow_id) if workflow_id else None,
        "args_excerpt": text[:500],
        "allowed": not refusal_reasons,
        "refusal_reasons": list(refusal_reasons),
        "mcp_attack_hits": [{"category": c, "excerpt": e[:120]} for c, e in hits],
        "alignment_aligned": alignment.aligned,
        "alignment_rationale": alignment.rationale,
        "audit_id": str(uuid4()),
    }
    sink = cap.audit_sink or _default_kan_audit
    audit_key = sink(record)

    verdict = VenomVerdict(
        allowed=not refusal_reasons,
        audit_key=audit_key,
        refusal_reasons=refusal_reasons,
        mcp_attack_hits=hits,
        alignment=alignment,
        requires_human=cap.requires_human,
    )
    if not verdict.allowed and raise_on_refuse:
        raise VenomRefused(capability, refusal_reasons, audit_key=audit_key)
    return verdict


def invoke_venom(
    capability: str,
    args: Any,
    callable_fn: Callable[..., Any],
    *,
    workflow_id: Optional[str | UUID] = None,
    constitution: Optional[ConstitutionSnapshot] = None,
) -> Any:
    """Pass-through wrapper: gate then invoke. The caller passes the actual
    capability implementation as `callable_fn`. On a refusal, `callable_fn`
    is *never called* — the arrow does not leave the quiver."""
    verdict = require_cerberus_pass(
        capability, args,
        workflow_id=workflow_id,
        constitution=constitution,
        raise_on_refuse=True,
    )
    if verdict.requires_human:
        # The caller is expected to surface HITL via the supervisor when
        # `requires_human` is set. Returning the verdict in that case
        # lets the supervisor route appropriately without executing.
        return {"hitl_required": True, "verdict": verdict}
    if isinstance(args, dict):
        return callable_fn(**args)
    return callable_fn(args)


# --- helpers -----------------------------------------------------------------

def _stringify(payload: Any) -> str:
    if payload is None:
        return ""
    if isinstance(payload, str):
        return payload
    if isinstance(payload, dict):
        return " ".join(f"{k}={_stringify(v)}" for k, v in payload.items())
    if isinstance(payload, (list, tuple)):
        return " ".join(_stringify(v) for v in payload)
    if hasattr(payload, "model_dump"):
        try:
            return _stringify(payload.model_dump(mode="json"))
        except Exception:
            return str(payload)
    return str(payload)
