/**
 * web/server/nonces.ts
 *
 * Server-issued single-use confirm nonces for high-risk cockpit writes.
 *
 * Design (per COCKPIT-DESIGN.md §2.5):
 *   - A nonce is minted server-side via POST /api/confirm/preview.
 *   - Each nonce is bound to an `action` and has a short TTL (~120s).
 *   - Nonces are single-use: consuming one removes it from the map.
 *   - High-risk writes (launch --live, approve, modify-budget, force-dispatch,
 *     change-squads, replay) must echo a valid nonce back in their request body.
 *   - Dry-run launch does NOT require a nonce (dryRun=true bypasses the check).
 *
 * Storage: in-memory map. A bridge restart invalidates all outstanding nonces —
 * this is intentional (same as the CSRF token restart semantics).
 *
 * The map is periodically swept to evict expired nonces (passive GC — sweep
 * only when a new nonce is minted, so the map stays bounded without a timer).
 */

import { randomBytes } from 'node:crypto';

export const NONCE_TTL_MS = 120_000; // 120 seconds

export interface NonceRecord {
  /** The action this nonce was minted for. */
  readonly action: string;
  /** Unix ms timestamp when the nonce expires. */
  readonly expiresAt: number;
}

// ---------------------------------------------------------------------------
// In-memory store
// ---------------------------------------------------------------------------

const _nonces = new Map<string, NonceRecord>();

// ---------------------------------------------------------------------------
// Sweep — remove expired nonces (called on each mint to bound the map size)
// ---------------------------------------------------------------------------

function sweep(): void {
  const now = Date.now();
  for (const [k, v] of _nonces) {
    if (v.expiresAt <= now) _nonces.delete(k);
  }
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

/**
 * Mint a new single-use nonce bound to `action`.
 * Returns the nonce string and its expiry timestamp (Unix ms).
 *
 * The nonce is 32 random bytes encoded as 64 hex characters.
 */
export function mintNonce(action: string): { nonce: string; expiresAt: number } {
  sweep(); // passive GC: evict stale entries before growing the map
  const nonce = randomBytes(32).toString('hex');
  const expiresAt = Date.now() + NONCE_TTL_MS;
  _nonces.set(nonce, { action, expiresAt });
  return { nonce, expiresAt };
}

/**
 * Consume a nonce: verify it exists, matches `action`, and has not expired.
 * On success the nonce is removed (single-use guarantee).
 * Returns true on valid consumption, false on any failure (missing, expired,
 * wrong action, or already consumed).
 *
 * This is the only way to verify a nonce — there is no peek/read-only check.
 */
export function consumeNonce(nonce: string | undefined | null, action: string): boolean {
  if (typeof nonce !== 'string' || nonce.length === 0) return false;
  const record = _nonces.get(nonce);
  if (record === undefined) return false;                  // unknown or already consumed
  if (record.action !== action) return false;              // wrong action
  if (record.expiresAt <= Date.now()) {
    _nonces.delete(nonce);                                 // lazy eviction on expiry check
    return false;
  }
  _nonces.delete(nonce);                                   // single-use: consume it
  return true;
}

/**
 * Returns the number of active (non-expired) nonces. Intended for tests only.
 */
export function _nonceCount(): number {
  sweep();
  return _nonces.size;
}

/**
 * Clear all nonces. For tests only — never call in production code.
 */
export function _clearNonces(): void {
  _nonces.clear();
}
