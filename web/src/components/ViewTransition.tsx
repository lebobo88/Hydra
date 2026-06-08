/**
 * ViewTransition — route change choreography wrapper.
 *
 * Uses the View Transitions API where supported, with a CSS class-based
 * fallback (fade + subtle settle) for browsers without it.
 *
 * When `viewKey` changes:
 *   - Supported: startViewTransition wraps the React state update
 *   - Fallback: adds .view-transition-enter class for 280ms CSS animation
 *
 * Reduced-motion: instant display, no transition (opacity only, 80ms).
 * aria-hidden: the wrapper is transparent; the child content carries aria.
 */

import { useEffect, useRef, useState } from 'react';

interface ViewTransitionProps {
  /** Changing this triggers the transition animation */
  viewKey: string;
  children: React.ReactNode;
}

export function ViewTransition({ viewKey, children }: ViewTransitionProps): JSX.Element {
  const prevKeyRef = useRef<string>(viewKey);
  const [animClass, setAnimClass] = useState<string>('');
  const containerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (viewKey === prevKeyRef.current) return;
    prevKeyRef.current = viewKey;

    // Fallback CSS animation (non-VTA browsers)
    if (!document.startViewTransition) {
      setAnimClass('view-transition-enter');
      const t = setTimeout(() => setAnimClass(''), 320);
      return () => clearTimeout(t);
    }

    // View Transitions API path — wrap the class update in the transition
    // The actual DOM change (React re-render with new children) happens in
    // a subsequent render, which VTA captures as the new state.
    // We trigger a mild animation class to coordinate with VTA timing.
    document.startViewTransition(() => {
      setAnimClass('view-transition-enter');
    });
    const t = setTimeout(() => setAnimClass(''), 400);
    return () => clearTimeout(t);
  }, [viewKey]);

  return (
    <div
      ref={containerRef}
      className={`view-transition-host${animClass ? ` ${animClass}` : ''}`}
      data-testid="view-transition-host"
      style={{ viewTransitionName: 'main-view' } as React.CSSProperties}
    >
      {children}
    </div>
  );
}
