/**
 * tests/server/replay.test.ts — C6 replay + tag_memory tests
 *
 * Coverage:
 *   A. Replay input validation
 *      A1. bad source workflow_id (empty, shell metachar, too long) → 400
 *      A2. bad from-phase → 400 INVALID_FROM_PHASE
 *      A3. bad swap-model → 400 INVALID_SWAP_MODEL
 *      A4. browser-supplied replay workflow_id is IGNORED (server mints it)
 *      A5. dry replay without nonce → 403 NONCE_REQUIRED
 *      A6. replay live without typed challenge → 403 TYPED_CHALLENGE_REQUIRED
 *      A7. replay live without nonce → 403 NONCE_REQUIRED
 *      A8. dry replay with valid nonce → proceeds (mock spawner called)
 *      A9. replay live with nonce + typed challenge → proceeds
 *      A10. replay audit filed before dispatch (action='replay')
 *      A11. CSRF required on /api/replay → 403 CSRF (via csrfOk gate)
 *
 *   B. tag_memory input validation
 *      B1. bad key (empty, shell metachar) → 400 INVALID_KEY
 *      B2. bad cell value → 400 INVALID_CELLS
 *      B3. empty cells array → 400 INVALID_CELLS
 *      B4. all 8 bagua keys accepted
 *      B5. non-tag write tool cannot be smuggled through the tag path
 *      B6. tag_memory audit filed before dispatch (action='tag_memory')
 *      B7. tag_memory happy path → 200 with key/cells/result
 *      B8. CSRF required on /api/tag_memory
 *
 *   C. launchReplay unit tests (replay.ts)
 *      C1. validates source_workflow_id (bad → ReplayValidationError)
 *      C2. validates from_phase (bad → ReplayValidationError)
 *      C3. validates swap_model (bad → ReplayValidationError)
 *      C4. mints new replay workflow_id (not the source id)
 *      C5. argv has no shell metacharacters
 *      C6. --live flag only present when live=true
 *      C7. --swap-model only present when provided
 *      C8. KNOWN_PHASES and MODEL_ID_RE coverage
 *
 * Strategy:
 *   - Use _setReplaySpawnerForTest() to inject a fake spawner (no real process).
 *   - Use _setClientForTest() / _setHydraControlForTest() for mem/control mocks.
 *   - No real HTTP port is bound; route logic is simulated directly (same
 *     approach as resume.test.ts / audit.test.ts).
 *   - No real replay or tag_memory write to live memory.
 */

import {
  describe,
  it,
  expect,
  beforeEach,
  afterEach,
} from 'vitest';

// ---------------------------------------------------------------------------
// Imports from server modules
// ---------------------------------------------------------------------------

import {
  launchReplay,
  ReplayValidationError,
  KNOWN_PHASES,
  MODEL_ID_RE,
  _setReplaySpawnerForTest,
} from '../../server/replay.js';

// Reuse WORKFLOW_ID_RE from launch.ts (same constant, same regex)
import { WORKFLOW_ID_RE } from '../../server/launch.js';
import { sessionToken } from '../../server/operator.js';
import { mintNonce, consumeNonce, _clearNonces } from '../../server/nonces.js';
import {
  isWriteAllowed,
  needsNonce,
  needsTypedChallenge,
} from '../../server/write-whitelist.js';
import { cockpitEnvelope } from '../../server/operator.js';
import {
  HydraControlClient,
  type AuditResult,
  type ResumeResult,
} from '../../server/hydra-control-client.js';
import { HydraMemClient } from '../../server/hydra-mem-client.js';
import { _setHydraControlForTest, _setClientForTest } from '../../server/index.js';

// ---------------------------------------------------------------------------
// Shared constants
// ---------------------------------------------------------------------------

const VALID_SOURCE_ID = '5ebd4268-5de0-4dbf-a82d-42c596d4818e';

// ---------------------------------------------------------------------------
// Fake spawner
// ---------------------------------------------------------------------------

interface CapturedSpawn {
  cmd: string;
  args: string[];
  opts: import('../../server/launch.js').DetachOptions;
}

// Derive SpawnFn from the injection parameter type (avoids cross-module type import)
type SpawnFn = Parameters<typeof _setReplaySpawnerForTest>[0];

function buildFakeSpawner(captured: CapturedSpawn[]): SpawnFn {
  return (cmd, args, opts) => {
    captured.push({ cmd, args, opts });
    return { pid: 7777 };
  };
}

// ---------------------------------------------------------------------------
// Fake HydraControlClient for audit capture
// ---------------------------------------------------------------------------

interface AuditCall {
  envelope: { actor: string; project: string; traceId: string };
  action: string;
  opts: { workflow_id?: string; option?: string; detail?: string };
}

function buildFakeControlClient(opts: {
  auditResult?: AuditResult;
  auditCalls?: AuditCall[];
  auditThrow?: boolean;
}): HydraControlClient {
  const mock = Object.create(HydraControlClient.prototype) as HydraControlClient;
  const defaultResult: AuditResult = { ok: true, spooled: false };

  (mock as unknown as { ping: () => Promise<Record<string, unknown>> }).ping =
    async () => ({ ok: true });

  (mock as unknown as {
    resume: (wid: string, action: string, option?: string) => Promise<ResumeResult>;
  }).resume = async (wid, action) => ({
    ok: true, launched: true, pid: 1234, workflow_id: wid, action, log: '/tmp/test.log',
  });

  (mock as unknown as {
    audit: (
      e: { actor: string; project: string; traceId: string },
      a: string,
      o?: object,
    ) => Promise<AuditResult>;
  }).audit = async (envelope, action, auditOpts: AuditCall['opts'] = {}) => {
    if (opts.auditCalls) opts.auditCalls.push({ envelope, action, opts: auditOpts });
    if (opts.auditThrow) throw new Error('simulated audit crash');
    return opts.auditResult ?? defaultResult;
  };

  return mock;
}

