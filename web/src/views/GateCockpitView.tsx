/**
 * Hydra Cockpit — Gate Cockpit (#/gate/:hitl_id).
 * Renders the HITLRequest VERBATIM (no paraphrase — per design spec §1.2.4).
 * Expiry countdown disables all actions on expiry.
 * All 5 resume actions each via ConfirmDialog.
 * default_option highlighted but NEVER preselected (silence ≠ consent).
 * Required resolution note on every resume.
 * Typed workflow-id challenge on high-risk gates and unconditionally on force-dispatch.
 */

import { useCallback, useEffect, useState } from 'react';
import { ConfirmDialog } from '../components/ConfirmDialog.tsx';
import { LoadingScreen, ErrorScreen, EmptyScreen, OfflineBanner, DegradedBanner } from '../components/StateScreens.tsx';
import type { CockpitDialogState, HitlGate } from '../cockpit/types.ts';
import { resumeGate, previewNonce, CockpitWriteError, fetchHitl } from '../api/client.ts';

const HIGH_RISK_REASONS = new Set(['prod_deploy', 'constitution_breach', 'policy_breach', 'high_risk']);

const RESUME_ACTIONS = [
  { action: 'approve', label: 'Approve', risk: 'med' as const },
  { action: 'reject', label: 'Reject', risk: 'low' as const },
  { action: 'modify-budget', label: 'Modify budget', risk: 'high' as const },
  { action: 'change-squads', label: 'Change squads', risk: 'med' as const },
  { action: 'force-dispatch', label: 'Force dispatch', risk: 'venom' as const },
] as const;

function useExpiryCountdown(expiresAt: string | null | undefined): { expired: boolean; remaining: string } {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    if (!expiresAt) return;
    const interval = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(interval);
  }, [expiresAt]);

  if (!expiresAt) return { expired: false, remaining: '' };
  const expiry = new Date(expiresAt).getTime();
  if (isNaN(expiry)) return { expired: false, remaining: expiresAt };
  const diffMs = expiry - now;
  if (diffMs <= 0) return { expired: true, remaining: 'Expired' };
  const h = Math.floor(diffMs / 3600000);
  const m = Math.floor((diffMs % 3600000) / 60000);
  const s = Math.floor((diffMs % 60000) / 1000);
  const remaining = [h > 0 ? `${h}h` : '', `${m.toString().padStart(2, '0')}m`, `${s.toString().padStart(2, '0')}s`]
    .filter(Boolean)
    .join(' ');
  return { expired: false, remaining };
}

interface GateCockpitViewProps {
  /** workflowId — used to look up the pending gate */
  workflowId: string;
  online: boolean;
}

