/**
 * web/server/replay.ts
 *
 * Detached-subprocess launcher for Hydra workflow replay (C6).
 * Mirrors launch.ts (launchWorkflow) in structure and security invariants.
 *
 * SECURITY INVARIANTS:
 *   1. The replay_workflow_id is minted SERVER-SIDE (uuid v4). The browser
 *      NEVER supplies it. The source workflow_id is validated before use.
 *   2. All user-supplied values are passed as separate argv tokens,
 *      never interpolated into a shell string. shell:false is the only mode.
 *   3. Each token is validated against its alphabet before use:
 *        source_workflow_id — WORKFLOW_ID_RE (byte-identical to cli.py)
 *        from_phase         — enum of 8 known phases
 *        swap_model         — MODEL_ID_RE (reasonable model-id charset)
 *      Validation rejects before spawn; no partial-execution on bad input.
 *   4. The log directory (~/.hydra/<replay_id>/replay.log) is created before
 *      spawn so stdout/stderr are captured even if the process dies immediately.
 *
 * The caller (index.ts POST /api/replay) has already verified:
 *   - CSRF header
 *   - write whitelist (action='replay')
 *   - confirm nonce (High risk)
 *   - typed challenge (if live=true, venom-gated)
 * This module does NOT re-check those — that is the route handler's job.
 */

import { spawn } from 'node:child_process';
import { mkdirSync, openSync, constants as fsConstants, existsSync } from 'node:fs';
import { join } from 'node:path';
import { homedir } from 'node:os';
import { randomUUID } from 'node:crypto';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { buildSpawnOptions, WORKFLOW_ID_RE, type SpawnFn, type DetachOptions } from './launch.js';

// ---------------------------------------------------------------------------
// Validation alphabets — matching _KNOWN_PHASES and _MODEL_ID_RE in cli.py
// ---------------------------------------------------------------------------

/**
 * The 8 known phase names (mirrors _KNOWN_PHASES in cli.py / supervisor.py).
 * Used to validate --from-phase before building the argv array.
 */
export const KNOWN_PHASES = Object.freeze([
  'intake', 'planning', 'approval', 'dispatch',
  'executing', 'judge', 'synthesis', 'postcheck',
] as const);

export type KnownPhase = (typeof KNOWN_PHASES)[number];

/**
 * Model-id charset: alphanumeric plus hyphen, dot, underscore, slash, colon.
 * Covers ids like "claude-sonnet-4-6", "gpt-4o", "gemini-2-flash".
 * Mirrors _MODEL_ID_RE in cli.py.
 */
export const MODEL_ID_RE = /^[A-Za-z0-9][A-Za-z0-9\-_./:]{0,127}$/;

// ---------------------------------------------------------------------------
// Hydra root resolution (mirrors launch.ts)
// ---------------------------------------------------------------------------

function findHydraRoot(): string {
  const envRoot = process.env['HYDRA_ROOT'];
  if (envRoot && existsSync(envRoot)) return envRoot;

  try {
    const __filename = fileURLToPath(import.meta.url);
    const __dirname = dirname(__filename);
    const candidates = [
      resolve(__dirname, '..', '..'),
      resolve(__dirname, '..', '..', '..'),
    ];
    for (const c of candidates) {
      if (existsSync(join(c, 'hydra_core', 'cli.py'))) return c;
    }
  } catch {
    // import.meta.url unavailable
  }

  return 'C:/AiAppDeployments/Hydra';
}

// ---------------------------------------------------------------------------
// Validation error
// ---------------------------------------------------------------------------

export class ReplayValidationError extends Error {
  constructor(
    message: string,
    public readonly code: string,
  ) {
    super(message);
    this.name = 'ReplayValidationError';
  }
}

// ---------------------------------------------------------------------------
// Spawner injection for tests (mirrors launch.ts pattern)
// ---------------------------------------------------------------------------

let _replaySpawnerOverride: SpawnFn | null = null;

/** For testing only — inject a fake spawner so no real process is started. */
export function _setReplaySpawnerForTest(fn: SpawnFn | null): void {
  _replaySpawnerOverride = fn;
}

// ---------------------------------------------------------------------------
// Input validation
// ---------------------------------------------------------------------------

function validateSourceWorkflowId(id: unknown): string {
  if (typeof id !== 'string' || id.length === 0) {
    throw new ReplayValidationError(
      'source workflow_id must be a non-empty string',
      'INVALID_SOURCE_WORKFLOW_ID',
    );
  }
  if (!WORKFLOW_ID_RE.test(id)) {
    throw new ReplayValidationError(
      `source workflow_id ${JSON.stringify(id)} must match ^[A-Za-z0-9][A-Za-z0-9\\-_]{0,63}$`,
      'INVALID_SOURCE_WORKFLOW_ID',
    );
  }
  return id;
}

