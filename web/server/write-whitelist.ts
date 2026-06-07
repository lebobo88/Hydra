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
 *   - isResumeAction(action) — true iff the action is one of the 5 gate-resume
 *     actions routed through hydra_control. Non-resume writes (launch, replay,
 *     tag_memory) are NOT routable via /api/resume.
 *   - validateOption(action, option) — validates the option field per-action.
 *     modify-budget: requires a numeric USD value (positive finite number string).
 *     change-squads: requires a comma-separated slug list.
 *     approve/reject/force-dispatch: option must be absent or empty.
 *     The option alphabet is BYTE-IDENTICAL to _OPTION_RE in hydra_control/server.py:
 *       /^[A-Za-z0-9 ,._\-]{0,200}$/
 *     This prevents the bridge from accepting an option the Python side would reject.
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

// ---------------------------------------------------------------------------
// Resume-action set — the 5 actions routed through hydra_control
// ---------------------------------------------------------------------------

/**
 * The 5 gate-resume actions that route through hydra_control.resume.
 * BYTE-IDENTICAL to _RESUME_ACTIONS in hydra_control/server.py.
 * Non-resume actions (launch, replay, tag_memory) must NOT be routed to /api/resume.
 */
export const RESUME_ACTIONS = Object.freeze([
  'approve',
  'reject',
  'modify-budget',
  'force-dispatch',
  'change-squads',
] as const);

export type ResumeAction = (typeof RESUME_ACTIONS)[number];

const RESUME_SET: ReadonlySet<string> = new Set(RESUME_ACTIONS);

/**
 * Returns true iff `action` is one of the 5 gate-resume actions
 * that route through the hydra_control child (hydra.workflow.resume).
 * Returns false for launch, replay, tag_memory, and all unknown actions.
 */
export function isResumeAction(action: string): action is ResumeAction {
  return RESUME_SET.has(action);
}

// ---------------------------------------------------------------------------
// Option validation — BYTE-IDENTICAL to hydra_control/server.py _OPTION_RE
// ---------------------------------------------------------------------------

/**
 * Validation alphabets byte-identical to hydra_control/server.py.
 *
 *   _OPTION_RE = re.compile(r"^[A-Za-z0-9 ,._\-]{0,200}$")
 *
 * Keeping these identical prevents the bridge from accepting a value the
 * Python side would reject (or vice-versa). Do not broaden the alphabet
 * without a matching change in hydra_control/server.py.
 */
export const OPTION_RE = /^[A-Za-z0-9 ,._\-]{0,200}$/;

/**
 * Error thrown when option validation fails.
 * Carries a `code` field for structured error responses.
 */
export class OptionValidationError extends Error {
  constructor(
    message: string,
    public readonly code: string,
  ) {
    super(message);
    this.name = 'OptionValidationError';
  }
}

/**
 * Validate the `option` field for a resume action.
 *
 * Per-action rules (mirror hydra_control semantics + COCKPIT-DESIGN.md §2.5):
 *   approve          — option must be absent or empty (no option accepted)
 *   reject           — option must be absent or empty
 *   force-dispatch   — option must be absent or empty
 *   modify-budget    — option REQUIRED; must be a positive finite number string
 *                      (e.g. "80", "120.50") — budget in USD
 *   change-squads    — option REQUIRED; must be a non-empty comma-separated
 *                      list of squad slugs (lowercase alphanumeric + hyphen)
 *
 * All accepted option strings must also match OPTION_RE (the Python-side alphabet).
 *
 * Throws OptionValidationError on failure.
 * Returns the trimmed option string (or undefined for actions that take no option).
 */
export function validateOption(action: string, option: unknown): string | undefined {
  const optStr = option !== undefined && option !== null && option !== ''
    ? String(option).trim()
    : undefined;

  switch (action) {
    case 'approve':
    case 'reject':
    case 'force-dispatch': {
      // No option accepted for these actions
      if (optStr !== undefined && optStr !== '') {
        throw new OptionValidationError(
          `action '${action}' does not accept an option argument`,
          'OPTION_NOT_ACCEPTED',
        );
      }
      return undefined;
    }

    case 'modify-budget': {
      if (optStr === undefined || optStr === '') {
        throw new OptionValidationError(
          'modify-budget requires an option: the new budget in USD (e.g. "120")',
          'OPTION_REQUIRED',
        );
      }
      // Must match the shared alphabet first
      if (!OPTION_RE.test(optStr)) {
        throw new OptionValidationError(
          'modify-budget option contains invalid characters',
          'OPTION_INVALID',
        );
      }
      // Must be a positive finite number
      const n = Number(optStr);
      if (!Number.isFinite(n) || n <= 0) {
        throw new OptionValidationError(
          'modify-budget option must be a positive finite number (USD amount)',
          'OPTION_INVALID',
        );
      }
      return optStr;
    }

    case 'change-squads': {
      if (optStr === undefined || optStr === '') {
        throw new OptionValidationError(
          'change-squads requires an option: a comma-separated list of squad slugs',
          'OPTION_REQUIRED',
        );
      }
      // Must match the shared alphabet first
      if (!OPTION_RE.test(optStr)) {
        throw new OptionValidationError(
          'change-squads option contains invalid characters',
          'OPTION_INVALID',
        );
      }
      // Each slug must be a lowercase alphanumeric + hyphen sequence
      const SLUG_RE = /^[a-z0-9][a-z0-9-]{0,63}$/;
      const slugs = optStr.split(',').map((s) => s.trim()).filter(Boolean);
      if (slugs.length === 0) {
        throw new OptionValidationError(
          'change-squads option must contain at least one squad slug',
          'OPTION_INVALID',
        );
      }
      for (const slug of slugs) {
        if (!SLUG_RE.test(slug)) {
          throw new OptionValidationError(
            `change-squads slug ${JSON.stringify(slug)} is invalid — must match [a-z0-9][a-z0-9-]{0,63}`,
            'OPTION_INVALID',
          );
        }
      }
      return optStr;
    }

    default:
      // Unknown action — fail closed
      throw new OptionValidationError(
        `unknown action '${action}' for option validation`,
        'UNKNOWN_ACTION',
      );
  }
}
