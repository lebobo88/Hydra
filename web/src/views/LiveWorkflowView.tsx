/**
 * Hydra Cockpit — THE LIVING RUN (R3 Living Workflow view).
 * Re-presents the existing SSE/poll data wiring as the Living Run:
 *   - Vertical phase spine with biolume peristaltic travel + coiling rings
 *   - Squad-head lateral arm emergence at dispatch (150ms stagger)
 *   - Trace as inscription (clip-path sweep, reflexion amber / violation venom borders)
 *   - "↓ new events" scroll-pause pill (amber, Esc resumes)
 *   - Budget tension strand (4px, role=meter, color-mix sinew→amber)
 *   - Task Register (collapsible, JetBrains Mono, ○/◐/● status)
 *   - Oracle synthesis wiring via SynthesisContext
 *   - 8-state: loading / empty / error / degraded / offline / live / confirm
 *
 * DATA WIRING: all SSE+poll stream logic via openWorkflowStream is PRESERVED
 * from the R0/C7 implementation. Only the presentation layer changes.
 */

import { useEffect, useReducer, useRef, useState, useCallback } from 'react';
import { ConfirmDialog } from '../components/ConfirmDialog.tsx';
import { ErrorScreen, DegradedBanner, OfflineBanner } from '../components/StateScreens.tsx';
import type { CockpitDialogState, HitlGate } from '../cockpit/types.ts';
import { PHASES } from '../cockpit/types.ts';
import { openWorkflowStream, previewNonce, resumeGate, replayWorkflow, CockpitWriteError } from '../api/client.ts';
import { useSynthesis } from '../cockpit/SynthesisContext.ts';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface TraceEntry {
  ts?: string | undefined;
  kind?: string | undefined;
  type: string;
  data: Record<string, unknown>;
  isReflexion?: boolean | undefined;
  isViolation?: boolean | undefined;
  isJudge?: boolean | undefined;
}

interface WorkflowState {
  phase: string | null;
  squads: string[];
  budget: { budget_usd?: number; spent_usd?: number } | null;
  gate: HitlGate | null;
  terminal: 'done' | 'surfaced' | null;
  traceEntries: TraceEntry[];
  tasks: Array<{ owner_squad?: string | null; status?: string | null; description?: string }>;
  synthesisDeclared: string | null;
  /** Count of envelopes per phase (for coiling ring progress) */
  phaseEnvelopeCounts: Record<string, number>;
}

interface StateData {
  phase?: string | undefined;
  selected_squads?: string[] | undefined;
  budget?: { budget_usd?: number; spent_usd?: number } | undefined;
}

type Action =
  | { type: 'state'; data: StateData }
  | { type: 'gate'; data: HitlGate }
  | { type: 'trace'; data: Record<string, unknown> }
  | { type: 'done'; data: { phase?: string | undefined } }
  | { type: 'reset'; initial?: Partial<WorkflowState> | undefined }
  | { type: 'synthesis'; text: string };

function reducer(state: WorkflowState, action: Action): WorkflowState {
  switch (action.type) {
    case 'state':
      return {
        ...state,
        phase: action.data.phase ?? state.phase,
        squads: action.data.selected_squads ?? state.squads,
        budget: action.data.budget ?? state.budget,
      };
    case 'gate':
      return { ...state, gate: action.data };
    case 'synthesis':
      return { ...state, synthesisDeclared: action.text };
    case 'trace': {
      const d = action.data;
      const kind = typeof d['kind'] === 'string' ? d['kind'] : typeof d['type'] === 'string' ? d['type'] : 'trace';
      const isJudge = kind.toLowerCase().includes('judge') || kind.toLowerCase().includes('verdict');
      const isReflexion = kind.toLowerCase().includes('reflexion') || (typeof d['retry_index'] === 'number' && d['retry_index'] > 0);
      const isViolation = isReflexion && typeof d['retry_index'] === 'number' && d['retry_index'] > 1;

      // Extract synthesis text from synthesis-phase trace events.
      // Priority (highest first):
      //  1. DECISION_RECORD kind — carries 'decision' + optional 'rationale'
      //  2. synthesis-bearing events — 'declaration' or 'text' field
      //  3. 'synthesis_declaration' field present on any event
      let synthUpdate: string | null = null;
      if (kind === 'DECISION_RECORD' && typeof d['decision'] === 'string') {
        // Primary synthesis carrier for completed workflows. Include rationale if present.
        const decision = d['decision'] as string;
        const rationale = typeof d['rationale'] === 'string' ? d['rationale'] : null;
        synthUpdate = rationale ? `${decision} ${rationale}` : decision;
      } else if (kind.toLowerCase().includes('synthesis') && typeof d['declaration'] === 'string') {
        synthUpdate = d['declaration'] as string;
      } else if (kind.toLowerCase().includes('synthesis') && typeof d['text'] === 'string') {
        synthUpdate = d['text'] as string;
      } else if (typeof d['synthesis_declaration'] === 'string') {
        synthUpdate = d['synthesis_declaration'] as string;
      }

      const entry: TraceEntry = {
        ts: typeof d['ts'] === 'string' ? d['ts'] : undefined,
        kind,
        type: isViolation ? 'reflexion-violation' : isReflexion ? 'reflexion' : isJudge ? 'judge' : 'trace',
        data: d,
        isJudge,
        isReflexion,
        isViolation,
      };

      // Update per-phase envelope count
      const phaseKey = state.phase ?? 'unknown';
      const updatedCounts = {
        ...state.phaseEnvelopeCounts,
        [phaseKey]: (state.phaseEnvelopeCounts[phaseKey] ?? 0) + 1,
      };

      const newEntries = [...state.traceEntries, entry].slice(-200);
      return {
        ...state,
        traceEntries: newEntries,
        phaseEnvelopeCounts: updatedCounts,
        synthesisDeclared: synthUpdate ?? state.synthesisDeclared,
      };
    }
    case 'done':
      return {
        ...state,
        terminal: (action.data.phase === 'surfaced' ? 'surfaced' : 'done'),
        phase: action.data.phase ?? state.phase,
      };
    case 'reset':
      return { ...defaultWorkflowState(), ...action.initial };
    default:
      return state;
  }
}

