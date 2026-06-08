/**
 * Hydra Cockpit — API client.
 * Bootstraps the CSRF token from /api/session, attaches X-Hydra-Token on writes,
 * handles the confirm-nonce preview flow, and provides EventSource + poll fallback.
 *
 * No email ever leaves this module; no actor/project fields are set client-side.
 */

// eslint-disable-next-line @typescript-eslint/no-explicit-any
const API_BASE: string = ((import.meta as any).env?.['VITE_HYDRA_API'] as string | undefined) ?? '/api';

// ---------------------------------------------------------------------------
// CSRF session token — per-session, cached, single re-bootstrap on 403
// ---------------------------------------------------------------------------

let _cachedToken: string | null = null;

export async function getSessionToken(force = false): Promise<string> {
  if (_cachedToken && !force) return _cachedToken;
  const res = await fetch(`${API_BASE}/session`, { method: 'GET' });
  if (!res.ok) throw new Error(`session bootstrap failed: ${res.status}`);
  const body = (await res.json()) as { token?: string };
  if (!body.token) throw new Error('session bootstrap returned no token');
  _cachedToken = body.token;
  return _cachedToken;
}

/** Exposed for tests only. */
export function _setCachedToken(t: string | null): void {
  _cachedToken = t;
}

// ---------------------------------------------------------------------------
// Write error class
// ---------------------------------------------------------------------------

export interface WriteError {
  ok: false;
  status: number;
  code?: string | undefined;
  error: string;
}

export class CockpitWriteError extends Error {
  constructor(public readonly detail: WriteError) {
    super(detail.error);
    this.name = 'CockpitWriteError';
  }
}

// ---------------------------------------------------------------------------
// Core GET helper (read-only, no CSRF required)
// ---------------------------------------------------------------------------

export async function apiFetch<T>(
  path: string,
  opts?: { signal?: AbortSignal; params?: Record<string, string> },
): Promise<T> {
  const url = new URL(`${API_BASE}${path}`, window.location.origin);
  if (opts?.params) {
    for (const [k, v] of Object.entries(opts.params)) {
      url.searchParams.set(k, v);
    }
  }
  const fetchOpts: RequestInit = {};
  if (opts?.signal) fetchOpts.signal = opts.signal;
  const res = await fetch(url.toString(), fetchOpts);
  if (!res.ok) {
    let errBody: Record<string, unknown> = {};
    try { errBody = await res.json() as Record<string, unknown>; } catch { /* ignore */ }
    const errCode = typeof errBody['code'] === 'string' ? errBody['code'] : undefined;
    const errMsg = typeof errBody['error'] === 'string' ? errBody['error'] : `HTTP ${res.status}`;
    const detail: WriteError = errCode !== undefined
      ? { ok: false, status: res.status, code: errCode, error: errMsg }
      : { ok: false, status: res.status, error: errMsg };
    throw new CockpitWriteError(detail);
  }
  return res.json() as Promise<T>;
}

// ---------------------------------------------------------------------------
// Core POST helper with CSRF + single re-bootstrap on 403 CSRF
// ---------------------------------------------------------------------------

export async function apiPost<T>(
  path: string,
  body: Record<string, unknown>,
  retry = true,
): Promise<T> {
  const token = await getSessionToken();
  const res = await fetch(`${API_BASE}${path}`, {
    method: 'POST',
    headers: {
      'content-type': 'application/json',
      'x-hydra-token': token,   // INVARIANT #4 — CSRF header, never X-Mesh-Token
    },
    body: JSON.stringify(body),
  });

  let parsed: unknown = null;
  try { parsed = await res.json(); } catch { /* non-JSON */ }

  if (!res.ok) {
    const p = (parsed ?? {}) as Record<string, unknown>;
    // 403 CSRF → drop cache + re-bootstrap once
    if (res.status === 403 && p['code'] === 'CSRF' && retry) {
      _cachedToken = null;
      return apiPost<T>(path, body, false);
    }
    const errCode2 = typeof p['code'] === 'string' ? p['code'] : undefined;
    const errMsg2 = typeof p['error'] === 'string' ? p['error'] : `write failed (${res.status})`;
    const detail2: WriteError = errCode2 !== undefined
      ? { ok: false, status: res.status, code: errCode2, error: errMsg2 }
      : { ok: false, status: res.status, error: errMsg2 };
    throw new CockpitWriteError(detail2);
  }
  return parsed as T;
}

// ---------------------------------------------------------------------------
// Confirm-nonce preview — POST /api/confirm/preview → { nonce, expiresAt }
// ---------------------------------------------------------------------------

export interface ConfirmNonce {
  nonce: string;
  expiresAt: string;
  action: string;
}

export async function previewNonce(action: string): Promise<ConfirmNonce> {
  return apiPost<ConfirmNonce>('/confirm/preview', { action });
}

// ---------------------------------------------------------------------------
// Typed API result shapes
// ---------------------------------------------------------------------------