export function GateCockpitView({ workflowId, online }: GateCockpitViewProps): JSX.Element {
  const [gate, setGate] = useState<HitlGate | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [resolved, setResolved] = useState(false);
  const [dialog, setDialog] = useState<CockpitDialogState | null>(null);
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [degraded, setDegraded] = useState(false);

  const loadGate = useCallback(async (): Promise<void> => {
    setLoading(true);
    setError(null);
    try {
      // First try direct workflow status for pending_hitl
      const wfRes = await fetch(`/api/workflows/${encodeURIComponent(workflowId)}`);
      if (!wfRes.ok) throw new Error(`workflow fetch failed: ${wfRes.status}`);
      const wfData = (await wfRes.json()) as Record<string, unknown>;
      const pendingGate = wfData['pending_hitl'] as HitlGate | null;
      if (pendingGate) {
        setGate(pendingGate);
        setLoading(false);
        return;
      }
      // Fallback: query /api/hitl list for this workflow
      const hitlBody = (await fetchHitl()) as unknown;
      let items: HitlGate[] = [];
      if (Array.isArray(hitlBody)) items = hitlBody as HitlGate[];
      else if (hitlBody && typeof hitlBody === 'object' && 'items' in hitlBody) {
        items = ((hitlBody as { items?: HitlGate[] }).items ?? []);
      }
      const found = items.find((i) => i.workflow_id === workflowId);
      if (found) {
        setGate(found);
      } else {
        setDegraded(true);
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [workflowId]);

  useEffect(() => {
    void loadGate();
    const interval = setInterval(() => { void loadGate(); }, 5000);
    return () => clearInterval(interval);
  }, [loadGate]);

  const { expired, remaining } = useExpiryCountdown(gate?.expires_at);

  async function openResumeDialog(action: string): Promise<void> {
    setActionError(null);
    if (!gate) return;

    const isHighRisk = HIGH_RISK_REASONS.has(gate.reason ?? '') || action === 'force-dispatch' || action === 'modify-budget';
    const isForceDispatch = action === 'force-dispatch';

    try {
      // Always request a nonce for approve, modify-budget, change-squads, force-dispatch
      // reject is Low risk (no nonce)
      let nonce: string | undefined;
      if (action !== 'reject') {
        const nonceData = await previewNonce(action);
        nonce = nonceData.nonce;
      }

      const actionMeta = RESUME_ACTIONS.find((a) => a.action === action);
      const riskLabel = actionMeta?.risk === 'venom' ? ' ⛔ venom-class · Cerberus-gated' :
        actionMeta?.risk === 'high' ? ' ⚠ High-risk' : '';

      setDialog({
        kind: 'gate-resume',
        title: `${actionMeta?.label ?? action}${riskLabel}`,
        verb: `${actionMeta?.label ?? action}`,
        lines: [
          `Workflow: ${workflowId.slice(0, 8)}`,
          gate.summary ? `Summary: ${gate.summary}` : '',
          action === 'force-dispatch'
            ? 'Force-dispatch is venom-class and Cerberus-gated server-side. You own this risk.'
            : action === 'modify-budget'
              ? 'Enter a new budget cap. High-risk write.'
              : action === 'change-squads'
                ? 'Enter replacement squad slugs.'
                : '',
          'A resolution note is required and will be recorded.',
        ].filter(Boolean),
        danger: isHighRisk || action === 'reject',
        withNote: true,
        ...((action === 'modify-budget') ? { options: ['modify-budget'], defaultOption: null } : {}),
        ...((action === 'change-squads') ? { options: ['change-squads'], defaultOption: null } : {}),
        ...((isHighRisk || isForceDispatch) ? { typedChallenge: workflowId, typedLabel: 'Type the workflow ID to confirm' } : {}),
        payload: {
          action,
          ...(nonce ? { confirmNonce: nonce } : {}),
        },
      });
    } catch (e) {
      setActionError(e instanceof Error ? e.message : String(e));
    }
  }

  async function handleDialogConfirm(params: {
    note?: string;
    option?: string;
    optionArg?: string;
    typedChallenge?: string;
  }): Promise<void> {
    if (!dialog || !gate) return;
    setBusy(true);
    setActionError(null);
    try {
      const action = String(dialog.payload['action'] ?? '');
      const nonce = dialog.payload['confirmNonce'] as string | undefined;
      const gateArgs: Parameters<typeof resumeGate>[0] = {
        workflow_id: workflowId,
        action,
      };
      const optVal = params.optionArg ?? params.option;
      if (optVal) gateArgs.option = optVal;
      if (nonce) gateArgs.confirmNonce = nonce;
      if (params.typedChallenge) gateArgs.typedChallenge = params.typedChallenge;
      await resumeGate(gateArgs);
      setDialog(null);
      setResolved(true);
    } catch (e) {
      setActionError(e instanceof CockpitWriteError ? e.detail.error : String(e));
    } finally {
      setBusy(false);
    }
  }

  if (loading && !gate) return <LoadingScreen label="Loading gate…" />;
  if (error && !gate) return <ErrorScreen message={error} onRetry={() => { void loadGate(); }} />;
  if (resolved) {
    return (
      <div className="gate-view">
        <header className="view-header">
          <a href={`#/workflow/${encodeURIComponent(workflowId)}`} className="back-link">
            ← Workflow {workflowId.slice(0, 8)}
          </a>
        </header>
        <EmptyScreen message="Gate resolved. The workflow should resume shortly." />
      </div>
    );
  }

  return (
    <div className="gate-view">
      {dialog ? (
        <ConfirmDialog
          state={dialog}
          onConfirm={(p) => { void handleDialogConfirm(p); }}
          onCancel={() => setDialog(null)}
          busy={busy}
        />
      ) : null}

      <header className="view-header">
        <a href={`#/workflow/${encodeURIComponent(workflowId)}`} className="back-link">
          ← Workflow {workflowId.slice(0, 8)}
        </a>
        <h1 className="view-title">Gate Cockpit</h1>
        {gate?.expires_at ? (
          <span
            className={`gate-expiry${expired ? ' gate-expiry--expired' : ''}`}
            role="status"
            aria-live="polite"
            aria-label={expired ? 'Gate expired — all actions disabled' : `Gate expires in ${remaining}`}
          >
            {expired ? '⏳ Expired — gate closed' : `⏳ expires in ${remaining}`}
          </span>
        ) : null}
      </header>

      {!online ? <OfflineBanner /> : null}
      {degraded ? (
        <DegradedBanner sources={['hydra-mem']} message="Gate data may be unavailable — no pending gate found for this workflow" />
      ) : null}
      {actionError ? (
        <div className="inline-error" role="alert" aria-live="assertive">
          <span aria-hidden="true">▲</span> {actionError}
        </div>
      ) : null}
      {expired ? (
        <div className="expiry-notice" role="alert" aria-live="assertive">
          <strong>Gate expired</strong> — all resume actions are disabled. The workflow is marked surfaced.
        </div>
      ) : null}

      {gate ? (
        <>
          {/* Verbatim HITL request — per spec: do not paraphrase */}
          <section
            className="hitl-verbatim"
            aria-label="HITL request — verbatim"
            role="region"
          >
            <header className="hitl-verbatim-header">HITL REQUEST — VERBATIM</header>
            <pre className="hitl-verbatim-body" tabIndex={0}>
              {[
                gate.workflow_id ? `workflow_id : ${gate.workflow_id}` : null,
                gate.reason ? `reason      : ${gate.reason}` : null,
                gate.summary ? `summary     : ${gate.summary}` : null,
                gate.options ? `options     : ${gate.options.join(' | ')}` : null,
                gate.default_option !== undefined && gate.default_option !== null
                  ? `default     : ${gate.default_option}`
                  : null,
                gate.expires_at ? `expires     : ${gate.expires_at}` : null,
              ]
                .filter(Boolean)
                .join('\n')}
            </pre>
          </section>

          {/* Default option notice */}
          {gate.default_option ? (
            <div className="default-option-notice" aria-label="Default option notice">
              <span aria-hidden="true">ℹ</span> Default option is{' '}
              <strong className="mono">{gate.default_option}</strong> — highlighted below but{' '}
              <em>never preselected</em>. You must choose explicitly. Silence is not consent.
            </div>
          ) : null}

          {/* Resume actions */}
          <section className="resume-actions" aria-labelledby="resume-heading">
            <h2 id="resume-heading" className="section-heading">Resume action</h2>
            <div className="resume-action-list" role="group" aria-label="Resume actions">
              {RESUME_ACTIONS.filter((a) => !gate.options || gate.options.includes(a.action) || gate.options.length === 0 || a.action === 'approve' || a.action === 'reject').map((meta) => {
                const isDefault = gate.default_option === meta.action;
                const isVenom = meta.risk === 'venom';
                const isHigh = meta.risk === 'high';
                const disabled = !online || expired || busy;
                return (
                  <button
                    key={meta.action}
                    type="button"
                    className={[
                      'resume-action-btn',
                      isDefault ? 'resume-action-btn--default' : '',
                      isVenom ? 'resume-action-btn--venom' : '',
                      isHigh ? 'resume-action-btn--high' : '',
                      meta.risk === 'low' ? 'resume-action-btn--low' : '',
                    ].filter(Boolean).join(' ')}
                    onClick={() => { void openResumeDialog(meta.action); }}
                    disabled={disabled}
                    aria-label={`${meta.label}${isDefault ? ' (gate default — requires explicit selection)' : ''}${isVenom ? ' — venom class, Cerberus-gated' : ''}`}
                    aria-pressed={false}
                  >
                    {meta.label}
                    {isDefault ? (
                      <span className="resume-default-indicator" aria-hidden="true"> ← default</span>
                    ) : null}
                    {isVenom ? (
                      <span className="venom-badge" aria-hidden="true"> ⛔ venom</span>
                    ) : isHigh ? (
                      <span className="risk-badge risk-badge--high" aria-hidden="true"> ⚠</span>
                    ) : null}
                  </button>
                );
              })}
            </div>
          </section>
        </>
      ) : (
        <EmptyScreen message="Gate already resolved or no pending gate for this workflow." />
      )}
    </div>
  );
}
