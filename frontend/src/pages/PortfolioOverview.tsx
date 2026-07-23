/**
 * PortfolioOverview — dashboard page showing the aggregate portfolio summary.
 *
 * Fetches `GET /api/v1/portfolio/summary` via the typed {@link apiClient} and
 * TanStack Query, then renders three summary cards: total portfolio value,
 * P&L, and active strategy count. Loading and error states are first-class —
 * there are no charts yet, just the data cards.
 *
 * The page is intentionally read-only and self-contained: it owns its query
 * (no shared hook) and renders its own skeleton/error UI so a failure here
 * degrades to an inline notice instead of blanking the shell.
 */
import { useQuery } from "@tanstack/react-query";
import {
  AlertTriangle,
  RefreshCw,
  TrendingDown,
  TrendingUp,
  Wallet,
  Zap,
} from "lucide-react";
import clsx from "clsx";

import { apiClient, type PortfolioSummaryData } from "../lib/api";

type Tone = "positive" | "negative" | "neutral";

const currencyFormatter = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  maximumFractionDigits: 2,
});

// signDisplay: "always" surfaces the +/− on the P&L card. The value is a
// percentage already expressed in "percent units" (e.g. 1.11 for 1.11%), so
// divide by 100 before formatting.
const pctFormatter = new Intl.NumberFormat("en-US", {
  style: "percent",
  maximumFractionDigits: 2,
  signDisplay: "always",
});

function formatCurrency(value: number): string {
  return Number.isFinite(value) ? currencyFormatter.format(value) : "—";
}

function formatPct(percent: number): string {
  return Number.isFinite(percent) ? pctFormatter.format(percent / 100) : "—";
}

export default function PortfolioOverview() {
  const { data, isLoading, isError, error, refetch, isFetching } = useQuery({
    queryKey: ["portfolio", "summary"],
    queryFn: () => apiClient.getPortfolioSummary(),
  });

  return (
    <div className="p-xl text-nx-text-primary">
      <div className="mx-auto max-w-7xl">
        <header className="mb-3xl">
          <span className="mb-sm block font-mono text-label uppercase text-nx-text-secondary">
            Dashboard
          </span>
          <h1 className="font-display text-heading text-nx-text-display">
            Portfolio Overview
          </h1>
          <p className="mt-xs font-mono text-caption text-nx-text-disabled">
            {data
              ? `As of ${new Date(data.as_of).toLocaleString("en-US")}`
              : "Live summary of portfolio value, P&L and active strategies."}
          </p>
        </header>

        {isLoading ? (
          <LoadingState />
        ) : isError ? (
          <ErrorState
            message={
              error instanceof Error
                ? error.message
                : "Failed to load portfolio summary."
            }
            onRetry={() => refetch()}
          />
        ) : data ? (
          <SummaryCards data={data} refreshing={isFetching} />
        ) : null}
      </div>
    </div>
  );
}

interface SummaryCardsProps {
  data: PortfolioSummaryData;
  refreshing: boolean;
}

function SummaryCards({ data, refreshing }: SummaryCardsProps) {
  const pnlTone: Tone =
    data.total_pnl > 0
      ? "positive"
      : data.total_pnl < 0
        ? "negative"
        : "neutral";

  return (
    <section
      aria-label="Portfolio summary metrics"
      data-testid="portfolio-summary-cards"
      className="grid grid-cols-1 gap-lg md:grid-cols-3"
    >
      <SummaryCard
        label="Total Portfolio Value"
        value={formatCurrency(data.total_value)}
        icon={<Wallet size={16} strokeWidth={1.5} />}
        caption={`${data.open_positions} open position${
          data.open_positions === 1 ? "" : "s"
        }`}
      />
      <SummaryCard
        label="P&L"
        value={formatCurrency(data.total_pnl)}
        sub={formatPct(data.total_pnl_pct)}
        tone={pnlTone}
        caption="Unrealised"
      />
      <SummaryCard
        label="Active Strategies"
        value={String(data.active_strategies)}
        icon={<Zap size={16} strokeWidth={1.5} />}
        caption={data.currency}
      />
      {refreshing && (
        <span
          className="col-span-full flex items-center gap-xs font-mono text-caption text-nx-text-disabled"
          data-testid="portfolio-summary-refreshing"
        >
          <RefreshCw size={12} strokeWidth={1.5} className="animate-spin" />
          Refreshing…
        </span>
      )}
    </section>
  );
}

