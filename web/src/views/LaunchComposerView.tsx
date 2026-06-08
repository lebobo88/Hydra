/**
 * Hydra Cockpit — Launch Composer (#/launch).
 * Goal textarea + router preview (from /api/squads) + squad hints + budget + dry/live toggle.
 * Live launch is High-risk: requires ConfirmDialog with server-issued nonce.
 * Dry-run is the default and is NOT high-risk.
 * Deep-link: reads ?goal= and ?squads= from hash params.
 */

import { useCallback, useEffect, useState } from 'react';
import { ConfirmDialog } from '../components/ConfirmDialog.tsx';
import type { CockpitDialogState } from '../cockpit/types.ts';
import type { SquadPack } from '../api/client.ts';
import { previewNonce, launchWorkflow, CockpitWriteError } from '../api/client.ts';
import { OfflineBanner } from '../components/StateScreens.tsx';
import { ViewHeader } from '../components/ViewHeader.tsx';

interface LaunchComposerViewProps {
  /** Pre-filled from deep-link ?goal= */
  initialGoal?: string;
  /** Pre-filled from deep-link ?squads= */
  initialSquads?: string[];
  online: boolean;
  onLaunched: (workflowId: string) => void;
}

export function LaunchComposerView({ initialGoal = '', initialSquads = [], online, onLaunched }: LaunchComposerViewProps): JSX.Element {
  const [goal, setGoal] = useState(initialGoal);
  const [selectedSquads, setSelectedSquads] = useState<string[]>(initialSquads);
  const [budget, setBudget] = useState<string>('80');
  const [isLive, setIsLive] = useState(false);
  const [squads, setSquads] = useState<SquadPack[]>([]);
  const [squadsLoading, setSquadsLoading] = useState(true);
  const [squadsError, setSquadsError] = useState<string | null>(null);
  const [previewScores, setPreviewScores] = useState<{ slug: string; score: number }[] | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [dialog, setDialog] = useState<CockpitDialogState | null>(null);
  const [busy, setBusy] = useState(false);
  const [launchError, setLaunchError] = useState<string | null>(null);

  useEffect(() => {
    // Load squads for routing hints
    fetch('/api/squads')
      .then((r) => {
        if (!r.ok) throw new Error(`squads error: ${r.status}`);
        return r.json() as Promise<unknown>;
      })
      .then((body) => {
        let packs: SquadPack[] = [];
        if (Array.isArray(body)) packs = body as SquadPack[];
        else if (body && typeof body === 'object' && 'squads' in body) {
          packs = (body as { squads?: SquadPack[] }).squads ?? [];
        }
        setSquads(packs);
        setSquadsLoading(false);
      })
      .catch((e: unknown) => {
        setSquadsError(e instanceof Error ? e.message : String(e));
        setSquadsLoading(false);
      });
  }, []);

  const handlePreview = useCallback(() => {
    if (!goal.trim()) return;
    setPreviewLoading(true);
    // Compute simple cosine-like score from goal keywords against squad description
    const goalLower = goal.toLowerCase();
    const scored = squads.map((sq) => {
      const text = `${sq.slug} ${sq.name ?? ''} ${sq.description ?? ''} ${(sq.industries ?? []).join(' ')}`.toLowerCase();
      const words = goalLower.split(/\s+/).filter((w) => w.length > 3);
      const hits = words.filter((w) => text.includes(w)).length;
      const score = words.length > 0 ? hits / words.length : 0;
      return { slug: sq.slug, score };
    }).sort((a, b) => b.score - a.score);
    setPreviewScores(scored.slice(0, 5));
    setPreviewLoading(false);
  }, [goal, squads]);

  function toggleSquad(slug: string): void {
    setSelectedSquads((prev) =>
      prev.includes(slug) ? prev.filter((s) => s !== slug) : [...prev, slug],
    );
  }

  async function handleDryRun(): Promise<void> {
    setLaunchError(null);
    setBusy(true);
    try {
      const launchArgs1: Parameters<typeof launchWorkflow>[0] = { goal: goal.trim(), live: false };
      if (selectedSquads.length > 0) launchArgs1.squads = selectedSquads;
      const budgetNum1 = Number(budget);
      if (budget && !isNaN(budgetNum1) && budgetNum1 > 0) launchArgs1.budgetUsd = budgetNum1;
      const result = await launchWorkflow(launchArgs1);
      onLaunched(result.workflow_id);
    } catch (e) {
      setLaunchError(e instanceof CockpitWriteError ? e.detail.error : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function handleLaunchClick(): Promise<void> {
    setLaunchError(null);
    try {
      // Fetch a confirm nonce for this high-risk write
      const nonceData = await previewNonce('launch');
      setDialog({
        kind: 'launch-live',
        title: 'Launch live workflow',
        verb: 'Launch (live)',
        lines: [
          `Goal: "${goal.trim()}"`,
          `Budget: $${budget}`,
          selectedSquads.length > 0 ? `Squads: ${selectedSquads.join(', ')}` : 'Squads: auto-routed',
          'This is a High-risk write. A real workflow will be dispatched.',
          `Confirm nonce expires: ${new Date(nonceData.expiresAt).toLocaleTimeString()}`,
        ],
        danger: true,
        payload: { confirmNonce: nonceData.nonce },
      });
    } catch (e) {
      setLaunchError(e instanceof Error ? e.message : String(e));
    }
  }

  async function handleDialogConfirm(): Promise<void> {
    if (!dialog) return;
    setBusy(true);
    setLaunchError(null);
    try {
      const nonce = String(dialog.payload['confirmNonce'] ?? '');
      const launchArgs2: Parameters<typeof launchWorkflow>[0] = { goal: goal.trim(), live: true, confirmNonce: nonce };
      if (selectedSquads.length > 0) launchArgs2.squads = selectedSquads;
      const budgetNum2 = Number(budget);
      if (budget && !isNaN(budgetNum2) && budgetNum2 > 0) launchArgs2.budgetUsd = budgetNum2;
      const result = await launchWorkflow(launchArgs2);
      setDialog(null);
      onLaunched(result.workflow_id);
    } catch (e) {
      setLaunchError(e instanceof CockpitWriteError ? e.detail.error : String(e));
    } finally {
      setBusy(false);
    }
  }

  const goalTrimmed = goal.trim();
  const budgetNum = Number(budget);
  const budgetOk = budget === '' || (!isNaN(budgetNum) && budgetNum > 0);
  const canLaunch = goalTrimmed.length > 0 && budgetOk && online;

  if (dialog) {
    return (
      <div className="launch-composer">
        <ConfirmDialog
          state={dialog}
          onConfirm={() => { void handleDialogConfirm(); }}
          onCancel={() => setDialog(null)}
          busy={busy}
        />
      </div>
    );
  }

  return (
    <div className="launch-composer">
      {!online ? <OfflineBanner /> : null}

      <ViewHeader title="Launch Composer" />

      {launchError ? (
        <div className="inline-error" role="alert" aria-live="assertive">
          <span aria-hidden="true">▲</span> {launchError}
        </div>
      ) : null}

      <form
        className="launch-form"
        onSubmit={(e) => e.preventDefault()}
        aria-label="Launch workflow form"
      >
        {/* Goal input */}
        <div className="form-field">
          <label htmlFor="launch-goal" className="form-label">
            Goal <span className="required-mark" aria-label="required">*</span>
          </label>
          <textarea
            id="launch-goal"
            className="form-textarea"
            value={goal}
            onChange={(e) => setGoal(e.target.value)}
            rows={4}
            placeholder="Describe the goal for this workflow…"
            disabled={!online || busy}
            aria-required="true"
            aria-describedby="launch-goal-hint"
          />
          <span id="launch-goal-hint" className="form-hint text-muted text-sm">
            A clear, specific goal improves routing accuracy.
          </span>
        </div>

        {/* Router preview */}
        <div className="form-field">
          <div className="form-label-row">
            <span className="form-label">Router preview <span className="text-muted text-sm">(read-only)</span></span>
            <button
              type="button"
              className="btn btn-sm"
              onClick={handlePreview}
              disabled={!goalTrimmed || squadsLoading || busy}
              aria-label="Preview routing scores"
            >
              {previewLoading ? 'Computing…' : 'Preview routing'}
            </button>
          </div>
          {squadsError ? (
            <div className="inline-error text-sm" role="alert">
              Router preview unavailable ({squadsError}) — manual squad hints only.
            </div>
          ) : null}
          {previewScores !== null ? (
            <div className="router-preview" role="region" aria-label="Router preview scores">
              {previewScores.map((s, i) => (
                <div key={s.slug} className={`router-score-row${i === 0 ? ' router-score-top' : ''}`}>
                  <span className="router-squad-slug mono">{s.slug}</span>
                  <div
                    className="router-score-bar"
                    role="meter"
                    aria-valuenow={Math.round(s.score * 100)}
                    aria-valuemin={0}
                    aria-valuemax={100}
                    aria-label={`${s.slug}: ${(s.score * 100).toFixed(0)}% match`}
                  >
                    <div className="router-score-fill" style={{ width: `${s.score * 100}%` }} />
                  </div>
                  <span className="router-score-val">{s.score.toFixed(2)}</span>
                  {i === 0 ? <span className="router-selected-badge">selected</span> : null}
                </div>
              ))}
            </div>
          ) : null}
        </div>

        {/* Squad hints */}
        <div className="form-field">
          <span className="form-label">Squad hints <span className="text-muted text-sm">(optional)</span></span>
          <div className="squad-hints" role="group" aria-label="Squad hints">
            {squads.slice(0, 13).map((sq) => (
              <button
                key={sq.slug}
                type="button"
                className={`squad-chip${selectedSquads.includes(sq.slug) ? ' squad-chip--selected' : ''}`}
                onClick={() => toggleSquad(sq.slug)}
                disabled={!online || busy}
                aria-pressed={selectedSquads.includes(sq.slug)}
                aria-label={`${sq.slug}${sq.name ? ` — ${sq.name}` : ''}`}
              >
                {sq.slug}
                {selectedSquads.includes(sq.slug) ? ' ✕' : ''}
              </button>
            ))}
            {squadsLoading ? <span className="text-muted text-sm">Loading squads…</span> : null}
          </div>
        </div>

        {/* Budget cap */}
        <div className="form-field">
          <label htmlFor="launch-budget" className="form-label">Budget cap (USD)</label>
          <input
            id="launch-budget"
            type="number"
            className="form-input form-input--narrow"
            value={budget}
            onChange={(e) => setBudget(e.target.value)}
            min="1"
            step="1"
            placeholder="80"
            disabled={!online || busy}
            aria-describedby="launch-budget-hint"
          />
          {!budgetOk ? (
            <span className="dlg-warn" role="alert">Budget must be a positive number</span>
          ) : null}
          <span id="launch-budget-hint" className="form-hint text-muted text-sm">
            Leave blank for no limit (not recommended).
          </span>
        </div>

        {/* Mode toggle */}
        <div className="form-field">
          <fieldset className="mode-fieldset">
            <legend className="form-label">Mode</legend>
            <label className={`mode-option${!isLive ? ' mode-option--active' : ''}`}>
              <input
                type="radio"
                name="launch-mode"
                value="dry-run"
                checked={!isLive}
                onChange={() => setIsLive(false)}
                disabled={!online || busy}
                aria-label="Dry-run mode: validate routing and plan, no dispatch"
              />
              <span>
                <strong>Dry-run</strong>{' '}
                <span className="text-muted text-sm">(validate routing + plan, NO dispatch)</span>
              </span>
              <span className="mode-default-badge" aria-hidden="true">default</span>
            </label>
            <label className={`mode-option mode-option--live${isLive ? ' mode-option--active' : ''}`}>
              <input
                type="radio"
                name="launch-mode"
                value="live"
                checked={isLive}
                onChange={() => setIsLive(true)}
                disabled={!online || busy}
                aria-label="Live mode: real dispatch — High-risk write"
              />
              <span>
                <strong>Live</strong>{' '}
                <span className="text-muted text-sm">(real dispatch — High-risk write)</span>
              </span>
              <span className="risk-badge risk-badge--high" aria-label="High risk">⚠</span>
            </label>
          </fieldset>
        </div>

        {/* Actions */}
        <div className="launch-actions">
          <button
            type="button"
            className="btn btn-primary"
            onClick={() => { void handleDryRun(); }}
            disabled={!canLaunch || busy}
            aria-label="Run dry-run: validate routing and plan without dispatching"
          >
            {busy && !isLive ? 'Running…' : 'Dry-run'}
          </button>
          <button
            type="button"
            className="btn btn-danger"
            onClick={() => { void handleLaunchClick(); }}
            disabled={!canLaunch || !isLive || busy}
            aria-label="Launch live workflow (High-risk — requires confirmation)"
          >
            {busy && isLive ? 'Launching…' : 'Launch (live) ⚠'}
          </button>
        </div>
      </form>
    </div>
  );
}