function defaultWorkflowState(): WorkflowState {
  return {
    phase: null,
    squads: [],
    budget: null,
    gate: null,
    terminal: null,
    traceEntries: [],
    tasks: [],
    synthesisDeclared: null,
    phaseEnvelopeCounts: {},
  };
}

function formatTime(iso: string | undefined): string {
  if (!iso) return '';
  try {
    return new Date(iso).toLocaleTimeString();
  } catch { return iso; }
}

// ---------------------------------------------------------------------------
// Crown family lookup (mirrors App.tsx)
// ---------------------------------------------------------------------------

type CrownFamily = 'exec' | 'forge' | 'garland';

const CROWN_MAP: Record<string, CrownFamily> = {
  executive: 'exec', legal: 'exec', finance: 'exec', compliance: 'exec',
  engineering: 'forge', forge: 'forge', platform: 'forge', infra: 'forge',
  devops: 'forge', security: 'forge',
  garland: 'garland', marketing: 'garland', creative: 'garland',
  design: 'garland', product: 'garland', research: 'garland',
};

function crownOf(slug: string): CrownFamily {
  return CROWN_MAP[slug.toLowerCase()] ?? 'forge';
}

// ---------------------------------------------------------------------------
// Phase helpers
// ---------------------------------------------------------------------------

type PhaseStatus = 'done' | 'active' | 'pending';
const INTERRUPT_PHASES = new Set(['approval', 'synthesis', 'judge']);

function phaseStatus(current: string | null, p: string, terminal: 'done' | 'surfaced' | null): PhaseStatus {
  if (terminal) return 'done';
  if (!current) return 'pending';
  const ci = PHASES.indexOf(current as typeof PHASES[number]);
  const pi = PHASES.indexOf(p as typeof PHASES[number]);
  if (pi < ci) return 'done';
  if (pi === ci) return 'active';
  return 'pending';
}

const PHASE_DISPLAY: Record<string, string> = {
  intake: 'Intake', planning: 'Planning', approval: 'Approval',
  dispatch: 'Dispatch', executing: 'Executing', judge: 'Judge',
  synthesis: 'Synthesis', postcheck: 'Postcheck',
};

// ---------------------------------------------------------------------------
// Budget helpers
// ---------------------------------------------------------------------------

function calcBudgetPct(spent: number, budget: number): number {
  if (!budget || budget <= 0) return 0;
  return Math.min(1, spent / budget);
}

// ---------------------------------------------------------------------------
// Vertical Phase Spine SVG — renders the spine track + coiling rings + arms
// ---------------------------------------------------------------------------

interface PhaseSpineProps {
  currentPhase: string | null;
  terminal: 'done' | 'surfaced' | null;
  squads: string[];
  phaseEnvelopeCounts: Record<string, number>;
  budgetPct: number;
}

