import clsx from "clsx";
import { AlertTriangle } from "lucide-react";

export function DisclaimerBanner({ variant = "warning", children, className }) {
  const styles = {
    warning: "bg-nx-warning/10 border-nx-warning/30 text-nx-warning",
    info: "bg-nx-interactive/10 border-nx-interactive/30 text-nx-interactive",
    danger: "bg-nx-accent/10 border-nx-accent/30 text-nx-accent",
  };

  return (
    <div
      role="alert"
      className={clsx(
        "flex items-start gap-md px-lg py-md border rounded-xl text-body-sm font-body",
        styles[variant],
        className
      )}
    >
      <AlertTriangle size={16} className="shrink-0 mt-0.5" aria-hidden="true" />
      <span>{children}</span>
    </div>
  );
}

export function BacktestDisclaimer({ className }) {
  return (
    <DisclaimerBanner variant="warning" className={className}>
      Past performance does not guarantee future results. Backtests are subject to look-ahead and selection bias.
    </DisclaimerBanner>
  );
}

export function PaperTradingDisclaimer({ className }) {
  return (
    <DisclaimerBanner variant="info" className={className}>
      Paper trading results may differ materially from live trading.
    </DisclaimerBanner>
  );
}

export function MarketplaceDisclaimer({ className }) {
  return (
    <DisclaimerBanner variant="danger" className={className}>
      You are running third-party code in your environment. Nexus is not responsible for author-provided strategies.
    </DisclaimerBanner>
  );
}