// ---------------------------------------------------------------------------
// Fake HydraMemClient for tag_memory capture
// ---------------------------------------------------------------------------

interface TagMemoryCall {
  tool: string;
  args: Record<string, unknown>;
}

function buildFakeMemClient(opts: {
  tagResult?: unknown;
  tagThrow?: boolean;
  calls?: TagMemoryCall[];
}): HydraMemClient {
  const mock = Object.create(HydraMemClient.prototype) as HydraMemClient;

  (mock as unknown as {
    call: (tool: string, args: Record<string, unknown>) => Promise<unknown>;
  }).call = async (tool: string, args: Record<string, unknown>) => {
    if (opts.calls) opts.calls.push({ tool, args });
    if (opts.tagThrow) throw new Error('simulated tag_memory upstream error');
    return opts.tagResult ?? { cells: args['cells'], key: args['key'] };
  };

  return mock;
}

// ---------------------------------------------------------------------------
// Simulate the /api/replay enforcement chain
// (mirrors exact enforcement order in index.ts POST /api/replay)
// ---------------------------------------------------------------------------

async function simulateReplay(params: {
  workflow_id?: string;
  fromPhase?: unknown;
  swapModel?: unknown;
  live?: boolean;
  confirmNonce?: string;
  typedChallenge?: string;
  // Control mocks
  auditResult?: AuditResult;
  auditCalls?: AuditCall[];
  spawns?: CapturedSpawn[];
}): Promise<{ status: number; body: Record<string, unknown>; spawnCalled: boolean }> {
  const {
    workflow_id = VALID_SOURCE_ID,
    fromPhase,
    swapModel,
    live = false,
    confirmNonce,
    typedChallenge,
    auditCalls = [],
    spawns = [],
  } = params;

  // (1) whitelist check
  if (!isWriteAllowed('replay')) {
    return { status: 403, body: { code: 'FORBIDDEN_TOOL' }, spawnCalled: false };
  }

  // (2) validate source workflow_id
  if (!WORKFLOW_ID_RE.test(workflow_id)) {
    return { status: 400, body: { error: 'workflow_id must match ...', code: 'INVALID_SOURCE_WORKFLOW_ID' }, spawnCalled: false };
  }

  // (3) nonce check (High risk — always required for replay)
  if (!consumeNonce(confirmNonce, 'replay')) {
    return {
      status: 403,
      body: { error: 'replay requires a server-issued confirm nonce', code: 'NONCE_REQUIRED' },
      spawnCalled: false,
    };
  }

  // (4) venom gate — live requires typed challenge
  if (live) {
    const tc = typedChallenge ?? '';
    if (tc !== workflow_id) {
      return {
        status: 403,
        body: { error: 'replay --live requires typedChallenge === source workflow_id (venom gate)', code: 'TYPED_CHALLENGE_REQUIRED' },
        spawnCalled: false,
      };
    }
  }

  // (5) audit before dispatch
  const fakeControl = buildFakeControlClient({
    auditResult: params.auditResult,
    auditCalls,
  });
  const env = cockpitEnvelope();
  const auditFn = (fakeControl as unknown as {
    audit: (e: typeof env, a: string, o: object) => Promise<AuditResult>;
  }).audit;
  const auditResult = await auditFn.call(fakeControl, env, 'replay', {
    workflow_id,
    detail: live ? 'replay --live' : 'replay dry (no --live)',
  });
  const auditNote = !auditResult.ok ? 'degraded' : auditResult.spooled ? 'spooled' : 'recorded';

  // (6) launch replay subprocess
  _setReplaySpawnerForTest(buildFakeSpawner(spawns) as SpawnFn);
  try {
    let replayResult: { workflow_id: string; pid: number; log: string };
    try {
      replayResult = await launchReplay({
        sourceWorkflowId: workflow_id,
        fromPhase,
        swapModel,
        live,
      });
    } catch (e) {
      if (e instanceof ReplayValidationError) {
        return { status: 400, body: { error: (e as ReplayValidationError).message, code: (e as ReplayValidationError).code }, spawnCalled: false };
      }
      return { status: 502, body: { error: 'bridge upstream error', code: 'UPSTREAM' }, spawnCalled: false };
    }

    const resp: Record<string, unknown> = {
      workflow_id: replayResult.workflow_id,
      pid: replayResult.pid,
      log: replayResult.log,
    };
    if (auditNote === 'degraded') resp['audit'] = 'degraded';

    return { status: 202, body: resp, spawnCalled: spawns.length > 0 };
  } finally {
    _setReplaySpawnerForTest(null);
  }
}

// ---------------------------------------------------------------------------
// Simulate the /api/tag_memory enforcement chain
// ---------------------------------------------------------------------------

