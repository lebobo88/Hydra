/**
 * tests/server/memory-cells.test.ts
 *
 * Unit tests for the /api/memory/cells bridge handler.
 * Covers both contract mismatches described in the C8-memory-bug driver:
 *
 *   Bug 1 — GRID OVERVIEW (no cell param):
 *     Old: passed cell='' to query_eights → upstream returned {error:'invalid_cell'} → 502.
 *     Fix: fan out all 8 bagua keys, return {cells:[{cell,count},...], degraded?:true}.
 *
 *   Bug 2 — DRILL-DOWN (?cell=<key>):
 *     Old: returned raw query_eights shape {cell,rows,count} → SPA read body.records → undefined → empty.
 *     Fix: return {cell, records, rows, count} so body.records is populated.
 *
 * Strategy: inject a fake HydraMemClient via _setClientForTest() and spin up a
 * lightweight HTTP test server that calls the exported handle() function.
 * No real hydra_memory process is spawned.
 */

import { describe, it, expect, beforeAll, afterAll, beforeEach, afterEach } from 'vitest';
import { createServer, type IncomingMessage } from 'node:http';
import { createServer as createNetServer } from 'node:net';
import { handle, _setClientForTest } from '../../server/index.js';
import { HydraMemClient } from '../../server/hydra-mem-client.js';

// ---------------------------------------------------------------------------
// Helpers: mock HydraMemClient
// ---------------------------------------------------------------------------

/** Per-call args captured for assertion. */
interface CapturedCall {
  tool: string;
  args: Record<string, unknown>;
}

type CallResult = Record<string, unknown>;

/**
 * Build a fake HydraMemClient whose call() returns from a per-tool map.
 * If the map has a special entry '__perCell', call it as a function keyed by
 * the `cell` argument (used to mock per-cell responses for the overview fan-out).
 */
function buildMockClient(
  responses: Map<string, CallResult | ((args: Record<string, unknown>) => CallResult)>,
  captured?: CapturedCall[],
): HydraMemClient {
  const mock = Object.create(HydraMemClient.prototype) as HydraMemClient;
  (mock as unknown as {
    call: (tool: string, args: Record<string, unknown>) => Promise<CallResult>;
  }).call = async (tool: string, args: Record<string, unknown>) => {
    if (captured) captured.push({ tool, args });
    const respOrFn = responses.get(tool);
    if (respOrFn === undefined) {
      throw Object.assign(
        new Error(`mock: tool '${tool}' not registered`),
        { code: 'FORBIDDEN_TOOL' },
      );
    }
    return typeof respOrFn === 'function' ? respOrFn(args) : respOrFn;
  };
  return mock;
}

// ---------------------------------------------------------------------------
// Helpers: free-port probe + HTTP request helper
// ---------------------------------------------------------------------------

function findFreePort(): Promise<number> {
  return new Promise((resolve, reject) => {
    const srv = createNetServer();
    srv.once('error', reject);
    srv.listen(0, '127.0.0.1', () => {
      const addr = srv.address();
      srv.close(() => {
        if (addr && typeof addr === 'object') resolve(addr.port);
        else reject(new Error('could not determine free port'));
      });
    });
  });
}

type TestResponse = { status: number; body: unknown };

function makeRequest(
  port: number,
  path: string,
  options: { method?: string; headers?: Record<string, string> } = {},
): Promise<TestResponse> {
  return new Promise((resolve, reject) => {
    // eslint-disable-next-line @typescript-eslint/no-require-imports
    const req = require('node:http').request(
      {
        hostname: '127.0.0.1',
        port,
        path,
        method: options.method ?? 'GET',
        headers: { host: '127.0.0.1', ...options.headers },
      },
      (res: IncomingMessage) => {
        let data = '';
        res.on('data', (c: Buffer) => { data += c.toString(); });
        res.on('end', () => {
          try {
            resolve({ status: res.statusCode ?? 0, body: JSON.parse(data) });
          } catch {
            resolve({ status: res.statusCode ?? 0, body: data });
          }
        });
      },
    );
    req.on('error', reject);
    req.end();
  });
}

// ---------------------------------------------------------------------------
// Test harness: a real HTTP server backed by the bridge handle() function.
// Each describe block that needs HTTP sets up its own server + port.
// ---------------------------------------------------------------------------

const BAGUA_KEYS = ['qian', 'kun', 'zhen', 'xun', 'kan', 'li', 'gen', 'dui'] as const;

