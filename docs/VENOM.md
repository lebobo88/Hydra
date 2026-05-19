# Venom Policy — Stage 5

> *"Heracles dipped his arrows in the Hydra's bile, making them lethally
> incurable. This is the dual-use signature of intelligence: the same
> capability that defeats your enemies will eventually return to wound
> you. Every powerful agent capability — code execution, browser control,
> payment rails, autonomous email — must be treated as venom: useful,
> irrevocable, traceable."* — Manifesto, Part I §1

## What "venom" means in Hydra

A **venom-class capability** is any irreversible or high-blast-radius
action whose effects propagate beyond the local workflow:

- destructive shell operations (`rm -rf`, `dd`, `mkfs`, raw disk writes)
- force-push to a protected branch
- production deploys
- outbound payment instructions
- autonomous email sent on the user's behalf
- browser actions on third-party accounts the user is logged into

Venom-class capabilities are **registered** at supervisor boot from
`squads/engineering/cerberus.yaml` (see `hydra_core/venom.load_cerberus_venoms`).
Every invocation flows through `require_cerberus_pass`, which:

1. Looks up the capability in the venom registry.
2. Applies the capability's own refusal patterns.
3. Applies the **constitution gate** (Stage 1) to the proposed args.
4. Applies the **MCP-attack scanner** (prompt injection, lookalike tools,
   cross-tool exfiltration, base64 obfuscation).
5. Writes an audit record to TheEights' **Kan cell** (whether the gate
   passes or refuses — the manifesto requires that the refusal itself
   be traceable).
6. Raises `VenomRefused` if any check failed.

The Python module is `hydra_core/venom.py`. The persona that *owns*
reviewing venom for new capabilities is **Cerberus**, defined as a head
in `squads/engineering/heads.yaml` and configured in
`squads/engineering/cerberus.yaml`.

## What changed at the squad boundary

`governance.redact_for_squad_boundary` now neutralizes MCP-attack
patterns alongside PII. The redaction replaces injection shapes with
`[REDACTED-INJECTION]` so the attempt remains visible in audit but the
payload is defanged. PII continues to redact as `[REDACTED]`.

## VEN-4 — external red-team engagement (open)

The manifesto's Stage 5 threshold is explicit:

> *"Threshold to proceed: an external red team has tried and failed for
> two weeks."*

This is the one threshold the system cannot self-certify. Before any
public-facing capability ships:

1. **Scope**: contract scope covers all registered venom capabilities at
   the time of engagement, the constitution gate (Stage 1), and the MCP
   tools listed in the cerberus.yaml `mcp_allowlist`.
2. **Duration**: two-week engagement minimum. The clock resets if the
   venom registry materially changes mid-engagement.
3. **Acceptance**: zero successful exfiltration, zero unauthorized
   capability execution, zero constitution-bypass. Any single bypass
   defers Stage 6 until remediated.
4. **Out of scope**: physical security, social engineering of the
   founder, supply-chain attacks against Python or Node base images —
   these are tracked under enterprise risk (COSO/ISO) elsewhere.

A red-team engagement RFP template will live at `docs/RED-TEAM-RFP.md`
when the engagement is scheduled. Not in this run.

## Decisions deferred

- Whether to forward Kan-cell audit records to an external SIEM
  (Cerberus' `audit.external_siem` field). Default: null until a SIEM
  is provisioned.
- Whether `requires_human: true` capabilities skip the Python gate and
  surface directly to HITL, or run the gate then surface. Current
  behavior: run the gate, return a verdict with `requires_human=True`,
  let the supervisor route HITL. Revisit if the supervisor's interrupt
  semantics change in a future stage.
- Whether to add a venom *cooldown* (per-capability rate limit) at this
  layer or push it to the audit-sink consumer. Current: no cooldown.