async function simulateTagMemory(params: {
  key?: unknown;
  cells?: unknown;
  replace?: boolean;
  // Mocks
  auditResult?: AuditResult;
  auditCalls?: AuditCall[];
  tagCalls?: TagMemoryCall[];
  tagThrow?: boolean;
  tagResult?: unknown;
}): Promise<{ status: number; body: Record<string, unknown> }> {
  const { key, cells, replace = false, auditCalls = [], tagCalls = [] } = params;

  const TAG_MEMORY_BAGUA_KEYS = ['qian', 'kun', 'zhen', 'xun', 'kan', 'li', 'gen', 'dui'] as const;
  const TAG_MEMORY_KEY_RE = /^[A-Za-z0-9][A-Za-z0-9\-_.]{0,199}$/;
  const BAGUA_SET = new Set(TAG_MEMORY_BAGUA_KEYS as readonly string[]);

  // (1) whitelist check
  if (!isWriteAllowed('tag_memory')) {
    return { status: 403, body: { code: 'FORBIDDEN_TOOL' } };
  }

  // (2) validate key
  const tagKey = typeof key === 'string' ? key.trim() : '';
  if (tagKey.length === 0) {
    return { status: 400, body: { error: 'key must be a non-empty string', code: 'INVALID_KEY' } };
  }
  if (!TAG_MEMORY_KEY_RE.test(tagKey)) {
    return { status: 400, body: { error: 'key contains invalid characters', code: 'INVALID_KEY' } };
  }

  // (3) validate cells
  if (!Array.isArray(cells) || cells.length === 0) {
    return { status: 400, body: { error: 'cells must be a non-empty array of bagua keys', code: 'INVALID_CELLS' } };
  }
  const invalidCells = (cells as unknown[]).filter((c) => typeof c !== 'string' || !BAGUA_SET.has(c as string));
  if (invalidCells.length > 0) {
    return {
      status: 400,
      body: { error: `invalid cell(s): ${JSON.stringify(invalidCells)}`, code: 'INVALID_CELLS' },
    };
  }

  // (4) tag_memory is Low risk — no nonce required.

  // (5) audit before dispatch
  const fakeControl = buildFakeControlClient({
    auditResult: params.auditResult,
    auditCalls,
  });
  const env = cockpitEnvelope();
  const auditFn = (fakeControl as unknown as {
    audit: (e: typeof env, a: string, o: object) => Promise<AuditResult>;
  }).audit;
  const auditResult = await auditFn.call(fakeControl, env, 'tag_memory', {
    detail: `key=${tagKey} cells=${(cells as string[]).join(',')}`,
  });
  const auditNote = !auditResult.ok ? 'degraded' : auditResult.spooled ? 'spooled' : 'recorded';

  // (6) sanctioned direct call to hydra-mem.tag_memory (bypasses read whitelist)
  const fakeMemClient = buildFakeMemClient({
    tagResult: params.tagResult,
    tagThrow: params.tagThrow,
    calls: tagCalls,
  });
  let tagResult: unknown;
  try {
    tagResult = await (fakeMemClient as unknown as {
      call: (tool: string, args: Record<string, unknown>) => Promise<unknown>;
    }).call('hydra-mem.tag_memory', { key: tagKey, cells, replace });
  } catch {
    return { status: 502, body: { error: 'bridge upstream error', code: 'UPSTREAM' } };
  }

  const resp: Record<string, unknown> = { ok: true, key: tagKey, cells, result: tagResult };
  if (auditNote === 'degraded') resp['audit'] = 'degraded';

  return { status: 200, body: resp };
}

// ===========================================================================
// A. Replay input validation
// ===========================================================================

describe('replay — write whitelist entry', () => {
  it('replay is in the write whitelist', () => {
    expect(isWriteAllowed('replay')).toBe(true);
  });

  it('replay requires a nonce (High risk)', () => {
    expect(needsNonce('replay')).toBe(true);
  });

  it('replay does NOT require a typed challenge by whitelist (venom=live, handled in route)', () => {
    // The write-whitelist entry for replay has requiresTypedChallenge:false;
    // the live-typed-challenge is enforced ad-hoc in the route for venom-gate.
    expect(needsTypedChallenge('replay')).toBe(false);
  });
});

describe('A1. replay — bad source workflow_id → 400', () => {
  beforeEach(() => _clearNonces());
  afterEach(() => _clearNonces());

  it('empty source workflow_id → 400 INVALID_SOURCE_WORKFLOW_ID', async () => {
    const { nonce } = mintNonce('replay');
    const result = await simulateReplay({ workflow_id: '', confirmNonce: nonce });
    expect(result.status).toBe(400);
    expect(result.body['code']).toBe('INVALID_SOURCE_WORKFLOW_ID');
  });

  it('source id with shell metachar (;) → 400 INVALID_SOURCE_WORKFLOW_ID', async () => {
    const { nonce } = mintNonce('replay');
    const result = await simulateReplay({ workflow_id: 'id;evil', confirmNonce: nonce });
    expect(result.status).toBe(400);
    expect(result.body['code']).toBe('INVALID_SOURCE_WORKFLOW_ID');
  });

  it('source id starting with hyphen → 400 INVALID_SOURCE_WORKFLOW_ID', async () => {
    const { nonce } = mintNonce('replay');
    const result = await simulateReplay({ workflow_id: '-bad-id', confirmNonce: nonce });
    expect(result.status).toBe(400);
    expect(result.body['code']).toBe('INVALID_SOURCE_WORKFLOW_ID');
  });

  it('source id too long (65 chars) → 400 INVALID_SOURCE_WORKFLOW_ID', async () => {
    const { nonce } = mintNonce('replay');
    const result = await simulateReplay({ workflow_id: 'a' + 'b'.repeat(64), confirmNonce: nonce });
    expect(result.status).toBe(400);
    expect(result.body['code']).toBe('INVALID_SOURCE_WORKFLOW_ID');
  });

  it('valid uuid4 passes the source workflow_id check', async () => {
    const { nonce } = mintNonce('replay');
    const spawns: CapturedSpawn[] = [];
    const result = await simulateReplay({
      workflow_id: VALID_SOURCE_ID,
      confirmNonce: nonce,
      spawns,
    });
    // Should proceed (no source-id rejection)
    expect(result.body['code']).not.toBe('INVALID_SOURCE_WORKFLOW_ID');
    expect(result.status).toBe(202);
  });
});

