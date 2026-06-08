/**
 * Hydra Cockpit — shared view header (back-link + title).
 * Consolidates the `<header class="view-header">` + `← back` + `<h1>` block
 * that was copy-pasted across Squads / Campaigns / Launch / Memory / Gate.
 */

import type { ReactNode } from 'react';

interface ViewHeaderProps {
  title: string;
  /** Where the back-link points. Defaults to the Launchpad. */
  backHref?: string;
  /** Back-link text (the "← " arrow is added automatically). */
  backLabel?: string;
  /** Optional trailing content rendered after the title (badges, actions). */
  children?: ReactNode;
}

export function ViewHeader({
  title,
  backHref = '#/',
  backLabel = 'Launchpad',
  children,
}: ViewHeaderProps): JSX.Element {
  return (
    <header className="view-header">
      <a href={backHref} className="back-link">← {backLabel}</a>
      <h1 className="view-title">{title}</h1>
      {children}
    </header>
  );
}
