/**
 * web/server/sse.ts
 *
 * SSE streaming + polling fallback for the Hydra Cockpit trace tail.
 *
 * DESIGN:
 *   trace_path = <hydra_root>/.hydra/<workflow_id>/trace.jsonl
 *   (per hydra_core.telemetry.trace_path — project-relative, NOT ~/.hydra)
 *
 *   Two entry points:
 *     streamWorkflow(req, res, workflowId, cursor)
 *       — SSE stream (text/event-stream). Tails trace.jsonl via fs.watchFile
 *         + byte cursor. Separately polls checkpoints.db mtime every 2s to
 *         catch interrupt pauses (approval/synthesis boundaries). Pings every 15s.
 *         Emits: trace | state | gate | ping | done
 *
 *     pollWorkflow(workflowId, cursor, getWorkflowStatus)
 *       — one-shot polling read for environments without SSE.
 *         Returns { state, traceLines, nextCursor }.
 *
 * EVENT SCHEMA (emitted to browser):
 *   trace  { ts, kind, workflow_id, ...kindSpecificFields }
 *            — raw trace record forwarded verbatim from trace.jsonl
 *   state  { workflow_id, phase, budget: { spent, cap, pct } | null,
 *             pending_hitl, tasks, envelope_count, verdict_count, updated_at }
 *            — current workflow state; emitted on checkpoint mtime bump
 *   gate   { workflow_id, gate_node, reason, summary, options, default_option, expires_at }
 *            — derived from first non-null pending_hitl; emitted once per hitl surfacing
 *   ping   (SSE comment frame ":" — keeps connection + proxies alive)
 *   done   { workflow_id, phase }
 *            — emitted when phase ∈ {done, surfaced}; connection closed
 *
 * PARTIAL-LINE SAFETY:
 *   The file reader accumulates bytes in a buffer. It only emits a JSON line
 *   when a complete '\n'-terminated sequence is found. A partial trailing line
 *   (no newline yet) is held in the buffer until the next read delivers the rest.
 *
 * SECURITY:
 *   - workflowId is validated by the caller (WORKFLOW_ID_RE) before calling into
 *     this module. This module does NO path-traversal-safe re-validation — the
 *     caller is responsible.
 *   - The trace.jsonl path is constructed from a resolved Hydra root + a
 *     validated workflow_id. No user-supplied path components are accepted.
 *   - SSE is a GET endpoint (read-only). No CSRF required.
 *     The Host-header check runs in index.ts before any route logic.
 */

import {
  createReadStream,
  watchFile,
  unwatchFile,
  existsSync,
  type Stats,
} from 'node:fs';
import { stat as statAsync } from 'node:fs/promises';
import { join } from 'node:path';
import type { IncomingMessage, ServerResponse } from 'node:http';
import { checkpointsReader } from './checkpoints-reader.js';
import { findHydraRoot } from './hydra-root.js';

// ---------------------------------------------------------------------------
// Hydra root resolution (shared — see ./hydra-root.ts)
// ---------------------------------------------------------------------------

export const HYDRA_ROOT = findHydraRoot();

// ---------------------------------------------------------------------------
// Trace path resolution
// ---------------------------------------------------------------------------

/**
 * Resolves the trace.jsonl path for a workflow.
 * Mirrors hydra_core.telemetry.trace_path():
 *   <project_root>/.hydra/<workflow_id>/trace.jsonl
 *
 * @param workflowId  Validated workflow id (caller's responsibility)
 * @param hydraRoot   Optional override for Hydra root (used in tests)
 */
export function traceJsonlPath(workflowId: string, hydraRoot?: string): string {
  return join(hydraRoot ?? HYDRA_ROOT, '.hydra', workflowId, 'trace.jsonl');
}

// ---------------------------------------------------------------------------
// Trace path resolver type — injectable for tests
// ---------------------------------------------------------------------------

/**
 * Resolver function type for trace.jsonl paths.
 * Production: use the default resolver (HYDRA_ROOT-based).
 * Tests: inject a custom resolver that returns temp file paths.
 */
export type TracePathResolver = (workflowId: string) => string;

/** Default production resolver — uses the module-level HYDRA_ROOT. */
export const defaultTracePathResolver: TracePathResolver = (wfId) => traceJsonlPath(wfId);

