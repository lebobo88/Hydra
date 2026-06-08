/**
 * Hydra Cockpit — Live Workflow view (#/workflow/:id).
 * 8-node phase-machine viz; SSE stream (→ poll fallback); budget ticker;
 * judge verdicts + Reflexion ×1 markers; task list.
 * Actions: Open gate / Modify budget / Abort / Replay.
 * 8-state handling per COCKPIT-DESIGN.md §1.5.
 */

import { useEffect, useReducer, useRef, useState } from 'react';
import { PhaseMachine } from '../components/PhaseMachine.tsx';
import { BudgetBar } from '../components/BudgetBar.tsx';
import { ConfirmDialog } from '../components/ConfirmDialog.tsx';
import { LoadingScreen, ErrorScreen, DegradedBanner, OfflineBanner, EmptyScreen } from '../components/StateScreens.tsx';
import type { CockpitDialogState, HitlGate } from '../cockpit/types.ts';
import { openWorkflowStream, previewNonce, resumeGate, replayWorkflow, CockpitWriteError } from '../api/client.ts';

interface TraceEntry {
  ts?: string | undefined;
  kind?: string | undefined;
  type: string;
  data: Record<string, unknown>;
  isReflexion?: boolean | undefined;
  isJudge?: boolean | undefined;
}

interface WorkflowState {
  phase: string | null;
  squads: string[];
  budget: { budget_usd?: number; spent_usd?: number } | null;
  gate: HitlGate | null;
  terminal: 'done' | 'surfaced' | null;
  traceEntries: TraceEntry[];
}

interface StateData {
  phase?: string | undefined;
  selected_squads?: string[] | undefined;
  budget?: { budget_usd?: number; spent_usd?: number } | undefined;
  workflow_id?: string | undefined;
}

type Action =
  | { type: 'state'; data: StateData }
  | { type: 'gate'; data: HitlGate }
  | { type: 'trace'; data: Record<string, unknown> }
  | { type: 'done'; data: { phase?: string | undefined } }
  | { type: 'reset'; initial?: Partial<WorkflowState> | undefined };

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
    case 'trace': {
      const d = action.data;
      const kind = typeof d['kind'] === 'string' ? d['kind'] : typeof d['type'] === 'string' ? d['type'] : 'trace';
      const isJudge = kind.toLowerCase().includes('judge') || kind.toLowerCase().includes('verdict');
      const isReflexion = kind.toLowerCase().includes('reflexion') || (typeof d['retry_index'] === 'number' && d['retry_index'] > 0);
      const violatesReflexion = isReflexion && typeof d['retry_index'] === 'number' && d['retry_index'] > 1;
      const entry: TraceEntry = {
        ts: typeof d['ts'] === 'string' ? d['ts'] : undefined,
        kind,
        type: violatesReflexion ? 'reflexion-violation' : isReflexion ? 'reflexion' : isJudge ? 'judge' : 'trace',
        data: d,
        isJudge,
        isReflexion,
      };
      // Keep last 200 entries
      const newEntries = [...state.traceEntries, entry].slice(-200);
      return { ...state, traceEntries: newEntries };
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
  return { phase: null, squads: [], budget: null, gate: null, terminal: null, traceEntries: [] };
}

