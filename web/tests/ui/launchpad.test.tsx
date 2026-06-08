/**
 * Unit tests for the deterministic, collision-free constellation layout
 * (src/cockpit/constellation-layout.ts) introduced in the Launchpad refinement
 * pass. The previous `start + hash % span` scheme let same-crown squads with
 * nearby hashes overlap; these tests pin the invariants that prevent that:
 *   - determinism: same slug set → identical geometry regardless of input order
 *   - no two heads collide (min pairwise distance > head diameter)
 *   - heads land in their crown's correct angular sector
 *   - dense crowns alternate label tiers so labels don't pile up
 */

import { describe, it, expect } from 'vitest';
import {
  distributeHeads, stableHash, CROWN_SECTOR, RING_R,
} from '../../src/cockpit/constellation-layout.ts';
import { crownOf } from '../../src/cockpit/crowns.ts';

const CX = 210;
const CY = 210;

// A realistically dense roster — many forge/garland squads, the case that
// produced the original bottom-arc pile-up.
const SLUGS = [
  'engineering', 'executive', 'legal-compliance', 'garland',
  'marketing-strategy', 'marketing-creative', 'marketing-ops',
  'marketing-production', 'marketing-research', 'research-ds',
  'sales-gtm', 'customer-support', 'healthcare', 'security',
];

function dist(a: { x: number; y: number }, b: { x: number; y: number }): number {
  return Math.hypot(a.x - b.x, a.y - b.y);
}

describe('constellation-layout · distributeHeads', () => {
  it('is deterministic and order-independent', () => {
    const a = distributeHeads(SLUGS, CX, CY);
    const shuffled = [...SLUGS].reverse();
    const b = distributeHeads(shuffled, CX, CY);

    const key = (p: { slug: string; x: number; y: number }) =>
      `${p.slug}:${p.x.toFixed(4)}:${p.y.toFixed(4)}`;
    expect(new Set(a.map(key))).toEqual(new Set(b.map(key)));
  });

  it('produces no overlapping heads (min pairwise distance > head diameter)', () => {
    const pos = distributeHeads(SLUGS, CX, CY);
    const HEAD_DIAMETER = 24; // head circle r=12 → diameter 24
    for (let i = 0; i < pos.length; i++) {
      for (let j = i + 1; j < pos.length; j++) {
        expect(dist(pos[i], pos[j])).toBeGreaterThan(HEAD_DIAMETER);
      }
    }
  });

  it('places every head within its crown angular sector', () => {
    for (const p of distributeHeads(SLUGS, CX, CY)) {
      expect(p.crown).toBe(crownOf(p.slug));
      const [start, end] = CROWN_SECTOR[p.crown];
      expect(p.angle).toBeGreaterThanOrEqual(start);
      expect(p.angle).toBeLessThanOrEqual(end);
    }
  });

  it('alternates label tiers within a dense crown so labels do not collide', () => {
    const pos = distributeHeads(SLUGS, CX, CY);
    // Find the densest crown (this roster has several marketing-* squads).
    const counts: Record<string, number> = {};
    pos.forEach((p) => { counts[p.crown] = (counts[p.crown] ?? 0) + 1; });
    const densest = Object.entries(counts).sort((a, b) => b[1] - a[1])[0][0];
    const members = pos.filter((p) => p.crown === densest);
    expect(members.length).toBeGreaterThan(3);
    // A crown with >3 members must use both label tiers.
    expect(new Set(members.map((p) => p.labelTier)).size).toBe(2);
  });

  it('centers a lone crown member and keeps it on its base ring', () => {
    const pos = distributeHeads(['executive'], CX, CY); // sole exec member
    expect(pos).toHaveLength(1);
    const [start, end] = CROWN_SECTOR.exec;
    expect(pos[0].angle).toBeCloseTo((start + end) / 2, 5);
    // Single member gets no radial stagger → exactly on the base ring.
    expect(dist(pos[0], { x: CX, y: CY })).toBeCloseTo(RING_R.exec, 5);
  });

  it('stableHash is stable and unsigned 32-bit', () => {
    expect(stableHash('engineering')).toBe(stableHash('engineering'));
    expect(stableHash('engineering')).toBeGreaterThanOrEqual(0);
    expect(stableHash('engineering')).toBeLessThan(2 ** 32);
    expect(stableHash('engineering')).not.toBe(stableHash('executive'));
  });
});
