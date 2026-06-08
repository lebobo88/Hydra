/**
 * Hydra Cockpit type definitions.
 * Adapts the AgentMesh 8-state union for the cockpit's write-first surface.
 */

import type { WorkflowSummary, WorkflowDetail, HitlGate, SquadPack } from '../api/client.ts';

// Re-export for convenience
export type { WorkflowSummary, WorkflowDetail, HitlGate, SquadPack };

// ---------------------------------------------------------------------------
// THE 8-STATE MATRIX (cockpit variant)
// ---------------------------------------------------------------------------

export type CockpitState =
  | { kind: 'loading' }
  | { kind: 'empty' }
  | { kind: 'error'; message: string; retryAt?: number }
  | { kind: 'degraded'; data: unknown; degradedSources: string[] }
  | { kind: 'offline'; lastData: unknown; since: number }
  | { kind: 'partial'; data: unknown; fallbackFields: string[] }
  | { kind: 'live'; data: unknown }
  | { kind: 'confirm'; data: unknown; dialog: CockpitDialogState };

// ---------------------------------------------------------------------------
// Confirm dialog state for cockpit writes
// ---------------------------------------------------------------------------

export type DialogKind =
  | 'launch-live'
  | 'gate-resume'
  | 'modify-budget'
  | 'force-dispatch'
  | 'change-squads'
  | 'abort'
  | 'replay';

export interface CockpitDialogState {
  kind: DialogKind;
  title: string;
  verb: string;
  lines: string[];
  /** If set, operator must type this string exactly before confirm is enabled */
  typedChallenge?: string | undefined;
  typedLabel?: string | undefined;
  /** Options rendered as radio buttons (gate resume) */
  options?: string[] | undefined;
  defaultOption?: string | null | undefined;
  /** True = red danger styling */
  danger?: boolean | undefined;
  /** Whether a resolution note is required */
  withNote?: boolean | undefined;
  /** For gate resume: the 5 resume actions */
  action?: string | undefined;
  /** Payload forwarded to the write call */
  payload: Record<string, unknown>;
}

// ---------------------------------------------------------------------------
// Hash routing — 7 views
// ---------------------------------------------------------------------------

export type ViewId =
  | 'launchpad'
  | 'launch'
  | 'workflow'
  | 'gate'
  | 'squads'
  | 'campaigns'
  | 'memory'
  | 'governance'
  | 'reliquary'
  | 'prototypes';

export interface ParsedView {
  view: ViewId;
  /** workflow id or hitl_id, if present in the hash */
  id?: string;
  /** query params from hash (e.g. ?goal=...) */
  params: URLSearchParams;
}

export function parseView(hash: string): ParsedView {
  // Strip leading #
  const clean = hash.replace(/^#\/?/, '');
  const [pathPart = '', queryPart = ''] = clean.split('?');
  const params = new URLSearchParams(queryPart);
  const segments = pathPart.split('/').filter(Boolean);
  const first = segments[0] ?? '';
  const second = segments[1];

  switch (first) {
    case '':
    case 'launchpad':
      return { view: 'launchpad', params };
    case 'launch':
      return { view: 'launch', params };
    case 'workflow':
      return { view: 'workflow', id: second, params };
    case 'gate':
      return { view: 'gate', id: second, params };
    case 'squads':
      return { view: 'squads', params };
    case 'campaigns':
      return { view: 'campaigns', params };
    case 'memory':
      return { view: 'memory', params };
    case 'governance':
      return { view: 'governance', params };
    case 'reliquary':
      return { view: 'reliquary', params };
    case 'prototypes':
      return { view: 'prototypes', params };
    default:
      return { view: 'launchpad', params };
  }
}

// ---------------------------------------------------------------------------
// Phase machine — 8 nodes
// ---------------------------------------------------------------------------

export const PHASES = [
  'intake',
  'planning',
  'approval',
  'dispatch',
  'executing',
  'judge',
  'synthesis',
  'postcheck',
] as const;

export type Phase = (typeof PHASES)[number] | 'surfaced' | 'done';

export function phaseIndex(phase: string): number {
  return PHASES.indexOf(phase as (typeof PHASES)[number]);
}

// ---------------------------------------------------------------------------
// Budget helpers
// ---------------------------------------------------------------------------

export function budgetPct(spent: number, budget: number): number {
  if (budget <= 0) return 0;
  return Math.min(100, (spent / budget) * 100);
}

export function budgetBand(pct: number): 'normal' | 'warn' | 'critical' {
  if (pct >= 100) return 'critical';
  if (pct >= 80) return 'warn';
  return 'normal';
}
