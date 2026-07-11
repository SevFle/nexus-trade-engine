import { Wallet, TrendingUp, TrendingDown } from "lucide-react";
import clsx from "clsx";

type Tone = "positive" | "negative" | "neutral";

export interface PortfolioSummaryProps {
  /** Total portfolio value, e.g. "$2,847,391.44". */
  totalValue?: string;
  /** Realised + unrealised day P&L, e.g. "+$31,204.18". */
  dayPnl?: string;
  /** Day P&L expressed as a percentage, e.g. "+1.11%". */
  dayPnlPct?: string;
  /**
   * Numeric P&L direction used to tone the Day P&L metric. Positive
   * values render an upward/success tone, negative values a downward/
   * accent tone, and zero (or undefined) a neutral tone.
   *
   * This is preferred over sniffing a "+"/"-" prefix off `dayPnl`, which
   * breaks for locales that format signed currency differently and for
   * negative-zero (`-0`) strings. Pass the raw signed delta here and the
   * formatted, locale-aware string via `dayPnl`.
   */
  pnlDirection?: number;
  /** Count of currently open positions. */
  openPositions?: number;
  /** Overall strategy-health score (0–100). */
  healthScore?: number;
  /**
   * Marks the card as showing placeholder data. Dims the surface slightly and
   * renders a "placeholder" tag so it is visually obvious the values are not
   * yet backed by the portfolio API.
   */
  placeholder?: boolean;
  /** Optional extra classes for the root <section>. */
  className?: string;
}

/**
 * PortfolioSummary — compact summary card surfaced in the app shell's main
 * content area.
 *
 * This is a scaffold placeholder: it renders sensible defaults and is ready to
 * be fed real data from the portfolio endpoint via TanStack Query once the
 * backend integration lands.
 */
export function PortfolioSummary({
  totalValue = "—",
  dayPnl = "—",
  dayPnlPct = "—",
  openPositions = 0,
  healthScore,
  pnlDirection,
  placeholder = true,
  className,
}: PortfolioSummaryProps) {
  // Tone is derived from the numeric P&L direction rather than from a
  // string prefix on `dayPnl`, so locale-specific formatting of the
  // currency string can't flip the colour by accident.
  const pnlTone: Tone =
    placeholder || pnlDirection == null
      ? "neutral"
      : pnlDirection > 0
        ? "positive"
        : pnlDirection < 0
          ? "negative"
          : "neutral";

  return (
    <section
      className={clsx(
        "mx-xl mt-md rounded-lg border border-nx-border bg-nx-surface-raised p-lg",
        placeholder && "opacity-80",
        className,
      )}
      aria-label="Portfolio summary"
      data-testid="portfolio-summary"
    >
      <div className="flex items-center gap-sm">
        <Wallet size={16} strokeWidth={1.5} className="text-nx-text-secondary" />
        <h2 className="text-label font-mono uppercase tracking-widest text-nx-text-secondary">
          Portfolio Summary
        </h2>
        {placeholder && (
          <span className="rounded-full border border-nx-border-visible px-xs text-caption font-mono uppercase text-nx-text-disabled">
            placeholder
          </span>
        )}
      </div>

      <div className="mt-md grid grid-cols-2 gap-lg md:grid-cols-4">
        <SummaryMetric label="Total Value" value={totalValue} />
        <SummaryMetric
          label="Day P&L"
          value={dayPnl}
          sub={dayPnlPct}
          tone={pnlTone}
        />
        <SummaryMetric label="Open Positions" value={String(openPositions)} />
        <SummaryMetric
          label="Health"
          value={healthScore != null ? String(healthScore) : "—"}
        />
      </div>
    </section>
  );
}

interface SummaryMetricProps {
  label: string;
  value: string;
  sub?: string;
  tone?: Tone;
}

function SummaryMetric({
  label,
  value,
  sub,
  tone = "neutral",
}: SummaryMetricProps) {
  const TrendIcon =
    tone === "positive" ? TrendingUp : tone === "negative" ? TrendingDown : null;

  return (
    <div className="flex flex-col gap-2xs">
      <span className="text-caption font-mono uppercase tracking-wider text-nx-text-disabled">
        {label}
      </span>
      <div className="flex items-baseline gap-xs">
        {TrendIcon && (
          <TrendIcon
            size={14}
            strokeWidth={1.5}
            className={clsx(
              tone === "positive" && "text-nx-success",
              tone === "negative" && "text-nx-accent",
            )}
          />
        )}
        <span className="font-mono text-subheading text-nx-text-display">
          {value}
        </span>
        {sub && (
          <span className="text-caption font-mono text-nx-text-secondary">
            {sub}
          </span>
        )}
      </div>
    </div>
  );
}

export default PortfolioSummary;
