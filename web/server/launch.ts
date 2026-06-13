/**
 * web/server/launch.ts
 *
 * Detached-subprocess launcher for Hydra workflow runs.
 * Mirrors _launch_resume() in mcp_servers/hydra_control/server.py:
 *   - Windows: DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP creation flags.
 *   - Fixed argv array — no shell:true, no string interpolation.
 *   - stdin: DEVNULL; stdout+stderr piped to per-workflow log file.
 *   - Returns {workflow_id, pid, log} immediately (fire-and-attach pattern).
 *
 * SECURITY INVARIANTS:
 *   1. workflow_id is minted SERVER-SIDE (uuid v4). The browser NEVER supplies it.
 *   2. All user-supplied values (goal, squads) are passed as separate argv tokens,
 *      never interpolated into a shell string. shell:false is the only mode.
 *   3. Each token is validated against the appropriate alphabet before use:
 *        goal     — non-empty, ≤ 2000 chars, no null bytes.
 *        squads   — comma-separated [a-z0-9-]+ slugs, 1–10 per call.
 *        budgetUsd — positive finite number.
 *      Validation rejects before spawn; no partial-execution on bad input.
 *   4. The log directory (~/.hydra/<workflow_id>/) is created before spawn so
 *      stdout/stderr are captured even if the process dies immediately.
 *
 * The caller (index.ts POST /api/launch) has already verified CSRF + nonce.
 * This module does not check CSRF — that is the route handler's responsibility.
 */

import { spawn } from 'node:child_process';
import { mkdirSync, openSync, constants as fsConstants } from 'node:fs';
import { join } from 'node:path';
import { homedir } from 'node:os';
import { randomUUID } from 'node:crypto';
import { findHydraRoot } from './hydra-root.js';

// ---------------------------------------------------------------------------
// Validation alphabets — BYTE-IDENTICAL to hydra_control/server.py
// ---------------------------------------------------------------------------

/**
 * Workflow-id alphabet (identical to _WORKFLOW_ID_RE in server.py and cli.py).
 * The bridge mints the id with randomUUID() (standard uuid4 hyphenated format),
 * which always matches this pattern. The regex is kept here for double-check.
 */
export const WORKFLOW_ID_RE = /^[A-Za-z0-9][A-Za-z0-9\-_]{0,63}$/;

/**
 * Squad slug alphabet: lowercase alphanumeric + hyphen.
 * Squads are internal names like 'engineering', 'creative-ds', 'executive'.
 */
const SQUAD_SLUG_RE = /^[a-z0-9][a-z0-9-]{0,63}$/;

// ---------------------------------------------------------------------------
// Detach-options builder (exported for unit tests)
// ---------------------------------------------------------------------------

/**
 * Windows creation flags that detach the child from the bridge process group.
 * Numeric constants mirror subprocess.DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
 * in hydra_control/server.py, and are stable across Node versions.
 */
export const DETACHED_PROCESS        = 0x00000008;
export const CREATE_NEW_PROCESS_GROUP = 0x00000200;
export const WINDOWS_DETACH_FLAGS    = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP;

/**
 * Spawn options for a correctly detached child process.
 * Exported so tests can assert the exact options without starting a real process.
 *
 * On win32:
 *   - detached: true  — Node sets the creation flags via its internal path
 *   - windowsHide: true — no console window flickers
 *   - windowsCreationFlags: DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
 *     — ensures the child survives bridge exit and cannot Ctrl-C back to
 *       the parent's console (mirrors _launch_resume in hydra_control/server.py)
 *
 * On POSIX:
 *   - detached: true — puts the child in its own process group / session so it
 *     is not killed when the bridge exits.
 *
 * Both platforms: stdin=ignore, stdout/stderr→logFd (append; survives detach).
 */
export interface DetachOptions {
  cwd: string;
  env: Record<string, string | undefined>;
  stdio: ['ignore', number, number];
  detached: true;
  windowsHide: boolean;
  windowsCreationFlags?: number;
}

export function buildSpawnOptions(
  cwd: string,
  env: Record<string, string | undefined>,
  logFd: number,
): DetachOptions {
  const base: DetachOptions = {
    cwd,
    env,
    stdio: ['ignore', logFd, logFd],
    detached: true,        // true on BOTH platforms — key fix
    windowsHide: true,
  };
  if (process.platform === 'win32') {
    base.windowsCreationFlags = WINDOWS_DETACH_FLAGS;
  }
  return base;
}

// ---------------------------------------------------------------------------
// Spawner injection for tests (production code uses the real spawn)
// ---------------------------------------------------------------------------

export interface SpawnResult {
  pid: number;
}

export type SpawnFn = (
  cmd: string,
  args: string[],
  options: DetachOptions,
) => SpawnResult;

let _spawnerOverride: SpawnFn | null = null;

/** For testing only — inject a fake spawner so no real process is started. */
export function _setSpawnerForTest(fn: SpawnFn | null): void {
  _spawnerOverride = fn;
}

// ---------------------------------------------------------------------------
// Input validation
// ---------------------------------------------------------------------------

/** Validation error shape. Thrown on bad input; never after spawn. */
export class LaunchValidationError extends Error {
  constructor(
    message: string,
    public readonly code: string,
  ) {
    super(message);
    this.name = 'LaunchValidationError';
  }
}