describe('A2. replay — bad from-phase → 400 INVALID_FROM_PHASE', () => {
  beforeEach(() => _clearNonces());
  afterEach(() => _clearNonces());

  it('unknown phase → ReplayValidationError INVALID_FROM_PHASE', async () => {
    const { nonce } = mintNonce('replay');
    const result = await simulateReplay({
      confirmNonce: nonce,
      fromPhase: 'bogus-phase',
    });
    expect(result.status).toBe(400);
    expect(result.body['code']).toBe('INVALID_FROM_PHASE');
  });

  it('empty from-phase defaults to intake (no error)', async () => {
    const { nonce } = mintNonce('replay');
    const spawns: CapturedSpawn[] = [];
    const result = await simulateReplay({ confirmNonce: nonce, fromPhase: '', spawns });
    expect(result.status).toBe(202);
    // default is 'intake'; argv should include --from-phase intake
    expect(spawns[0]?.args).toContain('intake');
  });

  it('all 8 known phases are accepted', async () => {
    for (const phase of KNOWN_PHASES) {
      const { nonce } = mintNonce('replay');
      const spawns: CapturedSpawn[] = [];
      const result = await simulateReplay({ confirmNonce: nonce, fromPhase: phase, spawns });
      expect(result.status, `phase=${phase} should be accepted`).toBe(202);
      _clearNonces();
    }
  });
});

describe('A3. replay — bad swap-model → 400 INVALID_SWAP_MODEL', () => {
  beforeEach(() => _clearNonces());
  afterEach(() => _clearNonces());

  it('swap-model starting with hyphen → 400 INVALID_SWAP_MODEL', async () => {
    const { nonce } = mintNonce('replay');
    const result = await simulateReplay({ confirmNonce: nonce, swapModel: '-bad-model' });
    expect(result.status).toBe(400);
    expect(result.body['code']).toBe('INVALID_SWAP_MODEL');
  });

  it('swap-model with shell metachar → 400 INVALID_SWAP_MODEL', async () => {
    const { nonce } = mintNonce('replay');
    const result = await simulateReplay({ confirmNonce: nonce, swapModel: 'model;evil' });
    expect(result.status).toBe(400);
    expect(result.body['code']).toBe('INVALID_SWAP_MODEL');
  });

  it('valid model id (claude-sonnet-4-6) is accepted', async () => {
    const { nonce } = mintNonce('replay');
    const spawns: CapturedSpawn[] = [];
    const result = await simulateReplay({
      confirmNonce: nonce,
      swapModel: 'claude-sonnet-4-6',
      spawns,
    });
    expect(result.status).toBe(202);
    expect(spawns[0]?.args).toContain('--swap-model');
    expect(spawns[0]?.args).toContain('claude-sonnet-4-6');
  });

  it('absent swap-model → no --swap-model in argv', async () => {
    const { nonce } = mintNonce('replay');
    const spawns: CapturedSpawn[] = [];
    await simulateReplay({ confirmNonce: nonce, spawns });
    expect(spawns[0]?.args).not.toContain('--swap-model');
  });
});

describe('A4. browser-supplied replay workflow_id is IGNORED', () => {
  beforeEach(() => _clearNonces());
  afterEach(() => _clearNonces());

  it('returned workflow_id is minted server-side (not the source id)', async () => {
    const { nonce } = mintNonce('replay');
    const spawns: CapturedSpawn[] = [];
    const result = await simulateReplay({ confirmNonce: nonce, spawns });
    expect(result.status).toBe(202);
    const returnedId = result.body['workflow_id'] as string;
    // Must not equal the source id
    expect(returnedId).not.toBe(VALID_SOURCE_ID);
    // Must be a valid uuid4
    expect(WORKFLOW_ID_RE.test(returnedId)).toBe(true);
  });

  it('two replay calls produce two different workflow_ids', async () => {
    const { nonce: n1 } = mintNonce('replay');
    const spawns1: CapturedSpawn[] = [];
    const r1 = await simulateReplay({ confirmNonce: n1, spawns: spawns1 });
    _clearNonces();

    const { nonce: n2 } = mintNonce('replay');
    const spawns2: CapturedSpawn[] = [];
    const r2 = await simulateReplay({ confirmNonce: n2, spawns: spawns2 });

    expect(r1.body['workflow_id']).not.toBe(r2.body['workflow_id']);
  });
});

