/**
 * web/server/hydra-root.ts
 *
 * Single source of truth for locating the Hydra repository root from the web
 * bridge. Previously each of hydra-mem-client.ts, hydra-control-client.ts,
 * launch.ts, replay.ts and sse.ts carried its own copy of findHydraRoot() that
 * ended in a hardcoded `return 'C:/AiAppDeployments/Hydra'` — non-portable and
 * silently wrong on any other checkout. This module replaces all five copies.
 *
 * Resolution order:
 *   1. HYDRA_ROOT env var — explicit operator override. Used only if set AND
 *      the path exists on disk.
 *   2. Anchor-relative — derive __dirname from import.meta.url, then:
 *        a. probe the two historical candidates (resolve('..','..') and
 *           resolve('..','..','..')), and
 *        b. walk UP parent directories from __dirname (capped at ~6 levels)
 *      returning the first directory that looks like a Hydra checkout.
 *   3. Convenience — join(homedir(), 'AiAppDeployments', 'Hydra'), returned ONLY
 *      if it exists on disk (existence-gated, never a blind return).
 *
 * If nothing resolves, THROW. There is no hardcoded absolute fallback — callers
 * must set HYDRA_ROOT (or run from inside a real checkout) on any host where the
 * anchor-relative walk does not find the repo.
 *
 * A directory is recognised as the Hydra root when it contains all of the
 * stable top-level markers: hydra_core/, mcp_servers/ and CONSTITUTION.md. This
 * multi-marker sentinel is more robust than any single per-file probe and is
 * correct for every caller regardless of which subtree it cares about.
 */

import { existsSync } from 'node:fs';
import { homedir } from 'node:os';
import { join, resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

/** Stable top-level markers present in every Hydra checkout. */
const HYDRA_ROOT_MARKERS = ['hydra_core', 'mcp_servers', 'CONSTITUTION.md'] as const;

/** True when `dir` contains all the Hydra root markers. */
function isHydraRoot(dir: string): boolean {
  return HYDRA_ROOT_MARKERS.every((m) => existsSync(join(dir, m)));
}

/**
 * Locate the Hydra repository root. See module doc comment for the full
 * resolution order. Throws if the root cannot be determined.
 */
export function findHydraRoot(): string {
  // 1. Explicit env override
  const envRoot = process.env['HYDRA_ROOT'];
  if (typeof envRoot === 'string' && envRoot.length > 0 && existsSync(envRoot)) {
    return envRoot;
  }

  // 2. Anchor-relative discovery
  try {
    const __filename = fileURLToPath(import.meta.url);
    const __dirname = dirname(__filename);

    // 2a. Historical fixed candidates (server/ → web/ → Hydra/, or one deeper
    //     when running from a built dist-server/ directory).
    const candidates = [
      resolve(__dirname, '..', '..'),
      resolve(__dirname, '..', '..', '..'),
    ];
    for (const c of candidates) {
      if (isHydraRoot(c)) return c;
    }

    // 2b. Robust upward walk from __dirname (capped to avoid runaway walks).
    let dir = __dirname;
    for (let i = 0; i < 6; i += 1) {
      if (isHydraRoot(dir)) return dir;
      const parent = dirname(dir);
      if (parent === dir) break; // reached filesystem root
      dir = parent;
    }
  } catch {
    // import.meta.url unavailable — fall through to convenience tier
  }

  // 3. Best-effort convenience (existence-gated, never blind)
  const conventional = join(homedir(), 'AiAppDeployments', 'Hydra');
  if (existsSync(conventional)) return conventional;

  // Nothing resolved — fail loudly. No hardcoded absolute fallback.
  throw new Error(
    'Cannot locate Hydra repo root. Set the HYDRA_ROOT environment variable ' +
      'to your Hydra checkout path.',
  );
}
