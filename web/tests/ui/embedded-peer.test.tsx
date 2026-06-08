/**
 * UI tests for EmbeddedPeer — the sibling-UI iframe surface with a graceful
 * fallback when the peer's dev server is unreachable.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import { EmbeddedPeer } from '../../src/components/EmbeddedPeer.tsx';

const SRC = 'http://127.0.0.1:5174';
const COMMON = {
  src: SRC,
  title: 'Peer UI',
  fallbackTitle: 'Peer isn’t reachable',
  fallbackHint: 'Start it then Retry.',
  testId: 'peer',
};

describe('EmbeddedPeer', () => {
  beforeEach(() => vi.clearAllMocks());
  afterEach(() => vi.unstubAllGlobals());

  it('renders the iframe when the peer is reachable (no-cors probe resolves)', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ type: 'opaque' }));
    render(<EmbeddedPeer {...COMMON} />);
    await waitFor(() => {
      const frame = screen.getByTestId('peer-frame') as HTMLIFrameElement;
      expect(frame.tagName).toBe('IFRAME');
      expect(frame.getAttribute('src')).toBe(SRC);
      expect(frame.getAttribute('title')).toContain('Peer UI');
    }, { timeout: 2000 });
  });

  it('shows the fallback card when the probe rejects (peer down)', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('refused')));
    render(<EmbeddedPeer {...COMMON} />);
    await waitFor(() => {
      expect(screen.getByTestId('peer-fallback')).toBeTruthy();
    }, { timeout: 2000 });
    expect(screen.getByText('Peer isn’t reachable')).toBeTruthy();
    expect(screen.queryByTestId('peer-frame')).toBeNull();
    // Open-in-new-tab affordance is always present.
    const links = screen.getAllByText('Open in new tab ↗');
    expect(links.length).toBeGreaterThan(0);
  });

  it('retries the probe when Retry is clicked', async () => {
    const fetchMock = vi.fn()
      .mockRejectedValueOnce(new Error('refused'))
      .mockResolvedValueOnce({ type: 'opaque' });
    vi.stubGlobal('fetch', fetchMock);
    render(<EmbeddedPeer {...COMMON} />);

    await waitFor(() => expect(screen.getByTestId('peer-fallback')).toBeTruthy(), { timeout: 2000 });
    fireEvent.click(screen.getByText('Retry'));
    await waitFor(() => expect(screen.getByTestId('peer-frame')).toBeTruthy(), { timeout: 2000 });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });
});
