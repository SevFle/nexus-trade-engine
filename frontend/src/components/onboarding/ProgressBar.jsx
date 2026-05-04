import clsx from "clsx";

export function ProgressBar({ steps, currentStepIndex, className }) {
  const progress = steps.length > 0 ? currentStepIndex / steps.length : 0;
  const pct = Math.round(progress * 100);

  return (
    <div className={clsx("w-full", className)}>
      <div className="flex items-center justify-between mb-xs">
        {steps.map((step, i) => (
          <div
            key={step}
            className={clsx(
              "flex items-center gap-xs",
              i < currentStepIndex && "text-nx-success",
              i === currentStepIndex && "text-nx-text-display",
              i > currentStepIndex && "text-nx-text-disabled",
            )}
          >
            <span
              className={clsx(
                "w-6 h-6 rounded-full flex items-center justify-center text-label font-mono",
                i < currentStepIndex && "bg-nx-success text-nx-black",
                i === currentStepIndex && "border-2 border-nx-text-display text-nx-text-display",
                i > currentStepIndex && "border border-nx-border text-nx-text-disabled",
              )}
            >
              {i < currentStepIndex ? "\u2713" : i + 1}
            </span>
            <span className="text-label font-mono uppercase hidden sm:inline">
              {step}
            </span>
          </div>
        ))}
      </div>
      <div className="w-full h-1 bg-nx-border rounded-full overflow-hidden">
        <div
          className="h-full bg-nx-success rounded-full transition-all duration-300"
          style={{ width: `${pct}%` }}
        />
      </div>
      <div className="mt-xs text-label font-mono uppercase text-nx-text-disabled text-right">
        {pct}%
      </div>
    </div>
  );
}