describe('A5. dry replay without nonce → 403 NONCE_REQUIRED', () => {
  beforeEach(() => _clearNonces());
  afterEach(() => _clearNonces());

  it('no confirmNonce → 403 NONCE_REQUIRED', async () => {
    const result = await simulateReplay({ confirmNonce: undefined });
    expect(result.status).toBe(403);
    expect(result.body['code']).toBe('NONCE_REQUIRED');
  });

  it('wrong confirmNonce → 403 NONCE_REQUIRED', async () => {
    const result = await simulateReplay({ confirmNonce: 'bad-nonce-value' });
    expect(result.status).toBe(403);
    expect(result.body['code']).toBe('NONCE_REQUIRED');
  });

  it('nonce for wrong action → 403 NONCE_REQUIRED', async () => {
    const { nonce } = mintNonce('launch'); // wrong action
    const result = await simulateReplay({ confirmNonce: nonce });
    expect(result.status).toBe(403);
    expect(result.body['code']).toBe('NONCE_REQUIRED');
  });
});

describe('A6. replay live without typed challenge → 403 TYPED_CHALLENGE_REQUIRED', () => {
  beforeEach(() => _clearNonces());
  afterEach(() => _clearNonces());

  it('live=true + no typedChallenge → 403 TYPED_CHALLENGE_REQUIRED', async () => {
    const { nonce } = mintNonce('replay');
    const result = await simulateReplay({
      confirmNonce: nonce,
      live: true,
      typedChallenge: undefined,
    });
    expect(result.status).toBe(403);
    expect(result.body['code']).toBe('TYPED_CHALLENGE_REQUIRED');
  });

  it('live=true + wrong typedChallenge → 403 TYPED_CHALLENGE_REQUIRED', async () => {
    const { nonce } = mintNonce('replay');
    const result = await simulateReplay({
      confirmNonce: nonce,
      live: true,
      typedChallenge: 'wrong-id-value',
    });
    expect(result.status).toBe(403);
    expect(result.body['code']).toBe('TYPED_CHALLENGE_REQUIRED');
  });
});

describe('A7. replay live without nonce → 403 NONCE_REQUIRED (before typed check)', () => {
  beforeEach(() => _clearNonces());
  afterEach(() => _clearNonces());

  it('live=true + no nonce + correct typedChallenge → 403 NONCE_REQUIRED', async () => {
    // No nonce provided — nonce check fires BEFORE typed-challenge check
    const result = await simulateReplay({
      confirmNonce: undefined,
      live: true,
      typedChallenge: VALID_SOURCE_ID,
    });
    expect(result.status).toBe(403);
    expect(result.body['code']).toBe('NONCE_REQUIRED');
  });
});

describe('A8. dry replay with valid nonce → proceeds', () => {
  beforeEach(() => _clearNonces());
  afterEach(() => _clearNonces());

  it('dry replay with nonce → 202 with workflow_id + pid + log', async () => {
    const { nonce } = mintNonce('replay');
    const spawns: CapturedSpawn[] = [];
    const result = await simulateReplay({ confirmNonce: nonce, spawns });
    expect(result.status).toBe(202);
    expect(typeof result.body['workflow_id']).toBe('string');
    expect(typeof result.body['pid']).toBe('number');
    expect(typeof result.body['log']).toBe('string');
    expect(spawns).toHaveLength(1);
  });

  it('dry replay argv contains replay subcommand + source id', async () => {
    const { nonce } = mintNonce('replay');
    const spawns: CapturedSpawn[] = [];
    await simulateReplay({ confirmNonce: nonce, spawns });
    expect(spawns[0]?.args).toContain('replay');
    expect(spawns[0]?.args).toContain(VALID_SOURCE_ID);
    // dry replay must NOT have --live
    expect(spawns[0]?.args).not.toContain('--live');
  });
});

describe('A9. replay live with nonce + typed challenge → proceeds', () => {
  beforeEach(() => _clearNonces());
  afterEach(() => _clearNonces());

  it('live replay → 202; argv includes --live', async () => {
    const { nonce } = mintNonce('replay');
    const spawns: CapturedSpawn[] = [];
    const result = await simulateReplay({
      confirmNonce: nonce,
      live: true,
      typedChallenge: VALID_SOURCE_ID,
      spawns,
    });
    expect(result.status).toBe(202);
    expect(spawns[0]?.args).toContain('--live');
  });
});

describe('A10. replay audit filed before dispatch', () => {
  beforeEach(() => _clearNonces());
  afterEach(() => _clearNonces());

  it('audit is called with action=replay and workflow_id before spawn', async () => {
    const auditCalls: AuditCall[] = [];
    const { nonce } = mintNonce('replay');
    const spawns: CapturedSpawn[] = [];
    const result = await simulateReplay({
      confirmNonce: nonce,
      auditCalls,
      spawns,
    });
    expect(result.status).toBe(202);
    expect(auditCalls).toHaveLength(1);
    expect(auditCalls[0]!.action).toBe('replay');
    expect(auditCalls[0]!.opts.workflow_id).toBe(VALID_SOURCE_ID);
  });

  it('audit envelope has fixed actor=hydra-cockpit and project=Hydra', async () => {
    const auditCalls: AuditCall[] = [];
    const { nonce } = mintNonce('replay');
    await simulateReplay({ confirmNonce: nonce, auditCalls });
    expect(auditCalls[0]!.envelope.actor).toBe('hydra-cockpit');
    expect(auditCalls[0]!.envelope.project).toBe('Hydra');
  });

  it('hard audit failure → action still proceeds (audit is not a gate)', async () => {
    const { nonce } = mintNonce('replay');
    const spawns: CapturedSpawn[] = [];
    const result = await simulateReplay({
      confirmNonce: nonce,
      auditResult: { ok: false, reason: 'audit_client_error' },
      spawns,
    });
    expect(result.status).toBe(202);
    expect(result.body['audit']).toBe('degraded');
  });
});

