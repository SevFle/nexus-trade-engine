import clsx from "clsx";

export function HeroMetric({ value, unit, label, status, className }) {
  return (
    <div className={clsx("flex flex-col gap-xs", className)}>
      {label && (
        <span className="text-label font-mono uppercase text-nx-text-secondary">{label}</span>
      )}
      <div className="flex items-baseline gap-xs">
        <span
          className={clsx(
            "text-display-xl font-display tabular-nums",
            status === "success" && "text-nx-success",
            status === "error" && "text-nx-accent",
            (!status || status === "neutral") && "text-nx-text-display",
          )}
        >
          {value}
        </span>
        {unit && <span className="text-label font-mono uppercase text-nx-text-secondary">{unit}</span>}
      </div>
    </div>
  );
}
