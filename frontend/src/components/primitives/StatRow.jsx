import clsx from "clsx";

export function StatRow({ label, value, unit, status, className }) {
  const statusColor = {
    success: "text-nx-success",
    warning: "text-nx-warning",
    error: "text-nx-accent",
    neutral: "text-nx-text-primary",
  };

  return (
    <div className={clsx("flex items-baseline justify-between py-sm border-b border-nx-border", className)}>
      <span className="text-label font-mono uppercase text-nx-text-secondary">{label}</span>
      <span className={clsx("text-body-sm font-mono tabular-nums", statusColor[status] || statusColor.neutral)}>
        {value}
        {unit && <span className="text-label ml-xs text-nx-text-disabled">{unit}</span>}
      </span>
    </div>
  );
}