// Canned per-cell responses (simulates query_eights returning {cell, rows, count})
function makeCellResponse(key: string, count: number): CallResult {
  return {
    cell: key,
    rows: Array.from({ length: count }, (_, i) => ({
      key: `ep:wf-test:event:${i}`,
      workflow_id: 'wf-test',
      kind: 'phase_boundary',
      created_at: '2026-06-07T00:00:00Z',
      cells: [key],
    })),
    count,
  };
}

// ---------------------------------------------------------------------------
// OVERVIEW: GET /api/memory/cells (no cell param)
// ---------------------------------------------------------------------------

describe('/api/memory/cells (no param) — overview fan-out', () => {
  let testServer: ReturnType<typeof createServer>;
  let testPort: number;
  let capturedCalls: CapturedCall[];

  beforeAll(async () => {
    testPort = await findFreePort();
    testServer = createServer((req, res) => {
      handle(req, res).catch((e) => {
        res.writeHead(500, { 'content-type': 'application/json' });
        res.end(JSON.stringify({ error: 'internal error', code: 'INTERNAL' }));
        process.stderr.write(`[test-server] handle error: ${String(e)}\n`);
      });
    });
    await new Promise<void>((resolve) => testServer.listen(testPort, '127.0.0.1', resolve));
  });

  afterAll(async () => {
    _setClientForTest(null);
    await new Promise<void>((resolve, reject) =>
      testServer.close((e) => (e ? reject(e) : resolve())),
    );
  });

  beforeEach(() => {
    capturedCalls = [];
  });

  afterEach(() => {
    _setClientForTest(null);
  });

  it('returns 200 with cells array of 8 entries when all cells succeed', async () => {
    // Mock: each cell returns a distinct count
    const counts: Record<string, number> = {
      qian: 10, kun: 5, zhen: 3, xun: 0, kan: 7, li: 2, gen: 1, dui: 4,
    };
    const mock = buildMockClient(
      new Map([
        [
          'hydra-mem.query_eights',
          (args: Record<string, unknown>) => {
            const key = String(args['cell'] ?? '');
            return makeCellResponse(key, counts[key] ?? 0);
          },
        ],
      ]),
      capturedCalls,
    );
    _setClientForTest(mock);

    const { status, body } = await makeRequest(testPort, '/api/memory/cells');
    expect(status).toBe(200);

    const b = body as { cells?: Array<{ cell: string; count: number }>; degraded?: boolean };
    expect(Array.isArray(b.cells)).toBe(true);
    expect(b.cells).toHaveLength(8);

    // Each bagua key must appear exactly once
    const found = new Set(b.cells!.map((c) => c.cell));
    for (const key of BAGUA_KEYS) {
      expect(found.has(key), `expected cell '${key}' in overview`).toBe(true);
    }

    // Counts must match what the mock returned
    for (const entry of b.cells!) {
      expect(entry.count).toBe(counts[entry.cell] ?? 0);
    }

    // No degraded flag when all cells succeeded
    expect(b.degraded).toBeUndefined();

    // 8 calls made (one per bagua key)
    expect(capturedCalls).toHaveLength(8);
    for (const call of capturedCalls) {
      expect(call.tool).toBe('hydra-mem.query_eights');
      expect(BAGUA_KEYS).toContain(call.args['cell']);
    }
  });

  it('returns degraded:true and count=0 for a failing cell, 200 overall (not 502)', async () => {
    const FAILING_CELL = 'kan';
    const counts: Record<string, number> = {
      qian: 3, kun: 0, zhen: 1, xun: 0, kan: -1, li: 2, gen: 0, dui: 1,
    };

    const mock = buildMockClient(
      new Map([
        [
          'hydra-mem.query_eights',
          (args: Record<string, unknown>) => {
            const key = String(args['cell'] ?? '');
            if (key === FAILING_CELL) throw new Error('upstream error for kan');
            return makeCellResponse(key, counts[key] ?? 0);
          },
        ],
      ]),
    );
    _setClientForTest(mock);

    const { status, body } = await makeRequest(testPort, '/api/memory/cells');

    // Must be 200 even though one cell errored (degraded-mode doctrine)
    expect(status).toBe(200);

    const b = body as { cells?: Array<{ cell: string; count: number }>; degraded?: boolean };
    expect(Array.isArray(b.cells)).toBe(true);
    expect(b.cells).toHaveLength(8);

    // degraded flag must be set
    expect(b.degraded).toBe(true);

    // The failing cell must have count 0
    const kanEntry = b.cells!.find((c) => c.cell === FAILING_CELL);
    expect(kanEntry).toBeDefined();
    expect(kanEntry!.count).toBe(0);

    // Other cells must have their correct counts
    for (const entry of b.cells!) {
      if (entry.cell !== FAILING_CELL) {
        expect(entry.count).toBe(counts[entry.cell] ?? 0);
      }
    }
  });

  it('returns degraded:true if ALL cells fail, but still 200 with 8 zero-count entries', async () => {
    const mock = buildMockClient(
      new Map([
        [
          'hydra-mem.query_eights',
          (_args: Record<string, unknown>) => {
            throw new Error('all cells down');
          },
        ],
      ]),
    );
    _setClientForTest(mock);

    const { status, body } = await makeRequest(testPort, '/api/memory/cells');
    expect(status).toBe(200);

    const b = body as { cells?: Array<{ cell: string; count: number }>; degraded?: boolean };
    expect(b.cells).toHaveLength(8);
    expect(b.degraded).toBe(true);
    for (const entry of b.cells!) {
      expect(entry.count).toBe(0);
    }
  });
});