function PhaseSpineSVG({ currentPhase, terminal, squads, phaseEnvelopeCounts, budgetPct }: PhaseSpineProps): JSX.Element {
  const nodeH = 48;   // height per phase node row
  const svgW = 28;    // narrow SVG width (just the spine track)
  const cx = 14;      // center X of spine
  const dotR = 7;     // phase dot radius
  const ringR = 10;   // progress ring radius (around dot)
  const ringCirc = 2 * Math.PI * ringR; // ≈ 62.8

  const phases = PHASES;
  const hasSquads = squads.length > 0;

  return (
    <svg
      className="phase-spine-svg"
      width={svgW}
      height={phases.length * nodeH}
      viewBox={`0 0 ${svgW} ${phases.length * nodeH}`}
      aria-hidden="true"
      focusable="false"
      data-testid="phase-spine-svg"
    >
      {/* Spine track line */}
      <line
        x1={cx} y1={dotR}
        x2={cx} y2={phases.length * nodeH - dotR}
        stroke="color-mix(in srgb, var(--bone-mid) 20%, transparent)"
        strokeWidth="1"
      />

      {phases.map((p, i) => {
        const cy = i * nodeH + nodeH / 2;
        const status = phaseStatus(currentPhase, p, terminal);
        const isInterrupt = INTERRUPT_PHASES.has(p);
        const isDispatch = p === 'dispatch';

        // Colors
        let dotFill = 'var(--bone-mid)';
        if (status === 'done') dotFill = 'var(--biolume-dim)';
        if (status === 'active') dotFill = 'var(--biolume)';

        // Connector between phases i-1 → i (serpentine cubic bezier)
        let connector: JSX.Element | null = null;
        if (i > 0) {
          const prevCy = (i - 1) * nodeH + nodeH / 2;
          const prevStatus = phaseStatus(currentPhase, phases[i - 1], terminal);
          const isConnectorDone = prevStatus === 'done' || status === 'done';
          const connectorStroke = isConnectorDone ? 'var(--biolume-dim)' : 'color-mix(in srgb, var(--bone-mid) 15%, transparent)';
          // Serpentine cubic bezier: slight S-curve
          const d = `M ${cx},${prevCy + dotR} C ${cx + 6},${prevCy + dotR + 8} ${cx - 6},${cy - dotR - 8} ${cx},${cy - dotR}`;
          const connLen = nodeH - dotR * 2;
          connector = (
            <path
              key={`conn-${p}`}
              d={d}
              fill="none"
              stroke={connectorStroke}
              strokeWidth="1.5"
              strokeDasharray={isConnectorDone ? '0' : '4 3'}
              className={isConnectorDone ? `phase-connector-draw` : ''}
              style={{ '--connector-index': i - 1, '--connector-len': connLen } as React.CSSProperties}
            />
          );
        }

        // Coiling progress ring — driven by envelope count for this phase
        const phaseCount = phaseEnvelopeCounts[p] ?? 0;
        const maxCount = 20; // normalize: at 20 envelopes = full ring
        const ringProgress = status === 'done' ? 1 : Math.min(1, phaseCount / maxCount);
        const ringDashoffset = ringCirc * (1 - ringProgress);

        // Dispatch phase: show squad heads if squads present
        const isActiveDispatch = isDispatch && hasSquads && (status === 'active' || status === 'done');

        return (
          <g key={p}>
            {connector}

            {/* Phase dot background */}
            <circle
              cx={cx} cy={cy} r={dotR}
              fill={dotFill}
              className={status === 'done' ? 'phase-dot-complete' : ''}
            />

            {/* Coiling progress ring */}
            {status !== 'pending' ? (
              <circle
                cx={cx} cy={cy} r={ringR}
                fill="none"
                stroke={status === 'done' ? 'var(--biolume-dim)' : 'var(--biolume)'}
                strokeWidth="2"
                strokeDasharray={`${ringCirc}`}
                strokeDashoffset={ringDashoffset}
                strokeLinecap="round"
                transform={`rotate(-90, ${cx}, ${cy})`}
                className={status === 'done' ? 'phase-ring-complete' : ''}
                aria-hidden="true"
              />
            ) : null}

            {/* Active phase biolume glow */}
            {status === 'active' ? (
              <circle
                cx={cx} cy={cy} r={dotR + 4}
                fill="none"
                stroke="var(--biolume)"
                strokeWidth="1"
                opacity="0.3"
              />
            ) : null}

            {/* Interrupt boundary marker */}
            {isInterrupt && status !== 'done' ? (
              <text
                x={cx + dotR + 2} y={cy + 4}
                fill="var(--spirit-amber)"
                fontSize="8"
                aria-hidden="true"
              >⚡</text>
            ) : null}

            {/* Dispatch: squad arm lateral lines */}
            {isActiveDispatch ? squads.slice(0, 5).map((squad, si) => {
              const armLen = 40 + si * 10;
              const armY = cy - (squads.length - 1) * 6 + si * 12;
              return (
                <g key={`arm-${squad}`}>
                  <line
                    x1={cx + dotR} y1={armY}
                    x2={cx + dotR + armLen} y2={armY}
                    stroke={`var(--crown-${crownOf(squad)})`}
                    strokeWidth="1.5"
                    strokeDasharray={`${armLen}`}
                    className="arm-draw"
                    style={{ '--head-index': si, '--arm-len': armLen } as React.CSSProperties}
                    aria-hidden="true"
                  />
                </g>
              );
            }) : null}

            {/* Peristalsis wave on active phase connector area */}
            {status === 'active' && i > 0 ? (
              <rect
                x={cx - 2} y={(i - 1) * nodeH + nodeH / 2 + dotR}
                width={4}
                height={nodeH - dotR * 2}
                fill="var(--biolume)"
                opacity="0.15"
                className="phase-peristalsis"
                aria-hidden="true"
              />
            ) : null}
          </g>
        );
      })}
    </svg>
  );
  void budgetPct; // used by caller for strand, not spine
}

// ---------------------------------------------------------------------------
// Squad-head chips (dispatch phase emergence)
// ---------------------------------------------------------------------------

interface SquadHeadChipsProps {
  squads: string[];
  currentPhase: string | null;
  terminal: 'done' | 'surfaced' | null;
}

