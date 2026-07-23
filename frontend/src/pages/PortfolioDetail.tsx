/**
 * PortfolioDetail — per-portfolio dashboard page with performance charts.
 *
 * Fetches `GET /api/v1/portfolio/{id}` via the typed {@link apiClient} and
 * TanStack Query, then renders:
 *   - a header with name, description, initial capital and created date
 *   - an equity-over-time Recharts {@link AreaChart} (the "equity curve")
 *   - a current-allocation Recharts {@link PieChart} with a legend table
 *
 * Loading and error states are first-class — the page owns its query (no
 * shared hook) and renders its own skeleton/error UI so a data or render
 * failure here degrades to an inline notice instead of blanking the shell.
 *
 * The portfolio detail endpoint currently returns metadata only, so the
 * optional `equity_curve` / `allocations` fields may be absent. The chart
 * sections fall back to empty states until the backend exposes them; the
 * header metrics always render from the core portfolio fields.
 */
import { useQuery } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";
import {
  Area,
  AreaChart,
  Cell,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import {
  AlertTriangle,
  ArrowLeft,
  CalendarDays,
  Layers,
  RefreshCw,
  Wallet,
} from "lucide-react";
import clsx from "clsx";

import { apiClient, type Portfolio, type AllocationSlice } from "../lib/api";

const currencyFormatter = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  maximumFractionDigits: 2,
});

function formatCurrency(value: number): string {
  return Number.isFinite(value) ? currencyFormatter.format(value) : "—";
}

function formatCompactCurrency(value: number): string {
  if (!Number.isFinite(value)) return "—";
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    notation: "compact",
    maximumFractionDigits: 1,
  }).format(value);
}

function formatAxisDate(value: unknown): string {
  if (typeof value !== "string") return "";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return value;
  return d.toLocaleDateString("en-US", { month: "short", day: "numeric" });
}

// Distinct, accessible palette cycled across allocation slices. Kept short
// on purpose — a portfolio rarely holds more than ~10 positions and a long
// palette would be indistinguishable on the pie.
const ALLOCATION_COLORS = [
  "#60a5fa",
  "#34d399",
  "#fbbf24",
  "#f87171",
  "#a78bfa",
  "#22d3ee",
  "#fb923c",
  "#4ade80",
  "#e879f9",
  "#94a3b8",
];

function colorFor(index: number): string {
  return ALLOCATION_COLORS[index % ALLOCATION_COLORS.length];
}

export default function PortfolioDetail() {
  const { id } = useParams<{ id: string }>();

  const { data, isLoading, isError, error, refetch, isFetching } = useQuery({
    queryKey: ["portfolio", "detail", id],
    queryFn: () => apiClient.getPortfolio(id as string),
    // The route guard ensures `id` is present; guard defensively anyway so
    // a render without a param doesn't fire a request against "/api/v1/portfolio/undefined".
    enabled: Boolean(id),
  });

  return (
    <div className="p-xl text-nx-text-primary">
      <div className="mx-auto max-w-7xl">
        <Link
          to="/portfolio"
          className="mb-md inline-flex items-center gap-xs font-mono text-label uppercase text-nx-text-secondary hover:text-nx-text-primary"
          data-testid="portfolio-detail-back"
        >
          <ArrowLeft size={14} strokeWidth={1.75} />
          Back to overview
        </Link>

        {isLoading ? (
          <LoadingState />
        ) : isError ? (
          <ErrorState
            message={
              error instanceof Error
                ? error.message
                : "Failed to load portfolio."
            }
            onRetry={() => refetch()}
          />
        ) : data ? (
          <PortfolioBody data={data} refreshing={isFetching} />
        ) : null}
      </div>
    </div>
  );
}

interface PortfolioBodyProps {
  data: Portfolio;
  refreshing: boolean;
}

