/**
 * UI tests for the launch composer nonce flow.
 * Covers:
 *  - Live launch requires a server-issued nonce path (opens ConfirmDialog)
 *  - Dry-run does NOT open ConfirmDialog (no nonce needed)
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { LaunchComposerView } from '../../src/views/LaunchComposerView.tsx';
import { _setCachedToken } from '../../src/api/client.ts';

const MOCK_SQUADS = { squads: [
  { slug: 'engineering', name: 'Engineering Pack', description: 'Software engineering tasks' },
], count: 1 };

const MOCK_NONCE = { nonce: 'nonce-xyz-123', expiresAt: '2026-06-07T19:00:00Z', action: 'launch' };

function makeSuccessResponse(body: unknown) {
  return { ok: true, status: 200, json: () => Promise.resolve(body) };
}

describe('LaunchComposerView nonce flow', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    _setCachedToken('test-csrf-token');
    window.location.hash = '#/launch';
  });

  it('dry-run launch does NOT open ConfirmDialog', async () => {
    const user = userEvent.setup();
    const onLaunched = vi.fn();

    const fetchMock = vi.fn().mockImplementation((url: string) => {
      if (url.endsWith('/squads')) return Promise.resolve(makeSuccessResponse(MOCK_SQUADS));
      if (url.endsWith('/launch')) return Promise.resolve(makeSuccessResponse({ workflow_id: 'wf-dry-1', pid: 123 }));
      return Promise.resolve(makeSuccessResponse({}));
    });
    vi.stubGlobal('fetch', fetchMock);

    render(<LaunchComposerView online={true} onLaunched={onLaunched} />);

    // Type a goal
    const goalInput = await waitFor(() => screen.getByRole('textbox', { name: /Goal/ }));
    await user.type(goalInput, 'Add idempotency key support');

    // Dry-run button — find by its full accessible label
    const dryRunBtn = screen.getByRole('button', { name: /validate routing/i });
    fireEvent.click(dryRunBtn);

    // Should NOT show ConfirmDialog
    await waitFor(() => {
      expect(screen.queryByTestId('confirm-dialog')).toBeNull();
    });

    // onLaunched should be called with the workflow_id
    await waitFor(() => {
      expect(onLaunched).toHaveBeenCalledWith('wf-dry-1');
    }, { timeout: 2000 });
  });

  it('live launch opens ConfirmDialog after fetching nonce', async () => {
    const user = userEvent.setup();
    const onLaunched = vi.fn();

    const fetchMock = vi.fn().mockImplementation((url: string) => {
      if (url.endsWith('/squads')) return Promise.resolve(makeSuccessResponse(MOCK_SQUADS));
      if (url.endsWith('/confirm/preview')) return Promise.resolve(makeSuccessResponse(MOCK_NONCE));
      if (url.endsWith('/launch')) return Promise.resolve(makeSuccessResponse({ workflow_id: 'wf-live-1', pid: 456 }));
      return Promise.resolve(makeSuccessResponse({}));
    });
    vi.stubGlobal('fetch', fetchMock);

    render(<LaunchComposerView online={true} onLaunched={onLaunched} />);

    // Type a goal
    const goalInput = await waitFor(() => screen.getByRole('textbox', { name: /Goal/ }));
    await user.type(goalInput, 'Launch live campaign');

    // Switch to live mode
    const liveRadio = screen.getByRole('radio', { name: /Live/ });
    await user.click(liveRadio);

    // Click Launch (live) — find by accessible name
    const launchBtn = screen.getByRole('button', { name: /launch live workflow/i });
    fireEvent.click(launchBtn);

    // ConfirmDialog should appear
    await waitFor(() => {
      expect(screen.getByTestId('confirm-dialog')).toBeTruthy();
    }, { timeout: 2000 });

    // Confirm button should be enabled (no typed challenge on launch dialog)
    const confirmBtn = screen.getByTestId('confirm-btn');
    expect(confirmBtn).not.toBeDisabled();
  });

  it('nonce fetch failure shows inline error, not ConfirmDialog', async () => {
    const user = userEvent.setup();

    const fetchMock = vi.fn().mockImplementation((url: string) => {
      if (url.endsWith('/squads')) return Promise.resolve(makeSuccessResponse(MOCK_SQUADS));
      if (url.endsWith('/confirm/preview')) return Promise.resolve({
        ok: false, status: 403,
        json: () => Promise.resolve({ error: 'CSRF invalid', code: 'CSRF' }),
      });
      return Promise.resolve(makeSuccessResponse({}));
    });
    vi.stubGlobal('fetch', fetchMock);

    render(<LaunchComposerView online={true} onLaunched={vi.fn()} />);

    const goalInput = await waitFor(() => screen.getByRole('textbox', { name: /Goal/ }));
    await user.type(goalInput, 'Failing launch');

    const liveRadio = screen.getByRole('radio', { name: /real dispatch/i });
    await user.click(liveRadio);

    const launchBtn = screen.getByRole('button', { name: /launch live workflow/i });
    fireEvent.click(launchBtn);

    // Should NOT show dialog; should show error
    await waitFor(() => {
      expect(screen.queryByTestId('confirm-dialog')).toBeNull();
      // Should have an error message visible
      const alerts = screen.queryAllByRole('alert');
      expect(alerts.length).toBeGreaterThan(0);
    }, { timeout: 2000 });
  });
});
