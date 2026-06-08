/**
 * Hydra Cockpit — 8-node phase-machine visualization.
 * Nodes: intake → planning → approval → dispatch → executing → judge → synthesis → postcheck
 * Terminal annotations: surfaced, done.
 *
 * Accessibility: aria-label on each node; prefers-reduced-motion honored.
 * Color is never the sole signal — text status always accompanies color.
 */

import { PHASES } from '../cockpit/types.ts';

type PhaseStatus = 'done' | 'active' | 'pending' | 'interrupt';

function nodeStatus(phase: string | null | undefined, nodePhase: string): PhaseStatus {
  if (!phase) return 'pending';
  const currentIdx = PHASES.indexOf(phase as (typeof PHASES)[number]);
  const nodeIdx = PHASES.indexOf(nodePhase as (typeof PHASES)[number]);
  if (phase === 'done' || phase === 'surfaced') {
    return nodeIdx < PHASES.length ? 'done' : 'pending';
  }
  if (nodeIdx < currentIdx) return 'done';
  if (nodeIdx === currentIdx) return 'active';
  return 'pending';
}

const INTERRUPT_PHASES = new Set(['approval', 'judge', 'synthesis']);

const PHASE_LABELS: Record<string, string> = {
  intake: 'intake',
  planning: 'planning',
  approval: 'approval',
  dispatch: 'dispatch',
  executing: 'exec',
  judge: 'judge',
  synthesis: 'synth',
  postcheck: 'postcheck',
};

interface PhaseMachineProps {
  currentPhase?: string | null | undefined;
  terminalState?: 'done' | 'surfaced' | null | undefined;
}

export function PhaseMachine({ currentPhase, terminalState }: PhaseMachineProps): JSX.Element {
  return (
    <div
      className="phase-machine"
      role="img"
      aria-label={`Phase machine: current phase is ${currentPhase ?? 'unknown'}${terminalState ? `, terminal: ${terminalState}` : ''}`}
    >
      <div className="phase-nodes" aria-hidden="true">
        {PHASES.map((p, i) => {
          const status = terminalState
            ? 'done'
            : nodeStatus(currentPhase, p);
          const isInterrupt = INTERRUPT_PHASES.has(p);
          return (
            <span key={p} className="phase-node-group">
              {i > 0 ? <span className="phase-arrow">→</span> : null}
              <span
                className={`phase-node phase-node--${status}${isInterrupt ? ' phase-node--interrupt' : ''}`}
                title={`${p}: ${status}${isInterrupt ? ' (interrupt_before)' : ''}`}
              >
                <span className="phase-dot" aria-hidden="true">
                  {status === 'done' ? '●' : status === 'active' ? '◐' : '○'}
                </span>
                <span className="phase-label">{PHASE_LABELS[p] ?? p}</span>
                {isInterrupt && status !== 'done' ? (
                  <span className="phase-hitl-marker" aria-label="HITL interrupt boundary">
                    ⚡
                  </span>
                ) : null}
              </span>
            </span>
          );
        })}
      </div>
      {/* Screen-reader accessible phase list */}
      <ol className="sr-only">
        {PHASES.map((p) => (
          <li key={p}>
            {p}: {terminalState ? 'done' : nodeStatus(currentPhase, p)}
            {INTERRUPT_PHASES.has(p) ? ' (HITL interrupt boundary)' : ''}
          </li>
        ))}
      </ol>
      {terminalState ? (
        <span className={`phase-terminal phase-terminal--${terminalState}`} aria-label={`Terminal state: ${terminalState}`}>
          {terminalState === 'done' ? '✓ done' : '⊡ surfaced'}
        </span>
      ) : null}
    </div>
  );
}
