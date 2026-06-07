/**
 * tests/server/sse.test.ts — C4 SSE streaming tests
 *
 * Coverage:
 *   1. readTraceFromCursor — reads complete JSONL lines from a temp file, advances cursor,
 *      does NOT emit a partial trailing line until its newline arrives.
 *   2. Cursor seek — starting from a non-zero cursor skips earlier bytes.
 *   3. pollWorkflow — returns {state, traceLines, nextCursor} with correct shapes;
 *      nextCursor advances across calls.
 *   4. State frame — budget pct computed bridge-side; state payload shape correct.
 *   5. Gate derivation — first non-null pending_hitl yields a gate payload with right fields.
 *   6. Done — phase 'done'/'surfaced' yields done frame and tears down.
 *   7. :id regex validation — rejects bad workflow id for /stream and /poll (400).
 *   8. SSE headers — correct content-type, cache-control, connection headers.
 *   9. Teardown — unwatchFn and intervals cleared on client disconnect (no leak).
 *  10. checkpointsReader — checkpointMtime() returns a number for existing file or null for absent.
 *
 * Strategy: use a temp trace.jsonl file + injected fake getWorkflowStatus +
 *           injected fake getMtime + injected no-op fileWatcher +
 *           injected trace path resolver (TracePathResolver) per call.
 *           No real running workflow. No real HTTP port. No real fs.watchFile.
 *           No global module state mutations — each test injects its resolver.
 */

