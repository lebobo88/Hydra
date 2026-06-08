/**
 * Hydra Cockpit — budget ticker bar.
 * Shows spent/budget with 80% downgrade band and 100% HITL band.
 * Color is NEVER the sole signal — text labels always accompany colors.
 */

import { budgetPct, budgetBand } from '../cockpit/types.ts';

interface BudgetBarProps {
  spent?: number;
  budget?: number;
  /** Compact = single-line bar only (for workflow cards) */
  compact?: boolean;
}

export function BudgetBar({ spent = 0, budget = 0, compact = false }: BudgetBarProps): JSX.Element {
  if (budget <= 0) {
    return <span className="budget-na text-muted text-sm">no budget set</span>;
  }

  const pct = budgetPct(spent, budget);
  const band = budgetBand(pct);

  const barStyle: React.CSSProperties = {
    width: `${Math.min(100, pct)}%`,
  };

  if (compact) {
    return (
      <div className={`budget-bar budget-bar--compact budget-band--${band}`} aria-label={`Budget: $${spent.toFixed(0)} of $${budget} (${pct.toFixed(0)}%)`}>
        <div className="budget-fill" style={barStyle} role="progressbar" aria-valuenow={pct} aria-valuemin={0} aria-valuemax={100} />
        <span className="budget-text-compact">${spent.toFixed(0)} / ${budget} ({pct.toFixed(0)}%)</span>
      </div>
    );
  }

  return (
    <div className={`budget-ticker budget-band--${band}`} aria-label={`Budget ticker: $${spent.toFixed(2)} of $${budget} used (${pct.toFixed(0)}%)`}>
      <div className="budget-bar-track" role="progressbar" aria-valuenow={pct} aria-valuemin={0} aria-valuemax={100}>
        <div className="budget-fill" style={barStyle} />
        {/* 80% marker */}
        <div className="budget-marker budget-marker--warn" style={{ left: '80%' }} aria-hidden="true" />
        {/* 100% marker */}
        <div className="budget-marker budget-marker--critical" style={{ left: '100%' }} aria-hidden="true" />
      </div>
      <div className="budget-labels">
        <span className="budget-spent">${spent.toFixed(2)}</span>
        <span className="budget-sep">/</span>
        <span className="budget-total">${budget}</span>
        <span className={`budget-pct budget-pct--${band}`}>
          {pct.toFixed(0)}%
          {band === 'warn' ? <span className="budget-band-label"> ⚠ 80% — model tier downgrade active</span> : null}
          {band === 'critical' ? <span className="budget-band-label"> ⛔ 100% — HITL gate triggered</span> : null}
        </span>
      </div>
    </div>
  );
}