// ---------------------------------------------------------------------------
// DRILL-DOWN: GET /api/memory/cells?cell=<key>
// ---------------------------------------------------------------------------

describe('/api/memory/cells?cell= — drill-down, response shape', () => {
  let testServer: ReturnType<typeof createServer>;
  let testPort: number;

  beforeAll(async () => {
    testPort = await findFreePort();
    testServer = createServer((req, res) => {
      handle(req, res).catch((e) => {
        res.writeHead(500, { 'content-type': 'application/json' });
        res.end(JSON.stringify({ error: 'internal error', code: 'INTERNAL' }));
        process.stderr.write(`[test-server] handle error: ${String(e)}\n`);
      });
    });
    await new Promise<void>((resolve) => testServer.listen(testPort, '127.0.0.1', resolve));
  });

  afterAll(async () => {
    _setClientForTest(null);
    await new Promise<void>((resolve, reject) =>
      testServer.close((e) => (e ? reject(e) : resolve())),
    );
  });

  afterEach(() => {
    _setClientForTest(null);
  });

  it('returns 200 with body.records (not just body.rows) for a valid cell', async () => {
    const rows = [
      { key: 'ep:wf-test:phase:1', workflow_id: 'wf-test', kind: 'phase_boundary', cells: ['qian'] },
      { key: 'ep:wf-test:phase:2', workflow_id: 'wf-test', kind: 'phase_boundary', cells: ['qian'] },
    ];
    const mock = buildMockClient(
      new Map([
        ['hydra-mem.query_eights', { cell: 'qian', rows, count: 2 }],
      ]),
    );
    _setClientForTest(mock);

    const { status, body } = await makeRequest(testPort, '/api/memory/cells?cell=qian');
    expect(status).toBe(200);

    const b = body as { cell?: string; records?: unknown[]; rows?: unknown[]; count?: number };
    // SPA reads body.records — this is the key fix (Bug 2)
    expect(Array.isArray(b.records)).toBe(true);
    expect(b.records).toHaveLength(2);
    // rows alias is preserved for back-compat
    expect(Array.isArray(b.rows)).toBe(true);
    expect(b.rows).toHaveLength(2);
    // count and cell are present
    expect(b.count).toBe(2);
    expect(b.cell).toBe('qian');
  });

  it('returns records:[] when query_eights returns rows:[]', async () => {
    const mock = buildMockClient(
      new Map([
        ['hydra-mem.query_eights', { cell: 'kun', rows: [], count: 0 }],
      ]),
    );
    _setClientForTest(mock);

    const { status, body } = await makeRequest(testPort, '/api/memory/cells?cell=kun');
    expect(status).toBe(200);
    const b = body as { records?: unknown[]; rows?: unknown[] };
    expect(b.records).toEqual([]);
    expect(b.rows).toEqual([]);
  });

  it('passes limit parameter through to query_eights', async () => {
    const captured: CapturedCall[] = [];
    const mock = buildMockClient(
      new Map([['hydra-mem.query_eights', { cell: 'li', rows: [], count: 0 }]]),
      captured,
    );
    _setClientForTest(mock);

    await makeRequest(testPort, '/api/memory/cells?cell=li&limit=25');
    expect(captured).toHaveLength(1);
    expect(captured[0]!.args['cell']).toBe('li');
    expect(captured[0]!.args['limit']).toBe(25);
  });

  it('passes workflow_id filter through to query_eights when provided', async () => {
    const captured: CapturedCall[] = [];
    const mock = buildMockClient(
      new Map([['hydra-mem.query_eights', { cell: 'gen', rows: [], count: 0 }]]),
      captured,
    );
    _setClientForTest(mock);

    await makeRequest(testPort, '/api/memory/cells?cell=gen&workflow_id=wf-test-123');
    expect(captured[0]!.args['workflow_id']).toBe('wf-test-123');
  });
});

