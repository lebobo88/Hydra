/**
 * web/server/write-whitelist.ts
 *
 * Frozen registry of the 8 sanctioned Hydra Cockpit write operations.
 * Defines risk classes, nonce requirements, and typed-challenge rules per the
 * COCKPIT-DESIGN.md §2.5 write whitelist.
 *
 * Rules:
 *   - The whitelist is FROZEN (Object.freeze). Adding a new write is a
 *     governance event, not an expedient code change.
 *   - isWriteAllowed(action) — true iff the action is in the whitelist.
 *   - needsNonce(action) — true for High and venom-class risk; Low/Med do not
 *     require a confirm nonce (though Med may add one in a later chunk).
 *   - needsTypedChallenge(action) — true for High writes with a typed-challenge
 *     requirement (modify-budget) and unconditionally for force-dispatch.
 *
 * Transport column is informational; actual dispatch lives in the route handlers.
 *
 * C2 wires only the `launch` entry to a real route. C3 wires resume-action
 * entries (approve/reject/modify-budget/force-dispatch/change-squads).
 * C6 wires replay and tag_memory.
 */

export type RiskClass = 'Low' | 'Med' | 'High' | 'venom';
export type Transport = 'detached-cli' | 'hydra_control-resume' | 'hydra_memory-tag';

export interface WriteEntry {
  /** The canonical action name used in route bodies and audit envelopes. */
  readonly action: string;
  /** Risk classification per COCKPIT-DESIGN.md §2.5. */
  readonly risk: RiskClass;
  /** True → this write requires a server-issued confirm nonce. */
  readonly requiresNonce: boolean;
  /** True → this write requires the operator to type the workflow id. */
  readonly requiresTypedChallenge: boolean;
  /** Transport that executes this write. */
  readonly transport: Transport;
  /** Free-text note for auditors; not used programmatically. */
  readonly note: string;
}

/**
 * THE FROZEN WRITE WHITELIST — exactly 8 entries per COCKPIT-DESIGN.md §2.5.
 *
 * Do NOT add entries here without a corresponding governance event.
 * Do NOT export a mutable reference; callers use the gates below.
 */
const WRITE_WHITELIST: readonly WriteEntry[] = Object.freeze([
  {
    action: 'launch',
    risk: 'High',
    requiresNonce: true,    // live launch requires nonce; dry-run does not (enforced in handler)
    requiresTypedChallenge: false,
    transport: 'detached-cli',
    note: 'Dry-run is default and not high-risk. live=true elevates to High; nonce required.',
  },
  {
    action: 'approve',
    risk: 'Med',
    requiresNonce: true,
    requiresTypedChallenge: false,
    transport: 'hydra_control-resume',
    note: 'Gate resume — approve the HITL request.',
  },
  {
    action: 'reject',
    risk: 'Low',
    requiresNonce: false,
    requiresTypedChallenge: false,
    transport: 'hydra_control-resume',
    note: 'Gate resume — reject the HITL request (default option per design).',
  },
  {
    action: 'modify-budget',
    risk: 'High',
    requiresNonce: true,
    requiresTypedChallenge: true,
    transport: 'hydra_control-resume',
    note: 'Patches state.budget.budget_usd. High-risk: nonce + typed workflow-id challenge.',
  },
  {
    action: 'force-dispatch',
    risk: 'venom',
    requiresNonce: true,
    requiresTypedChallenge: true,   // unconditional per §1.4
    transport: 'hydra_control-resume',
    note: 'venom-class: Cerberus-gated server-side + typed challenge + nonce. Emits policy_override.',
  },
  {
    action: 'change-squads',
    risk: 'Med',
    requiresNonce: true,
    requiresTypedChallenge: false,
    transport: 'hydra_control-resume',
    note: 'Replaces selected squads for an active workflow.',
  },
  {
    action: 'replay',
    risk: 'High',
    requiresNonce: true,
    requiresTypedChallenge: false,
    transport: 'detached-cli',
    note: 'High-risk; additionally venom-gated when --live. C6 wires this.',
  },
  {
    action: 'tag_memory',
    risk: 'Low',
    requiresNonce: false,
    requiresTypedChallenge: false,
    transport: 'hydra_memory-tag',
    note: 'Tags an episodic row with additional cells. C6 wires this.',
  },
] as const);

// ---------------------------------------------------------------------------
// Index for O(1) lookup
// ---------------------------------------------------------------------------

const _byAction = new Map<string, WriteEntry>(
  WRITE_WHITELIST.map((e) => [e.action, e]),
);

/**
 * Returns true iff `action` is in the sanctioned write whitelist.
 * This is the primary gate: call this before executing ANY write operation.
 */
export function isWriteAllowed(action: string): boolean {
  return _byAction.has(action);
}

/**
 * Returns true iff `action` requires a server-issued confirm nonce.
 * Callers must validate the nonce before dispatching.
 * Returns false for unknown actions (fail-closed).
 */
export function needsNonce(action: string): boolean {
  return _byAction.get(action)?.requiresNonce ?? false;
}

/**
 * Returns true iff `action` requires the operator to type the workflow id
 * as a challenge. UI enforces this; bridge double-checks for defense-in-depth.
 * Returns false for unknown actions (fail-closed).
 */
export function needsTypedChallenge(action: string): boolean {
  return _byAction.get(action)?.requiresTypedChallenge ?? false;
}

/**
 * Returns the WriteEntry for an action, or undefined if not in the whitelist.
 * Use for audit-envelope population only; gate via isWriteAllowed() first.
 */
export function getWriteEntry(action: string): WriteEntry | undefined {
  return _byAction.get(action);
}

/**
 * Returns the full frozen whitelist (read-only).
 * Use for admin/debug endpoints only — never expose this to browser callers.
 */
export function getWriteWhitelist(): readonly WriteEntry[] {
  return WRITE_WHITELIST;
}
