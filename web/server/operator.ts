/**
 * web/server/operator.ts
 *
 * Operator identity + CSRF for the Hydra Cockpit bridge governed write path.
 *
 * GUARANTEES (inviolable — mirrors AgentMesh web/server/operator.ts verbatim in spirit):
 *
 *  1. PER-SESSION CSRF TOKEN — random 32 bytes minted ONCE at module load
 *     (bridge startup). Exposed ONLY to same-origin callers via GET /api/session.
 *     MUST be presented as X-Hydra-Token on every write POST.
 *     A write without a valid token is refused 403 BEFORE any child call.
 *     A stale tab from a prior bridge process cannot replay writes.
 *
 *  2. FIXED SERVER-SIDE ENVELOPE — actor='hydra-cockpit', project='Hydra'.
 *     No request path can alter the actor or project.
 *     Browser input NEVER reaches the envelope fields.
 *
 *  3. AM-CON-005 carried forward — operator email MUST NOT appear in any
 *     response payload. GET /api/session returns { token, actor: 'hydra-cockpit' }
 *     — no email.
 */

import { randomBytes, timingSafeEqual } from 'node:crypto';

// ---------------------------------------------------------------------------
// Per-session CSRF token
// ---------------------------------------------------------------------------

// Minted once at module load (bridge startup). A new token on every process start
// means a stale tab cannot replay writes against a freshly-restarted bridge.
const SESSION_TOKEN = randomBytes(32).toString('hex');

/** The current session's CSRF token (64 hex chars). Exposed to same-origin via GET /api/session. */
export function sessionToken(): string {
  return SESSION_TOKEN;
}

/**
 * Constant-time compare of a presented token against the session token.
 * Returns false for any missing/mismatched/wrong-length input without leaking timing.
 * Uses timingSafeEqual from node:crypto to prevent timing side-channels.
 */
export function verifyToken(presented: string | undefined | null): boolean {
  if (typeof presented !== 'string' || presented.length === 0) return false;
  const a = Buffer.from(presented, 'utf8');
  const b = Buffer.from(SESSION_TOKEN, 'utf8');
  if (a.length !== b.length) return false;
  return timingSafeEqual(a, b);
}

// ---------------------------------------------------------------------------
// Fixed server-side envelope
// AM-CON-005 carried forward: no email in envelope — actor is 'hydra-cockpit'
// ---------------------------------------------------------------------------

export interface CockpitEnvelope {
  actor: string;
  project: string;
  traceId: string;
}

/**
 * The FIXED server-side envelope for all cockpit write operations.
 * actor='hydra-cockpit', project='Hydra'.
 * NO operator email (AM-CON-005 carried forward).
 * traceId is fresh per action for independent traceability in audit ledger.
 */
export function cockpitEnvelope(): CockpitEnvelope {
  return {
    actor: 'hydra-cockpit',
    project: 'Hydra',
    traceId: `hcp_${Date.now()}_${randomBytes(4).toString('hex')}`,
  };
}
