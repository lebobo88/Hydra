/**
 * web/server/hydra-mem-client.ts
 *
 * Stdio MCP child client for the hydra_memory MCP server.
 *
 * Forks the AgentMesh mesh-client.ts idioms verbatim in spirit:
 *   - Single-flight mutex: only one tool call in flight at a time.
 *   - Line-framed JSON-RPC over stdout/stdin.
 *   - MCP initialize handshake on connect.
 *   - call() with timeout + one busy-retry.
 *   - Graceful child shutdown.
 *   - Child-died surfaces as a clean degraded error; never hangs.
 *
 * Launch resolution order (HYDRA_ROOT env var → repo-relative fallback → absolute):
 *   1. HYDRA_ROOT env var (operator override)
 *   2. ../.. relative to web/ directory (C:\AiAppDeployments\Hydra)
 *   3. Absolute fallback: C:/AiAppDeployments/Hydra
 *
 * The server is launched as:
 *   python -m mcp_servers.hydra_memory
 * with:
 *   cwd = <hydra_root>           (so `mcp_servers` package resolves)
 *   PYTHONPATH = <hydra_root>    (belt-and-braces; server.py also does sys.path.insert)
 *
 * Fixed envelope injected on every call (INVARIANT #1 — server-side only):
 *   actor='hydra-cockpit', project='Hydra'
 * Browser input NEVER touches envelope fields.
 */