function SquadHeadChips({ squads, currentPhase, terminal }: SquadHeadChipsProps): JSX.Element | null {
  if (!squads.length) return null;
  const dispatchIdx = PHASES.indexOf('dispatch');
  const currentIdx = currentPhase ? PHASES.indexOf(currentPhase as typeof PHASES[number]) : -1;
  const isAtOrPastDispatch = terminal !== null || currentIdx >= dispatchIdx;
  if (!isAtOrPastDispatch) return null;

  return (
    <div className="dispatch-arms" data-testid="dispatch-arms" aria-label="Recruited squad heads">
      {squads.map((squad, i) => {
        const crown = crownOf(squad);
        return (
          <span
            key={squad}
            className={`dispatch-arm-head dispatch-arm-head--${crown} head-fill-emerge`}
            style={{ '--head-index': i } as React.CSSProperties}
            aria-label={`${squad} head`}
            data-testid={`dispatch-arm-${squad}`}
          >
            <span className="head-flame-icon" aria-hidden="true">🔥</span>
            {squad}
          </span>
        );
      })}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Budget Tension Strand
// ---------------------------------------------------------------------------

interface BudgetStrandProps {
  spent: number;
  budget: number;
}

function BudgetStrand({ spent, budget }: BudgetStrandProps): JSX.Element {
  const pct = calcBudgetPct(spent, budget);
  const bandClass = pct >= 1.0 ? 'budget-strand-fill--critical' : pct >= 0.8 ? 'budget-strand-fill--warn' : '';
  const fillPct = Math.min(100, pct * 100);

  return (
    <section className="budget-strand-section" aria-labelledby="budget-strand-heading" data-testid="budget-strand-section">
      <div className="budget-strand-label" id="budget-strand-heading">
        <span className="sr-only">Budget</span>
        <span aria-hidden="true">Budget</span>
        <span className="budget-strand-annotation" aria-hidden="true">
          §{spent.toFixed(2)} / §{budget.toFixed(2)}
        </span>
      </div>
      <div
        className="budget-strand-track"
        role="meter"
        aria-valuenow={Math.round(pct * 100)}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-label={`Budget: ${Math.round(pct * 100)}% of $${budget.toFixed(2)} consumed`}
        style={{ '--budget-pct': pct } as React.CSSProperties}
        data-testid="budget-strand"
      >
        <div
          className={`budget-strand-fill ${bandClass}${pct >= 1.0 ? ' budget-alarm' : ''}`}
          style={{ width: `${fillPct}%` }}
        />
        {/* 80% mark */}
        <div className="budget-strand-mark budget-strand-mark--80" aria-hidden="true" />
        {/* 100% mark */}
        <div className="budget-strand-mark budget-strand-mark--100" aria-hidden="true" />
      </div>
      {pct >= 1.0 ? (
        <span className="sr-only" role="alert" aria-live="assertive">
          Budget exceeded: ${spent.toFixed(2)} of ${budget.toFixed(2)} used.
        </span>
      ) : null}
    </section>
  );
}

// ---------------------------------------------------------------------------
// Task Register
// ---------------------------------------------------------------------------

interface TaskRegisterProps {
  tasks: Array<{ owner_squad?: string | null; status?: string | null; description?: string }>;
}

function TaskRegister({ tasks }: TaskRegisterProps): JSX.Element | null {
  const [open, setOpen] = useState(false);
  if (!tasks.length) return null;

  function statusMark(s: string | null | undefined): { mark: string; cls: string; label: string } {
    if (!s || s === 'pending') return { mark: '○', cls: 'task-status-mark--pending', label: 'pending' };
    if (s === 'done' || s === 'completed') return { mark: '●', cls: 'task-status-mark--done', label: 'done' };
    return { mark: '◐', cls: 'task-status-mark--active', label: 'in progress' };
  }

  return (
    <div className="task-register" data-testid="task-register">
      <div
        className="task-register-header"
        role="button"
        aria-expanded={open}
        aria-controls="task-register-list"
        tabIndex={0}
        onClick={() => setOpen((p) => !p)}
        onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); setOpen((p) => !p); } }}
      >
        <span className="task-register-header-label">Task Register</span>
        <span className="task-register-toggle" aria-hidden="true">{open ? '▴' : '▾'}</span>
        <span className="sr-only">{open ? 'Collapse' : 'Expand'} task list</span>
      </div>
      {open ? (
        <ul
          className="task-register-list"
          id="task-register-list"
          role="list"
          aria-label="Task Register"
          data-testid="task-register-list"
        >
          {tasks.map((task, i) => {
            const { mark, cls, label } = statusMark(task.status);
            return (
              <li
                key={i}
                className="task-register-item"
                role="listitem"
                aria-label={`Task ${i + 1}: ${label}${task.description ? ` — ${task.description}` : ''}`}
              >
                <span className={`task-status-mark ${cls}`} aria-hidden="true">{mark}</span>
                <span className="task-description">{task.description ?? `Task ${i + 1}`}</span>
                {task.owner_squad ? (
                  <span className="task-owner">{task.owner_squad}</span>
                ) : null}
              </li>
            );
          })}
        </ul>
      ) : null}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Trace Line
// ---------------------------------------------------------------------------

interface TraceLineProps {
  entry: TraceEntry;
  isNew: boolean;
}