function formatTime(iso: string | undefined): string {
  if (!iso) return '';
  try {
    return new Date(iso).toLocaleTimeString();
  } catch { return iso; }
}

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
  const streamRef = useRef<{ stop: () => void } | null>(null);
  const traceBottomRef = useRef<HTMLDivElement>(null);

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
        dispatch({
          type: 'state',
          data: {
            phase: typeof data['phase'] === 'string' ? data['phase'] : undefined,
            selected_squads: Array.isArray(data['selected_squads']) ? data['selected_squads'] as string[] : undefined,
            budget: b ?? undefined,
          },
        });
        if (data['pending_hitl'] && typeof data['pending_hitl'] === 'object') {
          dispatch({ type: 'gate', data: data['pending_hitl'] as HitlGate });
        }
        const phase = typeof data['phase'] === 'string' ? data['phase'] : null;
        if (phase === 'done' || phase === 'surfaced') {
          dispatch({ type: 'done', data: { phase } });
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
    // Don't stream if workflow is already terminal (skip stream for done/surfaced)
    // We still open it briefly to check for terminal events, then stop

    streamRef.current?.stop();

    const handle = openWorkflowStream(
      workflowId,
      (event) => {
        setStreamLive(true);
        setLoadError(null);
        if (event.type === 'state') dispatch({ type: 'state', data: event.data as StateData });
        else if (event.type === 'gate') dispatch({ type: 'gate', data: event.data as HitlGate });
        else if (event.type === 'trace') dispatch({ type: 'trace', data: event.data as Record<string, unknown> });
        else if (event.type === 'done') {
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
      handle.stop();
      streamRef.current = null;
    };
  }, [workflowId]);

  // Auto-scroll trace to bottom
  useEffect(() => {
    if (wfState.traceEntries.length > 0) {
      traceBottomRef.current?.scrollIntoView({ behavior: 'smooth' });
    }
  }, [wfState.traceEntries.length]);

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
          'Choose --from-phase and optionally --swap-model.',
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
        const resumeArgs: Parameters<typeof resumeGate>[0] = {
          workflow_id: workflowId,
          action: 'modify-budget',
          option: params.optionArg ?? '',
          confirmNonce: nonce,
        };
        if (params.typedChallenge) resumeArgs.typedChallenge = params.typedChallenge;
        await resumeGate(resumeArgs);
        setDialog(null);
      } else if (dialog.kind === 'abort') {
        const nonce = String(dialog.payload['confirmNonce'] ?? '');
        await resumeGate({
          workflow_id: workflowId,
          action: 'reject',
          confirmNonce: nonce,
        });
        setDialog(null);
        dispatch({ type: 'done', data: { phase: 'surfaced' } });
      } else if (dialog.kind === 'replay') {
        const nonce = String(dialog.payload['confirmNonce'] ?? '');
        const replayArgs: Parameters<typeof replayWorkflow>[0] = {
          workflow_id: workflowId,
          confirmNonce: nonce,
        };
        if (params.typedChallenge) replayArgs.typedChallenge = params.typedChallenge;
        const result = await replayWorkflow(replayArgs);
        setDialog(null);
        window.location.hash = `#/workflow/${encodeURIComponent(result.workflow_id)}`;
      }
    } catch (e) {
      setActionError(e instanceof CockpitWriteError ? e.detail.error : String(e));
    } finally {
      setBusy(false);
    }
  }

  if (initialLoading) return <LoadingScreen label={`Loading workflow ${workflowId.slice(0, 8)}…`} />;

  if (loadError && !wfState.phase) {
    return <ErrorScreen message={loadError} />;
  }

  const hasGate = !!wfState.gate;
  const isTerminal = !!wfState.terminal;

  return (
    <div className="workflow-view">
      {dialog ? (
        <ConfirmDialog
          state={dialog}
          onConfirm={(p) => { void handleDialogConfirm(p); }}
          onCancel={() => setDialog(null)}
          busy={busy}
        />
      ) : null}

      <header className="view-header">
        <a href="#/" className="back-link">← Launchpad</a>
        <div className="view-header-main">
          <h1 className="view-title">
            Workflow <span className="mono">{workflowId.slice(0, 8)}</span>
          </h1>
          {wfState.phase ? (
            <span className={`wf-phase-badge wf-phase--${wfState.phase}`}>{wfState.phase}</span>
          ) : null}
          <span
            className={`stream-pulse ${streamLive ? 'pulse--live' : 'pulse--offline'}`}
            role="status"
            aria-label={streamLive ? `Live stream (${usingSSE ? 'SSE' : 'poll'})` : 'Stream paused'}
            aria-live="polite"
          >
            {streamLive ? `● ${usingSSE ? 'live (SSE)' : 'live (poll)'}` : '⊗ paused'}
          </span>
        </div>
        <div className="view-header-actions">
          {hasGate ? (
            <a
              href={`#/gate/${encodeURIComponent(workflowId)}`}
              className="btn btn-danger btn-sm"
              aria-label="Open gate — pending HITL action required"
            >
              Open gate ⚠
            </a>
          ) : null}
          {!isTerminal && online ? (
            <>
              <button type="button" className="btn btn-sm" onClick={() => { void openModifyBudget(); }} disabled={busy}>
                Modify budget
              </button>
              <button type="button" className="btn btn-danger btn-sm" onClick={() => { void openAbort(); }} disabled={busy}>
                Abort
              </button>
            </>
          ) : null}
          {online ? (
            <button type="button" className="btn btn-sm" onClick={() => { void openReplay(); }} disabled={busy}>
              Replay
            </button>
          ) : null}
        </div>
      </header>

      {!online ? <OfflineBanner /> : null}
      {!usingSSE && streamLive ? (
        <DegradedBanner sources={['SSE']} message="SSE unavailable — using polling fallback" />
      ) : null}
      {actionError ? (
        <div className="inline-error" role="alert" aria-live="assertive">
          <span aria-hidden="true">▲</span> {actionError}
        </div>
      ) : null}

      {/* Phase machine */}
      <section className="workflow-section" aria-labelledby="phase-heading">
        <h2 id="phase-heading" className="section-heading">Phase machine</h2>
        <PhaseMachine currentPhase={wfState.phase} terminalState={wfState.terminal} />
      </section>

      {/* Budget ticker */}
      <section className="workflow-section" aria-labelledby="budget-heading">
        <h2 id="budget-heading" className="section-heading">Budget</h2>
        <BudgetBar
          spent={wfState.budget?.spent_usd ?? 0}
          budget={wfState.budget?.budget_usd ?? 0}
        />
      </section>

      {/* Envelope stream */}
      <section className="workflow-section" aria-labelledby="stream-heading">
        <h2 id="stream-heading" className="section-heading">
          Envelope stream
          <span className="text-muted text-sm" aria-label={`${wfState.traceEntries.length} entries`}>
            {' '}({wfState.traceEntries.length})
          </span>
        </h2>
        {wfState.traceEntries.length === 0 ? (
          <EmptyScreen message="No envelopes yet — waiting for the workflow to emit events." />
        ) : (
          <div
            className="trace-stream"
            role="log"
            aria-label="Envelope stream"
            aria-live="polite"
            aria-relevant="additions"
          >
            {wfState.traceEntries.map((entry, i) => (
              <TraceRow key={i} entry={entry} />
            ))}
            <div ref={traceBottomRef} aria-hidden="true" />
          </div>
        )}
      </section>
    </div>
  );
}