function PortfolioBody({ data, refreshing }: PortfolioBodyProps) {
  const created = new Date(data.created_at);
  const createdLabel = Number.isNaN(created.getTime())
    ? data.created_at
    : created.toLocaleDateString("en-US", {
        year: "numeric",
        month: "short",
        day: "numeric",
      });

  const totalAllocated =
    data.allocations?.reduce(
      (sum, slice) => sum + (Number.isFinite(slice.value) ? slice.value : 0),
      0,
    ) ?? 0;

  return (
    <>
      <header className="mb-3xl">
        <span className="mb-sm block font-mono text-label uppercase text-nx-text-secondary">
          Portfolio
        </span>
        <h1 className="font-display text-heading text-nx-text-display">
          {data.name}
        </h1>
        {data.description && (
          <p className="mt-xs font-mono text-body-sm text-nx-text-secondary">
            {data.description}
          </p>
        )}
      </header>

      <section
        aria-label="Portfolio detail metrics"
        data-testid="portfolio-detail-metrics"
        className="mb-3xl grid grid-cols-1 gap-lg sm:grid-cols-3"
      >
        <Metric
          label="Initial Capital"
          value={formatCurrency(data.initial_capital)}
          icon={<Wallet size={16} strokeWidth={1.5} />}
          caption="Deployed at creation"
        />
        <Metric
          label="Created"
          value={createdLabel}
          icon={<CalendarDays size={16} strokeWidth={1.5} />}
        />
        <Metric
          label="Holdings"
          value={String(data.allocations?.length ?? 0)}
          icon={<Layers size={16} strokeWidth={1.5} />}
          caption={
            totalAllocated > 0
              ? `${formatCurrency(totalAllocated)} allocated`
              : "No allocation data"
          }
        />
        {refreshing && (
          <span
            className="col-span-full flex items-center gap-xs font-mono text-caption text-nx-text-disabled"
            data-testid="portfolio-detail-refreshing"
          >
            <RefreshCw size={12} strokeWidth={1.5} className="animate-spin" />
            Refreshing…
          </span>
        )}
      </section>

      <div className="grid grid-cols-1 gap-3xl lg:grid-cols-3">
        <div className="lg:col-span-2">
          <EquityCurveCard data={data} />
        </div>
        <AllocationCard allocations={data.allocations ?? []} />
      </div>
    </>
  );
}

interface MetricProps {
  label: string;
  value: string;
  icon?: React.ReactNode;
  caption?: string;
}

function Metric({ label, value, icon, caption }: MetricProps) {
  return (
    <div
      className="nx-card flex flex-col gap-md bg-nx-surface-raised"
      data-testid={`portfolio-detail-metric-${label
        .toLowerCase()
        .replace(/[^a-z]+/g, "-")}`}
    >
      <div className="flex items-center gap-xs text-nx-text-secondary">
        {icon}
        <span className="font-mono text-label uppercase tracking-wider">
          {label}
        </span>
      </div>
      <span className="font-display text-display-md tabular-nums text-nx-text-display">
        {value}
      </span>
      {caption && (
        <span className="font-mono text-caption uppercase tracking-wider text-nx-text-disabled">
          {caption}
        </span>
      )}
    </div>
  );
}

interface EquityCurveCardProps {
  data: Portfolio;
}