function TraceLine({ entry, isNew }: TraceLineProps): JSX.Element {
  const kind = entry.kind ?? entry.type;
  const ts = formatTime(entry.ts);
  const d = entry.data;

  let cls = 'trace-line';
  if (entry.type === 'judge') cls += ' trace-line--judge';
  if (entry.type === 'reflexion') cls += ' trace-line--reflexion';
  if (entry.type === 'reflexion-violation') cls += ' trace-line--violation';
  if (isNew) cls += ' trace-inscribe';

  const outcome = typeof d['outcome'] === 'string' ? d['outcome'] : null;
  const retryIndex = typeof d['retry_index'] === 'number' ? d['retry_index'] : null;
  const actor = typeof d['actor'] === 'string' ? d['actor'] : null;
  const vendor = typeof d['vendor'] === 'string' ? d['vendor'] : null;

  const kindCls = entry.isJudge ? 'trace-kind-label trace-kind-label--judge'
    : entry.isReflexion ? 'trace-kind-label trace-kind-label--reflexion'
    : 'trace-kind-label';

  return (
    <div
      className={cls}
      aria-label={`${kind} event${ts ? ` at ${ts}` : ''}`}
      role={entry.isViolation ? 'alert' : undefined}
    >
      <span className="trace-ts mono">{ts}</span>
      <span className={kindCls}>{kind}</span>
      {actor ? <span className="trace-actor-label">{actor}</span> : null}
      {vendor ? <span className="trace-vendor-label">{vendor}</span> : null}
      {outcome ? (
        <span className={`trace-outcome-label trace-outcome-label--${outcome}`}>
          {outcome === 'approve' ? '✓ approve' : outcome === 'revise' ? '↺ revise' : outcome}
        </span>
      ) : null}
      {retryIndex !== null ? (
        <span
          className={`trace-retry-label${retryIndex > 1 ? ' trace-retry-label--violation' : ''}`}
          aria-label={retryIndex > 1
            ? `VIOLATION: Reflexion retry_index=${retryIndex} exceeds limit`
            : `Reflexion ×${retryIndex}`}
        >
          {retryIndex > 1 ? `⛔ Reflexion ×${retryIndex} — INVARIANT VIOLATION` : `↺ Reflexion ×${retryIndex}`}
        </span>
      ) : null}
      {entry.isReflexion && !entry.isViolation ? (
        <span className="trace-reflexion-annotation" aria-hidden="true">reflexion ×1</span>
      ) : null}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Scroll-pause pill
// ---------------------------------------------------------------------------

interface ScrollPausePillProps {
  onResume: () => void;
}

function ScrollPausePill({ onResume }: ScrollPausePillProps): JSX.Element {
  return (
    <button
      className="scroll-pause-pill"
      type="button"
      role="button"
      aria-label="New events — click or press Esc to resume live tail"
      onClick={onResume}
      data-testid="scroll-pause-pill"
    >
      ↓ new events
    </button>
  );
}

// ---------------------------------------------------------------------------
// Loading skeleton for the living run
// ---------------------------------------------------------------------------

function LivingRunSkeleton(): JSX.Element {
  return (
    <div className="living-run-skeleton" aria-label="Loading workflow" role="status" data-testid="living-run-skeleton">
      <div className="skeleton-line" style={{ width: '60%' }} />
      <div className="skeleton-line" style={{ width: '30%' }} />
      <div className="skeleton-line" style={{ width: '80%' }} />
      <div className="skeleton-line" style={{ width: '45%' }} />
      <div className="skeleton-line" style={{ width: '70%' }} />
      <div className="skeleton-line" style={{ width: '55%' }} />
      <div className="skeleton-line" style={{ width: '90%' }} />
      <div className="skeleton-line" style={{ width: '40%' }} />
    </div>
  );
}

// ---------------------------------------------------------------------------
// SynthesisConvergence — showpiece: many crown-colored streaks rushing inward
// ---------------------------------------------------------------------------

interface SynthesisConvergenceProps {
  squads: string[];
}

/** Crown color for a squad slug — mirrors the CROWN_MAP logic */
function squadCrownColor(slug: string): string {
  const exec = new Set(['executive', 'legal', 'finance', 'compliance']);
  const forge = new Set(['engineering', 'forge', 'platform', 'infra', 'devops', 'security']);
  const lower = slug.toLowerCase();
  if (exec.has(lower)) return 'var(--crown-exec)';
  if (forge.has(lower)) return 'var(--crown-forge)';
  return 'var(--crown-garland)';
}

function SynthesisConvergence({ squads }: SynthesisConvergenceProps): JSX.Element {
  const cx = 200;
  const cy = 80;
  const streakSources = squads.length > 0 ? squads.slice(0, 6) : ['a', 'b', 'c'];
  const total = streakSources.length;

  return (
    <div
      className="synthesis-convergence-wrapper"
      aria-hidden="true"
      data-testid="synthesis-convergence"
    >
      <svg
        viewBox="0 0 400 160"
        width="100%"
        height="160"
        aria-hidden="true"
        className="synthesis-convergence-svg"
      >
        {/* Ambient aura at convergence center */}
        <circle
          cx={cx} cy={cy} r={24}
          fill="none"
          stroke="var(--spirit-amber)"
          strokeWidth="0.6"
          opacity="0.4"
          className="synthesis-convergence-aura"
        />
        <circle
          cx={cx} cy={cy} r={14}
          fill="color-mix(in srgb, var(--spirit-amber) 15%, transparent)"
          stroke="var(--spirit-amber)"
          strokeWidth="1"
          opacity="0.7"
        />

        {/* Synthesis.png flourish at center */}
        <image
          href="/images/chosen/synthesis.png"
          x={cx - 20} y={cy - 20}
          width={40} height={40}
          preserveAspectRatio="xMidYMid meet"
          opacity="0.7"
          className="synthesis-hero-flourish"
        />

        {/* Convergence streaks — one per squad, from edges to center */}
        {streakSources.map((slug, i) => {
          const angle = (i / total) * Math.PI * 2 - Math.PI / 2;
          const srcR = 160;
          const srcX = cx + Math.cos(angle) * srcR;
          const srcY = cy + Math.sin(angle) * srcR;
          const color = squadCrownColor(slug);
          // Approximate path length for dasharray
          const pathLen = Math.sqrt((srcX - cx) ** 2 + (srcY - cy) ** 2);

          return (
            <line
              key={slug}
              x1={srcX} y1={srcY}
              x2={cx} y2={cy}
              stroke={color}
              strokeWidth="1.5"
              opacity="0.8"
              strokeDasharray={`${pathLen}`}
              strokeDashoffset={`${pathLen}`}
              className="convergence-streak"
              style={{
                '--streak-index': i,
                '--streak-len': pathLen,
              } as React.CSSProperties}
            />
          );
        })}

        {/* Oracle radiance bloom — expands outward after streaks converge */}
        <circle
          cx={cx} cy={cy} r={0}
          fill="none"
          stroke="var(--spirit-amber)"
          strokeWidth="1.5"
          opacity="0"
          className="oracle-radiance-bloom"
          style={{ '--streak-count': total } as React.CSSProperties}
        />

        {/* Label */}
        <text
          x={cx} y={cy + 44}
          textAnchor="middle"
          fontSize="9"
          fill="var(--spirit-amber)"
          opacity="0.7"
          fontFamily="var(--font-covenant)"
          letterSpacing="0.12em"
        >
          SYNTHESIS
        </text>
      </svg>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main LiveWorkflowView component
// ---------------------------------------------------------------------------

interface LiveWorkflowViewProps {
  workflowId: string;
  online: boolean;
}

export function LiveWorkflowView({ workflowId, online }: LiveWorkflowViewProps): JSX.Element {
  const [wfState, dispatch] = useReducer(reducer, defaultWorkflowState());
  const [streamLive, setStreamLive] = useState(false);
  const [usingSSE, setUsingSSE] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [initialLoading, setInitialLoading] = useState(true);
  const [dialog, setDialog] = useState<CockpitDialogState | null>(null);
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [scrollPaused, setScrollPaused] = useState(false);
  const [newEventCount, setNewEventCount] = useState(0);

  // Track last rendered entry count to identify "new" entries for inscription animation
  const lastRenderedCount = useRef(0);

  const streamRef = useRef<{ stop: () => void } | null>(null);
  const traceScrollRef = useRef<HTMLDivElement>(null);
  const traceBottomRef = useRef<HTMLDivElement>(null);

  // Oracle synthesis wiring (R3 — wire to shell)
  const { setSynthesis } = useSynthesis();

  // Derive phase info
  const currentPhase = wfState.phase;
  const isDispatchOrLater = !!currentPhase && (
    PHASES.indexOf(currentPhase as typeof PHASES[number]) >= PHASES.indexOf('dispatch')
  );
  const synthesisDone = !!currentPhase && (
    PHASES.indexOf(currentPhase as typeof PHASES[number]) > PHASES.indexOf('synthesis')
  );
  const isExecuting = !!currentPhase && !wfState.terminal;
  const budgetPct = calcBudgetPct(wfState.budget?.spent_usd ?? 0, wfState.budget?.budget_usd ?? 0);

  // Push synthesis up to Oracle via context
  useEffect(() => {
    setSynthesis(wfState.synthesisDeclared, isExecuting && !synthesisDone);
    return () => {
      // On unmount (workflow view closed), clear synthesis
      setSynthesis(null, false);
    };
  }, [wfState.synthesisDeclared, isExecuting, synthesisDone, setSynthesis]);

  // Initial load from /api/workflows/:id
  useEffect(() => {
    setInitialLoading(true);
    dispatch({ type: 'reset' });
    fetch(`/api/workflows/${encodeURIComponent(workflowId)}`)
      .then((r) => {
        if (!r.ok) throw new Error(`workflow fetch failed: ${r.status}`);
        return r.json() as Promise<Record<string, unknown>>;
      })
      .then((data) => {
        const b = data['budget'] as WorkflowState['budget'] | null | undefined;
        const tasks = Array.isArray(data['tasks'])
          ? data['tasks'] as WorkflowState['tasks']
          : [];
        dispatch({
          type: 'state',
          data: {
            phase: typeof data['phase'] === 'string' ? data['phase'] : undefined,
            selected_squads: Array.isArray(data['selected_squads']) ? data['selected_squads'] as string[] : undefined,
            budget: b ?? undefined,
          },
        });
        if (tasks.length > 0) {
          // Inject tasks into state via reset with initial
          dispatch({ type: 'reset', initial: {
            phase: typeof data['phase'] === 'string' ? data['phase'] : null,
            squads: Array.isArray(data['selected_squads']) ? data['selected_squads'] as string[] : [],
            budget: b ?? null,
            tasks,
          }});
        }
        if (data['pending_hitl'] && typeof data['pending_hitl'] === 'object') {
          dispatch({ type: 'gate', data: data['pending_hitl'] as HitlGate });
        }
        const phase = typeof data['phase'] === 'string' ? data['phase'] : null;
        if (phase === 'done' || phase === 'surfaced') {
          dispatch({ type: 'done', data: { phase } });
        }
        // If workflow already has a synthesis declaration
        if (typeof data['synthesis_declaration'] === 'string') {
          dispatch({ type: 'synthesis', text: data['synthesis_declaration'] as string });
        }
        setInitialLoading(false);
      })
      .catch((e: unknown) => {
        setLoadError(e instanceof Error ? e.message : String(e));
        setInitialLoading(false);
      });
  }, [workflowId]);

  // Open SSE stream (or poll fallback)
  useEffect(() => {
    if (!workflowId) return;

    streamRef.current?.stop();

    const handle = openWorkflowStream(
      workflowId,
      (event) => {
        setStreamLive(true);
        setLoadError(null);
        if (event.type === 'state') {
          dispatch({ type: 'state', data: event.data as StateData });
        } else if (event.type === 'gate') {
          dispatch({ type: 'gate', data: event.data as HitlGate });
        } else if (event.type === 'trace') {
          const d = event.data as Record<string, unknown>;
          dispatch({ type: 'trace', data: d });
          // If scrolled away, count new events
          if (scrollPaused) {
            setNewEventCount((c) => c + 1);
          }
        } else if (event.type === 'done') {
          dispatch({ type: 'done', data: event.data });
          handle.stop();
        }
        // ping: no-op
      },
      (err) => {
        setStreamLive(false);
        setLoadError(`stream error: ${err}`);
      },
      (isSSE) => {
        setUsingSSE(isSSE);
        if (!isSSE) setStreamLive(true); // poll mode still "live" (degraded)
      },
    );

    streamRef.current = handle;
    return () => {
      handle?.stop?.();
      streamRef.current = null;
    };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [workflowId]);

  // Esc key resumes scroll
  useEffect(() => {
    function onKeyDown(e: KeyboardEvent): void {
      if (e.key === 'Escape' && scrollPaused) {
        resumeScroll();
      }
    }
    document.addEventListener('keydown', onKeyDown);
    return () => document.removeEventListener('keydown', onKeyDown);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scrollPaused]);

  // Auto-scroll trace to bottom when not paused
  useEffect(() => {
    if (wfState.traceEntries.length > 0 && !scrollPaused) {
      const el = traceBottomRef.current;
      if (el && typeof el.scrollIntoView === 'function') {
        el.scrollIntoView({ behavior: 'smooth' });
      }
    }
  }, [wfState.traceEntries.length, scrollPaused]);

  // Detect scroll-pause
  const handleTraceScroll = useCallback(() => {
    const el = traceScrollRef.current;
    if (!el) return;
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
    if (atBottom && scrollPaused) {
      setScrollPaused(false);
      setNewEventCount(0);
    } else if (!atBottom && !scrollPaused) {
      setScrollPaused(true);
    }
  }, [scrollPaused]);

  function resumeScroll(): void {
    setScrollPaused(false);
    setNewEventCount(0);
    // Scroll to bottom
    setTimeout(() => {
      traceBottomRef.current?.scrollIntoView({ behavior: 'smooth' });
    }, 50);
  }

  // Action handlers
  async function openModifyBudget(): Promise<void> {
    setActionError(null);
    try {
      const nonceData = await previewNonce('modify-budget');
      setDialog({
        kind: 'modify-budget',
        title: 'Modify budget',
        verb: 'Modify budget',
        lines: [`Workflow: ${workflowId.slice(0, 8)}`, 'Enter new budget cap (USD). This is a High-risk write.'],
        withNote: true,
        options: ['modify-budget'],
        defaultOption: null,
        danger: true,
        typedChallenge: workflowId,
        typedLabel: 'Type the workflow ID to confirm',
        payload: { confirmNonce: nonceData.nonce },
      });
    } catch (e) {
      setActionError(e instanceof Error ? e.message : String(e));
    }
  }

  async function openAbort(): Promise<void> {
    setActionError(null);
    try {
      const nonceData = await previewNonce('reject');
      setDialog({
        kind: 'abort',
        title: 'Abort (reject) workflow',
        verb: 'Abort',
        lines: [
          `Workflow: ${workflowId.slice(0, 8)}`,
          'This will reject the current gate and park the workflow as surfaced.',
        ],
        withNote: true,
        danger: true,
        payload: { confirmNonce: nonceData.nonce },
      });
    } catch (e) {
      setActionError(e instanceof Error ? e.message : String(e));
    }
  }

  async function openReplay(): Promise<void> {
    setActionError(null);
    try {
      const nonceData = await previewNonce('replay');
      setDialog({
        kind: 'replay',
        title: 'Replay workflow',
        verb: 'Replay',
        lines: [
          `Replaying workflow: ${workflowId.slice(0, 8)}`,
          'Replay is a High-risk write. Live replay is additionally venom-gated.',
        ],
        withNote: false,
        payload: { confirmNonce: nonceData.nonce },
      });
    } catch (e) {
      setActionError(e instanceof Error ? e.message : String(e));
    }
  }

  async function handleDialogConfirm(params: { note?: string; option?: string; optionArg?: string; typedChallenge?: string }): Promise<void> {
    if (!dialog) return;
    setBusy(true);
    setActionError(null);
    try {
      if (dialog.kind === 'modify-budget') {
        const nonce = String(dialog.payload['confirmNonce'] ?? '');
        await resumeGate({
          workflow_id: workflowId,
          action: 'modify-budget',
          option: params.optionArg ?? '',
          confirmNonce: nonce,
          ...(params.typedChallenge ? { typedChallenge: params.typedChallenge } : {}),
        });
        setDialog(null);
      } else if (dialog.kind === 'abort') {
        const nonce = String(dialog.payload['confirmNonce'] ?? '');
        await resumeGate({ workflow_id: workflowId, action: 'reject', confirmNonce: nonce });
        setDialog(null);
        dispatch({ type: 'done', data: { phase: 'surfaced' } });
      } else if (dialog.kind === 'replay') {
        const nonce = String(dialog.payload['confirmNonce'] ?? '');
        const result = await replayWorkflow({
          workflow_id: workflowId,
          confirmNonce: nonce,
          ...(params.typedChallenge ? { typedChallenge: params.typedChallenge } : {}),
        });
        setDialog(null);
        window.location.hash = `#/workflow/${encodeURIComponent(result.workflow_id)}`;
      }
    } catch (e) {
      setActionError(e instanceof CockpitWriteError ? e.detail.error : String(e));
    } finally {
      setBusy(false);
    }
  }

  // ------------- 8-state render gates -------------

  if (initialLoading) return <LivingRunSkeleton />;

  if (loadError && !wfState.phase) {
    return <ErrorScreen message={loadError} />;
  }

  const hasGate = !!wfState.gate;
  const isTerminal = !!wfState.terminal;

  // Track which entries are "new" for the inscription animation
  const prevCount = lastRenderedCount.current;
  lastRenderedCount.current = wfState.traceEntries.length;

  return (
    <div className="living-run" data-testid="living-run">
      {/* Confirm dialog (gate/venom confirm path) */}
      {dialog ? (
        <ConfirmDialog
          state={dialog}
          onConfirm={(p) => { void handleDialogConfirm(p); }}
          onCancel={() => setDialog(null)}
          busy={busy}
        />
      ) : null}

      {/* ---- Header ---- */}
      <header className="living-run-header" data-testid="living-run-header">
        <a href="#/" className="living-run-back" aria-label="Back to Launchpad">
          ← Launchpad
        </a>
        <span className="living-run-id">
          Workflow <span className="mono">{workflowId.slice(0, 8)}</span>
        </span>
        {wfState.phase ? (
          <span className={`living-run-phase-badge living-run-phase-badge--${wfState.phase}`}>
            {wfState.phase}
          </span>
        ) : null}
        <span
          className={`living-run-stream-pill ${
            streamLive ? (usingSSE ? 'living-run-stream-pill--live' : 'living-run-stream-pill--poll') : 'living-run-stream-pill--paused'
          }`}
          role="status"
          aria-label={streamLive
            ? `Live stream — ${usingSSE ? 'SSE' : 'polling'}`
            : 'Stream paused'}
          aria-live="polite"
          data-testid="stream-status"
        >
          {streamLive ? `● ${usingSSE ? 'live' : 'poll'}` : '⊗ paused'}
        </span>
      </header>

      {/* ---- State banners ---- */}
      {!online ? <OfflineBanner /> : null}
      {!usingSSE && streamLive ? (
        <DegradedBanner sources={['SSE']} message="SSE severed — polling fallback active. Stream is degraded, not empty." />
      ) : null}
      {loadError && wfState.phase ? (
        <div className="inline-error" role="alert" aria-live="polite">
          Stream error: {loadError} — showing last known data.
        </div>
      ) : null}
      {actionError ? (
        <div className="inline-error" role="alert" aria-live="assertive">
          <span aria-hidden="true">▲</span> {actionError}
        </div>
      ) : null}

      {/* ---- Gate action shortcut ---- */}
      {hasGate ? (
        <a
          href={`#/gate/${encodeURIComponent(workflowId)}`}
          className="btn btn-danger btn-sm"
          aria-label="Open gate — pending HITL action required"
          style={{ alignSelf: 'flex-start' }}
        >
          Open gate ⚠
        </a>
      ) : null}

      {/* ---- Vertical Phase Spine ---- */}
      <section
        className="phase-spine-section"
        aria-labelledby="phase-spine-heading"
        data-testid="phase-spine-section"
      >
        <h2 id="phase-spine-heading" className="phase-spine-label">
          Phase spine
        </h2>

        <div className="phase-spine" data-testid="phase-spine">
          {/* SVG track */}
          <PhaseSpineSVG
            currentPhase={wfState.phase}
            terminal={wfState.terminal}
            squads={wfState.squads}
            phaseEnvelopeCounts={wfState.phaseEnvelopeCounts}
            budgetPct={budgetPct}
          />

          {/* Node labels */}
          <ol
            className="phase-node-list"
            aria-label="Workflow phases"
          >
            {PHASES.map((p) => {
              const status = phaseStatus(wfState.phase, p, wfState.terminal);
              const isInterrupt = INTERRUPT_PHASES.has(p);
              const isActive = status === 'active';

              const nameCls = isInterrupt && status !== 'done'
                ? 'phase-spine-name phase-spine-name--interrupt'
                : status === 'done'
                  ? 'phase-spine-name phase-spine-name--done'
                  : isActive
                    ? 'phase-spine-name phase-spine-name--active'
                    : 'phase-spine-name';

              return (
                <li
                  key={p}
                  className={`phase-spine-node${isActive ? ' phase-spine-node--active' : ''}${isInterrupt ? ' phase-spine-node--interrupt' : ''}`}
                  aria-current={isActive ? 'step' : undefined}
                  aria-label={`${PHASE_DISPLAY[p] ?? p}: ${status}${isInterrupt ? ', HITL interrupt boundary' : ''}`}
                  data-testid={`phase-node-${p}`}
                >
                  <span className={nameCls}>{PHASE_DISPLAY[p] ?? p}</span>
                  <span className="phase-spine-status sr-only">{status}</span>
                  {isInterrupt && status !== 'done' ? (
                    <span className="phase-interrupt-marker" aria-label="HITL interrupt boundary">⚡</span>
                  ) : null}
                  {/* Squad heads emerge at dispatch */}
                  {p === 'dispatch' && isDispatchOrLater ? (
                    <SquadHeadChips
                      squads={wfState.squads}
                      currentPhase={wfState.phase}
                      terminal={wfState.terminal}
                    />
                  ) : null}
                </li>
              );
            })}
          </ol>
        </div>

        {/* Screen-reader accessible phase list */}
        <ol className="sr-only">
          {PHASES.map((p) => (
            <li key={p}>
              {p}: {phaseStatus(wfState.phase, p, wfState.terminal)}
              {INTERRUPT_PHASES.has(p) ? ' (HITL interrupt boundary)' : ''}
            </li>
          ))}
        </ol>

        {wfState.terminal ? (
          <div
            className={`living-run-phase-badge living-run-phase-badge--${wfState.terminal}`}
            aria-label={`Workflow ${wfState.terminal}`}
            style={{ alignSelf: 'flex-start', marginTop: 'var(--sp-2)' }}
          >
            {wfState.terminal === 'done' ? '✓ done' : '⊡ surfaced'}
          </div>
        ) : null}
      </section>

      {/* ---- Budget Tension Strand ---- */}
      {(wfState.budget?.budget_usd ?? 0) > 0 ? (
        <BudgetStrand
          spent={wfState.budget?.spent_usd ?? 0}
          budget={wfState.budget?.budget_usd ?? 0}
        />
      ) : null}

      {/* ---- Synthesis Convergence Showpiece ---- */}
      {wfState.phase === 'synthesis' ? (
        <SynthesisConvergence squads={wfState.squads} />
      ) : null}

      {/* ---- Trace / Envelope Stream ---- */}
      <section
        className="trace-section"
        aria-labelledby="trace-heading"
        data-testid="trace-section"
      >
        <div className="trace-section-header">
          <h2 id="trace-heading" className="trace-section-label">
            Envelope stream
          </h2>
          <span className="trace-count-badge" aria-label={`${wfState.traceEntries.length} entries`}>
            ({wfState.traceEntries.length})
          </span>
        </div>

        {wfState.traceEntries.length === 0 && !loadError ? (
          // empty/partial: not blank — waiting notice
          <div
            className="trace-gap-notice"
            role="status"
            aria-live="polite"
            data-testid="trace-gap-notice"
          >
            Awaiting envelopes from the workflow — stream active, trace gap is not evidence of none.
          </div>
        ) : (
          <div
            ref={traceScrollRef}
            className="trace-scroll-container"
            role="log"
            aria-label="Envelope stream"
            aria-live="polite"
            aria-relevant="additions"
            onScroll={handleTraceScroll}
            data-testid="trace-stream"
          >
            {wfState.traceEntries.map((entry, i) => (
              <TraceLine
                key={i}
                entry={entry}
                isNew={i >= prevCount}
              />
            ))}
            <div ref={traceBottomRef} aria-hidden="true" />
          </div>
        )}

        {/* Scroll-pause pill */}
        {scrollPaused && newEventCount > 0 ? (
          <ScrollPausePill onResume={resumeScroll} />
        ) : null}
      </section>

      {/* ---- Task Register ---- */}
      {wfState.tasks.length > 0 ? (
        <TaskRegister tasks={wfState.tasks} />
      ) : null}

      {/* ---- Actions row ---- */}
      <div className="living-run-bottom-actions" role="group" aria-label="Workflow actions">
        {!isTerminal && online ? (
          <>
            <button
              type="button"
              className="btn btn-sm"
              onClick={() => { void openModifyBudget(); }}
              disabled={busy}
            >
              Modify budget
            </button>
            <button
              type="button"
              className="btn btn-danger btn-sm"
              onClick={() => { void openAbort(); }}
              disabled={busy}
            >
              Abort
            </button>
          </>
        ) : null}
        {online ? (
          <button
            type="button"
            className="btn btn-sm btn-venom-replay"
            onClick={() => { void openReplay(); }}
            disabled={busy || !online}
            aria-label="Replay workflow — venom-gated write"
          >
            Replay
          </button>
        ) : (
          <button
            type="button"
            className="btn btn-sm"
            disabled={true}
            aria-label="Replay disabled — bridge offline"
            title="Bridge offline — write actions disabled"
          >
            Replay (offline)
          </button>
        )}
      </div>
    </div>
  );
}
