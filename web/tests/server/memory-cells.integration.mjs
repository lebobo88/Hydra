/**
 * tests/server/memory-cells.integration.mjs
 *
 * LIVE integration test for /api/memory/cells.
 * Exercises the REAL bridge↔hydra_memory path — no mocks.
 *
 * The test reads the bridge port from web/.hydra-cockpit-bridge-port.
 * If the file is absent or the bridge is not reachable, the test skips cleanly.
 *
 * What it checks:
 *   1. GET /api/memory/cells (no param) → 200, cells array of 8,
 *      at least one cell has count > 0 (episodic DB has real rows).
 *   2. GET /api/memory/cells?cell=qian → 200, records[] non-empty,
 *      each record has expected fields.
 *   3. GET /api/memory/cells?cell=bogus → 400 INVALID_CELL (not 502).
 *
 * Run: node tests/server/memory-cells.integration.mjs
 * Or: node --import tsx/esm tests/server/memory-cells.integration.mjs
 */

import { readFileSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { request } from 'node:http';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

// web/ is two levels above this file (tests/server/ → tests/ → web/)
const WEB_DIR = resolve(__dirname, '..', '..');
const PORT_FILE = resolve(WEB_DIR, '.hydra-cockpit-bridge-port');

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function fetchJson(port, path) {
  return new Promise((resolve, reject) => {
    const req = request(
      {
        hostname: '127.0.0.1',
        port,
        path,
        method: 'GET',
        headers: { host: '127.0.0.1' },
      },
      (res) => {
        let data = '';
        res.on('data', (c) => { data += c.toString(); });
        res.on('end', () => {
          try {
            resolve({ status: res.statusCode, body: JSON.parse(data) });
          } catch {
            resolve({ status: res.statusCode, body: data });
          }
        });
      },
    );
    req.on('error', reject);
    req.setTimeout(10_000, () => {
      req.destroy(new Error('request timed out'));
    });
    req.end();
  });
}

function assert(condition, message) {
  if (!condition) {
    console.error(`  FAIL: ${message}`);
    process.exitCode = 1;
    return false;
  }
  console.log(`  PASS: ${message}`);
  return true;
}

function skip(reason) {
  console.log(`\nSKIP: ${reason}\n`);
  process.exit(0);
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

async function main() {
  // Read port file
  let port;
  try {
    port = parseInt(readFileSync(PORT_FILE, 'utf8').trim(), 10);
    if (!port || isNaN(port)) throw new Error('invalid port value');
  } catch {
    skip(`bridge port file not found or invalid at ${PORT_FILE} — bridge not running`);
  }

  console.log(`\nLIVE integration test: bridge on http://127.0.0.1:${port}\n`);

  // Quick reachability check
  try {
    const health = await fetchJson(port, '/api/health');
    if (health.status !== 200) {
      skip(`bridge /api/health returned ${health.status} — bridge not ready`);
    }
    console.log(`  Bridge healthy: ${JSON.stringify(health.body?.bridge ?? health.body)}`);
  } catch (e) {
    skip(`bridge not reachable on port ${port}: ${e.message}`);
  }

  // ---------------------------------------------------------------------------
  // TEST 1: GET /api/memory/cells (no param) → 200, 8 cells, at least 1 with count>0
  // ---------------------------------------------------------------------------
  console.log('\nTEST 1: GET /api/memory/cells (overview — no cell param)');
  {
    const { status, body } = await fetchJson(port, '/api/memory/cells');

    assert(status === 200, `status 200 (got ${status})`);
    assert(typeof body === 'object' && body !== null, 'body is an object');

    const cells = body.cells;
    assert(Array.isArray(cells), 'body.cells is an array');
    assert(cells.length === 8, `body.cells has 8 entries (got ${cells.length})`);

    const EXPECTED_KEYS = ['qian', 'kun', 'zhen', 'xun', 'kan', 'li', 'gen', 'dui'];
    const foundKeys = new Set(cells.map((c) => c.cell));
    for (const k of EXPECTED_KEYS) {
      assert(foundKeys.has(k), `cell '${k}' present in overview`);
    }

    const maxCount = Math.max(...cells.map((c) => c.count ?? 0));
    assert(maxCount > 0, `at least one cell has count>0 (max=${maxCount})`);

    // degraded must be false/absent (all cells should be reachable on a live bridge)
    assert(!body.degraded, `degraded flag absent/false (got ${body.degraded})`);

    console.log('\n  Cell counts from live DB:');
    for (const c of cells) {
      console.log(`    ${c.cell.padEnd(6)}: ${c.count}`);
    }
  }

  // ---------------------------------------------------------------------------
  // TEST 2: GET /api/memory/cells?cell=qian → 200, records[] non-empty, correct fields
  // ---------------------------------------------------------------------------
  console.log('\nTEST 2: GET /api/memory/cells?cell=qian (drill-down)');
  {
    const { status, body } = await fetchJson(port, '/api/memory/cells?cell=qian&limit=10');

    assert(status === 200, `status 200 (got ${status})`);
    assert(typeof body === 'object' && body !== null, 'body is an object');
    assert(body.cell === 'qian', `body.cell === 'qian' (got '${body.cell}')`);

    // SPA reads body.records — the key fix
    assert(Array.isArray(body.records), `body.records is an array`);
    assert(body.records.length > 0, `body.records is non-empty (got ${body.records.length} records)`);

    // rows alias present for back-compat
    assert(Array.isArray(body.rows), `body.rows alias present`);
    assert(body.rows.length === body.records.length, 'body.rows.length === body.records.length');

    // count field present
    assert(typeof body.count === 'number', `body.count is a number (got ${body.count})`);

    // Check that records have expected fields
    const first = body.records[0];
    assert(first !== null && typeof first === 'object', 'first record is an object');
    const hasExpectedFields = 'key' in first || 'workflow_id' in first || 'kind' in first;
    assert(hasExpectedFields, 'first record has at least one expected field (key/workflow_id/kind)');

    console.log(`\n  records.length: ${body.records.length}, count: ${body.count}`);
    if (first) {
      console.log(`  first record keys: ${Object.keys(first).join(', ')}`);
      if (first.workflow_id) console.log(`  first.workflow_id: ${String(first.workflow_id).slice(0, 20)}...`);
      if (first.kind) console.log(`  first.kind: ${first.kind}`);
      if (first.created_at) console.log(`  first.created_at: ${first.created_at}`);
    }
  }

  // ---------------------------------------------------------------------------
  // TEST 3: GET /api/memory/cells?cell=bogus → 400 INVALID_CELL (not 502)
  // ---------------------------------------------------------------------------
  console.log('\nTEST 3: GET /api/memory/cells?cell=bogus → 400 INVALID_CELL');
  {
    const { status, body } = await fetchJson(port, '/api/memory/cells?cell=bogus');

    assert(status === 400, `status 400 (got ${status})`);
    assert(body.code === 'INVALID_CELL', `code === 'INVALID_CELL' (got '${body.code}')`);
    assert(status !== 502, `not 502 (confirmed: ${status})`);
    console.log(`  error: ${body.error}`);
  }

  // ---------------------------------------------------------------------------
  // TEST 4: GET /api/memory/cells?cell=kun → 200 with records[] (even if empty)
  // ---------------------------------------------------------------------------
  console.log('\nTEST 4: GET /api/memory/cells?cell=kun → 200 with records[] shape');
  {
    const { status, body } = await fetchJson(port, '/api/memory/cells?cell=kun&limit=50');

    assert(status === 200, `status 200 (got ${status})`);
    assert(Array.isArray(body.records), `body.records is an array (got ${typeof body.records})`);
    console.log(`  kun records: ${body.records.length}`);
  }

  // ---------------------------------------------------------------------------
  // Summary
  // ---------------------------------------------------------------------------
  console.log('\n---');
  if (process.exitCode) {
    console.log('RESULT: SOME FAILURES — see above');
  } else {
    console.log('RESULT: ALL LIVE INTEGRATION TESTS PASSED');
  }
}

main().catch((e) => {
  console.error(`\nFATAL: ${e.message}`);
  process.exit(1);
});
