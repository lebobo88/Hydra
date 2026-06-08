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
import { HydraControlClient } from './hydra-control-client.js';
import { allowTool, READ_HYDRA_TOOLS } from './whitelist.js';
import { sessionToken, verifyToken, cockpitEnvelope } from './operator.js';
import { launchWorkflow, LaunchValidationError, WORKFLOW_ID_RE } from './launch.js';
import { mintNonce, consumeNonce } from './nonces.js';
import { isWriteAllowed, needsNonce, needsTypedChallenge, isResumeAction, validateOption, OptionValidationError } from './write-whitelist.js';
import { streamWorkflow, pollWorkflow, type GetWorkflowStatus } from './sse.js';
import { launchReplay, ReplayValidationError, _setReplaySpawnerForTest } from './replay.js';
import type { SpawnFn } from './launch.js';

// ---------------------------------------------------------------------------
// Replay spawner injection for tests (mirrors launch.ts pattern)
// ---------------------------------------------------------------------------

/** For testing only — inject a fake replay spawner. */
export function _setReplaySpawnerForTestBridge(fn: SpawnFn | null): void {
  _setReplaySpawnerForTest(fn);
}

// ---------------------------------------------------------------------------
// tag_memory write: sanctioned direct call to hydra-mem (bypasses read whitelist)
// ---------------------------------------------------------------------------

/**
 * TAG_MEMORY_BAGUA_KEYS — the 8 valid cell keys.
 * Mirrors the episodic cell enum in hydra_core/eights/__init__.py.
 * Validation here is belt-and-braces; the Python MCP tool validates again.
 */
const TAG_MEMORY_BAGUA_KEYS = Object.freeze([
  'qian', 'kun', 'zhen', 'xun', 'kan', 'li', 'gen', 'dui',
] as const);

type BaguaKey = (typeof TAG_MEMORY_BAGUA_KEYS)[number];

const BAGUA_SET: ReadonlySet<string> = new Set(TAG_MEMORY_BAGUA_KEYS);

/**
 * TAG_MEMORY_KEY_RE — key charset: alphanumeric, hyphen, underscore, dot.
 * Reasonable identifier charset; rejects shell metacharacters.
 */
const TAG_MEMORY_KEY_RE = /^[A-Za-z0-9][A-Za-z0-9\-_.]{0,199}$/;

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
// hydra-mem client singleton (read path)
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
// hydra-control client singleton (write path — resume actions only)
// ---------------------------------------------------------------------------

const hydraControl = new HydraControlClient();

// Allow injection of a fake hydra-control client for unit tests
let _hydraControlOverride: HydraControlClient | null = null;

/** For testing only — inject a mock hydra-control client. */
export function _setHydraControlForTest(c: HydraControlClient | null): void {
  _hydraControlOverride = c;
}