function EquityCurveCard({ data }: EquityCurveCardProps) {
  const series = data.equity_curve ?? [];

  const { latest, change, changePct } = (() => {
    if (series.length < 2) {
      return { latest: null, change: null, changePct: null };
    }
    const first = series[0].equity;
    const last = series[series.length - 1].equity;
    const delta = last - first;
    const pct = first !== 0 ? (delta / first) * 100 : 0;
    return { latest: last, change: delta, changePct: pct };
  })();

  const positive = (change ?? 0) >= 0;

  return (
    <section
      aria-label="Portfolio equity curve"
      className="nx-card flex h-full flex-col gap-lg bg-nx-surface-raised"
      data-testid="portfolio-equity-curve"
    >
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-xs text-nx-text-secondary">
          <Wallet size={16} strokeWidth={1.5} />
          <span className="font-mono text-label uppercase tracking-wider">
            Equity Curve
          </span>
        </div>
        {latest != null && change != null && changePct != null && (
          <div className="flex items-baseline gap-xs">
            <span className="font-display text-subheading tabular-nums text-nx-text-display">
              {formatCurrency(latest)}
            </span>
            <span
              className={clsx(
                "font-mono text-body tabular-nums",
                positive ? "text-nx-success" : "text-nx-accent",
              )}
              data-testid="portfolio-equity-change"
            >
              {positive ? "+" : ""}
              {formatCompactCurrency(change)} ({positive ? "+" : ""}
              {changePct.toFixed(2)}%)
            </span>
          </div>
        )}
      </div>

      {series.length < 2 ? (
        <EmptyChartState
          height={300}
          label="Equity curve data not available"
        />
      ) : (
        <div style={{ height: 300 }} data-testid="portfolio-equity-chart">
          <ResponsiveContainer width="100%" height="100%">
            <AreaChart
              data={series}
              margin={{ top: 8, right: 16, left: 0, bottom: 0 }}
            >
              <defs>
                <linearGradient id="equityGradient" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor="#60a5fa" stopOpacity={0.4} />
                  <stop offset="100%" stopColor="#60a5fa" stopOpacity={0.02} />
                </linearGradient>
              </defs>
              <XAxis
                dataKey="timestamp"
                tickFormatter={formatAxisDate}
                stroke="var(--text-disabled, #6b7280)"
                fontSize={11}
                tickLine={false}
                axisLine={false}
                minTickGap={32}
              />
              <YAxis
                stroke="var(--text-disabled, #6b7280)"
                fontSize={11}
                tickLine={false}
                axisLine={false}
                width={64}
                tickFormatter={formatCompactCurrency}
                domain={["dataMin", "dataMax"]}
              />
              <Tooltip
                content={<EquityTooltip />}
                cursor={{ stroke: "var(--border, #374151)", strokeWidth: 1 }}
              />
              <Area
                type="monotone"
                dataKey="equity"
                stroke="#60a5fa"
                strokeWidth={1.75}
                fill="url(#equityGradient)"
                isAnimationActive={false}
                dot={false}
              />
            </AreaChart>
          </ResponsiveContainer>
        </div>
      )}
    </section>
  );
}

interface EquityTooltipProps {
  active?: boolean;
  payload?: Array<{ payload?: { timestamp?: string; equity?: number } }>;
}

function EquityTooltip({ active, payload }: EquityTooltipProps) {
  if (!active || !payload || payload.length === 0) return null;
  const row = payload[0]?.payload;
  if (!row) return null;
  const d = new Date(row.timestamp ?? "");
  const when = Number.isNaN(d.getTime())
    ? row.timestamp
    : d.toLocaleString("en-US");
  return (
    <div className="rounded-md border border-nx-border bg-nx-surface p-sm font-mono text-caption text-nx-text-primary">
      <div className="mb-xs text-nx-text-secondary">{when}</div>
      <div className="tabular-nums">{formatCurrency(row.equity ?? 0)}</div>
    </div>
  );
}

interface AllocationCardProps {
  allocations: AllocationSlice[];
}

