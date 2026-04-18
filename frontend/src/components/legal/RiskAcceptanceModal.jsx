import { useState, useCallback } from "react";
import { useAcceptLegal } from "../../hooks/useLegal";

export function RiskAcceptanceModal({ documentSlug, version, onAccepted, onCancel }) {
  const [checked, setChecked] = useState(false);
  const acceptMutation = useAcceptLegal();

  const handleAccept = useCallback(() => {
    if (!checked) return;
    acceptMutation.mutate(
      [{ document_slug: documentSlug, version }],
      { onSuccess: () => onAccepted?.() }
    );
  }, [checked, acceptMutation, documentSlug, version, onAccepted]);

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label="Live trading risk acceptance"
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/80"
    >
      <div className="bg-nx-surface border border-nx-accent/30 rounded-2xl w-full max-w-lg p-xl">
        <h2 className="text-heading font-display text-nx-accent mb-lg">
          LIVE TRADING RISK ACKNOWLEDGMENT
        </h2>

        <div className="text-body-sm font-body text-nx-text-primary space-y-md mb-xl">
          <p>
            You are about to enable <strong>live trading</strong>. Real money will be at risk.
            Strategies may perform differently in live markets compared to backtests and paper trading.
          </p>
          <p>
            You acknowledge that trading involves substantial risk of loss and is not suitable for all investors.
            Past performance is not indicative of future results.
          </p>
        </div>

        <label className="flex items-start gap-md cursor-pointer mb-xl">
          <input
            type="checkbox"
            checked={checked}
            onChange={(e) => setChecked(e.target.checked)}
            className="mt-1 w-4 h-4 rounded border-nx-border-visible bg-nx-surface accent-nx-accent"
            aria-label="I accept the risks of live trading"
          />
          <span className="text-body-sm font-body text-nx-text-primary">
            I understand the risks involved and accept full responsibility for live trading outcomes.
          </span>
        </label>

        <div className="flex gap-md">
          <button
            type="button"
            onClick={onCancel}
            className="flex-1 px-lg py-md text-label font-mono uppercase rounded-full border border-nx-border text-nx-text-secondary hover:border-nx-border-visible transition-colors"
          >
            CANCEL
          </button>
          <button
            type="button"
            onClick={handleAccept}
            disabled={!checked || acceptMutation.isPending}
            className={`flex-1 px-lg py-md text-label font-mono uppercase rounded-full border transition-colors ${
              checked && !acceptMutation.isPending
                ? "bg-nx-accent text-white border-nx-accent hover:opacity-90"
                : "bg-nx-text-disabled/20 text-nx-text-disabled border-nx-border cursor-not-allowed"
            }`}
          >
            {acceptMutation.isPending ? "ACCEPTING..." : "I ACCEPT THIS RISK"}
          </button>
        </div>
      </div>
    </div>
  );
}