function getHydraControl(): HydraControlClient {
  return _hydraControlOverride ?? hydraControl;
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

/** Exported for test harnesses that spin up a dedicated test HTTP server. */
export async function handle(req: IncomingMessage, res: ServerResponse): Promise<void> {
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

      // C5: file an eights audit envelope before spawning (after nonce/CSRF validation).
      // Audit fires here: request is validated (CSRF + nonce for live), not yet dispatched.
      // Order: validate → audit → dispatch (per COCKPIT-DESIGN.md §2.6).
      // cockpitEnvelope() is fixed server-side (actor='hydra-cockpit', project='Hydra').
      // NEVER blocks the dispatch: if audit fails hard, include audit:"degraded" in response.
      const _launchEnv = cockpitEnvelope();
      const _launchAuditResult = await getHydraControl().audit(_launchEnv, 'launch', {
        detail: live ? 'live launch' : 'dry-run launch',
      });
      const _launchAuditNote = !_launchAuditResult.ok
        ? 'degraded'
        : _launchAuditResult.spooled
          ? 'spooled'
          : 'recorded';

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

      const launchResp: Record<string, unknown> = {
        workflow_id: result.workflow_id,
        pid: result.pid,
        log: result.log,
      };
      // Surface audit degradation to caller (degraded-mode doctrine: surface, don't block)
      if (_launchAuditNote === 'degraded') launchResp['audit'] = 'degraded';
      json(res, 202, launchResp);
      return;
    }

    // --- POST /api/resume — gate writes: all 5 resume actions ---
    if (path === '/api/resume') {
      let body: Record<string, unknown>;
      try {
        body = await readBody(req);
      } catch {
        json(res, 400, { error: 'invalid request body', code: 'BAD_BODY' });
        return;
      }

      const action = typeof body['action'] === 'string' ? body['action'] : '';

      // (2) action must be in the write whitelist AND must be a resume action.
      // Non-resume actions (launch, replay, tag_memory) are NOT routable here.
      if (!isWriteAllowed(action) || !isResumeAction(action)) {
        json(res, 400, {
          error: `action '${action}' is not a valid gate-resume action`,
          code: 'INVALID_ACTION',
        });
        return;
      }

      // (3) validate workflow_id — byte-identical regex to _WORKFLOW_ID_RE in hydra_control/server.py
      const workflowId = typeof body['workflow_id'] === 'string' ? body['workflow_id'] : '';
      if (!WORKFLOW_ID_RE.test(workflowId)) {
        json(res, 400, {
          error: 'workflow_id must match ^[A-Za-z0-9][A-Za-z0-9\\-_]{0,63}$',
          code: 'INVALID_WORKFLOW_ID',
        });
        return;
      }

      // (4) validate option per action
      let validatedOption: string | undefined;
      try {
        validatedOption = validateOption(action, body['option']);
      } catch (e) {
        if (e instanceof OptionValidationError) {
          json(res, 400, { error: e.message, code: e.code });
          return;
        }
        json(res, 400, { error: 'invalid option', code: 'OPTION_INVALID' });
        return;
      }

      // (5) typed challenge — required if needsTypedChallenge(action)
      if (needsTypedChallenge(action)) {
        const presented = typeof body['typedChallenge'] === 'string' ? body['typedChallenge'] : '';
        if (presented !== workflowId) {
          json(res, 403, {
            error: `action '${action}' requires typedChallenge === workflow_id`,
            code: 'TYPED_CHALLENGE_REQUIRED',
          });
          return;
        }
      }

      // (6) nonce — required if needsNonce(action)
      if (needsNonce(action)) {
        const presentedNonce = typeof body['confirmNonce'] === 'string' ? body['confirmNonce'] : undefined;
        if (!consumeNonce(presentedNonce, action)) {
          json(res, 403, {
            error: `action '${action}' requires a server-issued confirm nonce (POST /api/confirm/preview first)`,
            code: 'NONCE_REQUIRED',
          });
          return;
        }
      }

      // (7) C5: file an eights audit envelope before dispatching.
      // Order: validate (CSRF + nonce + typed challenge) → audit → dispatch.
      // A refused/invalid request never reaches this point (returned above).
      // cockpitEnvelope() is fixed server-side (actor='hydra-cockpit', project='Hydra').
      // NEVER blocks the dispatch: audit failure → include audit:"degraded" in response.
      const _resumeEnv = cockpitEnvelope();
      const _resumeAuditOpts: { workflow_id?: string; option?: string } = {
        workflow_id: workflowId,
      };
      if (validatedOption !== undefined) _resumeAuditOpts.option = validatedOption;
      const _resumeAuditResult = await getHydraControl().audit(_resumeEnv, action, _resumeAuditOpts);
      const _resumeAuditNote = !_resumeAuditResult.ok
        ? 'degraded'
        : _resumeAuditResult.spooled
          ? 'spooled'
          : 'recorded';

      // (8) call hydra_control.resume and return the result
      let resumeResult: Awaited<ReturnType<typeof hydraControl.resume>>;
      try {
        resumeResult = await getHydraControl().resume(workflowId, action, validatedOption);
      } catch (e) {
        // Child connectivity error — do not leak raw internals
        process.stderr.write(`[cockpit-bridge] resume upstream error: ${String(e)}\n`);
        json(res, 502, { error: 'bridge upstream error', code: 'UPSTREAM' });
        return;
      }

      // Map hydra_control refusal/venom-block to a clean non-leaking envelope.
      // ok:false from the Python side is a governed refusal (e.g. venom gate),
      // NOT a bridge error. Surface it as 409/403 with a code, no raw internals.
      if (!resumeResult.ok) {
        const errorCode = resumeResult.error ?? 'RESUME_REFUSED';
        // Map known hydra_control error tokens to HTTP status codes
        const status = errorCode === 'invalid_workflow_id' ? 400
          : errorCode === 'invalid_action' ? 400
          : errorCode === 'invalid_option' ? 400
          : errorCode === 'venom_blocked' ? 403
          : 409; // Conflict — gate already resolved, venom refused, etc.
        json(res, status, {
          error: 'resume refused by hydra_control',
          code: 'RESUME_REFUSED',
          // Surface the governed refusal reason without raw traceback/internals
          reason: errorCode,
        });
        return;
      }

      // Success: return the hydra_control result envelope (200 or 202)
      const resumeResp: Record<string, unknown> = {
        ok: resumeResult.ok,
        launched: resumeResult.launched,
        pid: resumeResult.pid,
        workflow_id: resumeResult.workflow_id,
        action: resumeResult.action,
        log: resumeResult.log,
      };
      // Surface audit degradation to caller (degraded-mode doctrine: surface, don't block)
      if (_resumeAuditNote === 'degraded') resumeResp['audit'] = 'degraded';
      json(res, 202, resumeResp);
      return;
    }

    // --- POST /api/replay — fire-and-attach replay launch (C6) ---
    if (path === '/api/replay') {
      let body: Record<string, unknown>;
      try {
        body = await readBody(req);
      } catch {
        json(res, 400, { error: 'invalid request body', code: 'BAD_BODY' });
        return;
      }

      // (1) write-whitelist check
      if (!isWriteAllowed('replay')) {
        json(res, 403, { error: 'replay is not a sanctioned write', code: 'FORBIDDEN_TOOL' });
        return;
      }

      // (2) validate source workflow_id (browser-supplied, must be an existing workflow)
      const sourceWorkflowId = typeof body['workflow_id'] === 'string' ? body['workflow_id'] : '';
      if (!WORKFLOW_ID_RE.test(sourceWorkflowId)) {
        json(res, 400, {
          error: 'workflow_id must match ^[A-Za-z0-9][A-Za-z0-9\\-_]{0,63}$',
          code: 'INVALID_SOURCE_WORKFLOW_ID',
        });
        return;
      }

      // (3) Replay is High-risk: confirm nonce required regardless of live/dry.
      //     (venom-gated typed challenge required ONLY when live=true)
      const live = body['live'] === true;
      const presentedNonce = typeof body['confirmNonce'] === 'string' ? body['confirmNonce'] : undefined;
      if (!needsNonce('replay') || !consumeNonce(presentedNonce, 'replay')) {
        json(res, 403, {
          error: 'replay requires a server-issued confirm nonce (POST /api/confirm/preview first)',
          code: 'NONCE_REQUIRED',
        });
        return;
      }

      // (4) venom gate — replay --live requires typed challenge (source workflow_id)
      if (live) {
        const typedChallenge = typeof body['typedChallenge'] === 'string' ? body['typedChallenge'] : '';
        if (typedChallenge !== sourceWorkflowId) {
          json(res, 403, {
            error: 'replay --live requires typedChallenge === source workflow_id (venom gate)',
            code: 'TYPED_CHALLENGE_REQUIRED',
          });
          return;
        }
      }

      // (5) C5-style audit (action='replay') before dispatch.
      // Order: validate → audit → dispatch. Never blocks on audit failure.
      const _replayEnv = cockpitEnvelope();
      const _replayAuditResult = await getHydraControl().audit(_replayEnv, 'replay', {
        workflow_id: sourceWorkflowId,
        detail: live ? 'replay --live' : 'replay dry (no --live)',
      });
      const _replayAuditNote = !_replayAuditResult.ok
        ? 'degraded'
        : _replayAuditResult.spooled
          ? 'spooled'
          : 'recorded';

      // (6) Launch the detached replay subprocess
      let replayResult: Awaited<ReturnType<typeof launchReplay>>;
      try {
        replayResult = await launchReplay({
          sourceWorkflowId,
          fromPhase: body['fromPhase'],
          swapModel: body['swapModel'],
          live,
        });
      } catch (e) {
        if (e instanceof ReplayValidationError) {
          json(res, 400, { error: e.message, code: e.code });
          return;
        }
        process.stderr.write(`[cockpit-bridge] replay error: ${String(e)}\n`);
        json(res, 502, { error: 'bridge upstream error', code: 'UPSTREAM' });
        return;
      }

      const replayResp: Record<string, unknown> = {
        workflow_id: replayResult.workflow_id,
        pid: replayResult.pid,
        log: replayResult.log,
      };
      if (_replayAuditNote === 'degraded') replayResp['audit'] = 'degraded';
      json(res, 202, replayResp);
      return;
    }

    // --- POST /api/tag_memory — tag an episodic row with additional cells (C6) ---
    if (path === '/api/tag_memory') {
      let body: Record<string, unknown>;
      try {
        body = await readBody(req);
      } catch {
        json(res, 400, { error: 'invalid request body', code: 'BAD_BODY' });
        return;
      }

      // (1) write-whitelist check
      if (!isWriteAllowed('tag_memory')) {
        json(res, 403, { error: 'tag_memory is not a sanctioned write', code: 'FORBIDDEN_TOOL' });
        return;
      }

      // (2) validate key — non-empty, reasonable charset
      const tagKey = typeof body['key'] === 'string' ? body['key'].trim() : '';
      if (tagKey.length === 0) {
        json(res, 400, { error: 'key must be a non-empty string', code: 'INVALID_KEY' });
        return;
      }
      if (!TAG_MEMORY_KEY_RE.test(tagKey)) {
        json(res, 400, {
          error: 'key contains invalid characters — must match [A-Za-z0-9][A-Za-z0-9\\-_.]{0,199}',
          code: 'INVALID_KEY',
        });
        return;
      }

      // (3) validate cells — subset of the 8 bagua keys
      const rawCells = body['cells'];
      if (!Array.isArray(rawCells) || rawCells.length === 0) {
        json(res, 400, {
          error: 'cells must be a non-empty array of bagua keys: ' + TAG_MEMORY_BAGUA_KEYS.join(', '),
          code: 'INVALID_CELLS',
        });
        return;
      }
      const invalidCells = rawCells.filter((c) => typeof c !== 'string' || !BAGUA_SET.has(c as string));
      if (invalidCells.length > 0) {
        json(res, 400, {
          error: `invalid cell(s): ${JSON.stringify(invalidCells)}. Valid: ${TAG_MEMORY_BAGUA_KEYS.join(', ')}`,
          code: 'INVALID_CELLS',
        });
        return;
      }
      const cells = rawCells as BaguaKey[];

      const replace = body['replace'] === true;

      // (4) tag_memory is Low risk — no nonce required.
      //     (CSRF is already checked above before this block is reached.)

      // (5) C5-style audit (action='tag_memory') before dispatch.
      const _tagEnv = cockpitEnvelope();
      const _tagAuditResult = await getHydraControl().audit(_tagEnv, 'tag_memory', {
        detail: `key=${tagKey} cells=${cells.join(',')}`,
      });
      const _tagAuditNote = !_tagAuditResult.ok
        ? 'degraded'
        : _tagAuditResult.spooled
          ? 'spooled'
          : 'recorded';

      // (6) Sanctioned direct write to hydra-mem.tag_memory.
      //     This is the ONE sanctioned hydra-mem write the cockpit makes.
      //     It BYPASSES allowTool() (the read-only gate) intentionally —
      //     tag_memory is a WRITE tool that the read whitelist correctly excludes.
      //     This bypass is ONLY for tag_memory; no other write can reach the
      //     mem client through this path.
      let tagResult: unknown;
      try {
        tagResult = await getClient().call('hydra-mem.tag_memory', {
          key: tagKey,
          cells,
          replace,
        });
      } catch (e) {
        process.stderr.write(`[cockpit-bridge] tag_memory upstream error: ${String(e)}\n`);
        json(res, 502, { error: 'bridge upstream error', code: 'UPSTREAM' });
        return;
      }

      const tagResp: Record<string, unknown> = {
        ok: true,
        key: tagKey,
        cells,
        result: tagResult,
      };
      if (_tagAuditNote === 'degraded') tagResp['audit'] = 'degraded';
      json(res, 200, tagResp);
      return;
    }

    // All other POST routes: 404.
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

    // GET /api/health — bridge liveness + both child liveness probes
    if (path === '/api/health') {
      // Probe child 1: hydra-mem (read path)
      let child1Ok = false;
      let ping1Detail: unknown = null;
      try {
        ping1Detail = await readTool('hydra-mem.ping');
        child1Ok = true;
      } catch {
        // child not reachable — degrade gracefully; still return 200 for bridge health
      }

      // Probe child 2: hydra-control (write path)
      let child2Ok = false;
      let ping2Detail: unknown = null;
      try {
        ping2Detail = await getHydraControl().ping();
        child2Ok = true;
      } catch {
        // child not reachable — degrade gracefully; still return 200 for bridge health
      }

      json(res, 200, {
        ok: true,
        bridge: 'hydra-cockpit',
        readTools: READ_HYDRA_TOOLS,
        // Legacy shape (child) kept for backwards compatibility with any C1 consumers
        child: { ok: child1Ok, ping: ping1Detail },
        // Extended shape: named children for clarity
        children: {
          'hydra-mem': { ok: child1Ok, ping: ping1Detail },
          'hydra-control': { ok: child2Ok, ping: ping2Detail },
        },
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
    //
    // Two modes:
    //   ABSENT cell param  → fan out all 8 bagua keys, return {cells:[{cell,count},...], degraded?:true}
    //   PRESENT cell param → query that single cell, return {cell, records, rows, count}
    //                        (rows is an alias kept for back-compat; records is what the SPA reads)
    //
    // Invalid cell → 400 (never 502).
    if (path === '/api/memory/cells') {
      const rawCell = url.searchParams.get('cell');
      const limit = Math.min(
        200,
        Math.max(1, parseInt(url.searchParams.get('limit') ?? '50', 10) || 50),
      );
      const workflowId = url.searchParams.get('workflow_id') ?? undefined;

      if (rawCell !== null && rawCell !== '') {
        // --- DRILL-DOWN: single cell ---
        // Validate the cell is one of the 8 bagua keys (400, not 502, on invalid).
        if (!BAGUA_SET.has(rawCell)) {
          json(res, 400, {
            error: `invalid cell '${rawCell}' — must be one of: ${TAG_MEMORY_BAGUA_KEYS.join(', ')}`,
            code: 'INVALID_CELL',
          });
          return;
        }
        const cellArgs: Record<string, unknown> = { cell: rawCell, limit };
        if (workflowId !== undefined) cellArgs['workflow_id'] = workflowId;
        const result = await readTool<{ cell?: string; rows?: unknown[]; count?: number }>(
          'hydra-mem.query_eights',
          cellArgs,
        );
        // Shape the response so the SPA's drill-down can read body.records.
        // Keep rows as an alias for any existing consumers.
        json(res, 200, {
          cell: result.cell ?? rawCell,
          records: result.rows ?? [],
          rows: result.rows ?? [],
          count: result.count ?? (result.rows?.length ?? 0),
        });
        return;
      }

      // --- OVERVIEW: fan out all 8 bagua keys ---
      // Each query_eights call with limit=200 gives an accurate count for
      // realistic episodic DB sizes; treat any per-cell error as count=0 + degraded.
      const OVERVIEW_LIMIT = 200;
      let degraded = false;
      const cellResults = await Promise.all(
        TAG_MEMORY_BAGUA_KEYS.map(async (key) => {
          try {
            const r = await readTool<{ cell?: string; rows?: unknown[]; count?: number }>(
              'hydra-mem.query_eights',
              { cell: key, limit: OVERVIEW_LIMIT },
            );
            return { cell: key, count: r.count ?? (r.rows?.length ?? 0) };
          } catch {
            degraded = true;
            return { cell: key, count: 0 };
          }
        }),
      );
      const overview: Record<string, unknown> = { cells: cellResults };
      if (degraded) overview['degraded'] = true;
      json(res, 200, overview);
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

    // GET /api/workflows/:id/stream — SSE trace tail
    // Headers: text/event-stream, cache-control: no-store, connection: keep-alive
    // Query: ?cursor=<byteOffset> (default 0)
    // This is a GET (read-only); Host guard applies; no CSRF required.
    const streamMatch = path.match(/^\/api\/workflows\/([A-Za-z0-9][A-Za-z0-9\-_]{0,63})\/stream$/);
    if (streamMatch !== null) {
      const wfId = streamMatch[1];
      if (wfId !== undefined) {
        // Parse optional byte cursor from query string
        const rawCursor = url.searchParams.get('cursor');
        const cursor = rawCursor !== null ? Math.max(0, parseInt(rawCursor, 10) || 0) : 0;

        // Set SSE headers and flush so the browser sees the stream begin
        res.writeHead(200, {
          'content-type': 'text/event-stream; charset=utf-8',
          'cache-control': 'no-store',
          'connection': 'keep-alive',
          'x-accel-buffering': 'no', // disable Nginx buffering if present
        });

        // Build the workflow_status getter that uses the whitelisted read path
        const getWorkflowStatus: GetWorkflowStatus = async (workflowId: string) => {
          return readTool<Record<string, unknown>>('hydra-mem.workflow_status', { workflow_id: workflowId });
        };

        // Hand off to the SSE streaming module
        streamWorkflow(req, res, wfId, cursor, getWorkflowStatus);
        return; // do NOT fall through — streamWorkflow owns res from here
      }
    }

    // GET /api/workflows/:id/poll — polling fallback endpoint
    // Returns { state, traceLines, nextCursor } — identical shapes to SSE events.
    // Query: ?cursor=<byteOffset> (default 0)
    // This is a GET (read-only); Host guard applies; no CSRF required.
    const pollMatch = path.match(/^\/api\/workflows\/([A-Za-z0-9][A-Za-z0-9\-_]{0,63})\/poll$/);
    if (pollMatch !== null) {
      const wfId = pollMatch[1];
      if (wfId !== undefined) {
        const rawCursor = url.searchParams.get('cursor');
        const cursor = rawCursor !== null ? Math.max(0, parseInt(rawCursor, 10) || 0) : 0;

        const getWorkflowStatus: GetWorkflowStatus = async (workflowId: string) => {
          return readTool<Record<string, unknown>>('hydra-mem.workflow_status', { workflow_id: workflowId });
        };

        const result = await pollWorkflow(wfId, cursor, getWorkflowStatus);
        json(res, 200, result);
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
  void hydraControl.close();
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
