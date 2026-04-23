import { useState } from "react";
import { Modal } from "../feedback/Modal";
import { useAcceptLegal } from "../../hooks/useLegal";

export function LiveTradingModal({ open, onAccept, onClose }) {
  const [checked, setChecked] = useState(false);
  const acceptMutation = useAcceptLegal();

  const handleAccept = async () => {
    await acceptMutation.mutateAsync([
      { document_slug: "risk-disclaimer", version: "latest" },
    ]);
    onAccept();
  };

  return (
    <Modal open={open} onClose={onClose} title="LIVE TRADING RISK ACKNOWLEDGMENT">
      <div className="space-y-lg">
        <p className="text-body-sm font-body text-nx-text-primary">
          You are about to engage in live trading with real capital. Live
          trading carries significant financial risk, including the possibility
          of losing your entire investment.
        </p>

        <div className="bg-nx-accent/5 border border-nx-accent/30 rounded-2xl p-lg text-body-sm font-body text-nx-text-primary">
          <ul className="list-disc pl-md space-y-xs">
            <li>Past performance does not guarantee future results.</li>
            <li>Algorithmic trading strategies may fail unexpectedly.</li>
            <li>Market conditions can change rapidly, causing significant losses.</li>
            <li>Slippage, latency, and system failures may impact execution.</li>
          </ul>
        </div>

        <label className="flex items-center gap-md cursor-pointer select-none">
          <input
            type="checkbox"
            checked={checked}
            onChange={(e) => setChecked(e.target.checked)}
            className="w-4 h-4 rounded border-nx-border accent-nx-accent"
          />
          <span className="text-body-sm font-body text-nx-text-primary">
            I understand the risks of live trading and accept full responsibility
            for any losses incurred.
          </span>
        </label>

        <div className="flex gap-md">
          <button
            type="button"
            onClick={onClose}
            className="flex-1 px-xl py-md text-label font-mono uppercase border border-nx-border rounded-full text-nx-text-secondary hover:border-nx-border-visible transition-colors"
          >
            CANCEL
          </button>
          <button
            type="button"
            disabled={!checked}
            onClick={handleAccept}
            className={`flex-1 px-xl py-md text-label font-mono uppercase rounded-full border transition-colors ${
              checked
                ? "bg-nx-accent text-nx-black border-nx-accent hover:bg-nx-accent/80"
                : "bg-nx-text-disabled/30 text-nx-text-disabled border-nx-text-disabled/30 cursor-not-allowed"
            }`}
          >
            BEGIN LIVE TRADING
          </button>
        </div>
      </div>
    </Modal>
  );
}