export interface WorkflowSummary {
  workflow_id: string;
  phase?: string | null;
  root_goal?: string;
  selected_squads?: string[];
  has_pending_hitl?: boolean;
  updated_at?: string;
  budget?: {
    budget_usd?: number;
    spent_usd?: number;
    spent_tokens?: number;
    token_limit?: number;
  } | null;
}

export interface WorkflowDetail extends WorkflowSummary {
  pending_hitl?: HitlGate | null;
  tasks?: Array<{ owner_squad?: string | null; status?: string | null; description?: string }>;
  envelope_count?: number;
  verdict_count?: number;
}

export interface HitlGate {
  hitl_id?: string;
  id?: string;
  workflow_id?: string;
  reason?: string;
  summary?: string;
  options?: string[];
  default_option?: string | null;
  gate_node?: string;
  expires_at?: string | null;
}

export interface SquadPack {
  slug: string;
  name?: string;
  version?: string;
  description?: string;
  entrypoint?: string;
  industries?: string[];
  accepts?: string[];
  emits?: string[];
  best_of_n?: number;
  agents?: Array<{ slug: string; role?: string | null; authority?: string | null; model_tier?: string | null }>;
}

export interface EightsCell {
  cell: string;
  count?: number;
  records?: EightsCellRecord[];
}

export interface EightsCellRecord {
  id?: string | undefined;
  workflow_id?: string | undefined;
  key?: string | undefined;
  cell?: string | undefined;
  content?: unknown | undefined;
  created_at?: string | undefined;
  ts?: string | undefined;
}

export interface SearchResult {
  results?: Array<{ score?: number; content?: unknown; workflow_id?: string; cell?: string }>;
}

export interface LaunchResult {
  workflow_id: string;
  pid?: number;
  log?: string;
  audit?: string;
}

export interface ResumeResult {
  ok: boolean;
  workflow_id?: string;
  action?: string;
  launched?: boolean;
  pid?: number;
  log?: string;
  audit?: string;
}

export interface ReplayResult {
  workflow_id: string;
  pid?: number;
  log?: string;
  audit?: string;
}

// ---------------------------------------------------------------------------
// Sanctioned reads
// ---------------------------------------------------------------------------

export function fetchHealth(): Promise<unknown> {
  return apiFetch('/health');
}

export function fetchWorkflows(limit = 50): Promise<{ workflows?: WorkflowSummary[]; count?: number } | WorkflowSummary[]> {
  return apiFetch('/workflows', { params: { limit: String(limit) } });
}

export function fetchWorkflow(id: string): Promise<WorkflowDetail> {
  return apiFetch(`/workflows/${encodeURIComponent(id)}`);
}

export function fetchSquads(): Promise<{ squads?: SquadPack[]; count?: number } | SquadPack[]> {
  return apiFetch('/squads');
}

export function fetchHitl(): Promise<{ items?: HitlGate[]; count?: number } | HitlGate[]> {
  return apiFetch('/hitl');
}

export function fetchMemoryCells(opts?: { cell?: string; limit?: number; workflow_id?: string }): Promise<EightsCell[] | { cells?: EightsCell[] }> {
  const params: Record<string, string> = {};
  if (opts?.cell) params['cell'] = opts.cell;
  if (opts?.limit) params['limit'] = String(opts.limit);
  if (opts?.workflow_id) params['workflow_id'] = opts.workflow_id;
  return apiFetch('/memory/cells', { params });
}

export function fetchMemorySearch(q: string, k = 5, workflow_id?: string): Promise<SearchResult> {
  const params: Record<string, string> = { q, k: String(k) };
  if (workflow_id) params['workflow_id'] = workflow_id;
  return apiFetch('/memory/search', { params });
}

export function fetchMemoryWorkflow(id: string): Promise<{ records?: EightsCellRecord[] }> {
  return apiFetch(`/memory/workflow/${encodeURIComponent(id)}`);
}

// ---------------------------------------------------------------------------
// Sanctioned writes
// ---------------------------------------------------------------------------

export interface LaunchParams {
  goal: string;
  squads?: string[] | undefined;
  budgetUsd?: number | undefined;
  live?: boolean | undefined;
  confirmNonce?: string | undefined;
}

export async function launchWorkflow(params: LaunchParams): Promise<LaunchResult> {
  const body: Record<string, unknown> = { goal: params.goal };
  if (params.squads && params.squads.length > 0) body['squads'] = params.squads;
  if (params.budgetUsd !== undefined) body['budgetUsd'] = params.budgetUsd;
  body['live'] = params.live ?? false;
  if (params.confirmNonce) body['confirmNonce'] = params.confirmNonce;
  return apiPost<LaunchResult>('/launch', body);
}

export interface ResumeParams {
  workflow_id: string;
  action: string;
  option?: string | undefined;
  confirmNonce?: string | undefined;
  typedChallenge?: string | undefined;
}

export async function resumeGate(params: ResumeParams): Promise<ResumeResult> {
  const body: Record<string, unknown> = {
    workflow_id: params.workflow_id,
    action: params.action,
  };
  if (params.option) body['option'] = params.option;
  if (params.confirmNonce) body['confirmNonce'] = params.confirmNonce;
  if (params.typedChallenge) body['typedChallenge'] = params.typedChallenge;
  return apiPost<ResumeResult>('/resume', body);
}