// ---------------------------------------------------------------------------
// SSE frame helpers
// ---------------------------------------------------------------------------

/**
 * Format a named SSE data frame.
 * Format: "event: <name>\ndata: <json>\n\n"
 */
function sseFrame(eventName: string, payload: unknown): string {
  return `event: ${eventName}\ndata: ${JSON.stringify(payload)}\n\n`;
}

/**
 * Format a SSE comment ping frame.
 * Format: ":\n\n"
 */
function ssePing(): string {
  return ':\n\n';
}

// ---------------------------------------------------------------------------
// State snapshot shape (emitted as 'state' SSE frame)
// ---------------------------------------------------------------------------

export interface BudgetSnapshot {
  spent: number;
  cap: number;
  pct: number;
}

export interface StatePayload {
  workflow_id: string;
  phase: string | null;
  budget: BudgetSnapshot | null;
  pending_hitl: unknown;
  tasks: unknown[];
  envelope_count: number;
  verdict_count: number;
  updated_at: string | null;
}

export interface GatePayload {
  workflow_id: string;
  gate_node: string | null;
  reason: string | null;
  summary: string | null;
  options: string[];
  default_option: string | null;
  expires_at: string | null;
}

// ---------------------------------------------------------------------------
// Budget pct computation (bridge-side, per design doc §2.4.1)
// ---------------------------------------------------------------------------

function computeBudgetSnapshot(budget: unknown): BudgetSnapshot | null {
  if (!budget || typeof budget !== 'object') return null;
  const b = budget as Record<string, unknown>;
  const spent = typeof b['spent_usd'] === 'number' ? b['spent_usd']
    : typeof b['spent'] === 'number' ? b['spent'] : 0;
  const cap = typeof b['budget_usd'] === 'number' ? b['budget_usd']
    : typeof b['cap'] === 'number' ? b['cap'] : 0;
  const pct = cap > 0 ? Math.round((spent / cap) * 100) : 0;
  return { spent, cap, pct };
}

// ---------------------------------------------------------------------------
// Gate derivation from pending_hitl
// ---------------------------------------------------------------------------

function deriveGatePayload(workflowId: string, pendingHitl: unknown): GatePayload | null {
  if (!pendingHitl || typeof pendingHitl !== 'object') return null;
  const hitl = pendingHitl as Record<string, unknown>;
  const options = Array.isArray(hitl['options'])
    ? (hitl['options'] as unknown[]).filter((o): o is string => typeof o === 'string')
    : typeof hitl['options'] === 'string'
      ? hitl['options'].split(',').map((s: string) => s.trim())
      : [];
  return {
    workflow_id: workflowId,
    gate_node: typeof hitl['node'] === 'string' ? hitl['node']
      : typeof hitl['interrupt_type'] === 'string' ? hitl['interrupt_type'] : null,
    reason: typeof hitl['reason'] === 'string' ? hitl['reason'] : null,
    summary: typeof hitl['summary'] === 'string' ? hitl['summary'] : null,
    options,
    default_option: typeof hitl['default_option'] === 'string' ? hitl['default_option']
      : typeof hitl['default'] === 'string' ? hitl['default'] : null,
    expires_at: typeof hitl['expires_at'] === 'string' ? hitl['expires_at']
      : typeof hitl['expires'] === 'string' ? hitl['expires'] : null,
  };
}

// ---------------------------------------------------------------------------
// workflow_status type (from hydra-mem.workflow_status via readTool)
// ---------------------------------------------------------------------------

export interface WorkflowStatus {
  workflow_id?: string;
  phase?: string | null;
  budget?: unknown;
  pending_hitl?: unknown;
  tasks?: unknown[];
  envelope_count?: number;
  verdict_count?: number;
  updated_at?: string | null;
}

// ---------------------------------------------------------------------------
// Trace file reader — byte-cursor + partial-line buffer
// ---------------------------------------------------------------------------

export interface TraceReadResult {
  /** Complete JSON lines (without trailing newline). */
  lines: string[];
  /** New byte cursor after reading. */
  nextCursor: number;
}

