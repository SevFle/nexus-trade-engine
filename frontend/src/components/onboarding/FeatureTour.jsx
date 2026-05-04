import { useState } from "react";
import { ProgressBar } from "./ProgressBar";
import { SkipForward, ChevronLeft, ChevronRight, Check } from "lucide-react";
import clsx from "clsx";

const STEPS_LABELS = ["Welcome", "Setup", "Tour"];

const TOUR_FEATURES = [
  {
    title: "Dashboard",
    description:
      "Your portfolio at a glance. Monitor value, daily P&L, strategy health, and performance sparklines in real time.",
    area: "dashboard",
  },
  {
    title: "Market Watch",
    description:
      "Live market data feeds. Track instruments, set alerts, and spot opportunities across global markets.",
    area: "market-watch",
  },
  {
    title: "Strategies",
    description:
      "Browse, create, and manage algorithmic trading strategies. Import from the marketplace or build your own.",
    area: "strategies",
  },
  {
    title: "Backtest",
    description:
      "Test strategies against historical data before risking capital. Analyze drawdowns, Sharpe ratios, and more.",
    area: "backtest",
  },
  {
    title: "Risk Monitor",
    description:
      "Real-time risk dashboard. Track exposure, concentration, VaR, and kill-switch status across all positions.",
    area: "risk",
  },
  {
    title: "Settings",
    description:
      "Manage your account, API keys, legal documents, and application preferences from one place.",
    area: "settings",
  },
];

export function FeatureTour({ open, onComplete, onSkip, currentStepIndex }) {
  const [featureIdx, setFeatureIdx] = useState(0);
  const current = TOUR_FEATURES[featureIdx];
  const isFirst = featureIdx === 0;
  const isLast = featureIdx === TOUR_FEATURES.length - 1;

  if (!open) return null;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center"
      role="dialog"
      aria-modal="true"
      aria-label="Feature tour"
    >
      <div className="absolute inset-0 bg-black/80" aria-hidden="true" />
      <div
        className={clsx(
          "relative bg-nx-surface border border-nx-border-visible rounded-2xl p-xl",
          "w-full max-w-lg",
          "max-h-[85vh] overflow-y-auto",
        )}
      >
        <div className="mb-lg">
          <ProgressBar
            steps={STEPS_LABELS}
            currentStepIndex={currentStepIndex}
            className="mb-lg"
          />
          <span className="text-label font-mono uppercase text-nx-text-secondary block mb-xs">
            FEATURE {featureIdx + 1} OF {TOUR_FEATURES.length}
          </span>
          <h2 className="text-heading font-display text-nx-text-display mb-sm">
            {current.title}
          </h2>
          <p className="text-body font-body text-nx-text-primary leading-relaxed">
            {current.description}
          </p>
        </div>

        <div className="flex gap-xs mb-lg">
          {TOUR_FEATURES.map((f, i) => (
            <button
              key={f.area}
              type="button"
              onClick={() => setFeatureIdx(i)}
              className={clsx(
                "w-8 h-1 rounded-full transition-colors",
                i === featureIdx
                  ? "bg-nx-text-display"
                  : i < featureIdx
                    ? "bg-nx-success"
                    : "bg-nx-border"
              )}
              aria-label={`Go to ${f.title}`}
            />
          ))}
        </div>

        <div className="flex items-center justify-between">
          <button
            type="button"
            onClick={onSkip}
            className="nx-btn-ghost flex items-center gap-xs"
          >
            <SkipForward size={14} strokeWidth={1.5} />
            SKIP TOUR
          </button>
          <div className="flex gap-sm">
            {!isFirst && (
              <button
                type="button"
                onClick={() => setFeatureIdx((i) => i - 1)}
                className="nx-btn-secondary flex items-center gap-xs"
              >
                <ChevronLeft size={14} strokeWidth={1.5} />
                BACK
              </button>
            )}
            <button
              type="button"
              onClick={() => {
                if (isLast) {
                  onComplete();
                } else {
                  setFeatureIdx((i) => i + 1);
                }
              }}
              className="nx-btn-primary flex items-center gap-xs"
            >
              {isLast ? (
                <>
                  <Check size={14} strokeWidth={1.5} />
                  FINISH
                </>
              ) : (
                <>
                  NEXT
                  <ChevronRight size={14} strokeWidth={1.5} />
                </>
              )}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