import { spawn, type ChildProcess } from 'node:child_process';
import type { Writable, Readable } from 'node:stream';
import { existsSync } from 'node:fs';
import { homedir } from 'node:os';
import { join, resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { cockpitEnvelope } from './operator.js';

// ---------------------------------------------------------------------------
// Hydra root discovery
// ---------------------------------------------------------------------------

function findHydraRoot(): string {
  // 1. Explicit env override
  const envRoot = process.env['HYDRA_ROOT'];
  if (envRoot && existsSync(envRoot)) return envRoot;

  // 2. Repo-relative: web/server/hydra-mem-client.ts → ../../ = Hydra root
  try {
    const __filename = fileURLToPath(import.meta.url);
    const __dirname = dirname(__filename);
    // Running from dist-server/: two levels up is web/, three is Hydra root
    // Running from server/ (tsx): two levels up is Hydra root
    const candidates = [
      resolve(__dirname, '..', '..'),         // server/ → web/ → Hydra/
      resolve(__dirname, '..', '..', '..'),   // dist-server/ → web/ → Hydra/
    ];
    for (const c of candidates) {
      if (existsSync(join(c, 'mcp_servers', 'hydra_memory', 'server.py'))) {
        return c;
      }
    }
  } catch {
    // import.meta.url unavailable — fall through
  }

  // 3. Absolute fallback
  const abs = join(homedir(), 'AiAppDeployments', 'Hydra');
  if (existsSync(abs)) return abs;

  return 'C:/AiAppDeployments/Hydra';
}

// ---------------------------------------------------------------------------
// Fixed cockpit envelope (injected on every call — browser cannot override)
// ---------------------------------------------------------------------------

function hydraEnvelope(): Record<string, unknown> {
  const env = cockpitEnvelope();
  return { actor: env.actor, project: env.project, traceId: env.traceId };
}

// ---------------------------------------------------------------------------
// HydraMemClient — stdio MCP child client
// ---------------------------------------------------------------------------

const CALL_TIMEOUT_MS = 15_000;
const BUSY_RETRY_DELAY_MS = 150;

export class HydraMemClient {
  private proc: (ChildProcess & { stdin: Writable; stdout: Readable }) | null = null;
  private nextId = 1;
  private buffer = '';
  private pending = new Map<
    number,
    { resolve: (v: unknown) => void; reject: (e: Error) => void; timer: ReturnType<typeof setTimeout> }
  >();

  // Single-flight serialization mutex (matches better-sqlite3 constraint on python side)
  private callChain: Promise<unknown> = Promise.resolve();

  // Handshake gate (Fix: first-call connect race)
  // -----------------------------------------------------------------------
  // `handshakeComplete` is only set to true AFTER notifications/initialized
  // is sent — the final step of the MCP initialize exchange. No tools/call
  // message is sent before this flag is true.
  //
  // `connectPromise` serialises concurrent first calls: the second caller
  // awaits the same in-flight handshake rather than spawning a second child.
  // It is cleared on failure so a subsequent call can attempt a fresh connect.
  private handshakeComplete = false;
  private connectPromise: Promise<void> | null = null;

  /** True iff the child is spawned AND the MCP handshake is complete. */
  get connected(): boolean {
    return this.proc !== null && this.handshakeComplete;
  }

  private hydraRoot(): string {
    return findHydraRoot();
  }

  async connect(): Promise<void> {
    const root = this.hydraRoot();
    const pythonPath = root;

    // Use 'python' on Windows (python3 may not exist); fall back to python3
    const pythonExe = process.platform === 'win32' ? 'python' : 'python3';

    const p = spawn(pythonExe, ['-m', 'mcp_servers.hydra_memory'], {
      cwd: root,
      stdio: ['pipe', 'pipe', 'inherit'],
      env: {
        ...process.env,
        PYTHONPATH: pythonPath,
        HYDRA_ROOT: root,
      },
    });

    if (!p.stdin || !p.stdout) {
      throw new Error('failed to attach stdio to hydra_memory child process');
    }

    // Assign proc early so send() can write to stdin, but do NOT set
    // handshakeComplete yet — ensureConnected() blocks until that flag is set.
    const proc = p as ChildProcess & { stdin: Writable; stdout: Readable };
    this.proc = proc;
    this.handshakeComplete = false;

    this.proc.stdout.setEncoding('utf8');
    this.proc.stdout.on('data', (chunk: string) => this.onChunk(chunk));
    this.proc.on('exit', (code) => {
      this.proc = null;
      this.handshakeComplete = false;
      this.connectPromise = null;
      const exitErr = new Error(
        `hydra_memory child exited${code != null ? ` with code ${code}` : ''}`,
      );
      for (const { reject, timer } of this.pending.values()) {
        clearTimeout(timer);
        reject(exitErr);
      }
      this.pending.clear();
    });

    // MCP initialize handshake — must complete before any tools/call is sent
    await this.send('initialize', {
      protocolVersion: '2024-11-05',
      capabilities: {},
      clientInfo: { name: 'hydra-cockpit', version: '0.1.0' },
    });
    this.notify('notifications/initialized', {});

    // Only after handshake + notifications/initialized: mark ready
    this.handshakeComplete = true;
  }

  /**
   * Call a hydra-mem.* tool.
   * Args are merged with the fixed cockpit envelope — browser cannot override envelope fields.
   */
  async call<T = unknown>(tool: string, args: Record<string, unknown> = {}): Promise<T> {
    await this.ensureConnected();
    return this.serialize<T>(async () => {
      try {
        return await this._callRaw<T>(tool, args);
      } catch (e) {
        if (isBusy(e)) {
          await sleep(BUSY_RETRY_DELAY_MS);
          return this._callRaw<T>(tool, args);
        }
        throw e;
      }
    });
  }

  private async _callRaw<T>(tool: string, args: Record<string, unknown>): Promise<T> {
    const result = (await this.sendWithTimeout('tools/call', {
      name: tool,
      arguments: { ...args, envelope: hydraEnvelope() },
    })) as { content?: Array<{ text?: string }>; isError?: boolean };

    const text = result.content?.[0]?.text ?? '{}';
    const parsed = JSON.parse(text) as unknown;
    if (result.isError) {
      throw new Error(
        typeof (parsed as { error?: string }).error === 'string'
          ? (parsed as { error: string }).error
          : JSON.stringify(parsed),
      );
    }
    return parsed as T;
  }

  async close(): Promise<void> {
    if (!this.proc) return;
    try {
      this.proc.stdin.end();
      this.proc.kill();
    } catch {
      // ignore errors during shutdown
    }
    this.proc = null;
    this.handshakeComplete = false;
    this.connectPromise = null;
  }

  // --------------------------------------------------------------------------
  // Private helpers
  // --------------------------------------------------------------------------

  /**
   * ensureConnected — serialises concurrent first calls behind a single
   * in-flight connect promise so no two callers race the handshake.
   *
   * Invariant: when this returns, `this.connected` is true (proc assigned
   * AND handshakeComplete). If connect() throws, connectPromise is cleared so
   * the next call can attempt a fresh connect rather than inheriting the
   * failed promise.
   */
  private async ensureConnected(): Promise<void> {
    if (this.connected) return;
    if (this.connectPromise === null) {
      this.connectPromise = this.connect().catch((err) => {
        // Clear on failure so a subsequent call can retry
        this.connectPromise = null;
        throw err;
      });
    }
    await this.connectPromise;
  }

  private serialize<T>(fn: () => Promise<T>): Promise<T> {
    const run = this.callChain.then(fn, fn) as Promise<T>;
    this.callChain = run.then(
      () => undefined,
      () => undefined,
    );
    return run;
  }

  private send(method: string, params: Record<string, unknown>): Promise<unknown> {
    if (!this.proc) return Promise.reject(new Error('hydra-mem client not connected'));
    const id = this.nextId++;
    const payload = { jsonrpc: '2.0', id, method, params };
    return new Promise((resolve, reject) => {
      this.pending.set(id, {
        resolve,
        reject,
        timer: setTimeout(() => {
          this.pending.delete(id);
          reject(new Error(`hydra-mem: timeout waiting for response to ${method} (id=${id})`));
        }, CALL_TIMEOUT_MS),
      });
      this.proc!.stdin.write(JSON.stringify(payload) + '\n');
    });
  }

  private sendWithTimeout(method: string, params: Record<string, unknown>): Promise<unknown> {
    // send() already installs a timeout; this is an alias for clarity
    return this.send(method, params);
  }

  private notify(method: string, params: Record<string, unknown>): void {
    if (!this.proc) return;
    const payload = { jsonrpc: '2.0', method, params };
    this.proc.stdin.write(JSON.stringify(payload) + '\n');
  }

  private onChunk(chunk: string): void {
    this.buffer += chunk;
    let idx: number;
    while ((idx = this.buffer.indexOf('\n')) !== -1) {
      const line = this.buffer.slice(0, idx);
      this.buffer = this.buffer.slice(idx + 1);
      if (!line.trim()) continue;
      try {
        const msg = JSON.parse(line) as {
          id?: number;
          result?: unknown;
          error?: { message: string };
        };
        if (typeof msg.id === 'number' && this.pending.has(msg.id)) {
          const handler = this.pending.get(msg.id)!;
          clearTimeout(handler.timer);
          this.pending.delete(msg.id);
          if (msg.error) handler.reject(new Error(msg.error.message));
          else handler.resolve(msg.result);
        }
      } catch {
        /* non-JSON line (e.g. python logging) — skip */
      }
    }
  }
}

const isBusy = (e: unknown): boolean =>
  e instanceof Error && /busy executing a query/i.test(e.message);

const sleep = (ms: number): Promise<void> => new Promise((r) => setTimeout(r, ms));