/**
 * Read new complete JSONL lines from a trace file starting at `cursor` bytes.
 * Partial trailing lines (no newline at EOF) are held back — not returned.
 * Returns the new byte cursor and the list of complete lines.
 *
 * This is a one-shot async read using a readable stream promise.
 * The stream is closed after the read to avoid holding the file handle.
 */
export async function readTraceFromCursor(
  tracePath: string,
  cursor: number,
): Promise<TraceReadResult> {
  // If the file doesn't exist yet, return empty with cursor unchanged
  if (!existsSync(tracePath)) {
    return { lines: [], nextCursor: cursor };
  }

  // Get file size to know how many bytes are available
  let fileSize: number;
  try {
    const st = await statAsync(tracePath);
    fileSize = st.size;
  } catch {
    return { lines: [], nextCursor: cursor };
  }

  if (fileSize <= cursor) {
    return { lines: [], nextCursor: cursor };
  }

  // Read bytes [cursor, fileSize)
  return new Promise<TraceReadResult>((resolve) => {
    const chunks: Buffer[] = [];
    const stream = createReadStream(tracePath, { start: cursor, end: fileSize - 1 });

    stream.on('data', (chunk: Buffer | string) => {
      chunks.push(typeof chunk === 'string' ? Buffer.from(chunk, 'utf8') : chunk);
    });

    stream.on('end', () => {
      const raw = Buffer.concat(chunks).toString('utf8');
      // Split on newlines, keeping track of partial last line
      const parts = raw.split('\n');
      // The last element is either '' (if raw ends with \n) or a partial line
      const trailingPartial = parts[parts.length - 1] ?? '';
      // Complete lines are all but the last element (they all had \n after them)
      const completeLines = parts.slice(0, -1).filter((l) => l.trim().length > 0);

      // Advance cursor by the number of bytes of complete lines + their newlines
      // = total bytes read minus trailing partial bytes
      const partialBytes = Buffer.byteLength(trailingPartial, 'utf8');
      const newCursor = fileSize - partialBytes;

      resolve({ lines: completeLines, nextCursor: newCursor });
    });

    stream.on('error', () => {
      resolve({ lines: [], nextCursor: cursor });
    });
  });
}

// ---------------------------------------------------------------------------
// TERMINAL PHASES
// ---------------------------------------------------------------------------

const TERMINAL_PHASES = new Set(['done', 'surfaced']);

function isTerminalPhase(phase: string | null | undefined): boolean {
  return typeof phase === 'string' && TERMINAL_PHASES.has(phase);
}

// ---------------------------------------------------------------------------
// SSE stream — GET /api/workflows/:id/stream
// ---------------------------------------------------------------------------

/**
 * Injectable workflow_status getter. In production, this calls readTool
 * via the bridge's hydra-mem client. In tests, this is mocked.
 */
export type GetWorkflowStatus = (workflowId: string) => Promise<WorkflowStatus>;

/**
 * Injectable checkpoints reader (for testing — allows injecting a fake mtime source).
 */
export type GetCheckpointMtime = () => number | null;

/**
 * Injectable file watcher for testing. Receives the trace path and a callback.
 * Returns an unwatch function.
 * Production: uses fs.watchFile (polling, cross-platform reliable on Windows).
 * Tests: inject a no-op to avoid real fs watches under fake timers.
 */
export type FileWatcherFn = (tracePath: string, onChange: () => void) => () => void;

// Intervals and timeouts
const STATE_POLL_INTERVAL_MS = 2_000;
const PING_INTERVAL_MS = 15_000;

/**
 * Default production file watcher — uses fs.watchFile (polling).
 * fs.watchFile is chosen over fs.watch for cross-platform reliability on Windows.
 */
const defaultFileWatcher: FileWatcherFn = (tracePath: string, onChange: () => void) => {
  const listener = (_curr: Stats, _prev: Stats): void => {
    onChange();
  };
  watchFile(tracePath, { interval: 1000, persistent: false }, listener);
  return () => {
    unwatchFile(tracePath, listener);
  };
};