function validateFromPhase(phase: unknown): string {
  const p = typeof phase === 'string' ? phase.trim() : 'intake';
  if (p.length === 0) return 'intake';
  if (!(KNOWN_PHASES as readonly string[]).includes(p)) {
    throw new ReplayValidationError(
      `from_phase ${JSON.stringify(p)} is not a known phase. Valid: ${KNOWN_PHASES.join(', ')}`,
      'INVALID_FROM_PHASE',
    );
  }
  return p;
}

function validateSwapModel(model: unknown): string | null {
  if (model === undefined || model === null || model === '') return null;
  if (typeof model !== 'string') {
    throw new ReplayValidationError(
      'swap_model must be a string or absent',
      'INVALID_SWAP_MODEL',
    );
  }
  const m = model.trim();
  if (m.length === 0) return null;
  if (!MODEL_ID_RE.test(m)) {
    throw new ReplayValidationError(
      `swap_model ${JSON.stringify(m)} contains invalid characters. ` +
      'Must match ^[A-Za-z0-9][A-Za-z0-9\\-_./:]{{0,127}}$',
      'INVALID_SWAP_MODEL',
    );
  }
  return m;
}

// ---------------------------------------------------------------------------
// Replay launcher
// ---------------------------------------------------------------------------

export interface ReplayInput {
  /** Source workflow_id to replay from (server-validated). */
  sourceWorkflowId: unknown;
  /** Phase to restart from (default: 'intake'). */
  fromPhase?: unknown;
  /** Optional model to substitute. */
  swapModel?: unknown;
  /** True → live MCP dispatcher (real spend). False → dry reconstruct. */
  live?: unknown;
}

export interface ReplayResult {
  /** The newly-minted replay workflow id. */
  workflow_id: string;
  /** PID of the detached process (0 if spawner returned no pid). */
  pid: number;
  /** Absolute path to the replay log file. */
  log: string;
}

/**
 * Spawn a detached `python -m hydra_core.cli replay <source_id>` subprocess
 * and return {workflow_id, pid, log} immediately (fire-and-attach).
 *
 * The replay_workflow_id is minted server-side; the browser never supplies it.
 * Throws ReplayValidationError on bad input (before any spawn).
 */
export async function launchReplay(input: ReplayInput): Promise<ReplayResult> {
  // --- validate inputs ---
  const sourceWorkflowId = validateSourceWorkflowId(input.sourceWorkflowId);
  const fromPhase = validateFromPhase(input.fromPhase);
  const swapModel = validateSwapModel(input.swapModel);
  const live = input.live === true;

  // --- mint replay workflow id server-side (NEVER from browser) ---
  const replayWorkflowId = randomUUID();

  // Belt-and-braces assertion: randomUUID() always produces a valid uuid4.
  if (!WORKFLOW_ID_RE.test(replayWorkflowId)) {
    throw new ReplayValidationError(
      `minted replay workflow_id ${replayWorkflowId} failed validation`,
      'INTERNAL',
    );
  }

  // --- prepare log directory ---
  const hydraRoot = findHydraRoot();
  const logDir = join(homedir(), '.hydra', replayWorkflowId);
  mkdirSync(logDir, { recursive: true });
  const logPath = join(logDir, 'replay.log');

  const logFd = openSync(
    logPath,
    fsConstants.O_WRONLY | fsConstants.O_CREAT | fsConstants.O_APPEND,
  );

  // --- build argv (no shell, no interpolation) ---
  const pythonExe = process.platform === 'win32' ? 'python' : 'python3';
  const argv: string[] = [
    '-m', 'hydra_core.cli',
    'replay', sourceWorkflowId,
    '--from-phase', fromPhase,
  ];

  if (swapModel !== null) {
    argv.push('--swap-model', swapModel);
  }

  if (live) {
    argv.push('--live');
  }

  const env: Record<string, string | undefined> = {
    ...process.env,
    PYTHONPATH: process.env['PYTHONPATH'] ?? hydraRoot,
    HYDRA_ROOT: hydraRoot,
  };

  const spawnOpts = buildSpawnOptions(hydraRoot, env, logFd);

  const spawner: SpawnFn = _replaySpawnerOverride ?? ((cmd, args, opts) => {
    const child = spawn(cmd, args, opts as Parameters<typeof spawn>[2]);
    child.unref();
    return { pid: child.pid ?? 0 };
  });

  const result = spawner(pythonExe, argv, spawnOpts);

  return {
    workflow_id: replayWorkflowId,
    pid: result.pid,
    log: logPath,
  };
}
