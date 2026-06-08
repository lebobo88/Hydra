/**
 * EmbeddedPeer — surface a sibling loopback UI (TheEights Atlas, the Hydra
 * Cockpit, …) inside this console via an iframe, with a graceful fallback when
 * the peer's dev server isn't running.
 *
 * Liveness: a cross-origin `fetch` to the peer would be CORS-blocked, so we
 * probe with `{mode:'no-cors'}` — an opaque response means "reachable" (render
 * the frame); a thrown network error means "down" (render the fallback card).
 * Both states always offer an "open in new tab" affordance. Display-only: the
 * frame is cross-origin, so there is no DOM reach-in and no new write authority.
 */

import { useCallback, useEffect, useRef, useState } from 'react';

interface EmbeddedPeerProps {
  /** Origin/URL to embed (the peer's Vite dev server, e.g. http://127.0.0.1:5174). */
  src: string;
  /** Accessible iframe title. */
  title: string;
  /** Reachability probe URL; defaults to `src`. */
  probeUrl?: string;
  /** Fallback card copy when the peer is unreachable. */
  fallbackTitle: string;
  fallbackHint: string;
  /** Label for the always-present open-in-new-tab link. */
  openLabel?: string;
  /** testid for the wrapper. */
  testId?: string;
}

type PeerState = 'checking' | 'ok' | 'down';

export function EmbeddedPeer({
  src, title, probeUrl, fallbackTitle, fallbackHint,
  openLabel = 'Open in new tab ↗', testId = 'embedded-peer',
}: EmbeddedPeerProps): JSX.Element {
  const [state, setState] = useState<PeerState>('checking');
  const [attempt, setAttempt] = useState(0);
  const mounted = useRef(true);

  const probe = useCallback(async () => {
    setState('checking');
    try {
      // no-cors: we can't read the body, but resolution proves the dev server
      // is accepting connections; a refused connection rejects.
      await fetch(probeUrl ?? src, { mode: 'no-cors', cache: 'no-store' });
      if (mounted.current) setState('ok');
    } catch {
      if (mounted.current) setState('down');
    }
  }, [src, probeUrl]);

  useEffect(() => {
    mounted.current = true;
    void probe();
    return () => { mounted.current = false; };
  }, [probe, attempt]);

  return (
    <div className="embedded-peer" data-testid={testId}>
      <div className="embedded-peer-bar">
        <span className="embedded-peer-src mono" aria-hidden="true">{src}</span>
        <a
          className="embedded-peer-open"
          href={src}
          target="_blank"
          rel="noopener noreferrer"
        >
          {openLabel}
        </a>
      </div>

      {state === 'ok' ? (
        <iframe
          className="embedded-peer-frame"
          src={src}
          title={title}
          data-testid={`${testId}-frame`}
          referrerPolicy="no-referrer"
        />
      ) : state === 'checking' ? (
        <div className="embedded-peer-fallback" role="status" aria-live="polite">
          <span className="spinner" aria-hidden="true" />
          <span>Connecting to {title}…</span>
        </div>
      ) : (
        <div className="embedded-peer-fallback" role="status" data-testid={`${testId}-fallback`}>
          <span className="state-icon" aria-hidden="true">◌</span>
          <p className="embedded-peer-fallback-title">{fallbackTitle}</p>
          <p className="embedded-peer-fallback-hint">{fallbackHint}</p>
          <div className="embedded-peer-fallback-actions">
            <button type="button" className="btn btn-sm" onClick={() => setAttempt((a) => a + 1)}>
              Retry
            </button>
            <a className="btn btn-sm btn-ghost" href={src} target="_blank" rel="noopener noreferrer">
              {openLabel}
            </a>
          </div>
        </div>
      )}
    </div>
  );
}