/**
 * Attach an SSE stream to a workflow's trace.jsonl.
 * Handles cleanup on client disconnect.
 *
 * @param req            Incoming request (used to detect client disconnect)
 * @param res            Server response (headers already flushed by caller)
 * @param workflowId     Validated workflow id (caller must validate WORKFLOW_ID_RE)
 * @param initialCursor  Byte offset to start reading from (0 = from beginning)
 * @param getWorkflowStatus  Injected status getter (production: readTool wrapper)
 * @param getMtime       Injected mtime getter (default: checkpointsReader.checkpointMtime)
 * @param fileWatcher    Injected file watcher (default: fs.watchFile; tests: no-op)
 * @param resolveTracePath  Injected trace path resolver (default: defaultTracePathResolver)
 */
export function streamWorkflow(
  req: IncomingMessage,
  res: ServerResponse,
  workflowId: string,
  initialCursor: number,
  getWorkflowStatus: GetWorkflowStatus,
  getMtime: GetCheckpointMtime = () => checkpointsReader.checkpointMtime(),
  fileWatcher: FileWatcherFn = defaultFileWatcher,
  resolveTracePath: TracePathResolver = defaultTracePathResolver,
): void {
  const tracePath = resolveTracePath(workflowId);

  let cursor = initialCursor;
  let closed = false;
  let lastMtime: number | null = getMtime();
  let gateEmitted = false; // track if we've emitted a gate for the current hitl

  // -------------------------------------------------------------------------
  // Write helpers — guard against writing after close
  // -------------------------------------------------------------------------

  function writeFrame(frame: string): void {
    if (closed) return;
    try {
      res.write(frame);
    } catch {
      // Socket closed mid-write — teardown will handle it
    }
  }

  // -------------------------------------------------------------------------
  // Teardown
  // -------------------------------------------------------------------------

  let stateInterval: ReturnType<typeof setInterval> | null = null;
  let pingInterval: ReturnType<typeof setInterval> | null = null;
  let unwatchFn: (() => void) | null = null;

  function teardown(): void {
    if (closed) return;
    closed = true;

    if (stateInterval !== null) {
      clearInterval(stateInterval);
      stateInterval = null;
    }
    if (pingInterval !== null) {
      clearInterval(pingInterval);
      pingInterval = null;
    }
    if (unwatchFn !== null) {
      unwatchFn();
      unwatchFn = null;
    }

    try {
      res.end();
    } catch {
      // ignore
    }
  }

  // -------------------------------------------------------------------------
  // Trace reading + emitting
  // -------------------------------------------------------------------------

  async function readAndEmitTrace(): Promise<void> {
    if (closed) return;
    try {
      const { lines, nextCursor } = await readTraceFromCursor(tracePath, cursor);
      cursor = nextCursor;
      for (const line of lines) {
        if (closed) return;
        try {
          const parsed = JSON.parse(line) as unknown;
          writeFrame(sseFrame('trace', parsed));
        } catch {
          // Non-JSON line — skip
        }
      }
    } catch {
      // Read error — continue streaming; don't tear down
    }
  }

  // -------------------------------------------------------------------------
  // State pulling + 'state' / 'gate' / 'done' emission
  // -------------------------------------------------------------------------

  async function pullAndEmitState(): Promise<void> {
    if (closed) return;
    try {
      const status = await getWorkflowStatus(workflowId);
      const budget = computeBudgetSnapshot(status.budget);
      const statePayload: StatePayload = {
        workflow_id: workflowId,
        phase: status.phase ?? null,
        budget,
        pending_hitl: status.pending_hitl ?? null,
        tasks: Array.isArray(status.tasks) ? status.tasks : [],
        envelope_count: status.envelope_count ?? 0,
        verdict_count: status.verdict_count ?? 0,
        updated_at: status.updated_at ?? null,
      };
      writeFrame(sseFrame('state', statePayload));

      // Derive 'gate' frame from first non-null pending_hitl (emit once per hitl surfacing)
      const pendingHitl = status.pending_hitl;
      if (pendingHitl != null) {
        if (!gateEmitted) {
          const gate = deriveGatePayload(workflowId, pendingHitl);
          if (gate !== null) {
            writeFrame(sseFrame('gate', gate));
            gateEmitted = true;
          }
        }
      } else {
        // HITL cleared — reset so next one triggers a gate frame
        gateEmitted = false;
      }

      // 'done' frame when phase is terminal
      if (isTerminalPhase(status.phase)) {
        writeFrame(sseFrame('done', { workflow_id: workflowId, phase: status.phase }));
        teardown();
      }
    } catch {
      // State pull failed (hydra-mem unreachable) — degrade silently; keep streaming trace
    }
  }

  // -------------------------------------------------------------------------
  // File watcher — emit new trace lines on file change
  // -------------------------------------------------------------------------

  function setupFileWatcher(): void {
    const unwatch = fileWatcher(tracePath, () => {
      void readAndEmitTrace();
    });
    unwatchFn = unwatch;
  }

  // -------------------------------------------------------------------------
  // Main flow
  // -------------------------------------------------------------------------

  // 1. Emit existing trace lines from cursor
  void readAndEmitTrace().then(async () => {
    if (closed) return;

    // 2. Pull initial state + possibly emit gate/done
    await pullAndEmitState();
    if (closed) return;

    // 3. Set up file watcher for future appends
    if (existsSync(tracePath)) {
      setupFileWatcher();
    } else {
      // File doesn't exist yet — set up periodic check; once it appears, start watcher
      const appearCheck = setInterval(() => {
        if (closed) { clearInterval(appearCheck); return; }
        if (existsSync(tracePath)) {
          clearInterval(appearCheck);
          setupFileWatcher();
          void readAndEmitTrace();
        }
      }, 1000);
    }

    // 4. State poll every 2s (mtime-gated)
    stateInterval = setInterval(() => {
      if (closed) return;
      const currentMtime = getMtime();
      if (currentMtime !== null && currentMtime !== lastMtime) {
        lastMtime = currentMtime;
        void pullAndEmitState();
      }
    }, STATE_POLL_INTERVAL_MS);

    // 5. Ping every 15s
    pingInterval = setInterval(() => {
      if (closed) return;
      writeFrame(ssePing());
    }, PING_INTERVAL_MS);
  });

  // -------------------------------------------------------------------------
  // Client disconnect cleanup
  // -------------------------------------------------------------------------

  req.on('close', () => {
    teardown();
  });

  req.on('error', () => {
    teardown();
  });
}

