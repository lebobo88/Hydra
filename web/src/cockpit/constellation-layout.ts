/**
 * Hydra Cockpit — deterministic constellation geometry.
 *
 * Pure, side-effect-free layout maths for the Launchpad constellation. Lives in
 * its own module so the collision-free placement invariant can be unit-tested
 * directly (see tests/ui/launchpad.test.tsx) instead of only through a render.
 *
 * Heads are spread *evenly* across each crown's angular sector. The previous
 * scheme placed each slug at an independent `start + hash % span` angle, which
 * let two squads with nearby hashes land on the same point of the same ring and
 * overlap. Even spacing makes same-ring collisions impossible while staying
 * fully deterministic — within a crown, order is by stableHash.
 */

import { crownOf } from './crowns.ts';
import type { CrownFamily } from './crowns.ts';

/**
 * Stable hash of a string → unsigned 32-bit integer (djb2 variant). The same
 * slug always returns the same integer across renders and page loads — the
 * bedrock of the deterministic layout guarantee.
 */
export function stableHash(str: string): number {
  let h = 5381;
  for (let i = 0; i < str.length; i++) {
    h = ((h << 5) + h) ^ str.charCodeAt(i);
    h = h >>> 0; // unsigned 32-bit
  }
  return h;
}

/** Polar → Cartesian. 0° points to the top; angle increases clockwise. */
export function polarToXY(
  cx: number, cy: number, r: number, angleDeg: number,
): { x: number; y: number } {
  const rad = ((angleDeg - 90) * Math.PI) / 180;
  return { x: cx + r * Math.cos(rad), y: cy + r * Math.sin(rad) };
}

/** Angular sector (degrees) each crown family occupies, with seams between. */
export const CROWN_SECTOR: Record<CrownFamily, [number, number]> = {
  exec:    [0,   110],
  forge:   [120, 240],
  garland: [250, 360],
};

/** Base ring radius (px) per crown family. */
export const RING_R: Record<CrownFamily, number> = {
  exec:    100,
  forge:   140,
  garland: 175,
};

export interface HeadGeometry {
  slug: string;
  x: number;
  y: number;
  angle: number;
  crown: CrownFamily;
  /** 0/1 alternating tier — pushes every other label further out so dense
      arcs (many garland/forge squads) keep their labels from colliding. */
  labelTier: number;
}

/**
 * Compute collision-free head geometry for the given squad slugs around a
 * centre (cx, cy). Deterministic: same input set → same output regardless of
 * input order (members of a crown are sorted by stableHash before spacing).
 */
export function distributeHeads(
  slugs: string[], cx: number, cy: number,
): HeadGeometry[] {
  const byCrown: Record<CrownFamily, string[]> = { exec: [], forge: [], garland: [] };
  slugs.forEach((slug) => { byCrown[crownOf(slug)].push(slug); });
  (Object.keys(byCrown) as CrownFamily[]).forEach((c) =>
    byCrown[c].sort((a, b) => (stableHash(a) - stableHash(b)) || a.localeCompare(b)),
  );

  const out: HeadGeometry[] = [];
  (Object.keys(byCrown) as CrownFamily[]).forEach((crown) => {
    const members = byCrown[crown];
    const n = members.length;
    if (n === 0) return;
    const [start, end] = CROWN_SECTOR[crown];
    const span = end - start;
    const baseR = RING_R[crown];
    members.forEach((slug, i) => {
      // Half-step inset keeps heads off the sector seams (and off the boundary
      // with the neighbouring crown's sector).
      const frac = n === 1 ? 0.5 : (i + 0.5) / n;
      const angle = start + span * frac;
      // Alternating radial stagger breaks tangency between neighbours and gives
      // the ring organic depth — deterministic by index parity.
      const r = baseR + (n > 2 ? (i % 2 === 0 ? -12 : 12) : 0);
      const { x, y } = polarToXY(cx, cy, r, angle);
      out.push({ slug, x, y, angle, crown, labelTier: n > 3 ? (i % 2) : 0 });
    });
  });
  return out;
}
