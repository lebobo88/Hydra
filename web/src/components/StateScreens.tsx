/**
 * Hydra Cockpit — per-state screen wrappers.
 * Renders loading, empty, error, degraded, offline placeholders.
 * "empty is not evidence of none" — degraded states never show a clean empty.
 */

import type { ReactNode } from 'react';
import { useEffect, useState } from 'react';

interface LoadingProps { label?: string }
export function LoadingScreen({ label = 'Loading…' }: LoadingProps): JSX.Element {
  return (
    <div className="state-screen state-loading" role="status" aria-label={label} aria-live="polite">
      <span className="spinner" aria-hidden="true" />
      <span>{label}</span>
    </div>
  );
}

interface EmptyProps { message?: string; action?: ReactNode }
export function EmptyScreen({ message = 'Nothing here yet.', action }: EmptyProps): JSX.Element {
  return (
    <div className="state-screen state-empty" role="status">
      <span aria-hidden="true" className="state-icon">◎</span>
      <p>{message}</p>
      {action}
    </div>
  );
}

interface ErrorProps { message: string; onRetry?: () => void }
export function ErrorScreen({ message, onRetry }: ErrorProps): JSX.Element {
  return (
    <div className="state-screen state-error" role="alert" aria-label="Error">
      <span aria-hidden="true" className="state-icon error-icon">▲</span>
      <p>{message}</p>
      {onRetry ? (
        <button className="btn btn-sm" type="button" onClick={onRetry}>
          Retry
        </button>
      ) : null}
    </div>
  );
}

interface DegradedProps {
  sources: string[];
  children?: ReactNode;
  message?: string;
}
export function DegradedBanner({ sources, children, message }: DegradedProps): JSX.Element {
  return (
    <div className="degraded-notice" role="alert" data-testid="degraded-notice">
      <div className="degraded-banner">
        <span className="banner-icon" aria-hidden="true">▲</span>
        <div>
          <strong>Source unreachable</strong>
          {sources.length > 0 ? <> — {sources.join(', ')}</> : null}
          {message ? <> — {message}</> : null}
          . Data shown may be stale; an empty list is not evidence of none.
        </div>
      </div>
      {children}
    </div>
  );
}

interface OfflineProps { since?: number | undefined; children?: ReactNode | undefined }
export function OfflineBanner({ since, children }: OfflineProps): JSX.Element {
  // Tick once a second so the elapsed counter advances live instead of being
  // frozen at whatever the last parent re-render happened to capture.
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!since) return undefined;
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, [since]);

  const elapsed = since ? Math.max(0, Math.round((now - since) / 1000)) : 0;
  return (
    <div className="offline-banner" role="alert" aria-label="Bridge offline" data-testid="offline-banner">
      <span className="banner-icon" aria-hidden="true">⊗</span>
      <span>
        Bridge unreachable{elapsed > 0 ? ` (${elapsed}s ago)` : ''}. Showing last known data.
        Write actions are disabled.
      </span>
      {children}
    </div>
  );
}