// ---------------------------------------------------------------------------
// VALIDATION: invalid cell → 400 (not 502)
// ---------------------------------------------------------------------------

describe('/api/memory/cells?cell=<invalid> — 400 not 502', () => {
  let testServer: ReturnType<typeof createServer>;
  let testPort: number;

  beforeAll(async () => {
    testPort = await findFreePort();
    testServer = createServer((req, res) => {
      handle(req, res).catch((e) => {
        res.writeHead(500, { 'content-type': 'application/json' });
        res.end(JSON.stringify({ error: 'internal error', code: 'INTERNAL' }));
        process.stderr.write(`[test-server] handle error: ${String(e)}\n`);
      });
    });
    await new Promise<void>((resolve) => testServer.listen(testPort, '127.0.0.1', resolve));
  });

  afterAll(async () => {
    _setClientForTest(null);
    await new Promise<void>((resolve, reject) =>
      testServer.close((e) => (e ? reject(e) : resolve())),
    );
  });

  afterEach(() => {
    _setClientForTest(null);
  });

  const INVALID_CELLS = ['bogus', 'QIAN', 'heaven', '', '../../../../etc/passwd', 'drop-table'];

  // Note: empty cell ('') is treated as "absent" and triggers the overview fan-out.
  // We test the non-empty invalid cases here.
  for (const badCell of INVALID_CELLS.filter((c) => c !== '')) {
    it(`returns 400 INVALID_CELL for cell='${badCell}' (not 502)`, async () => {
      // Mock not needed — validation fires before any tool call
      const mock = buildMockClient(new Map()); // empty map — call should never reach it
      _setClientForTest(mock);

      const { status, body } = await makeRequest(
        testPort,
        `/api/memory/cells?cell=${encodeURIComponent(badCell)}`,
      );
      expect(status).toBe(400);
      const b = body as { code?: string; error?: string };
      expect(b.code).toBe('INVALID_CELL');
      // error message must name the cell; must not be 502 UPSTREAM
      expect(b.error).toBeDefined();
    });
  }

  it('returns 400 for a plausible-but-invalid cell key', async () => {
    const mock = buildMockClient(new Map());
    _setClientForTest(mock);
    const { status, body } = await makeRequest(testPort, '/api/memory/cells?cell=water');
    expect(status).toBe(400);
    expect((body as { code?: string }).code).toBe('INVALID_CELL');
  });

  it('all 8 valid bagua keys are accepted (200, not 400 or 502)', async () => {
    for (const key of BAGUA_KEYS) {
      const mock = buildMockClient(
        new Map([['hydra-mem.query_eights', { cell: key, rows: [], count: 0 }]]),
      );
      _setClientForTest(mock);
      const { status } = await makeRequest(testPort, `/api/memory/cells?cell=${key}`);
      expect(status, `expected 200 for valid cell '${key}'`).toBe(200);
      _setClientForTest(null);
    }
  });
});

// ---------------------------------------------------------------------------
// SECURITY: Host-guard applies to /api/memory/cells (INVARIANT #3)
// ---------------------------------------------------------------------------

describe('/api/memory/cells — Host guard still enforced', () => {
  let testServer: ReturnType<typeof createServer>;
  let testPort: number;

  beforeAll(async () => {
    testPort = await findFreePort();
    testServer = createServer((req, res) => {
      handle(req, res).catch((e) => {
        res.writeHead(500, { 'content-type': 'application/json' });
        res.end(JSON.stringify({ error: 'internal error', code: 'INTERNAL' }));
        process.stderr.write(`[test-server] handle error: ${String(e)}\n`);
      });
    });
    await new Promise<void>((resolve) => testServer.listen(testPort, '127.0.0.1', resolve));
  });

  afterAll(async () => {
    _setClientForTest(null);
    await new Promise<void>((resolve, reject) =>
      testServer.close((e) => (e ? reject(e) : resolve())),
    );
  });

  it('rejects non-loopback Host with 403 HOST_REJECTED', async () => {
    const { status, body } = await makeRequest(testPort, '/api/memory/cells', {
      headers: { host: 'evil.attacker.com' },
    });
    expect(status).toBe(403);
    expect((body as { code?: string }).code).toBe('HOST_REJECTED');
  });
});
