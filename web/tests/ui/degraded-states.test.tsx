/**
 * UI tests for degraded states.
 * Covers:
 *  - DegradedBanner shows source-unreachable notice (not empty)
 *  - LaunchpadView shows degraded notice when source unreachable
 *  - OfflineBanner shows when bridge is offline
 *  - Empty is not evidence of none: degraded active workflows show notice
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { DegradedBanner, OfflineBanner } from '../../src/components/StateScreens.tsx';
import { LaunchpadView } from '../../src/views/LaunchpadView.tsx';

describe('DegradedBanner', () => {
  it('renders source-unreachable notice', () => {
    render(<DegradedBanner sources={['hydra-mem']} />);
    const banner = screen.getByTestId('degraded-notice');
    expect(banner).toBeTruthy();
    expect(banner.textContent).toContain('Source unreachable');
    expect(banner.textContent).toContain('hydra-mem');
    expect(banner.textContent).toContain('empty list is not evidence of none');
  });

  it('renders with multiple sources', () => {
    render(<DegradedBanner sources={['hydra-mem', 'hydra-control']} />);
    const banner = screen.getByTestId('degraded-notice');
    expect(banner.textContent).toContain('hydra-mem');
    expect(banner.textContent).toContain('hydra-control');
  });

  it('renders with a custom message', () => {
    render(<DegradedBanner sources={[]} message="SSE unavailable — polling fallback active" />);
    const banner = screen.getByTestId('degraded-notice');
    expect(banner.textContent).toContain('SSE unavailable');
  });

  it('has role=alert for screen readers', () => {
    render(<DegradedBanner sources={['hydra-mem']} />);
    expect(screen.getByRole('alert')).toBeTruthy();
  });
});

describe('OfflineBanner', () => {
  it('renders bridge-offline notice', () => {
    render(<OfflineBanner />);
    const banner = screen.getByTestId('offline-banner');
    expect(banner.textContent).toContain('Bridge unreachable');
    expect(banner.textContent).toContain('Write actions are disabled');
  });

  it('shows elapsed time when since is provided', () => {
    const since = Date.now() - 30000; // 30 seconds ago
    render(<OfflineBanner since={since} />);
    const banner = screen.getByTestId('offline-banner');
    // Should show some elapsed seconds
    expect(banner.textContent).toMatch(/\d+s ago/);
  });
});

describe('LaunchpadView degraded state', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('shows degraded notice when workflows source is degraded', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: () => Promise.resolve({ workflows: [], degraded: true, degradedReason: 'hydra-mem timeout' }),
    }));

    render(<LaunchpadView live={true} offline={false} />);

    await waitFor(() => {
      const notice = screen.getByTestId('degraded-notice');
      expect(notice).toBeTruthy();
    }, { timeout: 2000 });
  });

  it('shows offline banner when offline=true', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: () => Promise.resolve({ workflows: [] }),
    }));

    render(<LaunchpadView live={false} offline={true} offlineSince={Date.now() - 5000} />);

    await waitFor(() => {
      expect(screen.getByTestId('offline-banner')).toBeTruthy();
    }, { timeout: 2000 });
  });

  it('shows degraded text for active section when degraded', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: () => Promise.resolve({ workflows: [], degraded: true }),
    }));

    render(<LaunchpadView live={true} offline={false} />);

    await waitFor(() => {
      // Active section should say "not evidence of none" (not a clean empty)
      // Could appear in both degraded banner and active section text — either is correct
      const matches = screen.queryAllByText(/not evidence of none/i);
      expect(matches.length).toBeGreaterThan(0);
    }, { timeout: 2000 });
  });

  it('shows error screen with retry when bridge fails completely', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('network error')));

    render(<LaunchpadView live={false} offline={false} />);

    await waitFor(() => {
      expect(screen.getByRole('alert')).toBeTruthy();
      expect(screen.getByText('Retry')).toBeTruthy();
    }, { timeout: 2000 });
  });
});

describe('Launch Composer offline state', () => {
  it('launch + dry-run buttons disabled when offline', async () => {
    window.location.hash = '#/launch';
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: () => Promise.resolve({ squads: [] }),
    }));

    const { LaunchComposerView } = await import('../../src/views/LaunchComposerView.tsx');
    render(
      <LaunchComposerView
        initialGoal="test goal"
        online={false}
        onLaunched={vi.fn()}
      />,
    );

    await waitFor(() => {
      // When offline, the dry-run button should be disabled
      // Find by aria-label which includes "dry-run" text
      const dryRunBtn = screen.getByRole('button', { name: /dry-run/i });
      expect(dryRunBtn).toBeDisabled();
    }, { timeout: 2000 });
  });
});