interface SummaryCardProps {
  label: string;
  value: string;
  sub?: string;
  tone?: Tone;
  icon?: React.ReactNode;
  caption?: string;
}

function SummaryCard({
  label,
  value,
  sub,
  tone = "neutral",
  icon,
  caption,
}: SummaryCardProps) {
  const TrendIcon =
    tone === "positive"
      ? TrendingUp
      : tone === "negative"
        ? TrendingDown
        : null;

  return (
    <div
      className={clsx(
        "nx-card flex flex-col gap-md",
        "bg-nx-surface-raised",
      )}
      data-testid={`portfolio-card-${label.toLowerCase().replace(/[^a-z]+/g, "-")}`}
    >
      <div className="flex items-center gap-xs text-nx-text-secondary">
        {icon}
        <span className="font-mono text-label uppercase tracking-wider">
          {label}
        </span>
      </div>
      <div className="flex items-baseline gap-xs">
        {TrendIcon && (
          <TrendIcon
            size={16}
            strokeWidth={1.75}
            className={clsx(
              tone === "positive" && "text-nx-success",
              tone === "negative" && "text-nx-accent",
            )}
          />
        )}
        <span
          className={clsx(
            "font-display text-display-md tabular-nums",
            tone === "positive" && "text-nx-success",
            tone === "negative" && "text-nx-accent",
            tone === "neutral" && "text-nx-text-display",
          )}
        >
          {value}
        </span>
        {sub && (
          <span
            className={clsx(
              "font-mono text-body tabular-nums",
              tone === "positive" && "text-nx-success",
              tone === "negative" && "text-nx-accent",
              tone === "neutral" && "text-nx-text-secondary",
            )}
          >
            {sub}
          </span>
        )}
      </div>
      {caption && (
        <span className="font-mono text-caption uppercase tracking-wider text-nx-text-disabled">
          {caption}
        </span>
      )}
    </div>
  );
}

function LoadingState() {
  return (
    <section
      aria-label="Portfolio summary loading"
      aria-busy="true"
      data-testid="portfolio-summary-loading"
      className="grid grid-cols-1 gap-lg md:grid-cols-3"
    >
      {[0, 1, 2].map((i) => (
        <div
          key={i}
          className="nx-card flex animate-pulse flex-col gap-md bg-nx-surface-raised"
        >
          <div className="h-3 w-1/2 rounded-full bg-nx-border-visible" />
          <div className="h-8 w-3/4 rounded-full bg-nx-border" />
          <div className="h-3 w-1/4 rounded-full bg-nx-border" />
        </div>
      ))}
    </section>
  );
}

interface ErrorStateProps {
  message: string;
  onRetry: () => void;
}

function ErrorState({ message, onRetry }: ErrorStateProps) {
  return (
    <section
      role="alert"
      aria-label="Portfolio summary error"
      data-testid="portfolio-summary-error"
      className="flex flex-col items-start gap-md rounded-lg border border-nx-accent/40 bg-nx-surface-raised p-lg"
    >
      <div className="flex items-center gap-xs text-nx-accent">
        <AlertTriangle size={16} strokeWidth={1.75} />
        <span className="font-mono text-label uppercase tracking-wider">
          Couldn&apos;t load portfolio summary
        </span>
      </div>
      <p className="font-mono text-body-sm text-nx-text-secondary">{message}</p>
      <button
        type="button"
        onClick={onRetry}
        className="nx-btn-secondary"
      >
        <RefreshCw size={14} strokeWidth={1.75} className="mr-xs" />
        Retry
      </button>
    </section>
  );
}
