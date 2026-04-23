import clsx from "clsx";
import { AlertTriangle } from "lucide-react";

const VARIANTS = {
  warning: "border-nx-warning/30 bg-nx-warning/5 text-nx-warning",
  info: "border-nx-border-visible bg-nx-surface-raised text-nx-text-secondary",
  danger: "border-nx-accent/30 bg-nx-accent/5 text-nx-accent",
};

export function DisclaimerBanner({ children, variant = "warning", className }) {
  return (
    <div
      role="alert"
      className={clsx(
        "flex items-start gap-md px-lg py-md border rounded-2xl text-body-sm font-body",
        VARIANTS[variant],
        className
      )}
    >
      <AlertTriangle size={16} className="shrink-0 mt-xs" strokeWidth={1.5} />
      <span>{children}</span>
    </div>
  );
}