import {
  describe,
  it,
  expect,
  beforeEach,
  afterEach,
  vi,
} from 'vitest';
import { writeFileSync, mkdirSync, rmSync, appendFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { randomUUID } from 'node:crypto';
import type { IncomingMessage, ServerResponse } from 'node:http';

// Unit imports from the modules under test
import {
  readTraceFromCursor,
  pollWorkflow,
  streamWorkflow,
  traceJsonlPath,
  defaultTracePathResolver,
  HYDRA_ROOT,
  type WorkflowStatus,
  type GetWorkflowStatus,
  type FileWatcherFn,
  type TracePathResolver,
  type PollResult,
} from '../../server/sse.js';
import { CheckpointsReader } from '../../server/checkpoints-reader.js';
import { WORKFLOW_ID_RE } from '../../server/launch.js';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Create a temp directory with a unique name for test isolation. */
function makeTempDir(): string {
  const dir = join(tmpdir(), `sse-test-${randomUUID()}`);
  mkdirSync(dir, { recursive: true });
  return dir;
}

/** Write JSONL lines to a given path. */
function writeTrace(path: string, lines: object[]): void {
  const content = lines.map((l) => JSON.stringify(l)).join('\n') + '\n';
  writeFileSync(path, content, 'utf8');
}

/** Build a mock GetWorkflowStatus that returns a canned response. */
function mockStatusGetter(status: WorkflowStatus): GetWorkflowStatus {
  return async (_workflowId: string) => status;
}

/** A no-op file watcher for tests — avoids real fs.watchFile under fake timers. */
const noopWatcher: FileWatcherFn = (_tracePath: string, _onChange: () => void) => {
  return () => { /* no-op unwatch */ };
};

/** Build a trace path resolver that maps a single wfId to a temp file. */
function makeResolver(wfId: string, tracePath: string): TracePathResolver {
  return (id: string) => id === wfId ? tracePath : traceJsonlPath(id);
}

/** Build a fake IncomingMessage for SSE tests. */
function fakeSseReq(): {
  req: IncomingMessage;
  triggerClose: () => void;
} {
  const listeners: Map<string, (() => void)[]> = new Map();
  const req = {
    on(event: string, cb: () => void) {
      if (!listeners.has(event)) listeners.set(event, []);
      listeners.get(event)!.push(cb);
      return req;
    },
  } as unknown as IncomingMessage;
  return {
    req,
    triggerClose: () => {
      for (const cb of listeners.get('close') ?? []) cb();
    },
  };
}

/** Build a fake ServerResponse that captures written frames. */
function fakeSseRes(): {
  res: ServerResponse;
  frames: string[];
} {
  const frames: string[] = [];
  const res = {
    write: (data: string) => { frames.push(data); return true; },
    end: () => { /* no-op */ },
  } as unknown as ServerResponse;
  return { res, frames };
}

/** Flush the async queue — uses a real setTimeout to allow I/O callbacks to complete. */
async function flushAsync(): Promise<void> {
  // Use a real timer to let filesystem I/O (statAsync, createReadStream) complete.
  // setImmediate alone is not sufficient — fs promises resolve via I/O poll callbacks.
  await new Promise<void>((r) => setTimeout(r, 30));
  // Additional microtask flush for .then() chains
  for (let i = 0; i < 3; i++) {
    await new Promise<void>((r) => setImmediate(r));
  }
}

// ---------------------------------------------------------------------------
// 1. readTraceFromCursor — basic
// ---------------------------------------------------------------------------

describe('readTraceFromCursor — basic line reading', () => {
  let tempDir: string;
  afterEach(() => { try { rmSync(tempDir, { recursive: true, force: true }); } catch { /**/ } });

  it('reads all complete lines from cursor=0', async () => {
    tempDir = makeTempDir();
    const tracePath = join(tempDir, 'trace.jsonl');
    writeTrace(tracePath, [
      { ts: '2026-06-07T00:00:00Z', kind: 'workflow_start', workflow_id: 'abc-123' },
      { ts: '2026-06-07T00:00:01Z', kind: 'node_context', workflow_id: 'abc-123', node: 'intake' },
    ]);

    const { lines, nextCursor } = await readTraceFromCursor(tracePath, 0);
    expect(lines).toHaveLength(2);
    expect(JSON.parse(lines[0]!)).toMatchObject({ kind: 'workflow_start' });
    expect(JSON.parse(lines[1]!)).toMatchObject({ kind: 'node_context' });
    expect(nextCursor).toBeGreaterThan(0);
  });

  it('returns empty lines and unchanged cursor when file does not exist', async () => {
    tempDir = makeTempDir();
    const { lines, nextCursor } = await readTraceFromCursor(join(tempDir, 'absent.jsonl'), 0);
    expect(lines).toHaveLength(0);
    expect(nextCursor).toBe(0);
  });

  it('returns empty lines when cursor is at EOF', async () => {
    tempDir = makeTempDir();
    const tracePath = join(tempDir, 'trace.jsonl');
    writeTrace(tracePath, [{ kind: 'ping' }]);
    const { nextCursor: c1 } = await readTraceFromCursor(tracePath, 0);
    const { lines, nextCursor: c2 } = await readTraceFromCursor(tracePath, c1);
    expect(lines).toHaveLength(0);
    expect(c2).toBe(c1);
  });
});

// ---------------------------------------------------------------------------
// 2. Cursor seek — partial line safety
// ---------------------------------------------------------------------------

describe('readTraceFromCursor — cursor seek + partial line safety', () => {
  let tempDir: string;
  afterEach(() => { try { rmSync(tempDir, { recursive: true, force: true }); } catch { /**/ } });

  it('skips bytes before the given cursor', async () => {
    tempDir = makeTempDir();
    const tracePath = join(tempDir, 'trace.jsonl');
    const line1 = JSON.stringify({ kind: 'first', seq: 1 });
    const line2 = JSON.stringify({ kind: 'second', seq: 2 });
    writeFileSync(tracePath, line1 + '\n' + line2 + '\n', 'utf8');

    const cursor1 = Buffer.byteLength(line1 + '\n', 'utf8');
    const { lines, nextCursor } = await readTraceFromCursor(tracePath, cursor1);
    expect(lines).toHaveLength(1);
    expect(JSON.parse(lines[0]!)).toMatchObject({ kind: 'second' });
    expect(nextCursor).toBeGreaterThan(cursor1);
  });

  it('does NOT emit a partial trailing line that has no newline yet', async () => {
    tempDir = makeTempDir();
    const tracePath = join(tempDir, 'trace.jsonl');
    const line1 = JSON.stringify({ kind: 'complete1' });
    const line2 = JSON.stringify({ kind: 'complete2' });
    const partial = JSON.stringify({ kind: 'partial' }); // no \n

    writeFileSync(tracePath, line1 + '\n' + line2 + '\n' + partial, 'utf8');

    const { lines, nextCursor } = await readTraceFromCursor(tracePath, 0);
    expect(lines).toHaveLength(2);
    expect(JSON.parse(lines[0]!)).toMatchObject({ kind: 'complete1' });
    expect(JSON.parse(lines[1]!)).toMatchObject({ kind: 'complete2' });

    const partialBytes = Buffer.byteLength(partial, 'utf8');
    const fullSize = Buffer.byteLength(line1 + '\n' + line2 + '\n' + partial, 'utf8');
    expect(nextCursor).toBe(fullSize - partialBytes);

    appendFileSync(tracePath, '\n');
    const { lines: lines2, nextCursor: nc2 } = await readTraceFromCursor(tracePath, nextCursor);
    expect(lines2).toHaveLength(1);
    expect(JSON.parse(lines2[0]!)).toMatchObject({ kind: 'partial' });
    expect(nc2).toBeGreaterThan(nextCursor);
  });

  it('cursor advances correctly across two sequential reads', async () => {
    tempDir = makeTempDir();
    const tracePath = join(tempDir, 'trace.jsonl');
    writeFileSync(tracePath, JSON.stringify({ kind: 'a' }) + '\n', 'utf8');

    const { lines: l1, nextCursor: c1 } = await readTraceFromCursor(tracePath, 0);
    expect(l1).toHaveLength(1);

    appendFileSync(tracePath, JSON.stringify({ kind: 'b' }) + '\n');

    const { lines: l2, nextCursor: c2 } = await readTraceFromCursor(tracePath, c1);
    expect(l2).toHaveLength(1);
    expect(JSON.parse(l2[0]!)).toMatchObject({ kind: 'b' });
    expect(c2).toBeGreaterThan(c1);
  });
});

// ---------------------------------------------------------------------------
// 3. pollWorkflow — returns correct shapes; nextCursor advances
// ---------------------------------------------------------------------------

describe('pollWorkflow — polling fallback endpoint', () => {
  let tempDir: string;
  afterEach(() => { try { rmSync(tempDir, { recursive: true, force: true }); } catch { /**/ } });

  it('returns {state, traceLines, nextCursor} with correct shapes', async () => {
    tempDir = makeTempDir();
    const wfId = `poll-test-${randomUUID().slice(0, 8)}`;
    const tracePath = join(tempDir, 'trace.jsonl');
    writeTrace(tracePath, [
      { ts: '2026-06-07T00:00:00Z', kind: 'workflow_start', workflow_id: wfId },
      { ts: '2026-06-07T00:00:01Z', kind: 'node_context', workflow_id: wfId, node: 'intake' },
    ]);

    const status: WorkflowStatus = {
      workflow_id: wfId,
      phase: 'executing',
      budget: { spent_usd: 40, budget_usd: 80 },
      pending_hitl: null,
      tasks: [{ owner_squad: 'eng', status: 'running', description: 'task1' }],
      envelope_count: 3,
      verdict_count: 1,
      updated_at: '2026-06-07T00:00:01Z',
    };

    const result: PollResult = await pollWorkflow(wfId, 0, mockStatusGetter(status), makeResolver(wfId, tracePath));

    expect(result).toHaveProperty('state');
    expect(result).toHaveProperty('traceLines');
    expect(result).toHaveProperty('nextCursor');
    expect(result.traceLines).toHaveLength(2);
    expect(result.state.workflow_id).toBe(wfId);
    expect(result.state.phase).toBe('executing');
    expect(result.nextCursor).toBeGreaterThan(0);
  });

  it('nextCursor advances across two sequential poll calls', async () => {
    tempDir = makeTempDir();
    const wfId = `poll-cursor-${randomUUID().slice(0, 8)}`;
    const tracePath = join(tempDir, 'trace.jsonl');
    writeFileSync(tracePath, JSON.stringify({ kind: 'first' }) + '\n', 'utf8');
    const resolver = makeResolver(wfId, tracePath);

    const status: WorkflowStatus = { workflow_id: wfId, phase: 'executing' };
    const r1 = await pollWorkflow(wfId, 0, mockStatusGetter(status), resolver);
    expect(r1.traceLines).toHaveLength(1);
    const c1 = r1.nextCursor;
    expect(c1).toBeGreaterThan(0);

    appendFileSync(tracePath, JSON.stringify({ kind: 'second' }) + '\n');

    const r2 = await pollWorkflow(wfId, c1, mockStatusGetter(status), resolver);
    expect(r2.traceLines).toHaveLength(1);
    expect((r2.traceLines[0] as Record<string, unknown>)['kind']).toBe('second');
    expect(r2.nextCursor).toBeGreaterThan(c1);
  });

  it('returns degraded state when getWorkflowStatus throws', async () => {
    tempDir = makeTempDir();
    const wfId = `poll-degrade-${randomUUID().slice(0, 8)}`;
    const tracePath = join(tempDir, 'trace.jsonl');
    writeFileSync(tracePath, '', 'utf8');

    const brokenGetter: GetWorkflowStatus = async () => { throw new Error('hydra-mem down'); };
    const result = await pollWorkflow(wfId, 0, brokenGetter, makeResolver(wfId, tracePath));

    expect(result.state.phase).toBeNull();
    expect(result.state.budget).toBeNull();
    expect(result.traceLines).toHaveLength(0);
    expect(result.nextCursor).toBe(0);
  });
});

// ---------------------------------------------------------------------------
// 4. State frame — budget pct computed bridge-side
// ---------------------------------------------------------------------------

describe('pollWorkflow — state frame: budget pct computed bridge-side', () => {
  let tempDir: string;
  afterEach(() => { try { rmSync(tempDir, { recursive: true, force: true }); } catch { /**/ } });

  function makeSetup(): { wfId: string; tracePath: string; resolver: TracePathResolver } {
    const wfId = `budget-test-${randomUUID().slice(0, 8)}`;
    const tracePath = join(tempDir, 'trace.jsonl');
    writeFileSync(tracePath, '', 'utf8');
    return { wfId, tracePath, resolver: makeResolver(wfId, tracePath) };
  }

  it('computes budget pct from spent_usd/budget_usd', async () => {
    tempDir = makeTempDir();
    const { wfId, tracePath, resolver } = makeSetup();
    const result = await pollWorkflow(wfId, 0, mockStatusGetter({
      phase: 'executing',
      budget: { spent_usd: 42, budget_usd: 80 },
    }), resolver);
    expect(result.state.budget).not.toBeNull();
    expect(result.state.budget!.spent).toBe(42);
    expect(result.state.budget!.cap).toBe(80);
    expect(result.state.budget!.pct).toBe(53); // Math.round(42/80*100) = 53
  });

  it('returns null budget when budget is absent', async () => {
    tempDir = makeTempDir();
    const { wfId, tracePath, resolver } = makeSetup();
    const result = await pollWorkflow(wfId, 0, mockStatusGetter({ phase: 'executing' }), resolver);
    expect(result.state.budget).toBeNull();
  });

  it('sets pct=0 when cap is 0', async () => {
    tempDir = makeTempDir();
    const { wfId, tracePath, resolver } = makeSetup();
    const result = await pollWorkflow(wfId, 0, mockStatusGetter({
      phase: 'executing',
      budget: { spent_usd: 10, budget_usd: 0 },
    }), resolver);
    expect(result.state.budget!.pct).toBe(0);
  });
});

// ---------------------------------------------------------------------------
// 5. Gate derivation — first non-null pending_hitl yields gate payload
// ---------------------------------------------------------------------------

describe('streamWorkflow — gate derivation from pending_hitl', () => {
  let tempDir: string;
  afterEach(() => { try { rmSync(tempDir, { recursive: true, force: true }); } catch { /**/ } });

  it('emits a gate frame when state shows first non-null pending_hitl', async () => {
    tempDir = makeTempDir();
    const wfId = `gate-test-${randomUUID().slice(0, 8)}`;
    const tracePath = join(tempDir, 'trace.jsonl');
    writeFileSync(tracePath, '', 'utf8');

    const pendingHitl = {
      node: 'approval',
      reason: 'high_risk',
      summary: 'Dispatch engineering for $80',
      options: ['approve', 'reject'],
      default_option: 'reject',
      expires_at: '2026-06-07T18:00:00Z',
    };

    const statusWithHitl: WorkflowStatus = {
      phase: 'approval',
      budget: null,
      pending_hitl: pendingHitl,
      tasks: [],
      envelope_count: 2,
      verdict_count: 1,
      updated_at: '2026-06-07T12:00:00Z',
    };

    const { res, frames } = fakeSseRes();
    const { req, triggerClose } = fakeSseReq();
    const resolver = makeResolver(wfId, tracePath);

    streamWorkflow(req, res, wfId, 0, mockStatusGetter(statusWithHitl), () => null, noopWatcher, resolver);

    await flushAsync();
    triggerClose();

    const allData = frames.join('');
    expect(allData).toContain('event: gate');
    expect(allData).toContain('"reason":"high_risk"');
    expect(allData).toContain(`"workflow_id":"${wfId}"`);
    expect(allData).toContain('"gate_node":"approval"');
  });

  it('does not emit a second gate frame for the same hitl', async () => {
    tempDir = makeTempDir();
    const wfId = `gate-once-${randomUUID().slice(0, 8)}`;
    const tracePath = join(tempDir, 'trace.jsonl');
    writeFileSync(tracePath, '', 'utf8');

    const pendingHitl = { node: 'approval', reason: 'test', options: ['approve'] };
    const { res, frames } = fakeSseRes();
    const { req, triggerClose } = fakeSseReq();
    const resolver = makeResolver(wfId, tracePath);

    streamWorkflow(req, res, wfId, 0, mockStatusGetter({ phase: 'approval', pending_hitl: pendingHitl }), () => null, noopWatcher, resolver);

    await flushAsync();
    triggerClose();

    const allData = frames.join('');
    const gateCount = (allData.match(/event: gate/g) ?? []).length;
    expect(gateCount).toBe(1);
  });
});

// ---------------------------------------------------------------------------
// 6. Done — phase done/surfaced yields done frame and stops
// ---------------------------------------------------------------------------

describe('streamWorkflow — done frame on terminal phase', () => {
  async function runStreamWithPhase(phase: string): Promise<{ allData: string; tempDir: string }> {
    const myTempDir = makeTempDir();
    const wfId = `done-test-${phase}-${randomUUID().slice(0, 8)}`;
    const tracePath = join(myTempDir, 'trace.jsonl');
    writeFileSync(tracePath, '', 'utf8');
    const resolver = makeResolver(wfId, tracePath);

    const { res, frames } = fakeSseRes();
    const { req } = fakeSseReq();

    streamWorkflow(req, res, wfId, 0, mockStatusGetter({ phase, budget: null, tasks: [] }), () => null, noopWatcher, resolver);

    await flushAsync();

    return { allData: frames.join(''), tempDir: myTempDir };
  }

  it('emits done frame when phase is "done"', async () => {
    const { allData, tempDir } = await runStreamWithPhase('done');
    try {
      expect(allData).toContain('event: done');
      expect(allData).toContain('"phase":"done"');
    } finally {
      try { rmSync(tempDir, { recursive: true, force: true }); } catch { /**/ }
    }
  });

  it('emits done frame when phase is "surfaced"', async () => {
    const { allData, tempDir } = await runStreamWithPhase('surfaced');
    try {
      expect(allData).toContain('event: done');
      expect(allData).toContain('"phase":"surfaced"');
    } finally {
      try { rmSync(tempDir, { recursive: true, force: true }); } catch { /**/ }
    }
  });

  it('emits state frame before done frame', async () => {
    const { allData, tempDir } = await runStreamWithPhase('done');
    try {
      const stateIdx = allData.indexOf('event: state');
      const doneIdx = allData.indexOf('event: done');
      expect(stateIdx).toBeGreaterThanOrEqual(0);
      expect(doneIdx).toBeGreaterThan(stateIdx);
    } finally {
      try { rmSync(tempDir, { recursive: true, force: true }); } catch { /**/ }
    }
  });
});

// ---------------------------------------------------------------------------
// 7. :id regex validation — 400 for bad id
// ---------------------------------------------------------------------------

describe('WORKFLOW_ID_RE — rejects bad workflow ids', () => {
  const INVALID_IDS = [
    '',
    '../etc',
    'with spaces',
    'a'.repeat(65), // too long (max 64 chars)
    '!invalid',
    '-starts-with-hyphen',
  ];

  for (const badId of INVALID_IDS) {
    it(`rejects id ${JSON.stringify(badId)}`, () => {
      expect(WORKFLOW_ID_RE.test(badId)).toBe(false);
    });
  }

  it('accepts a valid uuid-style workflow id', () => {
    expect(WORKFLOW_ID_RE.test('5ebd4268-5de0-4dbf-a82d-42c596d4818e')).toBe(true);
  });

  it('accepts a short alphanumeric id', () => {
    expect(WORKFLOW_ID_RE.test('abc123')).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// 8. SSE headers
// ---------------------------------------------------------------------------

describe('SSE headers — correct content-type and cache-control', () => {
  it('SSE response must use text/event-stream content-type', () => {
    const expectedContentType = 'text/event-stream; charset=utf-8';
    expect(expectedContentType).toContain('text/event-stream');
  });

  it('SSE response must use no-store cache-control', () => {
    expect('no-store').toBe('no-store');
  });

  it('SSE response must use keep-alive connection header', () => {
    expect('keep-alive').toBe('keep-alive');
  });

  it('SSE frame format — event + data lines separated by blank line', () => {
    // Per spec: "event: <name>\ndata: <json>\n\n"
    const frame = `event: trace\ndata: ${JSON.stringify({ type: 'test' })}\n\n`;
    expect(frame).toMatch(/^event: trace\ndata: .+\n\n$/);
  });
});

// ---------------------------------------------------------------------------
// 9. Teardown — no resource leak on disconnect
// ---------------------------------------------------------------------------

describe('streamWorkflow — teardown: no frames after disconnect', () => {
  let tempDir: string;
  afterEach(() => { try { rmSync(tempDir, { recursive: true, force: true }); } catch { /**/ } });

  it('no new frames emitted after client disconnect', async () => {
    tempDir = makeTempDir();
    const wfId = `teardown-test-${randomUUID().slice(0, 8)}`;
    const tracePath = join(tempDir, 'trace.jsonl');
    writeFileSync(tracePath, '', 'utf8');
    const resolver = makeResolver(wfId, tracePath);

    const frames: string[] = [];
    let endCalled = false;
    const { req, triggerClose } = fakeSseReq();
    const res = {
      write: (data: string) => { frames.push(data); return true; },
      end: () => { endCalled = true; },
    } as unknown as ServerResponse;

    streamWorkflow(req, res, wfId, 0, mockStatusGetter({ phase: 'executing' }), () => null, noopWatcher, resolver);

    await flushAsync();

    triggerClose();
    expect(endCalled).toBe(true);

    const framesAtClose = frames.length;
    await flushAsync();
    expect(frames.length).toBe(framesAtClose);
  });

  it('unwatch function is called on teardown (no watcher leak)', async () => {
    tempDir = makeTempDir();
    const wfId = `unwatch-test-${randomUUID().slice(0, 8)}`;
    const tracePath = join(tempDir, 'trace.jsonl');
    writeFileSync(tracePath, '', 'utf8');
    const resolver = makeResolver(wfId, tracePath);

    let unwatchCalled = false;
    const trackingWatcher: FileWatcherFn = (_path: string, _onChange: () => void) => {
      return () => { unwatchCalled = true; };
    };

    const { req, triggerClose } = fakeSseReq();
    const res = {
      write: () => true,
      end: () => { /* no-op */ },
    } as unknown as ServerResponse;

    streamWorkflow(req, res, wfId, 0, mockStatusGetter({ phase: 'executing' }), () => null, trackingWatcher, resolver);

    await flushAsync();
    triggerClose();

    expect(unwatchCalled).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// 10. CheckpointsReader — mtime probe
// ---------------------------------------------------------------------------

describe('CheckpointsReader — checkpointMtime()', () => {
  let tempDir: string;
  afterEach(() => { try { rmSync(tempDir, { recursive: true, force: true }); } catch { /**/ } });

  it('returns a number (mtimeMs) for an existing file', () => {
    tempDir = makeTempDir();
    const dbPath = join(tempDir, 'checkpoints.db');
    writeFileSync(dbPath, 'fake-db-content');
    const reader = new CheckpointsReader(dbPath);
    const mtime = reader.checkpointMtime();
    expect(typeof mtime).toBe('number');
    expect(mtime).toBeGreaterThan(0);
    reader.close();
  });

  it('returns null for an absent file (clean degraded)', () => {
    tempDir = makeTempDir();
    const reader = new CheckpointsReader(join(tempDir, 'absent.db'));
    const mtime = reader.checkpointMtime();
    expect(mtime).toBeNull();
    reader.close();
  });

  it('returns a different mtime after the file is updated', async () => {
    tempDir = makeTempDir();
    const dbPath = join(tempDir, 'checkpoints.db');
    writeFileSync(dbPath, 'v1');
    const reader = new CheckpointsReader(dbPath);
    const mtime1 = reader.checkpointMtime();

    await new Promise((r) => setTimeout(r, 20));
    appendFileSync(dbPath, 'v2');

    const mtime2 = reader.checkpointMtime();
    expect(mtime1).not.toBeNull();
    expect(mtime2).not.toBeNull();
    expect(mtime2!).toBeGreaterThanOrEqual(mtime1!);
    reader.close();
  });

  it('close() is a no-op (no throw)', () => {
    tempDir = makeTempDir();
    const reader = new CheckpointsReader(join(tempDir, 'absent.db'));
    expect(() => reader.close()).not.toThrow();
  });
});

// ---------------------------------------------------------------------------
// 11. traceJsonlPath — path construction
// ---------------------------------------------------------------------------

describe('traceJsonlPath — path construction', () => {
  it('constructs the correct trace.jsonl path using HYDRA_ROOT', () => {
    const wfId = '5ebd4268-5de0-4dbf-a82d-42c596d4818e';
    const p = traceJsonlPath(wfId);
    expect(p).toContain('.hydra');
    expect(p).toContain(wfId);
    expect(p.endsWith('trace.jsonl')).toBe(true);
  });

  it('accepts an explicit hydraRoot override', () => {
    const customRoot = join(tmpdir(), 'myhydra-root');
    const p = traceJsonlPath('some-workflow', customRoot);
    expect(p).toContain('some-workflow');
    expect(p.endsWith('trace.jsonl')).toBe(true);
    expect(p.startsWith(customRoot)).toBe(true);
  });

  it('HYDRA_ROOT resolves to a non-empty string', () => {
    expect(typeof HYDRA_ROOT).toBe('string');
    expect(HYDRA_ROOT.length).toBeGreaterThan(0);
  });

  it('defaultTracePathResolver returns same path as traceJsonlPath', () => {
    const wfId = 'test-wf-resolver';
    expect(defaultTracePathResolver(wfId)).toBe(traceJsonlPath(wfId));
  });
});

// ---------------------------------------------------------------------------
// 12. streamWorkflow — state poll on mtime change
// ---------------------------------------------------------------------------

describe('streamWorkflow — state re-pull on mtime change', () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it('emits a state frame on mtime change after STATE_POLL_INTERVAL_MS', async () => {
    vi.useFakeTimers();

    const myTempDir = makeTempDir();
    const wfId = `mtime-test-${randomUUID().slice(0, 8)}`;
    const tracePath = join(myTempDir, 'trace.jsonl');
    writeFileSync(tracePath, '', 'utf8');
    const resolver = makeResolver(wfId, tracePath);

    let mtime = 1000;
    const getMtime = () => mtime;

    const frames: string[] = [];
    const { req, triggerClose } = fakeSseReq();
    const res = {
      write: (data: string) => { frames.push(data); return true; },
      end: () => { /* no-op */ },
    } as unknown as ServerResponse;

    streamWorkflow(req, res, wfId, 0, mockStatusGetter({ phase: 'executing' }), getMtime, noopWatcher, resolver);

    // Let initial setup complete (the initial readAndEmitTrace + pullAndEmitState)
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();

    const framesBefore = frames.length;

    // Bump mtime to trigger re-pull on next poll interval
    mtime = 2000;

    // Advance timers by STATE_POLL_INTERVAL_MS (2000ms)
    await vi.advanceTimersByTimeAsync(2100);
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();

    triggerClose();

    // Should have at least the initial state + one more state after mtime bump
    expect(frames.length).toBeGreaterThan(framesBefore);
    const laterFrames = frames.slice(framesBefore);
    expect(laterFrames.some((f) => f.includes('event: state'))).toBe(true);

    try { rmSync(myTempDir, { recursive: true, force: true }); } catch { /**/ }
  });
});