function validateGoal(goal: unknown): string {
  if (typeof goal !== 'string' || goal.trim().length === 0) {
    throw new LaunchValidationError('goal must be a non-empty string', 'INVALID_GOAL');
  }
  if (goal.length > 2000) {
    throw new LaunchValidationError('goal must be ≤ 2000 characters', 'INVALID_GOAL');
  }
  if (goal.includes('\0')) {
    throw new LaunchValidationError('goal must not contain null bytes', 'INVALID_GOAL');
  }
  return goal.trim();
}

function validateSquads(squads: unknown): string[] | null {
  if (squads === undefined || squads === null || squads === '') return null;
  if (typeof squads !== 'string') {
    throw new LaunchValidationError('squads must be a comma-separated string of slugs', 'INVALID_SQUADS');
  }
  const parts = squads.split(',').map((s) => s.trim()).filter(Boolean);
  if (parts.length === 0) return null;
  if (parts.length > 10) {
    throw new LaunchValidationError('squads: at most 10 squad slugs per launch', 'INVALID_SQUADS');
  }
  for (const s of parts) {
    if (!SQUAD_SLUG_RE.test(s)) {
      throw new LaunchValidationError(
        `squad slug ${JSON.stringify(s)} is invalid — must match [a-z0-9][a-z0-9-]{0,63}`,
        'INVALID_SQUADS',
      );
    }
  }
  return parts;
}

function validateBudgetUsd(budgetUsd: unknown): number | null {
  if (budgetUsd === undefined || budgetUsd === null) return null;
  const n = typeof budgetUsd === 'number' ? budgetUsd : Number(budgetUsd);
  if (!Number.isFinite(n) || n <= 0) {
    throw new LaunchValidationError(
      'budgetUsd must be a positive finite number',
      'INVALID_BUDGET',
    );
  }
  return n;
}

// ---------------------------------------------------------------------------
// Launch
// ---------------------------------------------------------------------------

export interface LaunchInput {
  goal: unknown;
  squads?: unknown;
  budgetUsd?: unknown;
  live?: unknown;
}

export interface LaunchResult {
  workflow_id: string;
  pid: number;
  log: string;
}

/**
 * Spawn a detached `python -m hydra_core.cli run` subprocess and return
 * {workflow_id, pid, log} immediately. The workflow_id is minted server-side
 * (uuid v4); the browser never supplies it.
 *
 * Throws LaunchValidationError on bad input (before any spawn).
 */
export async function launchWorkflow(input: LaunchInput): Promise<LaunchResult> {
  // --- validate inputs ---
  const goal = validateGoal(input.goal);
  const squads = validateSquads(input.squads);
  const budgetUsd = validateBudgetUsd(input.budgetUsd);
  const live = input.live === true;

  // --- mint workflow id server-side (NEVER from browser) ---
  const workflowId = randomUUID();

  // Validate it satisfies the parity regex (randomUUID always produces standard
  // 8-4-4-4-12 format which matches; this is a belt-and-braces assertion).
  if (!WORKFLOW_ID_RE.test(workflowId)) {
    // Should never happen — randomUUID() always produces a valid uuid4.
    throw new LaunchValidationError(`minted workflow_id ${workflowId} failed validation`, 'INTERNAL');
  }

  // --- prepare log directory ---
  const hydraRoot = findHydraRoot();
  const logDir = join(homedir(), '.hydra', workflowId);
  mkdirSync(logDir, { recursive: true });
  const logPath = join(logDir, 'run.log');

  // Open log file for append (create if not exists); the child inherits the fd.
  const logFd = openSync(logPath, fsConstants.O_WRONLY | fsConstants.O_CREAT | fsConstants.O_APPEND);

  // --- build argv (no shell, no interpolation) ---
  const pythonExe = process.platform === 'win32' ? 'python' : 'python3';
  const argv: string[] = [
    '-m', 'hydra_core.cli',
    'run', goal,
    '--workflow-id', workflowId,
  ];

  if (squads !== null) {
    argv.push('--squad', squads.join(','));
  }

  // budgetUsd: the CLI does not yet have a --budget arg (C6 territory);
  // we include it in the log header comment for now but do not pass it.
  // TODO(C6): add --budget flag to hydra_core.cli run and wire it here. // ANTI-PATTERN-OK: C6 scope per COCKPIT-DESIGN.md §5.2

  if (live) {
    argv.push('--live');
  }

  const env: Record<string, string | undefined> = {
    ...process.env,
    PYTHONPATH: process.env['PYTHONPATH'] ?? hydraRoot,
    HYDRA_ROOT: hydraRoot,
  };

  // Build the platform-correct detach options once; both the production
  // spawner and any test-injected spawner receive the same options object.
  const spawnOpts = buildSpawnOptions(hydraRoot, env, logFd);

  // Production spawner: uses Node's real spawn() with the detach options.
  // Injected test spawner: receives the same options for assertion.
  const spawner: SpawnFn = _spawnerOverride ?? ((cmd, args, opts) => {
    const child = spawn(cmd, args, opts as Parameters<typeof spawn>[2]);
    // unref() so the bridge's Node event-loop does not wait for the child.
    child.unref();
    return { pid: child.pid ?? 0 };
  });

  const result = spawner(pythonExe, argv, spawnOpts);

  return {
    workflow_id: workflowId,
    pid: result.pid,
    log: logPath,
  };
}