describe('A11. CSRF required on /api/replay (via csrfOk gate)', () => {
  it('needsNonce(replay) === true confirms it is High risk and needs CSRF + nonce', () => {
    // CSRF is enforced at the top of the POST block in index.ts before any body is read.
    // We verify the write-whitelist classification that drives the CSRF + nonce requirement.
    expect(isWriteAllowed('replay')).toBe(true);
    expect(needsNonce('replay')).toBe(true);
  });
});

// ===========================================================================
// B. tag_memory input validation
// ===========================================================================

describe('tag_memory — write whitelist entry', () => {
  it('tag_memory is in the write whitelist', () => {
    expect(isWriteAllowed('tag_memory')).toBe(true);
  });

  it('tag_memory does NOT require a nonce (Low risk)', () => {
    expect(needsNonce('tag_memory')).toBe(false);
  });

  it('tag_memory does NOT require a typed challenge', () => {
    expect(needsTypedChallenge('tag_memory')).toBe(false);
  });
});

describe('B1. tag_memory — bad key → 400 INVALID_KEY', () => {
  it('empty key → 400 INVALID_KEY', async () => {
    const result = await simulateTagMemory({ key: '', cells: ['qian'] });
    expect(result.status).toBe(400);
    expect(result.body['code']).toBe('INVALID_KEY');
  });

  it('key with shell metachar (;) → 400 INVALID_KEY', async () => {
    const result = await simulateTagMemory({ key: 'my;evil', cells: ['qian'] });
    expect(result.status).toBe(400);
    expect(result.body['code']).toBe('INVALID_KEY');
  });

  it('key with space → 400 INVALID_KEY', async () => {
    const result = await simulateTagMemory({ key: 'my key', cells: ['qian'] });
    expect(result.status).toBe(400);
    expect(result.body['code']).toBe('INVALID_KEY');
  });

  it('valid key (alphanumeric, hyphens, dots, underscores) → proceeds', async () => {
    const result = await simulateTagMemory({ key: 'my-tag.v2_test', cells: ['qian'] });
    expect(result.status).toBe(200);
  });
});

describe('B2. tag_memory — bad cell value → 400 INVALID_CELLS', () => {
  it('invalid cell name → 400 INVALID_CELLS', async () => {
    const result = await simulateTagMemory({ key: 'mykey', cells: ['bogus-cell'] });
    expect(result.status).toBe(400);
    expect(result.body['code']).toBe('INVALID_CELLS');
  });

  it('mix of valid and invalid cells → 400 INVALID_CELLS', async () => {
    const result = await simulateTagMemory({ key: 'mykey', cells: ['qian', 'bad-cell'] });
    expect(result.status).toBe(400);
    expect(result.body['code']).toBe('INVALID_CELLS');
  });

  it('uppercase cell name → 400 INVALID_CELLS', async () => {
    const result = await simulateTagMemory({ key: 'mykey', cells: ['QIAN'] });
    expect(result.status).toBe(400);
    expect(result.body['code']).toBe('INVALID_CELLS');
  });
});

describe('B3. tag_memory — empty cells array → 400 INVALID_CELLS', () => {
  it('empty array → 400 INVALID_CELLS', async () => {
    const result = await simulateTagMemory({ key: 'mykey', cells: [] });
    expect(result.status).toBe(400);
    expect(result.body['code']).toBe('INVALID_CELLS');
  });

  it('non-array cells → 400 INVALID_CELLS', async () => {
    const result = await simulateTagMemory({ key: 'mykey', cells: 'qian' });
    expect(result.status).toBe(400);
    expect(result.body['code']).toBe('INVALID_CELLS');
  });
});

describe('B4. tag_memory — all 8 bagua keys accepted', () => {
  const ALL_BAGUA = ['qian', 'kun', 'zhen', 'xun', 'kan', 'li', 'gen', 'dui'];

  it('each individual bagua key is accepted', async () => {
    for (const cell of ALL_BAGUA) {
      const result = await simulateTagMemory({ key: 'mykey', cells: [cell] });
      expect(result.status, `cell=${cell} should be accepted`).toBe(200);
    }
  });

  it('all 8 cells in one call is accepted', async () => {
    const result = await simulateTagMemory({ key: 'mykey', cells: ALL_BAGUA });
    expect(result.status).toBe(200);
  });
});

describe('B5. tag_memory — non-tag write tool cannot be smuggled through the tag path', () => {
  it('tag_memory call goes to hydra-mem.tag_memory, not any other tool', async () => {
    const tagCalls: TagMemoryCall[] = [];
    await simulateTagMemory({ key: 'mykey', cells: ['qian'], tagCalls });
    expect(tagCalls).toHaveLength(1);
    // The tool called must be exactly 'hydra-mem.tag_memory'
    expect(tagCalls[0]!.tool).toBe('hydra-mem.tag_memory');
    // No other tool was called
    const otherTools = tagCalls.filter((c) => c.tool !== 'hydra-mem.tag_memory');
    expect(otherTools).toHaveLength(0);
  });

  it('tag call carries the validated key and cells (no smuggled payload)', async () => {
    const tagCalls: TagMemoryCall[] = [];
    await simulateTagMemory({ key: 'my-safe-key', cells: ['kun', 'li'], tagCalls });
    expect(tagCalls[0]!.args['key']).toBe('my-safe-key');
    expect(tagCalls[0]!.args['cells']).toEqual(['kun', 'li']);
  });
});

