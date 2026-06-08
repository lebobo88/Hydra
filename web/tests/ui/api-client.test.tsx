/**
 * UI tests for the API client.
 * Covers:
 *  - X-Hydra-Token attached on writes, omitted on reads
 *  - CSRF 403 triggers re-bootstrap and retry
 *  - Confirm-nonce preview flow
 *  - CockpitWriteError thrown on non-ok responses
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import {
  getSessionToken,
  apiPost,
  apiFetch,
  previewNonce,
  _setCachedToken,
  CockpitWriteError,
} from '../../src/api/client.ts';

function makeResponse(status: number, body: unknown, ok = status >= 200 && status < 300) {
  return {
    ok,
    status,
    json: () => Promise.resolve(body),
    headers: new Headers(),
  };
}

describe('API client', () => {
  beforeEach(() => {
    _setCachedToken(null);
    vi.clearAllMocks();
  });

  describe('getSessionToken', () => {
    it('fetches token from /api/session on first call', async () => {
      const fetchMock = vi.fn().mockResolvedValue(
        makeResponse(200, { token: 'tok-abc', actor: 'hydra-cockpit' }),
      );
      vi.stubGlobal('fetch', fetchMock);

      const token = await getSessionToken();
      expect(token).toBe('tok-abc');
      expect(fetchMock).toHaveBeenCalledWith('/api/session', { method: 'GET' });
    });

    it('caches token after first fetch', async () => {
      const fetchMock = vi.fn().mockResolvedValue(
        makeResponse(200, { token: 'tok-xyz', actor: 'hydra-cockpit' }),
      );
      vi.stubGlobal('fetch', fetchMock);

      await getSessionToken();
      await getSessionToken();
      expect(fetchMock).toHaveBeenCalledOnce();
    });

    it('force=true re-fetches', async () => {
      const fetchMock = vi.fn().mockResolvedValue(
        makeResponse(200, { token: 'tok-new' }),
      );
      vi.stubGlobal('fetch', fetchMock);
      _setCachedToken('old-token');

      const token = await getSessionToken(true);
      expect(token).toBe('tok-new');
      expect(fetchMock).toHaveBeenCalledOnce();
    });
  });

  describe('reads (apiFetch)', () => {
    it('does NOT attach X-Hydra-Token on GET reads', async () => {
      const fetchMock = vi.fn().mockResolvedValue(makeResponse(200, { ok: true }));
      vi.stubGlobal('fetch', fetchMock);

      await apiFetch('/health');

      const [, opts] = fetchMock.mock.calls[0] as [string, RequestInit];
      const headers = (opts?.headers ?? {}) as Record<string, string>;
      expect(headers['x-hydra-token']).toBeUndefined();
    });

    it('throws CockpitWriteError on non-ok response', async () => {
      vi.stubGlobal('fetch', vi.fn().mockResolvedValue(
        makeResponse(502, { error: 'upstream error', code: 'UPSTREAM' }, false),
      ));

      await expect(apiFetch('/health')).rejects.toThrow(CockpitWriteError);
    });
  });

  describe('writes (apiPost)', () => {
    it('attaches X-Hydra-Token on POST', async () => {
      _setCachedToken('csrf-token-123');
      const fetchMock = vi.fn().mockResolvedValue(makeResponse(200, { ok: true }));
      vi.stubGlobal('fetch', fetchMock);

      await apiPost('/launch', { goal: 'test', live: false });

      const [, opts] = fetchMock.mock.calls[0] as [string, RequestInit];
      const headers = (opts?.headers ?? {}) as Record<string, string>;
      expect(headers['x-hydra-token']).toBe('csrf-token-123');
    });

    it('does NOT attach X-Mesh-Token (wrong header name)', async () => {
      _setCachedToken('csrf-token-123');
      const fetchMock = vi.fn().mockResolvedValue(makeResponse(200, { ok: true }));
      vi.stubGlobal('fetch', fetchMock);

      await apiPost('/launch', { goal: 'test', live: false });

      const [, opts] = fetchMock.mock.calls[0] as [string, RequestInit];
      const headers = (opts?.headers ?? {}) as Record<string, string>;
      expect(headers['x-mesh-token']).toBeUndefined();
    });

    it('re-bootstraps and retries on 403 CSRF', async () => {
      let callCount = 0;
      const fetchMock = vi.fn().mockImplementation((url: string) => {
        if (url.endsWith('/session')) {
          return Promise.resolve(makeResponse(200, { token: 'new-tok' }));
        }
        callCount++;
        if (callCount === 1) {
          return Promise.resolve(makeResponse(403, { error: 'CSRF', code: 'CSRF' }, false));
        }
        return Promise.resolve(makeResponse(200, { ok: true }));
      });
      vi.stubGlobal('fetch', fetchMock);
      _setCachedToken('stale-token');

      const result = await apiPost('/resume', { action: 'approve' });
      expect(result).toEqual({ ok: true });
      // Should have fetched session once + called /resume twice
      expect(callCount).toBe(2);
    });

    it('throws CockpitWriteError on non-CSRF non-ok', async () => {
      _setCachedToken('tok');
      vi.stubGlobal('fetch', vi.fn().mockResolvedValue(
        makeResponse(400, { error: 'bad input', code: 'INVALID_GOAL' }, false),
      ));

      await expect(apiPost('/launch', {})).rejects.toThrow(CockpitWriteError);
    });
  });

  describe('previewNonce', () => {
    it('POSTs to /api/confirm/preview and returns nonce', async () => {
      _setCachedToken('tok');
      const noncePayload = { nonce: 'nonce-abc', expiresAt: '2026-06-07T19:00:00Z', action: 'launch' };
      const fetchMock = vi.fn().mockResolvedValue(makeResponse(200, noncePayload));
      vi.stubGlobal('fetch', fetchMock);

      const result = await previewNonce('launch');
      expect(result.nonce).toBe('nonce-abc');
      expect(result.action).toBe('launch');

      const [url] = fetchMock.mock.calls[0] as [string];
      expect(url).toBe('/api/confirm/preview');
    });
  });
});
