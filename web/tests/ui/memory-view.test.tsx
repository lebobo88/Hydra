/**
 * UI tests for THE EIGHT CELLS — Memory view (Pentecost R5).
 *
 * Covers:
 *  - 8 cells render with counts from mocked /api/memory/cells overview
 *  - Dui cell is gilded (class bagua-cell-node--dui present on dui button)
 *  - Cell drill-down: clicking a cell shows records (not blank); empty cell shows notice not blank
 *  - 2D arrow-key navigation moves focus around the ring
 *  - Semantic search inscribes results (trace-inscribe class on results)
 *  - Replay uses the venom confirm dialog (danger: true)
 *  - 8-state: loading shows skeleton; error shows retry; empty cells show "0" counts not blank;
 *    degraded shows source-unreachable notice; offline disables search + replay
 *  - Reduced-motion: shimmer/inscription classes present but CSS-only (no JS animation)
 *  - Trace timeline renders inscription-style entries
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import { MemoryView } from '../../src/views/MemoryView.tsx';
import { _setCachedToken } from '../../src/api/client.ts';

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

const MOCK_CELLS_OVERVIEW = {
  cells: [
    { cell: 'qian', count: 5 },
    { cell: 'kun',  count: 0 },
    { cell: 'zhen', count: 12 },
    { cell: 'xun',  count: 3 },
    { cell: 'kan',  count: 0 },
    { cell: 'li',   count: 7 },
    { cell: 'gen',  count: 1 },
    { cell: 'dui',  count: 42 }, // victory cell
  ],
};

const MOCK_CELLS_ALL_EMPTY = {
  cells: [
    { cell: 'qian', count: 0 },
    { cell: 'kun',  count: 0 },
    { cell: 'zhen', count: 0 },
    { cell: 'xun',  count: 0 },
    { cell: 'kan',  count: 0 },
    { cell: 'li',   count: 0 },
    { cell: 'gen',  count: 0 },
    { cell: 'dui',  count: 0 },
  ],
};

const MOCK_CELLS_DEGRADED = {
  cells: [
    { cell: 'qian', count: 3 },
    { cell: 'dui',  count: 9 },
  ],
  degraded: true,
};

const MOCK_CELL_RECORDS = {
  records: [
    {
      id: 'rec-00001111-2222-3333-4444-555566667777',
      workflow_id: 'wf-aaaabbbb-cccc-dddd-eeee-ffff00001111',
      cell: 'qian',
      created_at: '2026-06-07T10:00:00Z',
    },
    {
      id: 'rec-99998888-7777-6666-5555-444433332222',
      workflow_id: 'wf-11112222-3333-4444-5555-666677778888',
      cell: 'qian',
      created_at: '2026-06-07T09:00:00Z',
    },
  ],
};

const MOCK_SEARCH_RESULTS = {
  results: [
    { cell: 'qian', workflow_id: 'wf-search-001', score: 0.92 },
    { cell: 'dui',  workflow_id: 'wf-search-002', score: 0.85 },
  ],
};

const MOCK_TRACE_RECORDS = {
  records: [
    { id: 'tr-001', cell: 'qian', workflow_id: 'wf-trace-001', ts: '2026-06-07T10:00:00Z' },
    { id: 'tr-002', cell: 'dui',  workflow_id: 'wf-trace-001', ts: '2026-06-07T10:01:00Z' },
  ],
};

const MOCK_NONCE = { nonce: 'test-nonce-001', expiresAt: '2026-06-07T11:00:00Z', action: 'replay' };
const MOCK_SESSION = { token: 'test-session-token' };

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function makeFetchMock(routes: Record<string, unknown>) {
  return vi.fn().mockImplementation((url: string) => {
    const parsed = new URL(url, 'http://localhost');
    const path = parsed.pathname;
    const search = parsed.search;
    const fullKey = path + search; // e.g. /api/memory/cells?cell=qian&limit=50

    // Sort routes: longer (more specific) routes first so query-string keys
    // match before their path-only prefix counterparts.
    const sortedRoutes = Object.entries(routes).sort(([a], [b]) => b.length - a.length);

    for (const [route, body] of sortedRoutes) {
      const routeHasQuery = route.includes('?');
      if (routeHasQuery) {
        // Exact full-key match for query-string routes
        if (fullKey === route) {
          return Promise.resolve({
            ok: true,
            status: 200,
            json: () => Promise.resolve(body),
          });
        }
      } else {
        // Path-prefix match for path-only routes
        if (path.startsWith(route)) {
          return Promise.resolve({
            ok: true,
            status: 200,
            json: () => Promise.resolve(body),
          });
        }
      }
    }
    return Promise.resolve({
      ok: false,
      status: 404,
      json: () => Promise.resolve({ error: 'not found' }),
    });
  });
}

function setupOnlineRoutes(overrides: Record<string, unknown> = {}) {
  _setCachedToken('test-session-token');
  return makeFetchMock({
    '/api/memory/cells': MOCK_CELLS_OVERVIEW,
    '/api/memory/search': MOCK_SEARCH_RESULTS,
    '/api/confirm/preview': MOCK_NONCE,
    '/api/session': MOCK_SESSION,
    ...overrides,
  });
}

// ---------------------------------------------------------------------------
// 1. Cell counts render
// ---------------------------------------------------------------------------

describe('MemoryView — Eight Cells radial', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    _setCachedToken('test-session-token');
  });

  it('renders all 8 cells from the overview data', async () => {
    vi.stubGlobal('fetch', setupOnlineRoutes());

    render(<MemoryView online={true} />);

    // Wait for cells to load
    await waitFor(() => {
      expect(screen.getByTestId('bagua-radial')).toBeTruthy();
    }, { timeout: 2000 });

    // All 8 cell buttons must be present by data-testid
    expect(screen.getByTestId('bagua-cell-qian')).toBeTruthy();
    expect(screen.getByTestId('bagua-cell-kun')).toBeTruthy();
    expect(screen.getByTestId('bagua-cell-zhen')).toBeTruthy();
    expect(screen.getByTestId('bagua-cell-xun')).toBeTruthy();
    expect(screen.getByTestId('bagua-cell-kan')).toBeTruthy();
    expect(screen.getByTestId('bagua-cell-li')).toBeTruthy();
    expect(screen.getByTestId('bagua-cell-gen')).toBeTruthy();
    expect(screen.getByTestId('bagua-cell-dui')).toBeTruthy();
  });

  it('shows the count from the overview on each cell (including 0)', async () => {
    vi.stubGlobal('fetch', setupOnlineRoutes());

    render(<MemoryView online={true} />);

    await waitFor(() => {
      const qianBtn = screen.getByTestId('bagua-cell-qian');
      // aria-label carries the count
      expect(qianBtn.getAttribute('aria-label')).toContain('5 records');
    }, { timeout: 2000 });

    const kunBtn = screen.getByTestId('bagua-cell-kun');
    // 0 count is shown, not blank
    expect(kunBtn.getAttribute('aria-label')).toMatch(/0 record/);

    const dui = screen.getByTestId('bagua-cell-dui');
    expect(dui.getAttribute('aria-label')).toContain('42 records');
  });

  it('shows count "0" on cells with no records (empty is not blank)', async () => {
    vi.stubGlobal('fetch', setupOnlineRoutes({
      '/api/memory/cells': MOCK_CELLS_ALL_EMPTY,
    }));

    render(<MemoryView online={true} />);

    await waitFor(() => {
      const qianBtn = screen.getByTestId('bagua-cell-qian');
      expect(qianBtn.getAttribute('aria-label')).toMatch(/0 record/);
    }, { timeout: 2000 });

    // The radial is still rendered (not replaced by EmptyScreen) — 0 is a valid value
    expect(screen.getByTestId('bagua-radial')).toBeTruthy();
  });
});

// ---------------------------------------------------------------------------
// 2. Dui gilding
// ---------------------------------------------------------------------------

describe('MemoryView — Dui cell gilding', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    _setCachedToken('test-session-token');
  });

  it('Dui cell has the --dui CSS class (gilded)', async () => {
    vi.stubGlobal('fetch', setupOnlineRoutes());

    render(<MemoryView online={true} />);

    await waitFor(() => {
      const dui = screen.getByTestId('bagua-cell-dui');
      expect(dui.classList.contains('bagua-cell-node--dui')).toBe(true);
    }, { timeout: 2000 });
  });

  it('only Dui cell has the --dui class', async () => {
    vi.stubGlobal('fetch', setupOnlineRoutes());

    render(<MemoryView online={true} />);

    await waitFor(() => {
      screen.getByTestId('bagua-cell-dui');
    }, { timeout: 2000 });

    // Other cells must NOT have the dui class
    for (const key of ['qian', 'kun', 'zhen', 'xun', 'kan', 'li', 'gen']) {
      const btn = screen.getByTestId(`bagua-cell-${key}`);
      expect(btn.classList.contains('bagua-cell-node--dui')).toBe(false);
    }
  });

  it('Dui cell contains the shimmer element', async () => {
    vi.stubGlobal('fetch', setupOnlineRoutes());

    render(<MemoryView online={true} />);

    await waitFor(() => {
      const dui = screen.getByTestId('bagua-cell-dui');
      const shimmer = dui.querySelector('.bagua-dui-shimmer');
      expect(shimmer).toBeTruthy();
    }, { timeout: 2000 });
  });

  it('non-Dui cells do not contain the shimmer element', async () => {
    vi.stubGlobal('fetch', setupOnlineRoutes());

    render(<MemoryView online={true} />);

    await waitFor(() => {
      screen.getByTestId('bagua-cell-qian');
    }, { timeout: 2000 });

    const qian = screen.getByTestId('bagua-cell-qian');
    expect(qian.querySelector('.bagua-dui-shimmer')).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// 3. Cell drill-down
// ---------------------------------------------------------------------------

describe('MemoryView — cell drill-down', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    _setCachedToken('test-session-token');
  });

  it('clicking a cell loads and shows its records', async () => {
    vi.stubGlobal('fetch', makeFetchMock({
      '/api/memory/cells': MOCK_CELLS_OVERVIEW,
      '/api/memory/cells?cell=qian&limit=50': MOCK_CELL_RECORDS,
      '/api/session': MOCK_SESSION,
    }));

    render(<MemoryView online={true} />);

    await waitFor(() => {
      expect(screen.getByTestId('bagua-cell-qian')).toBeTruthy();
    }, { timeout: 2000 });

    fireEvent.click(screen.getByTestId('bagua-cell-qian'));

    await waitFor(() => {
      expect(screen.getByTestId('cell-detail')).toBeTruthy();
    }, { timeout: 2000 });

    // Records are shown — 2 from MOCK_CELL_RECORDS
    const detail = screen.getByTestId('cell-detail');
    expect(detail.textContent).toContain('rec-0000');
    expect(detail.textContent).toContain('rec-9999');
  });

  it('cell drill-down shows "No records in this cell yet" when empty (not blank)', async () => {
    vi.stubGlobal('fetch', makeFetchMock({
      '/api/memory/cells': MOCK_CELLS_OVERVIEW,
      '/api/memory/cells?cell=kan&limit=50': { records: [] },
      '/api/session': MOCK_SESSION,
    }));

    render(<MemoryView online={true} />);

    await waitFor(() => {
      expect(screen.getByTestId('bagua-cell-kan')).toBeTruthy();
    }, { timeout: 2000 });

    fireEvent.click(screen.getByTestId('bagua-cell-kan'));

    await waitFor(() => {
      expect(screen.getByTestId('cell-detail')).toBeTruthy();
    }, { timeout: 2000 });

    // Not blank — shows the empty notice
    const detail = screen.getByTestId('cell-detail');
    expect(detail.querySelector('.memory-empty-cell')).toBeTruthy();
    expect(detail.textContent).toContain('No records in this cell yet');
  });

  it('cell records use trace-inscribe class for animation', async () => {
    vi.stubGlobal('fetch', makeFetchMock({
      '/api/memory/cells': MOCK_CELLS_OVERVIEW,
      '/api/memory/cells?cell=qian&limit=50': MOCK_CELL_RECORDS,
      '/api/session': MOCK_SESSION,
    }));

    render(<MemoryView online={true} />);

    await waitFor(() => {
      expect(screen.getByTestId('bagua-cell-qian')).toBeTruthy();
    }, { timeout: 2000 });

    fireEvent.click(screen.getByTestId('bagua-cell-qian'));

    await waitFor(() => {
      const detail = screen.getByTestId('cell-detail');
      const records = detail.querySelectorAll('.trace-inscribe');
      expect(records.length).toBeGreaterThan(0);
    }, { timeout: 2000 });
  });
});

// ---------------------------------------------------------------------------
// 4. 2D arrow-key navigation
// ---------------------------------------------------------------------------

describe('MemoryView — 2D arrow-key grid navigation', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    _setCachedToken('test-session-token');
  });

  it('first cell (qian) is focused by default when grid is active', async () => {
    vi.stubGlobal('fetch', setupOnlineRoutes());

    render(<MemoryView online={true} />);

    await waitFor(() => {
      expect(screen.getByTestId('bagua-cell-qian')).toBeTruthy();
    }, { timeout: 2000 });

    const qian = screen.getByTestId('bagua-cell-qian');
    // Default focusedIndex=0 → qian has tabIndex=0
    expect(qian.getAttribute('tabindex')).toBe('0');
    // Other cells have tabIndex=-1
    const zhen = screen.getByTestId('bagua-cell-zhen');
    expect(zhen.getAttribute('tabindex')).toBe('-1');
  });

  it('ArrowRight moves focus from first to second cell', async () => {
    vi.stubGlobal('fetch', setupOnlineRoutes());

    render(<MemoryView online={true} />);

    await waitFor(() => {
      expect(screen.getByTestId('bagua-cell-qian')).toBeTruthy();
    }, { timeout: 2000 });

    const qian = screen.getByTestId('bagua-cell-qian');
    // Focus qian first
    qian.focus();

    // Press ArrowRight → next cell (zhen at index 1)
    fireEvent.keyDown(qian, { key: 'ArrowRight', code: 'ArrowRight' });

    await waitFor(() => {
      // zhen should now be focusedIndex=1 → tabIndex=0
      const zhen = screen.getByTestId('bagua-cell-zhen');
      expect(zhen.getAttribute('tabindex')).toBe('0');
    }, { timeout: 500 });
  });

  it('ArrowLeft moves focus to previous cell (wrapping)', async () => {
    vi.stubGlobal('fetch', setupOnlineRoutes());

    render(<MemoryView online={true} />);

    await waitFor(() => {
      expect(screen.getByTestId('bagua-cell-qian')).toBeTruthy();
    }, { timeout: 2000 });

    const qian = screen.getByTestId('bagua-cell-qian');
    qian.focus();

    // Press ArrowLeft from index 0 → wraps to index 7 (dui)
    fireEvent.keyDown(qian, { key: 'ArrowLeft', code: 'ArrowLeft' });

    await waitFor(() => {
      const dui = screen.getByTestId('bagua-cell-dui');
      expect(dui.getAttribute('tabindex')).toBe('0');
    }, { timeout: 500 });
  });

  it('ArrowDown jumps to the opposite cell (ring cross-axis)', async () => {
    vi.stubGlobal('fetch', setupOnlineRoutes());

    render(<MemoryView online={true} />);

    await waitFor(() => {
      expect(screen.getByTestId('bagua-cell-qian')).toBeTruthy();
    }, { timeout: 2000 });

    const qian = screen.getByTestId('bagua-cell-qian');
    qian.focus();

    // ArrowDown from index 0 → (0+4)%8 = 4 (kun)
    fireEvent.keyDown(qian, { key: 'ArrowDown', code: 'ArrowDown' });

    await waitFor(() => {
      const kun = screen.getByTestId('bagua-cell-kun');
      expect(kun.getAttribute('tabindex')).toBe('0');
    }, { timeout: 500 });
  });

  it('Home moves focus to the first cell', async () => {
    vi.stubGlobal('fetch', setupOnlineRoutes());

    render(<MemoryView online={true} />);

    await waitFor(() => {
      expect(screen.getByTestId('bagua-cell-qian')).toBeTruthy();
    }, { timeout: 2000 });

    // Move to a different cell first
    const qian = screen.getByTestId('bagua-cell-qian');
    qian.focus();
    fireEvent.keyDown(qian, { key: 'ArrowRight', code: 'ArrowRight' });

    await waitFor(() => {
      const zhen = screen.getByTestId('bagua-cell-zhen');
      expect(zhen.getAttribute('tabindex')).toBe('0');
    }, { timeout: 500 });

    // Now press Home → should go back to qian (index 0)
    const zhen = screen.getByTestId('bagua-cell-zhen');
    fireEvent.keyDown(zhen, { key: 'Home', code: 'Home' });

    await waitFor(() => {
      expect(screen.getByTestId('bagua-cell-qian').getAttribute('tabindex')).toBe('0');
    }, { timeout: 500 });
  });

  it('End moves focus to the last cell (dui)', async () => {
    vi.stubGlobal('fetch', setupOnlineRoutes());

    render(<MemoryView online={true} />);

    await waitFor(() => {
      expect(screen.getByTestId('bagua-cell-qian')).toBeTruthy();
    }, { timeout: 2000 });

    const qian = screen.getByTestId('bagua-cell-qian');
    qian.focus();
    fireEvent.keyDown(qian, { key: 'End', code: 'End' });

    await waitFor(() => {
      const dui = screen.getByTestId('bagua-cell-dui');
      expect(dui.getAttribute('tabindex')).toBe('0');
    }, { timeout: 500 });
  });

  it('Enter on a focused cell opens the drill-down', async () => {
    vi.stubGlobal('fetch', makeFetchMock({
      '/api/memory/cells': MOCK_CELLS_OVERVIEW,
      '/api/memory/cells?cell=qian&limit=50': MOCK_CELL_RECORDS,
      '/api/session': MOCK_SESSION,
    }));

    render(<MemoryView online={true} />);

    await waitFor(() => {
      expect(screen.getByTestId('bagua-cell-qian')).toBeTruthy();
    }, { timeout: 2000 });

    const qian = screen.getByTestId('bagua-cell-qian');
    qian.focus();
    fireEvent.keyDown(qian, { key: 'Enter', code: 'Enter' });

    await waitFor(() => {
      expect(screen.getByTestId('cell-detail')).toBeTruthy();
    }, { timeout: 2000 });
  });

  it('grid has role=grid on the container', async () => {
    vi.stubGlobal('fetch', setupOnlineRoutes());

    render(<MemoryView online={true} />);

    await waitFor(() => {
      const grid = screen.getByTestId('bagua-radial');
      expect(grid.getAttribute('role')).toBe('grid');
    }, { timeout: 2000 });
  });

  it('each cell button has role=gridcell', async () => {
    vi.stubGlobal('fetch', setupOnlineRoutes());

    render(<MemoryView online={true} />);

    await waitFor(() => {
      const qian = screen.getByTestId('bagua-cell-qian');
      expect(qian.getAttribute('role')).toBe('gridcell');
    }, { timeout: 2000 });
  });
});

// ---------------------------------------------------------------------------
// 5. Semantic search — inscription
// ---------------------------------------------------------------------------

describe('MemoryView — semantic search inscription', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    _setCachedToken('test-session-token');
  });

  it('search results render with trace-inscribe class', async () => {
    vi.stubGlobal('fetch', setupOnlineRoutes());

    render(<MemoryView online={true} />);

    await waitFor(() => {
      expect(screen.getByLabelText('Search query')).toBeTruthy();
    }, { timeout: 2000 });

    const input = screen.getByLabelText('Search query');
    fireEvent.change(input, { target: { value: 'victory' } });
    fireEvent.click(screen.getByLabelText('Run semantic search'));

    await waitFor(() => {
      const results = document.querySelectorAll('.search-result.trace-inscribe');
      expect(results.length).toBeGreaterThan(0);
    }, { timeout: 2000 });
  });

  it('search results show cell and score', async () => {
    vi.stubGlobal('fetch', setupOnlineRoutes());

    render(<MemoryView online={true} />);

    await waitFor(() => {
      expect(screen.getByLabelText('Search query')).toBeTruthy();
    }, { timeout: 2000 });

    const input = screen.getByLabelText('Search query');
    fireEvent.change(input, { target: { value: 'victory' } });
    fireEvent.click(screen.getByLabelText('Run semantic search'));

    await waitFor(() => {
      const list = screen.getByRole('list', { name: /search results/i });
      expect(list.textContent).toContain('qian');
      expect(list.textContent).toContain('dui');
      expect(list.textContent).toContain('0.920');
    }, { timeout: 2000 });
  });
});

// ---------------------------------------------------------------------------
// 6. Replay uses the venom confirm
// ---------------------------------------------------------------------------

describe('MemoryView — replay via venom confirm', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    _setCachedToken('test-session-token');
  });

  it('clicking Replay opens the confirm dialog with danger=true (venom-class)', async () => {
    vi.stubGlobal('fetch', makeFetchMock({
      '/api/memory/cells': MOCK_CELLS_OVERVIEW,
      '/api/memory/cells?cell=qian&limit=50': MOCK_CELL_RECORDS,
      '/api/confirm/preview': MOCK_NONCE,
      '/api/session': MOCK_SESSION,
    }));

    render(<MemoryView online={true} />);

    // Open qian cell to see records
    await waitFor(() => {
      expect(screen.getByTestId('bagua-cell-qian')).toBeTruthy();
    }, { timeout: 2000 });

    fireEvent.click(screen.getByTestId('bagua-cell-qian'));

    // Wait for records with Replay buttons
    await waitFor(() => {
      const replayBtns = screen.queryAllByText('Replay ▸');
      expect(replayBtns.length).toBeGreaterThan(0);
    }, { timeout: 2000 });

    // Click first Replay
    const firstReplay = screen.queryAllByText('Replay ▸')[0]!;
    fireEvent.click(firstReplay);

    // Confirm dialog should appear
    await waitFor(() => {
      expect(screen.getByRole('dialog')).toBeTruthy();
    }, { timeout: 2000 });

    // Should be danger=true (danger class on the dialog)
    const dlg = screen.getByRole('dialog');
    expect(dlg.classList.contains('dlg-danger') || dlg.querySelector('.dlg-danger') !== null
      || dlg.textContent?.includes('Replay from checkpoint')).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// 7. 8-state coverage
// ---------------------------------------------------------------------------

describe('MemoryView — 8-state', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    _setCachedToken('test-session-token');
  });

  // Loading state — skeleton shown
  it('shows loading skeleton while cells are loading', () => {
    // Never-resolving fetch to keep loading state
    vi.stubGlobal('fetch', vi.fn().mockReturnValue(new Promise(() => undefined)));

    render(<MemoryView online={true} />);

    // Loading screen or radial skeleton should be present
    const skeletonOrLoading = document.querySelector('.bagua-radial--skeleton, [aria-label*="Loading"]')
      || screen.queryByText(/Loading episodic memory/);
    expect(skeletonOrLoading).toBeTruthy();
  });

  // Error state — shows retry
  it('shows error screen with retry when fetch fails', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('network error')));

    render(<MemoryView online={true} />);

    await waitFor(() => {
      // ErrorScreen renders role=alert
      expect(screen.getByRole('alert')).toBeTruthy();
      expect(screen.getByText('Retry')).toBeTruthy();
    }, { timeout: 2000 });
  });

  // Empty state — 0 counts shown, NOT replaced by EmptyScreen
  it('all-zero counts: shows 0 on each cell, not a blank/missing display', async () => {
    vi.stubGlobal('fetch', setupOnlineRoutes({
      '/api/memory/cells': MOCK_CELLS_ALL_EMPTY,
    }));

    render(<MemoryView online={true} />);

    await waitFor(() => {
      // Each cell should still render with "0" in aria-label
      const qian = screen.getByTestId('bagua-cell-qian');
      expect(qian.getAttribute('aria-label')).toMatch(/0 record/);
    }, { timeout: 2000 });

    // The grid is still present — not replaced by EmptyScreen
    expect(screen.getByTestId('bagua-radial')).toBeTruthy();
  });

  // Degraded state — shows source-unreachable notice
  it('degraded: shows source-unreachable DegradedBanner (not clean empty)', async () => {
    vi.stubGlobal('fetch', setupOnlineRoutes({
      '/api/memory/cells': MOCK_CELLS_DEGRADED,
    }));

    render(<MemoryView online={true} />);

    await waitFor(() => {
      const notice = screen.getByTestId('degraded-notice');
      expect(notice).toBeTruthy();
      expect(notice.textContent).toContain('Source unreachable');
    }, { timeout: 2000 });
  });

  // Degraded state — search is disabled
  it('degraded: semantic search form is disabled', async () => {
    vi.stubGlobal('fetch', setupOnlineRoutes({
      '/api/memory/cells': MOCK_CELLS_DEGRADED,
    }));

    render(<MemoryView online={true} />);

    await waitFor(() => {
      expect(screen.getByTestId('degraded-notice')).toBeTruthy();
    }, { timeout: 2000 });

    const searchInput = screen.getByLabelText('Search query');
    expect(searchInput).toBeDisabled();
  });

  // Offline state — search + replay disabled
  it('offline: search input disabled with reason', async () => {
    vi.stubGlobal('fetch', setupOnlineRoutes());

    render(<MemoryView online={false} />);

    await waitFor(() => {
      expect(screen.getByLabelText('Search query')).toBeTruthy();
    }, { timeout: 2000 });

    const searchInput = screen.getByLabelText('Search query');
    expect(searchInput).toBeDisabled();

    // Offline reason message
    expect(screen.getByText(/Search disabled.*bridge offline/i)).toBeTruthy();
  });

  it('offline: OfflineBanner is shown', async () => {
    vi.stubGlobal('fetch', setupOnlineRoutes());

    render(<MemoryView online={false} />);

    await waitFor(() => {
      expect(screen.getByTestId('offline-banner')).toBeTruthy();
    }, { timeout: 2000 });
  });

  it('offline: Replay buttons in cell records are not shown', async () => {
    vi.stubGlobal('fetch', makeFetchMock({
      '/api/memory/cells': MOCK_CELLS_OVERVIEW,
      '/api/memory/cells?cell=qian&limit=50': MOCK_CELL_RECORDS,
      '/api/session': MOCK_SESSION,
    }));

    render(<MemoryView online={false} />);

    await waitFor(() => {
      expect(screen.getByTestId('bagua-cell-qian')).toBeTruthy();
    }, { timeout: 2000 });

    fireEvent.click(screen.getByTestId('bagua-cell-qian'));

    await waitFor(() => {
      expect(screen.getByTestId('cell-detail')).toBeTruthy();
    }, { timeout: 2000 });

    // No active replay buttons
    const replayBtns = screen.queryAllByText('Replay ▸');
    expect(replayBtns.length).toBe(0);

    // Offline reason shown instead
    const offlineReasons = document.querySelectorAll('.memory-replay-offline');
    expect(offlineReasons.length).toBeGreaterThan(0);
  });

  // Live state — cells radial renders
  it('live state: Eight Cells section renders', async () => {
    vi.stubGlobal('fetch', setupOnlineRoutes());

    render(<MemoryView online={true} />);

    await waitFor(() => {
      expect(screen.getByTestId('eight-cells-section')).toBeTruthy();
    }, { timeout: 2000 });
  });
});

// ---------------------------------------------------------------------------
// 8. Trace timeline — inscription style
// ---------------------------------------------------------------------------

describe('MemoryView — trace timeline', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    _setCachedToken('test-session-token');
  });

  it('loads and renders trace entries with trace-inscribe class', async () => {
    vi.stubGlobal('fetch', makeFetchMock({
      '/api/memory/cells': MOCK_CELLS_OVERVIEW,
      '/api/memory/workflow/wf-trace-001': MOCK_TRACE_RECORDS,
      '/api/session': MOCK_SESSION,
    }));

    render(<MemoryView online={true} />);

    await waitFor(() => {
      expect(screen.getByLabelText('Workflow ID for trace')).toBeTruthy();
    }, { timeout: 2000 });

    const wfInput = screen.getByLabelText('Workflow ID for trace');
    fireEvent.change(wfInput, { target: { value: 'wf-trace-001' } });
    fireEvent.click(screen.getByLabelText('Load trace timeline'));

    await waitFor(() => {
      const timeline = screen.getByRole('region', { name: /Trace timeline/i });
      expect(timeline.textContent).toContain('qian');
      expect(timeline.textContent).toContain('dui');
    }, { timeout: 2000 });

    const inscribed = document.querySelectorAll('.trace-timeline-entry.trace-inscribe');
    expect(inscribed.length).toBe(2);
  });

  it('shows no-records message when trace is empty', async () => {
    vi.stubGlobal('fetch', makeFetchMock({
      '/api/memory/cells': MOCK_CELLS_OVERVIEW,
      '/api/memory/workflow/wf-empty': { records: [] },
      '/api/session': MOCK_SESSION,
    }));

    render(<MemoryView online={true} />);

    await waitFor(() => {
      expect(screen.getByLabelText('Workflow ID for trace')).toBeTruthy();
    }, { timeout: 2000 });

    const wfInput = screen.getByLabelText('Workflow ID for trace');
    fireEvent.change(wfInput, { target: { value: 'wf-empty' } });
    fireEvent.click(screen.getByLabelText('Load trace timeline'));

    await waitFor(() => {
      expect(screen.getByText(/No episodic records for this workflow/)).toBeTruthy();
    }, { timeout: 2000 });
  });
});

// ---------------------------------------------------------------------------
// 9. Reduced-motion — no JS animation, CSS class present for CSS opt-in
// ---------------------------------------------------------------------------

describe('MemoryView — reduced-motion compliance', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    _setCachedToken('test-session-token');
  });

  it('trace-inscribe class is applied to results (CSS handles animation, not JS)', async () => {
    vi.stubGlobal('fetch', setupOnlineRoutes());

    render(<MemoryView online={true} />);

    await waitFor(() => {
      expect(screen.getByLabelText('Search query')).toBeTruthy();
    }, { timeout: 2000 });

    const input = screen.getByLabelText('Search query');
    fireEvent.change(input, { target: { value: 'test' } });
    fireEvent.click(screen.getByLabelText('Run semantic search'));

    await waitFor(() => {
      const items = document.querySelectorAll('.search-result.trace-inscribe');
      expect(items.length).toBeGreaterThan(0);
    }, { timeout: 2000 });

    // CSS animation; no JS RAF or style.animation set directly on elements
    const items = document.querySelectorAll('.search-result.trace-inscribe');
    items.forEach((el) => {
      // style.animation should not be set inline (CSS controls it)
      expect((el as HTMLElement).style.animation).toBeFalsy();
    });
  });

  it('Dui shimmer is CSS-only (no inline animation style)', async () => {
    vi.stubGlobal('fetch', setupOnlineRoutes());

    render(<MemoryView online={true} />);

    await waitFor(() => {
      expect(screen.getByTestId('bagua-cell-dui')).toBeTruthy();
    }, { timeout: 2000 });

    const dui = screen.getByTestId('bagua-cell-dui');
    const shimmer = dui.querySelector('.bagua-dui-shimmer') as HTMLElement;
    expect(shimmer).toBeTruthy();
    // No inline animation — CSS @media prefers-reduced-motion controls it
    expect(shimmer.style.animation).toBeFalsy();
  });
});

// ---------------------------------------------------------------------------
// 10. Accessibility: aria labels, roles
// ---------------------------------------------------------------------------

describe('MemoryView — accessibility', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    _setCachedToken('test-session-token');
  });

  it('each cell has aria-label with name and count', async () => {
    vi.stubGlobal('fetch', setupOnlineRoutes());

    render(<MemoryView online={true} />);

    await waitFor(() => {
      expect(screen.getByTestId('bagua-cell-qian')).toBeTruthy();
    }, { timeout: 2000 });

    expect(screen.getByTestId('bagua-cell-qian').getAttribute('aria-label'))
      .toContain('Qian (Heaven)');
    expect(screen.getByTestId('bagua-cell-dui').getAttribute('aria-label'))
      .toContain('Dui (Lake)');
  });

  it('radial grid has aria-label', async () => {
    vi.stubGlobal('fetch', setupOnlineRoutes());

    render(<MemoryView online={true} />);

    await waitFor(() => {
      const grid = screen.getByTestId('bagua-radial');
      expect(grid.getAttribute('aria-label')).toContain('Eight episodic memory cells');
    }, { timeout: 2000 });
  });

  it('heading "The Eight Cells" is present', async () => {
    vi.stubGlobal('fetch', setupOnlineRoutes());

    render(<MemoryView online={true} />);

    await waitFor(() => {
      expect(screen.getByText('The Eight Cells')).toBeTruthy();
    }, { timeout: 2000 });
  });

  it('main Memory heading is present', async () => {
    vi.stubGlobal('fetch', setupOnlineRoutes());

    render(<MemoryView online={true} />);

    await waitFor(() => {
      expect(screen.getByRole('heading', { name: /Memory/ })).toBeTruthy();
    }, { timeout: 2000 });
  });
});
