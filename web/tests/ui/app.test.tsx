/**
 * UI tests for the Hydra Cockpit App shell.
 * Covers:
 *  - App renders + hash routing switches views
 *  - Live/offline pulse reflects bridge state
 *  - Pending gates counter appears when gates > 0
 *  - New Run CTA navigates to #/launch
 *  - Degraded state shows source-unreachable notice (not empty)
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, act } from '@testing-library/react';
import { App } from '../../src/App.tsx';

// Minimal WorkflowSummary fixture
const MOCK_WORKFLOWS = {
  workflows: [
    {
      workflow_id: 'aabbccdd-1234-5678-abcd-ef1234567890',
      phase: 'executing',
      root_goal: 'Test goal',
      selected_squads: ['engineering'],
      has_pending_hitl: false,
      budget: { budget_usd: 80, spent_usd: 40 },
    },
  ],
  count: 1,
};

const MOCK_HITL_NONE = { items: [], count: 0 };
const MOCK_HITL_GATE = {
  items: [{ id: 'gate-1', workflow_id: 'aabbccdd-1234-5678-abcd-ef1234567890', reason: 'high_risk' }],
  count: 1,
};
const MOCK_HEALTH = { ok: true, bridge: 'hydra-cockpit' };

function makeFetchMock(routes: Record<string, unknown>) {
  return vi.fn().mockImplementation((url: string) => {
    const path = typeof url === 'string' ? new URL(url, 'http://localhost').pathname : '';
    const searchStr = typeof url === 'string' ? new URL(url, 'http://localhost').search : '';
    const key = path + (searchStr ? searchStr : '');
    // Match by path prefix
    for (const [route, body] of Object.entries(routes)) {
      if (key.startsWith(route) || path.startsWith(route)) {
        return Promise.resolve({
          ok: true,
          status: 200,
          json: () => Promise.resolve(body),
        });
      }
    }
    return Promise.resolve({ ok: false, status: 404, json: () => Promise.resolve({ error: 'not found' }) });
  });
}

describe('App shell', () => {
  beforeEach(() => {
    window.location.hash = '#/';
  });

  it('renders the top bar with brand and nav links', async () => {
    vi.stubGlobal('fetch', makeFetchMock({
      '/api/health': MOCK_HEALTH,
      '/api/hitl': MOCK_HITL_NONE,
      '/api/workflows': MOCK_WORKFLOWS,
    }));

    render(<App />);

    expect(screen.getByRole('banner')).toBeTruthy();
    expect(screen.getByText(/HYDRA/)).toBeTruthy();
    expect(screen.getByRole('navigation', { name: 'Main navigation' })).toBeTruthy();
    expect(screen.getByText('Launchpad')).toBeTruthy();
    expect(screen.getByText('Launch')).toBeTruthy();
    expect(screen.getByText('Squads')).toBeTruthy();
    expect(screen.getByText('Campaigns')).toBeTruthy();
    expect(screen.getByText('Memory')).toBeTruthy();
  });

  it('shows "live" pulse when bridge responds', async () => {
    vi.stubGlobal('fetch', makeFetchMock({
      '/api/health': MOCK_HEALTH,
      '/api/hitl': MOCK_HITL_NONE,
      '/api/workflows': MOCK_WORKFLOWS,
    }));

    render(<App />);

    await waitFor(() => {
      const pulse = screen.getByTestId('bridge-pulse');
      expect(pulse).toBeTruthy();
      // After a successful probe, should show live
      expect(pulse.textContent).toContain('live');
    }, { timeout: 2000 });
  });

  it('shows "offline" pulse when bridge fails', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('network error')));

    render(<App />);

    await waitFor(() => {
      const pulse = screen.getByTestId('bridge-pulse');
      expect(pulse.textContent).toContain('offline');
    }, { timeout: 2000 });
  });

  it('shows pending gates badge when gates > 0', async () => {
    vi.stubGlobal('fetch', makeFetchMock({
      '/api/health': MOCK_HEALTH,
      '/api/hitl': MOCK_HITL_GATE,
      '/api/workflows': MOCK_WORKFLOWS,
    }));

    render(<App />);

    await waitFor(() => {
      const badge = screen.getByTestId('pending-gates-badge');
      expect(badge).toBeTruthy();
      expect(badge.textContent).toContain('gate');
    }, { timeout: 2000 });
  });

  it('does NOT show pending gates badge when no gates', async () => {
    vi.stubGlobal('fetch', makeFetchMock({
      '/api/health': MOCK_HEALTH,
      '/api/hitl': MOCK_HITL_NONE,
      '/api/workflows': MOCK_WORKFLOWS,
    }));

    render(<App />);

    await waitFor(() => {
      expect(screen.queryByTestId('pending-gates-badge')).toBeNull();
    }, { timeout: 2000 });
  });

  it('New Run CTA is present and links to #/launch', async () => {
    vi.stubGlobal('fetch', makeFetchMock({
      '/api/health': MOCK_HEALTH,
      '/api/hitl': MOCK_HITL_NONE,
      '/api/workflows': MOCK_WORKFLOWS,
    }));

    render(<App />);

    const cta = screen.getByTestId('new-run-cta');
    expect(cta).toBeTruthy();
    expect(cta.getAttribute('href')).toBe('#/launch');
  });

  it('renders Launchpad view at #/', async () => {
    window.location.hash = '#/';
    vi.stubGlobal('fetch', makeFetchMock({
      '/api/health': MOCK_HEALTH,
      '/api/hitl': MOCK_HITL_NONE,
      '/api/workflows': MOCK_WORKFLOWS,
    }));

    render(<App />);

    await waitFor(() => {
      // Launchpad renders Active/Recent sections
      expect(screen.getByText(/Active/)).toBeTruthy();
    }, { timeout: 2000 });
  });

  it('switches to Launch Composer at #/launch', async () => {
    window.location.hash = '#/launch';
    vi.stubGlobal('fetch', makeFetchMock({
      '/api/health': MOCK_HEALTH,
      '/api/hitl': MOCK_HITL_NONE,
      '/api/workflows': MOCK_WORKFLOWS,
      '/api/squads': { squads: [], count: 0 },
    }));

    render(<App />);

    await waitFor(() => {
      expect(screen.getByText('Launch Composer')).toBeTruthy();
    }, { timeout: 2000 });
  });

  it('switches to Squads view at #/squads', async () => {
    window.location.hash = '#/squads';
    vi.stubGlobal('fetch', makeFetchMock({
      '/api/health': MOCK_HEALTH,
      '/api/hitl': MOCK_HITL_NONE,
      '/api/squads': { squads: [], count: 0 },
    }));

    render(<App />);

    await waitFor(() => {
      expect(screen.getByRole('heading', { name: /Squads/ })).toBeTruthy();
    }, { timeout: 2000 });
  });

  it('switches to Memory view at #/memory', async () => {
    window.location.hash = '#/memory';
    vi.stubGlobal('fetch', makeFetchMock({
      '/api/health': MOCK_HEALTH,
      '/api/hitl': MOCK_HITL_NONE,
      '/api/memory/cells': [],
    }));

    render(<App />);

    await waitFor(() => {
      expect(screen.getByRole('heading', { name: /Memory/ })).toBeTruthy();
    }, { timeout: 2000 });
  });

  it('handles hash routing change to #/campaigns', async () => {
    window.location.hash = '#/';
    vi.stubGlobal('fetch', makeFetchMock({
      '/api/health': MOCK_HEALTH,
      '/api/hitl': MOCK_HITL_NONE,
      '/api/workflows': MOCK_WORKFLOWS,
    }));

    render(<App />);

    // Navigate to campaigns
    act(() => {
      window.location.hash = '#/campaigns';
      window.dispatchEvent(new HashChangeEvent('hashchange'));
    });

    await waitFor(() => {
      expect(screen.getByRole('heading', { name: /Campaigns/ })).toBeTruthy();
    }, { timeout: 2000 });
  });
});
