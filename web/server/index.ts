/**
 * web/server/index.ts — Hydra Cockpit loopback bridge.
 *
 * Loopback-only HTTP server bridging the browser cockpit to hydra_memory (stdio MCP).
 *
 * SECURITY GUARANTEES (inviolable — forked verbatim in spirit from AgentMesh
 * web/server/index.ts, adapted for the Hydra Cockpit):
 *
 *   INVARIANT #1 — FIXED ENVELOPE: actor='hydra-cockpit', project='Hydra'.
 *     No request path can alter actor or project. Browser input NEVER touches
 *     the envelope fields (injected server-side on every audited call).
 *
 *   INVARIANT #2 — READ-ONLY path (C1): every read call goes through allowTool()
 *     (hard whitelist + forbidden-verb denylist). No write tool is reachable
 *     via GET routes. The write path is structurally separate (POST only,
 *     CSRF-gated, write-whitelist of 8 tools — C3 delivers the write path).
 *
 *   INVARIANT #3 — LOOPBACK ONLY: binds 127.0.0.1 (never 0.0.0.0).
 *     isLoopbackHost() checks Host header on EVERY request — DNS-rebinding defense.
 *     Non-loopback Host rejected with 403 HOST_REJECTED before any logic runs.
 *
 *   INVARIANT #4 — CSRF: X-Hydra-Token header required on all POSTs;
 *     timing-safe comparison. 403 on missing/wrong token.
 *     Token minted at startup; no stale-tab replay across restarts.
 *
 *   INVARIANT #5 — AM-CON-005 carried forward: operator email NEVER in any
 *     response payload. GET /api/session returns { token, actor: 'hydra-cockpit' }
 *     — no email.
 *
 * Port strategy:
 *   Preferred port 8795; probes forward to 8820 (HYDRA_COCKPIT_BRIDGE_PORT env
 *   override pins to one port — failure is fatal, not roll-forward).
 *   Bound port written atomically to .hydra-cockpit-bridge-port on listen.
 *   Port file removed on SIGINT / SIGTERM / process exit.
 *
 * Routes:
 *   GET  /api/health                       — bridge up + hydra-mem.ping liveness
 *   GET  /api/session                      — CSRF token + actor (no email)
 *   GET  /api/workflows                    — hydra-mem.workflows_list
 *   GET  /api/workflows/:id                — hydra-mem.workflow_status
 *   GET  /api/squads                       — hydra-mem.squad_list
 *   GET  /api/hitl                         — hydra-mem.hitl_pending
 *   GET  /api/memory/cells                 — hydra-mem.query_eights
 *   GET  /api/memory/search?q=&k=          — hydra-mem.semantic_search
 *   GET  /api/memory/workflow/:id          — hydra-mem.list_workflow
 *
 * All GET routes are read-only; POST routes (C3) will require CSRF + write-whitelist.
 * 404 for unknown routes; 405 for non-GET on read routes.
 */

