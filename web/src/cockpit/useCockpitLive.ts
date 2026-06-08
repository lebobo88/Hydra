/**
 * Hydra Cockpit — polling hook.
 * Adaptive cadence: 2–3s for the active viewed workflow, 8s elsewhere.
 * Pauses when tab is hidden (visibilitychange). Stops when workflow is done/surfaced.
 * Keeps last known snapshot when offline.
 */

import { useCallback, useEffect, useRef, useState } from 'react';

const POLL_MS_ACTIVE = 3000;
const POLL_MS_IDLE = 8000;

export interface UseCockpitLive<T> {
  data: T | null;
  loading: boolean;
  error: string | null;
  live: boolean;
  refetch: () => void;
}

type Fetcher<T> = (signal: AbortSignal) => Promise<T>;

/** Whether the phase indicates the workflow is terminal (stop polling). */
function isTerminal(phase: unknown): boolean {
  return phase === 'done' || phase === 'surfaced';
}

export function useCockpitLive<T>(
  fetcher: Fetcher<T> | null,
  opts?: {
    active?: boolean;      // true = faster 3s cadence (viewed workflow)
    stopWhen?: (data: T) => boolean;
  },
): UseCockpitLive<T> {
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [live, setLive] = useState(true);
  const [nonce, setNonce] = useState(0);
  const dataRef = useRef<T | null>(null);
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;

  const refetch = useCallback(() => setNonce((n) => n + 1), []);

  const pollMs = opts?.active ? POLL_MS_ACTIVE : POLL_MS_IDLE;
  const pollMsRef = useRef(pollMs);
  pollMsRef.current = pollMs;

  const stopWhenRef = useRef(opts?.stopWhen);
  stopWhenRef.current = opts?.stopWhen;

  useEffect(() => {
    if (!fetcher) {
      setLoading(false);
      return;
    }

    let stopped = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let ctrl = new AbortController();

    function scheduleNext(): void {
      if (!stopped) {
        timer = setTimeout(() => { void poll(); }, pollMsRef.current);
      }
    }

    async function poll(): Promise<void> {
      // Pause when tab is hidden
      if (document.visibilityState === 'hidden') {
        scheduleNext();
        return;
      }

      try {
        const result = await fetcherRef.current!(ctrl.signal);
        if (stopped) return;
        dataRef.current = result;
        setData(result);
        setLive(true);
        setError(null);
        setLoading(false);

        // Stop if terminal
        if (stopWhenRef.current?.(result)) {
          stopped = true;
          return;
        }
      } catch (e) {
        if (stopped) return;
        const msg = e instanceof Error ? e.message : String(e);
        if (msg.includes('abort') || msg.includes('AbortError')) return;

        setLive(false);
        setError(msg);
        setLoading(false);
        // Keep last data (offline mode)
      }
      scheduleNext();
    }

    // Handle tab visibility changes
    function onVisibility(): void {
      if (document.visibilityState === 'visible') {
        ctrl.abort();
        ctrl = new AbortController();
        void poll();
      }
    }
    document.addEventListener('visibilitychange', onVisibility);

    void poll();

    return () => {
      stopped = true;
      ctrl.abort();
      if (timer) clearTimeout(timer);
      document.removeEventListener('visibilitychange', onVisibility);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [nonce, fetcher !== null]);

  return { data, loading, error, live, refetch };
}

/** Specialized hook for the workflows list (Launchpad/Campaigns). */
export function useWorkflowsLive() {
  return useCockpitLive<unknown>(
    (signal) => fetch('/api/workflows?limit=50', { signal }).then((r) => {
      if (!r.ok) throw new Error(`workflows fetch failed: ${r.status}`);
      return r.json() as Promise<unknown>;
    }),
    { active: false },
  );
}

/** Specialized hook for a single workflow (live view). */
export function useWorkflowDetail(id: string | undefined) {
  const fetcherMemo = useCallback(
    (signal: AbortSignal) => {
      if (!id) return Promise.reject(new Error('no workflow id'));
      return fetch(`/api/workflows/${encodeURIComponent(id)}`, { signal }).then((r) => {
        if (!r.ok) throw new Error(`workflow fetch failed: ${r.status}`);
        return r.json() as Promise<Record<string, unknown>>;
      });
    },
    [id],
  );

  return useCockpitLive<Record<string, unknown>>(
    id ? fetcherMemo : null,
    {
      active: true,
      stopWhen: (d) => isTerminal(d['phase']),
    },
  );
}