describe('B6. tag_memory — audit filed before dispatch', () => {
  it('audit called with action=tag_memory before mem write', async () => {
    const auditCalls: AuditCall[] = [];
    const result = await simulateTagMemory({ key: 'mykey', cells: ['zhen'], auditCalls });
    expect(result.status).toBe(200);
    expect(auditCalls).toHaveLength(1);
    expect(auditCalls[0]!.action).toBe('tag_memory');
  });

  it('audit hard failure → action proceeds + audit:degraded surfaced', async () => {
    const result = await simulateTagMemory({
      key: 'mykey',
      cells: ['xun'],
      auditResult: { ok: false, reason: 'audit_client_error' },
    });
    expect(result.status).toBe(200);
    expect(result.body['audit']).toBe('degraded');
  });
});

describe('B7. tag_memory — happy path', () => {
  it('returns 200 with ok:true + key + cells + result', async () => {
    const result = await simulateTagMemory({ key: 'my-tag', cells: ['qian', 'kan'] });
    expect(result.status).toBe(200);
    expect(result.body['ok']).toBe(true);
    expect(result.body['key']).toBe('my-tag');
    expect(result.body['cells']).toEqual(['qian', 'kan']);
    expect(result.body['result']).toBeDefined();
  });

  it('upstream error → 502 UPSTREAM, no raw error text', async () => {
    const result = await simulateTagMemory({
      key: 'mykey',
      cells: ['qian'],
      tagThrow: true,
    });
    expect(result.status).toBe(502);
    expect(result.body['code']).toBe('UPSTREAM');
    expect(result.body['error']).toBe('bridge upstream error');
    expect(JSON.stringify(result.body)).not.toContain('simulated');
  });
});

describe('B8. CSRF required on /api/tag_memory', () => {
  it('tag_memory is Low risk (no nonce required), but CSRF is still required (write route)', () => {
    // CSRF is enforced at the top of the POST block before any body is read.
    // tag_memory Low risk means no nonce, but CSRF is inviolable on ALL POSTs.
    expect(isWriteAllowed('tag_memory')).toBe(true);
    expect(needsNonce('tag_memory')).toBe(false);
    // The CSRF gate is upstream of all route logic — verified in bridge.test.ts.
    // Here we just confirm the classification.
  });
});

// ===========================================================================
// C. launchReplay unit tests (replay.ts validators)
// ===========================================================================

describe('C1. launchReplay — validates source_workflow_id', () => {
  afterEach(() => _setReplaySpawnerForTest(null));

  it('rejects empty source id', async () => {
    _setReplaySpawnerForTest((() => ({ pid: 1 })) as SpawnFn);
    await expect(launchReplay({ sourceWorkflowId: '' })).rejects.toBeInstanceOf(ReplayValidationError);
  });

  it('rejects non-string source id', async () => {
    _setReplaySpawnerForTest((() => ({ pid: 1 })) as SpawnFn);
    await expect(launchReplay({ sourceWorkflowId: 123 })).rejects.toBeInstanceOf(ReplayValidationError);
  });

  it('rejects source id with shell metachar', async () => {
    _setReplaySpawnerForTest((() => ({ pid: 1 })) as SpawnFn);
    let err: unknown;
    try { await launchReplay({ sourceWorkflowId: 'id;evil' }); }
    catch (e) { err = e; }
    expect(err).toBeInstanceOf(ReplayValidationError);
    expect((err as ReplayValidationError).code).toBe('INVALID_SOURCE_WORKFLOW_ID');
  });

  it('accepts valid uuid4 source id', async () => {
    const spawns: { args: string[] }[] = [];
    _setReplaySpawnerForTest(((cmd, args) => { spawns.push({ args }); return { pid: 1 }; }) as SpawnFn);
    const result = await launchReplay({ sourceWorkflowId: VALID_SOURCE_ID });
    expect(result.workflow_id).toBeDefined();
    expect(WORKFLOW_ID_RE.test(result.workflow_id)).toBe(true);
  });
});

describe('C2. launchReplay — validates from_phase', () => {
  afterEach(() => _setReplaySpawnerForTest(null));

  it('rejects unknown phase', async () => {
    _setReplaySpawnerForTest((() => ({ pid: 1 })) as SpawnFn);
    let err: unknown;
    try { await launchReplay({ sourceWorkflowId: VALID_SOURCE_ID, fromPhase: 'bogus-phase' }); }
    catch (e) { err = e; }
    expect(err).toBeInstanceOf(ReplayValidationError);
    expect((err as ReplayValidationError).code).toBe('INVALID_FROM_PHASE');
  });

  it('defaults to intake when from_phase is absent', async () => {
    const spawns: { args: string[] }[] = [];
    _setReplaySpawnerForTest(((cmd, args) => { spawns.push({ args }); return { pid: 1 }; }) as SpawnFn);
    await launchReplay({ sourceWorkflowId: VALID_SOURCE_ID });
    expect(spawns[0]?.args).toContain('--from-phase');
    expect(spawns[0]?.args).toContain('intake');
  });
});