function AllocationCard({ allocations }: AllocationCardProps) {
  const total =
    allocations.reduce(
      (sum, slice) => sum + (Number.isFinite(slice.value) ? slice.value : 0),
      0,
    ) ?? 0;

  return (
    <section
      aria-label="Portfolio allocation"
      className="nx-card flex h-full flex-col gap-lg bg-nx-surface-raised"
      data-testid="portfolio-allocation"
    >
      <div className="flex items-center gap-xs text-nx-text-secondary">
        <Layers size={16} strokeWidth={1.5} />
        <span className="font-mono text-label uppercase tracking-wider">
          Allocation
        </span>
      </div>

      {allocations.length === 0 || total <= 0 ? (
        <EmptyChartState height={300} label="No open allocations" />
      ) : (
        <>
          <div
            style={{ height: 220 }}
            data-testid="portfolio-allocation-chart"
          >
            <ResponsiveContainer width="100%" height="100%">
              <PieChart>
                <Pie
                  data={allocations}
                  dataKey="value"
                  nameKey="name"
                  cx="50%"
                  cy="50%"
                  innerRadius={48}
                  outerRadius={88}
                  paddingAngle={2}
                  isAnimationActive={false}
                  stroke="var(--surface-raised, #0f172a)"
                  strokeWidth={2}
                >
                  {allocations.map((slice, i) => (
                    <Cell key={slice.name} fill={colorFor(i)} />
                  ))}
                </Pie>
                <Tooltip content={<AllocationTooltip total={total} />} />
              </PieChart>
            </ResponsiveContainer>
          </div>
          <ul
            className="flex flex-col gap-sm"
            data-testid="portfolio-allocation-legend"
          >
            {allocations.map((slice, i) => {
              const weight = total !== 0 ? (slice.value / total) * 100 : 0;
              return (
                <li
                  key={slice.name}
                  className="flex items-center justify-between gap-sm"
                  data-testid={`portfolio-allocation-row-${slice.name
                    .toLowerCase()
                    .replace(/[^a-z0-9]+/g, "-")}`}
                >
                  <span className="flex items-center gap-xs">
                    <span
                      className="inline-block h-xs w-sm rounded-full"
                      style={{ backgroundColor: colorFor(i) }}
                      aria-hidden="true"
                    />
                    <span className="font-mono text-body-sm text-nx-text-primary">
                      {slice.name}
                    </span>
                  </span>
                  <span className="flex items-baseline gap-xs tabular-nums">
                    <span className="font-mono text-body-sm text-nx-text-secondary">
                      {formatCurrency(slice.value)}
                    </span>
                    <span className="font-mono text-caption text-nx-text-disabled">
                      {weight.toFixed(1)}%
                    </span>
                  </span>
                </li>
              );
            })}
          </ul>
        </>
      )}
    </section>
  );
}

interface AllocationTooltipProps {
  active?: boolean;
  payload?: Array<{ payload?: { name?: string; value?: number } }>;
  total: number;
}

function AllocationTooltip({ active, payload, total }: AllocationTooltipProps) {
  if (!active || !payload || payload.length === 0) return null;
  const row = payload[0]?.payload;
  if (!row) return null;
  const weight = total !== 0 && row.value != null ? (row.value / total) * 100 : 0;
  return (
    <div className="rounded-md border border-nx-border bg-nx-surface p-sm font-mono text-caption text-nx-text-primary">
      <div className="mb-xs text-nx-text-secondary">{row.name}</div>
      <div className="tabular-nums">
        {formatCurrency(row.value ?? 0)} ({weight.toFixed(1)}%)
      </div>
    </div>
  );
}

function EmptyChartState({
  height,
  label,
}: {
  height: number;
  label: string;
}) {
  return (
    <div
      className="flex items-center justify-center rounded-lg border border-nx-border bg-nx-surface text-nx-text-disabled font-mono text-label uppercase"
      style={{ height }}
      data-testid="portfolio-chart-empty"
    >
      {label}
    </div>
  );
}

function LoadingState() {
  return (
    <section
      aria-label="Portfolio detail loading"
      aria-busy="true"
      data-testid="portfolio-detail-loading"
      className="flex flex-col gap-3xl"
    >
      <div className="grid grid-cols-1 gap-lg sm:grid-cols-3">
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
      </div>
      <div className="nx-card h-[360px] animate-pulse rounded-lg bg-nx-surface-raised" />
      <div className="nx-card h-[360px] animate-pulse rounded-lg bg-nx-surface-raised" />
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
      aria-label="Portfolio detail error"
      data-testid="portfolio-detail-error"
      className="flex flex-col items-start gap-md rounded-lg border border-nx-accent/40 bg-nx-surface-raised p-lg"
    >
      <div className="flex items-center gap-xs text-nx-accent">
        <AlertTriangle size={16} strokeWidth={1.75} />
        <span className="font-mono text-label uppercase tracking-wider">
          Couldn&apos;t load portfolio
        </span>
      </div>
      <p className="font-mono text-body-sm text-nx-text-secondary">{message}</p>
      <button type="button" onClick={onRetry} className="nx-btn-secondary">
        <RefreshCw size={14} strokeWidth={1.75} className="mr-xs" />
        Retry
      </button>
    </section>
  );
}