export interface ReplayParams {
  workflow_id: string;
  fromPhase?: string | undefined;
  swapModel?: string | undefined;
  live?: boolean | undefined;
  confirmNonce: string;
  typedChallenge?: string | undefined;
}

export async function replayWorkflow(params: ReplayParams): Promise<ReplayResult> {
  const body: Record<string, unknown> = {
    workflow_id: params.workflow_id,
    confirmNonce: params.confirmNonce,
  };
  if (params.fromPhase) body['fromPhase'] = params.fromPhase;
  if (params.swapModel) body['swapModel'] = params.swapModel;
  body['live'] = params.live ?? false;
  if (params.typedChallenge) body['typedChallenge'] = params.typedChallenge;
  return apiPost<ReplayResult>('/replay', body);
}

export async function tagMemory(params: {
  key: string;
  cells: string[];
  replace?: boolean;
}): Promise<{ ok: boolean; key: string; cells: string[] }> {
  return apiPost('/tag_memory', {
    key: params.key,
    cells: params.cells,
    replace: params.replace ?? false,
  });
}

// ---------------------------------------------------------------------------
// SSE stream with EventSource + poll fallback
// ---------------------------------------------------------------------------

export type SseEventType = 'trace' | 'state' | 'gate' | 'ping' | 'done';

export interface SseTraceEvent {
  type: 'trace';
  data: Record<string, unknown>;
}
export interface SseStateEvent {
  type: 'state';
  data: { phase?: string; selected_squads?: string[]; budget?: { budget_usd?: number; spent_usd?: number }; workflow_id?: string };
}
export interface SseGateEvent {
  type: 'gate';
  data: HitlGate;
}
export interface SsePingEvent {
  type: 'ping';
  data: Record<string, never>;
}
export interface SseDoneEvent {
  type: 'done';
  data: { phase?: string; workflow_id?: string };
}

export type SseEvent = SseTraceEvent | SseStateEvent | SseGateEvent | SsePingEvent | SseDoneEvent;

export interface StreamHandle {
  stop: () => void;
}

type OnEvent = (event: SseEvent) => void;
type OnError = (err: string) => void;
type OnStateChange = (isSSE: boolean) => void;

export function openWorkflowStream(
  workflowId: string,
  onEvent: OnEvent,
  onError: OnError,
  onStateChange: OnStateChange,
  initialCursor = 0,
): StreamHandle {
  let stopped = false;
  let cursor = initialCursor;
  let pollTimer: ReturnType<typeof setTimeout> | undefined;
  let sse: EventSource | null = null;

  function startPoll(): void {
    if (stopped) return;
    onStateChange(false); // degraded — poll mode

    async function doPoll(): Promise<void> {
      if (stopped) return;
      try {
        const url = `${API_BASE}/workflows/${encodeURIComponent(workflowId)}/poll?cursor=${cursor}`;
        const res = await fetch(url);
        if (!res.ok) {
          onError(`poll error: ${res.status}`);
        } else {
          const data = (await res.json()) as {
            traceLines?: Record<string, unknown>[];
            state?: SseStateEvent['data'];
            nextCursor?: number;
          };
          if (data.state) onEvent({ type: 'state', data: data.state });
          for (const line of data.traceLines ?? []) {
            onEvent({ type: 'trace', data: line });
          }
          if (data.nextCursor !== undefined) cursor = data.nextCursor;
        }
      } catch (e) {
        onError(e instanceof Error ? e.message : String(e));
      }
      if (!stopped) {
        pollTimer = setTimeout(() => { void doPoll(); }, 3000);
      }
    }
    void doPoll();
  }

  function startSSE(): void {
    const url = `${API_BASE}/workflows/${encodeURIComponent(workflowId)}/stream?cursor=${cursor}`;
    sse = new EventSource(url);
    onStateChange(true);

    for (const evtType of ['trace', 'state', 'gate', 'ping', 'done'] as const) {
      sse.addEventListener(evtType, (e: Event) => {
        if (stopped) return;
        try {
          const msgEvent = e as MessageEvent<string>;
          const parsed = JSON.parse(msgEvent.data) as Record<string, unknown>;
          onEvent({ type: evtType, data: parsed } as SseEvent);
          // Update cursor from Last-Event-ID if present
          if (msgEvent.lastEventId) {
            const n = parseInt(msgEvent.lastEventId, 10);
            if (!isNaN(n)) cursor = n;
          }
        } catch { /* ignore parse errors */ }
      });
    }

    sse.onerror = () => {
      if (stopped) return;
      sse?.close();
      sse = null;
      // Fall back to polling on SSE error
      startPoll();
    };
  }

  // Try SSE first; if EventSource is not available (unlikely in browsers), fall back
  try {
    startSSE();
  } catch {
    startPoll();
  }

  return {
    stop(): void {
      stopped = true;
      sse?.close();
      if (pollTimer) clearTimeout(pollTimer);
    },
  };
}