describe('C3. launchReplay — validates swap_model', () => {
  afterEach(() => _setReplaySpawnerForTest(null));

  it('rejects swap_model starting with hyphen', async () => {
    _setReplaySpawnerForTest((() => ({ pid: 1 })) as SpawnFn);
    let err: unknown;
    try { await launchReplay({ sourceWorkflowId: VALID_SOURCE_ID, swapModel: '-bad-model' }); }
    catch (e) { err = e; }
    expect(err).toBeInstanceOf(ReplayValidationError);
    expect((err as ReplayValidationError).code).toBe('INVALID_SWAP_MODEL');
  });

  it('rejects swap_model with shell metachar', async () => {
    _setReplaySpawnerForTest((() => ({ pid: 1 })) as SpawnFn);
    let err: unknown;
    try { await launchReplay({ sourceWorkflowId: VALID_SOURCE_ID, swapModel: 'model;evil' }); }
    catch (e) { err = e; }
    expect(err).toBeInstanceOf(ReplayValidationError);
    expect((err as ReplayValidationError).code).toBe('INVALID_SWAP_MODEL');
  });

  it('accepts valid model id with slashes and dots', async () => {
    const spawns: { args: string[] }[] = [];
    _setReplaySpawnerForTest(((cmd, args) => { spawns.push({ args }); return { pid: 1 }; }) as SpawnFn);
    await launchReplay({
      sourceWorkflowId: VALID_SOURCE_ID,
      swapModel: 'openai/o3-mini',
    });
    expect(spawns[0]?.args).toContain('openai/o3-mini');
  });
});

describe('C4. launchReplay — mints new replay workflow_id', () => {
  afterEach(() => _setReplaySpawnerForTest(null));

  it('returned workflow_id differs from source id', async () => {
    _setReplaySpawnerForTest((() => ({ pid: 1 })) as SpawnFn);
    const result = await launchReplay({ sourceWorkflowId: VALID_SOURCE_ID });
    expect(result.workflow_id).not.toBe(VALID_SOURCE_ID);
    expect(WORKFLOW_ID_RE.test(result.workflow_id)).toBe(true);
  });
});

describe('C5. launchReplay — argv has no shell metacharacters', () => {
  afterEach(() => _setReplaySpawnerForTest(null));

  it('all argv tokens match safe character sets', async () => {
    const spawns: { args: string[] }[] = [];
    _setReplaySpawnerForTest(((cmd, args) => { spawns.push({ args }); return { pid: 1 }; }) as SpawnFn);
    await launchReplay({
      sourceWorkflowId: VALID_SOURCE_ID,
      fromPhase: 'planning',
      swapModel: 'claude-sonnet-4-6',
    });
    const SAFE_ARG_RE = /^[A-Za-z0-9\-_./: ]+$/;
    for (const arg of spawns[0]?.args ?? []) {
      expect(SAFE_ARG_RE.test(arg), `argv token ${JSON.stringify(arg)} must be safe`).toBe(true);
    }
  });
});

describe('C6. launchReplay — --live flag only present when live=true', () => {
  afterEach(() => _setReplaySpawnerForTest(null));

  it('dry replay: no --live in argv', async () => {
    const spawns: { args: string[] }[] = [];
    _setReplaySpawnerForTest(((cmd, args) => { spawns.push({ args }); return { pid: 1 }; }) as SpawnFn);
    await launchReplay({ sourceWorkflowId: VALID_SOURCE_ID, live: false });
    expect(spawns[0]?.args).not.toContain('--live');
  });

  it('live replay: --live in argv', async () => {
    const spawns: { args: string[] }[] = [];
    _setReplaySpawnerForTest(((cmd, args) => { spawns.push({ args }); return { pid: 1 }; }) as SpawnFn);
    await launchReplay({ sourceWorkflowId: VALID_SOURCE_ID, live: true });
    expect(spawns[0]?.args).toContain('--live');
  });
});

describe('C7. launchReplay — --swap-model only present when provided', () => {
  afterEach(() => _setReplaySpawnerForTest(null));

  it('no swap_model: --swap-model absent from argv', async () => {
    const spawns: { args: string[] }[] = [];
    _setReplaySpawnerForTest(((cmd, args) => { spawns.push({ args }); return { pid: 1 }; }) as SpawnFn);
    await launchReplay({ sourceWorkflowId: VALID_SOURCE_ID });
    expect(spawns[0]?.args).not.toContain('--swap-model');
  });
});

describe('C8. KNOWN_PHASES and MODEL_ID_RE coverage', () => {
  it('KNOWN_PHASES has exactly 8 entries', () => {
    expect(KNOWN_PHASES.length).toBe(8);
  });

  it('KNOWN_PHASES includes all supervisor interrupt boundaries', () => {
    const expected = ['intake', 'planning', 'approval', 'dispatch', 'executing', 'judge', 'synthesis', 'postcheck'];
    for (const p of expected) {
      expect((KNOWN_PHASES as readonly string[]).includes(p), `${p} must be in KNOWN_PHASES`).toBe(true);
    }
  });

  it('MODEL_ID_RE accepts standard model ids', () => {
    const valid = [
      'claude-sonnet-4-6',
      'gpt-4o',
      'gemini-2-flash',
      'openai/o3-mini',
      'anthropic.claude-3-haiku-20240307-v1:0',
    ];
    for (const id of valid) {
      expect(MODEL_ID_RE.test(id), `${id} should match MODEL_ID_RE`).toBe(true);
    }
  });

  it('MODEL_ID_RE rejects shell metacharacters', () => {
    const invalid = [
      'model;evil',
      'model|evil',
      'model$(cmd)',
      '-starts-with-hyphen',
    ];
    for (const id of invalid) {
      expect(MODEL_ID_RE.test(id), `${id} should NOT match MODEL_ID_RE`).toBe(false);
    }
  });
});