function TraceRow({ entry }: { entry: TraceEntry }): JSX.Element {
  const kind = entry.kind ?? entry.type;
  const ts = formatTime(entry.ts);
  const data = entry.data;

  let className = 'trace-row';
  if (entry.type === 'judge') className += ' trace-row--judge';
  if (entry.type === 'reflexion') className += ' trace-row--reflexion';
  if (entry.type === 'reflexion-violation') className += ' trace-row--violation';

  const outcome = typeof data['outcome'] === 'string' ? data['outcome'] : null;
  const retryIndex = typeof data['retry_index'] === 'number' ? data['retry_index'] : null;
  const actor = typeof data['actor'] === 'string' ? data['actor'] : null;
  const vendor = typeof data['vendor'] === 'string' ? data['vendor'] : null;

  return (
    <div className={className} aria-label={`${kind} event at ${ts}`}>
      <span className="trace-ts mono text-sm">{ts}</span>
      <span className={`trace-kind mono${entry.isJudge ? ' trace-kind--judge' : entry.isReflexion ? ' trace-kind--reflexion' : ''}`}>
        {kind}
      </span>
      {actor ? <span className="trace-actor text-muted text-sm">{actor}</span> : null}
      {vendor ? <span className="trace-vendor text-muted text-sm">{vendor}</span> : null}
      {outcome ? (
        <span className={`trace-outcome trace-outcome--${outcome}`}>
          {outcome === 'approve' ? '✓ approve' : outcome === 'revise' ? '↺ revise' : outcome}
        </span>
      ) : null}
      {retryIndex !== null ? (
        <span
          className={`trace-retry${retryIndex > 1 ? ' trace-retry--violation' : ''}`}
          role={retryIndex > 1 ? 'alert' : undefined}
          aria-label={retryIndex > 1 ? `VIOLATION: Reflexion retry_index=${retryIndex} exceeds limit` : `Reflexion ×${retryIndex}`}
        >
          {retryIndex > 1 ? `⛔ Reflexion ×${retryIndex} — INVARIANT VIOLATION` : `↺ Reflexion ×${retryIndex}`}
        </span>
      ) : null}
    </div>
  );
}