import { createServer, type IncomingMessage, type ServerResponse } from 'node:http';
import { createServer as createNetServer } from 'node:net';
import { writeFileSync, rmSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { HydraMemClient } from './hydra-mem-client.js';
import { allowTool, READ_HYDRA_TOOLS } from './whitelist.js';
import { sessionToken, verifyToken } from './operator.js';
import { launchWorkflow, LaunchValidationError } from './launch.js';
import { mintNonce, consumeNonce } from './nonces.js';
import { isWriteAllowed, needsNonce } from './write-whitelist.js';

const HOST = '127.0.0.1'; // INVARIANT #3 — loopback only, NEVER 0.0.0.0
const PREFERRED_PORT = Number(process.env['HYDRA_COCKPIT_BRIDGE_PORT'] ?? 8795);
const PORT_MAX = 8820;
const PORT_PINNED = process.env['HYDRA_COCKPIT_BRIDGE_PORT'] != null;

let boundPort = PREFERRED_PORT;

// Resolve port file path relative to this module (web/.hydra-cockpit-bridge-port)
function resolvePortFile(): string {
  try {
    const __filename = fileURLToPath(import.meta.url);
    const __dirname = dirname(__filename);
    // server/ or dist-server/ → web/ parent has the port file
    return resolve(__dirname, '..', '.hydra-cockpit-bridge-port');
  } catch {
    return '.hydra-cockpit-bridge-port';
  }
}

const PORT_FILE = resolvePortFile();

// ---------------------------------------------------------------------------
// Port management — probe 8795→8820, write file atomically on listen
// ---------------------------------------------------------------------------

function portFree(port: number): Promise<boolean> {
  return new Promise((resolve) => {
    const probe = createNetServer();
    probe.once('error', () => resolve(false));
    probe.once('listening', () => probe.close(() => resolve(true)));
    probe.listen(port, HOST);
  });
}

async function choosePort(): Promise<number> {
  if (await portFree(PREFERRED_PORT)) return PREFERRED_PORT;
  if (PORT_PINNED) {
    throw new Error(
      `HYDRA_COCKPIT_BRIDGE_PORT=${PREFERRED_PORT} is already in use. ` +
        `Free that port or set HYDRA_COCKPIT_BRIDGE_PORT to a free one.`,
    );
  }
  for (let p = PREFERRED_PORT + 1; p <= PORT_MAX; p++) {
    if (await portFree(p)) return p;
  }
  throw new Error(
    `no free port in ${PREFERRED_PORT}..${PORT_MAX}; ` +
      `set HYDRA_COCKPIT_BRIDGE_PORT to a free port.`,
  );
}

// Exported for tests to verify port-file helpers directly
export { choosePort, PORT_FILE };

// ---------------------------------------------------------------------------
// hydra-mem client singleton
// ---------------------------------------------------------------------------

const client = new HydraMemClient();

// Allow injection of a fake client for unit tests
let _clientOverride: HydraMemClient | null = null;

/** For testing only — inject a mock client. */
export function _setClientForTest(c: HydraMemClient | null): void {
  _clientOverride = c;
}

function getClient(): HydraMemClient {
  return _clientOverride ?? client;
}

// ---------------------------------------------------------------------------
// INVARIANT #3 — DNS-rebinding defense: validate Host header on every request
// ---------------------------------------------------------------------------

export function isLoopbackHost(req: IncomingMessage): boolean {
  const raw = req.headers['host'] ?? '';
  const host = raw.split(':')[0]?.toLowerCase() ?? '';
  return host === '127.0.0.1' || host === 'localhost';
}

// ---------------------------------------------------------------------------
// HTTP utilities
// ---------------------------------------------------------------------------

function json(res: ServerResponse, status: number, body: unknown): void {
  const text = JSON.stringify(body);
  res.writeHead(status, {
    'content-type': 'application/json; charset=utf-8',
    'cache-control': 'no-store',
    'x-content-type-options': 'nosniff',
  });
  res.end(text);
}

// readBody exported for write-path handlers (C3 will use it)
export function readBody(req: IncomingMessage): Promise<Record<string, unknown>> {
  return new Promise((resolve, reject) => {
    let raw = '';
    let tooBig = false;
    req.on('data', (c: Buffer) => {
      raw += c.toString('utf8');
      if (raw.length > 64 * 1024) {
        tooBig = true;
        req.destroy();
      }
    });
    req.on('end', () => {
      if (tooBig) return reject(new Error('request body too large (max 64KB)'));
      if (!raw.trim()) return resolve({});
      try {
        const v = JSON.parse(raw) as unknown;
        if (v && typeof v === 'object' && !Array.isArray(v)) {
          resolve(v as Record<string, unknown>);
        } else {
          reject(new Error('body must be a JSON object'));
        }
      } catch {
        reject(new Error('invalid JSON body'));
      }
    });
    req.on('error', reject);
  });
}

// ---------------------------------------------------------------------------
// CSRF gate — checks X-Hydra-Token header (INVARIANT #4)
// ---------------------------------------------------------------------------

export function csrfOk(req: IncomingMessage, res: ServerResponse): boolean {
  const presented = req.headers['x-hydra-token'];
  const token = Array.isArray(presented) ? presented[0] : presented;
  if (!verifyToken(token)) {
    json(res, 403, {
      error: 'missing or invalid X-Hydra-Token (CSRF)',
      code: 'CSRF',
    });
    return false;
  }
  return true;
}

// ---------------------------------------------------------------------------
// Read tool call — enforces whitelist (INVARIANT #2)
// ---------------------------------------------------------------------------

async function readTool<T = unknown>(
  tool: string,
  args: Record<string, unknown> = {},
): Promise<T> {
  if (!allowTool(tool)) {
    const err = Object.assign(
      new Error(`tool '${tool}' is not on the cockpit read whitelist`),
      { code: 'FORBIDDEN_TOOL' },
    );
    throw err;
  }
  return getClient().call<T>(tool, args);
}

// ---------------------------------------------------------------------------
// Main request handler
// ---------------------------------------------------------------------------

async function handle(req: IncomingMessage, res: ServerResponse): Promise<void> {
  // INVARIANT #3 — DNS-rebinding defense: Host must be loopback on EVERY request
  if (!isLoopbackHost(req)) {
    json(res, 403, { error: 'loopback host required (DNS-rebinding protection)', code: 'HOST_REJECTED' });
    return;
  }

  const url = new URL(req.url ?? '/', `http://${HOST}:${boundPort}`);
  const path = url.pathname;

  // -------------------------------------------------------------------------
  // Write path — POST only, CSRF-gated
  // -------------------------------------------------------------------------
  if (req.method === 'POST') {
    // INVARIANT #4 — CSRF required on ALL POSTs before any body is read.
    if (!csrfOk(req, res)) return;

    // --- POST /api/confirm/preview — mint a single-use confirm nonce ---
    if (path === '/api/confirm/preview') {
      let body: Record<string, unknown>;
      try {
        body = await readBody(req);
      } catch {
        json(res, 400, { error: 'invalid request body', code: 'BAD_BODY' });
        return;
      }
      const action = typeof body['action'] === 'string' ? body['action'] : '';
      if (!isWriteAllowed(action)) {
        json(res, 400, { error: `unknown or unsanctioned action: ${action}`, code: 'UNKNOWN_ACTION' });
        return;
      }
      const { nonce, expiresAt } = mintNonce(action);
      json(res, 200, { nonce, expiresAt, action });
      return;
    }

    // --- POST /api/launch — fire-and-attach workflow launch ---
    if (path === '/api/launch') {
      let body: Record<string, unknown>;
      try {
        body = await readBody(req);
      } catch {
        json(res, 400, { error: 'invalid request body', code: 'BAD_BODY' });
        return;
      }

      const live = body['live'] === true;

      // Live launch is High-risk: requires a valid confirm nonce.
      // Dry-run does NOT require a nonce (it dispatches nothing).
      if (live) {
        const presented = typeof body['confirmNonce'] === 'string' ? body['confirmNonce'] : undefined;
        if (!needsNonce('launch') || !consumeNonce(presented, 'launch')) {
          json(res, 403, {
            error: 'live launch requires a server-issued confirm nonce (POST /api/confirm/preview first)',
            code: 'NONCE_REQUIRED',
          });
          return;
        }
      }

      // TODO(C5): file an eights envelope here before spawning. // ANTI-PATTERN-OK: C5 scope per COCKPIT-DESIGN.md §5.2 + explicit instruction in C2 deliverable brief
      // cockpitEnvelope() provides actor='hydra-cockpit', project='Hydra'.
      // Leave hook: await fileEightsEnvelope(cockpitEnvelope(), 'launch', { live, goal: body['goal'] });

      let result: Awaited<ReturnType<typeof launchWorkflow>>;
      try {
        result = await launchWorkflow({
          goal: body['goal'],
          squads: body['squads'],
          budgetUsd: body['budgetUsd'],
          live,
        });
      } catch (e) {
        if (e instanceof LaunchValidationError) {
          json(res, 400, { error: e.message, code: e.code });
          return;
        }
        process.stderr.write(`[cockpit-bridge] launch error: ${String(e)}\n`);
        json(res, 502, { error: 'bridge upstream error', code: 'UPSTREAM' });
        return;
      }

      json(res, 202, {
        workflow_id: result.workflow_id,
        pid: result.pid,
        log: result.log,
      });
      return;
    }

    // All other POST routes: 404.
    // C3 will add: /api/resume (gate writes — approve/reject/modify-budget/force-dispatch/change-squads)
    // C6 will add: /api/replay, /api/tag_memory
    json(res, 404, { error: 'not found' });
    return;
  }

  // Only GET allowed for read path
  if (req.method !== 'GET') {
    json(res, 405, { error: 'method not allowed' });
    return;
  }

  try {
    // -----------------------------------------------------------------------
    // Route table — read-only GET routes
    // -----------------------------------------------------------------------

    // GET /api/health — bridge liveness + hydra-mem.ping child liveness
    if (path === '/api/health') {
      let childOk = false;
      let pingDetail: unknown = null;
      try {
        pingDetail = await readTool('hydra-mem.ping');
        childOk = true;
      } catch {
        // child not reachable — degrade gracefully; still return 200 for bridge health
      }
      json(res, 200, {
        ok: true,
        bridge: 'hydra-cockpit',
        readTools: READ_HYDRA_TOOLS,
        child: { ok: childOk, ping: pingDetail },
      });
      return;
    }

    // GET /api/session — CSRF token + actor (INVARIANT #5 — no email)
    if (path === '/api/session') {
      json(res, 200, { token: sessionToken(), actor: 'hydra-cockpit' });
      return;
    }

    // GET /api/workflows — workflow summaries (Launchpad/Campaigns)
    if (path === '/api/workflows') {
      const limit = Math.min(
        200,
        Math.max(1, parseInt(url.searchParams.get('limit') ?? '50', 10) || 50),
      );
      json(res, 200, await readTool('hydra-mem.workflows_list', { limit }));
      return;
    }

    // GET /api/squads — discovered squad packs
    if (path === '/api/squads') {
      json(res, 200, await readTool('hydra-mem.squad_list'));
      return;
    }

    // GET /api/hitl — pending HITL gates across workflows
    if (path === '/api/hitl') {
      json(res, 200, await readTool('hydra-mem.hitl_pending'));
      return;
    }

    // GET /api/memory/cells?cell=<bagua_key>&limit=&workflow_id= — query_eights
    if (path === '/api/memory/cells') {
      const cell = url.searchParams.get('cell') ?? '';
      const limit = Math.min(
        200,
        Math.max(1, parseInt(url.searchParams.get('limit') ?? '50', 10) || 50),
      );
      const workflowId = url.searchParams.get('workflow_id') ?? undefined;
      const cellArgs: Record<string, unknown> = { cell, limit };
      if (workflowId !== undefined) cellArgs['workflow_id'] = workflowId;
      json(res, 200, await readTool('hydra-mem.query_eights', cellArgs));
      return;
    }

    // GET /api/memory/search?q=&k= — semantic_search
    if (path === '/api/memory/search') {
      const q = (url.searchParams.get('q') ?? '').trim();
      const k = Math.min(50, Math.max(1, parseInt(url.searchParams.get('k') ?? '5', 10) || 5));
      const workflowId = url.searchParams.get('workflow_id') ?? undefined;
      const searchArgs: Record<string, unknown> = { query: q, k };
      if (workflowId !== undefined) searchArgs['workflow_id'] = workflowId;
      json(res, 200, await readTool('hydra-mem.semantic_search', searchArgs));
      return;
    }

    // GET /api/workflows/:id — one workflow's live state
    const wfMatch = path.match(/^\/api\/workflows\/([A-Za-z0-9][A-Za-z0-9\-_]{0,63})$/);
    if (wfMatch !== null) {
      const wfId = wfMatch[1];
      if (wfId !== undefined) {
        json(res, 200, await readTool('hydra-mem.workflow_status', { workflow_id: wfId }));
        return;
      }
    }

    // GET /api/memory/workflow/:id — list episodic rows for a workflow
    const memWfMatch = path.match(/^\/api\/memory\/workflow\/([A-Za-z0-9][A-Za-z0-9\-_]{0,63})$/);
    if (memWfMatch !== null) {
      const wfId = memWfMatch[1];
      if (wfId !== undefined) {
        json(res, 200, await readTool('hydra-mem.list_workflow', { workflow_id: wfId }));
        return;
      }
    }

    // 404 for everything else
    json(res, 404, { error: 'not found' });
  } catch (e) {
    const err = e as Error & { code?: string };
    if (err.code === 'FORBIDDEN_TOOL') {
      json(res, 403, { error: err.message, code: 'FORBIDDEN_TOOL' });
      return;
    }
    // Log real detail server-side; never expose raw upstream error text to the browser.
    process.stderr.write(`[cockpit-bridge] upstream error: ${String(err.message)}\n`);
    json(res, 502, { error: 'bridge upstream error', code: 'UPSTREAM' });
  }
}

// ---------------------------------------------------------------------------
// HTTP server + lifecycle
// ---------------------------------------------------------------------------

const server = createServer((req, res) => {
  handle(req, res).catch((e) => {
    // Log raw error server-side; return a generic envelope to the browser.
    process.stderr.write(`[cockpit-bridge] internal error: ${String(e)}\n`);
    json(res, 500, { error: 'internal error', code: 'INTERNAL' });
  });
});

function removePortFile(): void {
  try {
    rmSync(PORT_FILE, { force: true });
  } catch {
    /* ignore */
  }
}

const shutdown = (): void => {
  removePortFile();
  server.close();
  void client.close();
  process.exit(0);
};

process.on('SIGINT', shutdown);
process.on('SIGTERM', shutdown);
process.on('exit', removePortFile);

async function main(): Promise<void> {
  boundPort = await choosePort();

  server.once('error', (err: NodeJS.ErrnoException) => {
    process.stderr.write(
      err.code === 'EADDRINUSE'
        ? `[cockpit-bridge] FATAL: port ${boundPort} became busy between preflight and bind.\n`
        : `[cockpit-bridge] FATAL: ${String(err)}\n`,
    );
    process.exit(1);
  });

  server.listen(boundPort, HOST, () => {
    // Atomic write: write the port file only after the socket is confirmed bound
    try {
      writeFileSync(PORT_FILE, String(boundPort), 'utf8');
    } catch {
      /* non-fatal — bridge still runs without the port file */
    }
    const rolled = boundPort !== PREFERRED_PORT ? ` (preferred ${PREFERRED_PORT} was busy)` : '';
    process.stderr.write(
      `[cockpit-bridge] Hydra Cockpit bridge listening on http://${HOST}:${boundPort}${rolled} ` +
        `(loopback only · ${READ_HYDRA_TOOLS.length} read tools · CSRF-gated for writes)\n`,
    );
  });
}

void main().catch((err) => {
  process.stderr.write(`[cockpit-bridge] startup failed: ${String(err)}\n`);
  process.exit(1);
});