// ---------------------------------------------------------------------------
// Polling fallback — GET /api/workflows/:id/poll
// ---------------------------------------------------------------------------

export interface PollResult {
  state: StatePayload;
  traceLines: unknown[];
  nextCursor: number;
}

/**
 * One-shot polling read — returns identical data shapes to the SSE stream
 * but as a single JSON response. For environments where EventSource is
 * unavailable (corporate proxies, etc.).
 *
 * @param workflowId        Validated workflow id
 * @param cursor            Byte offset from previous poll (0 = from beginning)
 * @param getWorkflowStatus Injected status getter
 * @param resolveTracePath  Injected trace path resolver (default: defaultTracePathResolver)
 */
export async function pollWorkflow(
  workflowId: string,
  cursor: number,
  getWorkflowStatus: GetWorkflowStatus,
  resolveTracePath: TracePathResolver = defaultTracePathResolver,
): Promise<PollResult> {
  const tracePath = resolveTracePath(workflowId);

  // Read new trace lines from cursor
  const { lines, nextCursor } = await readTraceFromCursor(tracePath, cursor);

  // Parse trace lines (skip unparseable)
  const traceLines: unknown[] = [];
  for (const line of lines) {
    try {
      traceLines.push(JSON.parse(line) as unknown);
    } catch {
      // skip
    }
  }

  // Pull current state
  let statePayload: StatePayload;
  try {
    const status = await getWorkflowStatus(workflowId);
    const budget = computeBudgetSnapshot(status.budget);
    statePayload = {
      workflow_id: workflowId,
      phase: status.phase ?? null,
      budget,
      pending_hitl: status.pending_hitl ?? null,
      tasks: Array.isArray(status.tasks) ? status.tasks : [],
      envelope_count: status.envelope_count ?? 0,
      verdict_count: status.verdict_count ?? 0,
      updated_at: status.updated_at ?? null,
    };
  } catch {
    // hydra-mem unreachable — return a degraded state
    statePayload = {
      workflow_id: workflowId,
      phase: null,
      budget: null,
      pending_hitl: null,
      tasks: [],
      envelope_count: 0,
      verdict_count: 0,
      updated_at: null,
    };
  }

  return { state: statePayload, traceLines, nextCursor };
}
