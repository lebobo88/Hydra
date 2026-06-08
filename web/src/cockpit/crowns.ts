/**
 * Crown registry — the single source of truth mapping squad slugs to their
 * Crown family. Previously duplicated in App.tsx and LaunchpadView.tsx (the
 * latter via brittle `slug.includes()` checks); consolidated here so the shell
 * rail, the constellation, and the neck-flow colouring all agree.
 *
 * Extend CROWN_MAP as squads grow. Unknown slugs fall back to 'forge'.
 */

export type CrownFamily = 'exec' | 'forge' | 'garland';

export const CROWN_MAP: Record<string, CrownFamily> = {
  // Executive
  executive: 'exec',
  legal: 'exec',
  finance: 'exec',
  compliance: 'exec',
  'legal-compliance': 'exec',
  // Forge
  engineering: 'forge',
  forge: 'forge',
  platform: 'forge',
  infra: 'forge',
  devops: 'forge',
  security: 'forge',
  // Garland (creative / marketing / research / go-to-market)
  garland: 'garland',
  marketing: 'garland',
  'marketing-strategy': 'garland',
  'marketing-creative': 'garland',
  'marketing-ops': 'garland',
  'marketing-production': 'garland',
  'marketing-research': 'garland',
  creative: 'garland',
  design: 'garland',
  product: 'garland',
  research: 'garland',
  'research-ds': 'garland',
  'sales-gtm': 'garland',
};

export function crownOf(slug: string): CrownFamily {
  return CROWN_MAP[slug.toLowerCase()] ?? 'forge';
}

/** CSS custom-property reference for a crown family's colour token. */
export function crownColorVar(family: CrownFamily): string {
  return `var(--crown-${family})`;
}
