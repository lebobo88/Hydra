/**
 * web/server/checkpoints-reader.ts
 *
 * Lightweight change-detector for ~/.hydra/checkpoints.db (LangGraph SqliteSaver).
 *
 * DESIGN:
 *   - Opens the DB read-only (readonly:true, fileMustExist:true).
 *   - Exposes ONLY checkpointMtime() — the mtimeMs of the DB file via fs.statSync.
 *     The mtime bump is used by the SSE stream as a cheap "a checkpoint was written"
 *     signal, so we catch interrupt pauses (approval/synthesis/judge_synthesis) that
 *     checkpoint BEFORE emitting a trace line.
 *   - We intentionally do NOT query the DB schema here — workflow_status comes from
 *     the hydra-mem MCP client which already handles the SqliteSaver schema.
 *   - Guard: if the DB file is absent, checkpointMtime() returns null (clean degraded).
 *     No write, no migration, no schema access.
 *
 * Env override: HYDRA_CHECKPOINTS_DB — absolute path to checkpoints.db.
 * Default: ~/.hydra/checkpoints.db
 */

import { statSync } from 'node:fs';
import { join } from 'node:path';
import { homedir } from 'node:os';

// ---------------------------------------------------------------------------
// Path resolution
// ---------------------------------------------------------------------------

/** Resolve the checkpoints.db path. Env var overrides the default. */
export function resolveCheckpointsDbPath(): string {
  const envPath = process.env['HYDRA_CHECKPOINTS_DB'];
  if (typeof envPath === 'string' && envPath.length > 0) return envPath;
  return join(homedir(), '.hydra', 'checkpoints.db');
}

export const CHECKPOINTS_DB_PATH = resolveCheckpointsDbPath();

// ---------------------------------------------------------------------------
// CheckpointsReader — read-only mtime probe
// ---------------------------------------------------------------------------

export class CheckpointsReader {
  private readonly dbPath: string;

  constructor(dbPath: string = CHECKPOINTS_DB_PATH) {
    this.dbPath = dbPath;
  }

  /**
   * Returns the last-modified timestamp (milliseconds since epoch) of the
   * checkpoints.db file, or null if the file does not exist.
   *
   * This is used as a CHEAP CHANGE DETECTOR by the SSE streamer:
   *   previous = checkpointMtime();
   *   // ... 2s later ...
   *   current = checkpointMtime();
   *   if (current !== previous && current !== null) { re-pull workflow_status }
   *
   * Uses fs.statSync — this is synchronous but fast (single syscall) and called
   * from a setInterval, not from a hot path.
   */
  checkpointMtime(): number | null {
    try {
      const st = statSync(this.dbPath);
      return st.mtimeMs;
    } catch {
      // File absent or inaccessible — degrade cleanly
      return null;
    }
  }

  /**
   * No-op close for interface symmetry. Nothing to close: we do not hold
   * an open DB handle (by design — mtime is purely a filesystem stat).
   */
  close(): void {
    // intentionally no-op: no handle to close
  }
}

/** Singleton for production use. Tests may construct their own instance. */
export const checkpointsReader = new CheckpointsReader();
