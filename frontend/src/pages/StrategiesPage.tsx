/**
 * StrategiesPage — dashboard listing of every strategy registered with the
 * Nexus engine.
 *
 * Fetches `GET /api/v1/strategies/` via the typed {@link apiClient} and
 * TanStack Query, then renders a responsive table of name, description,
 * runtime status, and P&L. Loading (skeleton), error (retry), and empty
 * states are first-class so a slow or absent backend degrades to an inline
 * notice instead of blanking the shell.
 *
 * The engine list endpoint is permitted to return either rich entry objects
 * (the documented shape — see {@link StrategySummary}) or bare identifier
 * strings (the minimal legacy registry). Both are normalized into
 * {@link StrategyRow} before rendering, so the page never crashes on a
 * schema mismatch.
 */
import { useQuery } from "@tanstack/react-query";
import { AlertTriangle, RefreshCw } from "lucide-react";
import clsx from "clsx";

import { apiClient, type StrategySummary } from "../lib/api";

// ---------------------------------------------------------------------------
// Formatting helpers
// ---------------------------------------------------------------------------

const currencyFormatter = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  maximumFractionDigits: 2,
});

// signDisplay: "always" surfaces the +/− on the P&L cell. The value is a
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

// ---------------------------------------------------------------------------
// Normalization
// ---------------------------------------------------------------------------

/** Tokens the status badge knows how to color. */
type StatusToken = "active" | "idle" | "error" | "unknown";

/** A normalized, render-ready strategy row. */
interface StrategyRow {
  id: string;
  name: string;
  description: string;
  status: StatusToken;
  statusLabel: string;
  pnl: number | null;
  pnlPct: number | null;
}

const ACTIVE_TOKENS = new Set([
  "active",
  "loaded",
  "running",
  "live",
  "ok",
  "healthy",
]);
const ERROR_TOKENS = new Set([
  "error",
  "failed",
  "crashed",
  "unhealthy",
  "stopped",
]);
const IDLE_TOKENS = new Set([
  "idle",
  "available",
  "paused",
  "inactive",
  "pending",
]);

function classifyStatus(token: string | undefined, isLoaded?: boolean): {
  status: StatusToken;
  label: string;
} {
  if (token) {
    const lower = token.toLowerCase();
    if (ACTIVE_TOKENS.has(lower)) return { status: "active", label: "Active" };
    if (ERROR_TOKENS.has(lower)) return { status: "error", label: "Error" };
    if (IDLE_TOKENS.has(lower)) return { status: "idle", label: "Available" };
  }
  // Fall back to the is_loaded flag when no explicit status is provided.
  if (isLoaded) return { status: "active", label: "Active" };
  if (isLoaded === false) return { status: "idle", label: "Available" };
  return { status: "unknown", label: "Unknown" };
}

function toNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

/**
 * Normalize a single list entry into a {@link StrategyRow}.
 *
 * Handles three shapes defensively:
 *   - a bare id string (minimal legacy registry: `list_all()` -> `str[]`)
 *   - a rich object matching {@link StrategySummary}
 *   - an object with extra/unknown keys (forward-compatible)
 */
function normalizeStrategy(raw: unknown): StrategyRow {
  if (typeof raw === "string") {
    const { status, label } = classifyStatus(undefined);
    return {
      id: raw,
      name: raw,
      description: "",
      status,
      statusLabel: label,
      pnl: null,
      pnlPct: null,
    };
  }

  const entry = (raw ?? {}) as Partial<StrategySummary>;
  const id =
    typeof entry.id === "string" && entry.id.length > 0
      ? entry.id
      : typeof entry.name === "string" && entry.name.length > 0
        ? entry.name
        : "unknown";
  const name = typeof entry.name === "string" ? entry.name : id;
  const description =
    typeof entry.description === "string" ? entry.description : "";
  const { status, label } = classifyStatus(entry.status, entry.is_loaded);

  return {
    id,
    name,
    description,
    status,
    statusLabel: label,
    pnl: toNumber(entry.pnl),
    pnlPct: toNumber(entry.pnl_pct),
  };
}

// ---------------------------------------------------------------------------
// Status badge
// ---------------------------------------------------------------------------

interface StatusBadgeProps {
  status: StatusToken;
  label: string;
}

const STATUS_BADGE_STYLES: Record<StatusToken, string> = {
  active: "bg-nx-success/15 text-nx-success border-nx-success/40",
  idle: "bg-nx-border-visible/30 text-nx-text-secondary border-nx-border-visible",
  error: "bg-nx-accent/15 text-nx-accent border-nx-accent/40",
  unknown: "bg-nx-border text-nx-text-disabled border-nx-border-visible",
};

function StatusBadge({ status, label }: StatusBadgeProps) {
  return (
    <span
      className={clsx(
        "inline-flex items-center gap-xs rounded-full border px-sm py-2xs",
        "font-mono text-label uppercase tracking-wider",
        STATUS_BADGE_STYLES[status],
      )}
    >
      <span
        className={clsx(
          "h-1.5 w-1.5 rounded-full",
          status === "active" && "bg-nx-success",
          status === "idle" && "bg-nx-text-secondary",
          status === "error" && "bg-nx-accent",
          status === "unknown" && "bg-nx-text-disabled",
        )}
        aria-hidden="true"
      />
      {label}
    </span>
  );
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export default function StrategiesPage() {
  const { data, isLoading, isError, error, refetch, isFetching } = useQuery({
    queryKey: ["strategies", "list"],
    queryFn: () => apiClient.listStrategies(),
  });

  const rows: StrategyRow[] = data
    ? data.strategies.map(normalizeStrategy)
    : [];

  return (
    <div className="p-xl text-nx-text-primary">
      <div className="mx-auto max-w-7xl">
        <header className="mb-3xl">
          <span className="mb-sm block font-mono text-label uppercase text-nx-text-secondary">
            Strategies
          </span>
          <h1 className="font-display text-heading text-nx-text-display">
            Registered Strategies
          </h1>
          <p className="mt-xs font-mono text-caption text-nx-text-disabled">
            All strategies installed in the engine, with live status and P&amp;L.
          </p>
        </header>

        {isLoading ? (
          <LoadingState />
        ) : isError ? (
          <ErrorState
            message={
              error instanceof Error
                ? error.message
                : "Failed to load strategies."
            }
            onRetry={() => refetch()}
          />
        ) : rows.length === 0 ? (
          <EmptyState />
        ) : (
          <StrategiesTable rows={rows} refreshing={isFetching} />
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Table
// ---------------------------------------------------------------------------

interface StrategiesTableProps {
  rows: StrategyRow[];
  refreshing: boolean;
}

function StrategiesTable({ rows, refreshing }: StrategiesTableProps) {
  return (
    <section
      aria-label="Registered strategies"
      data-testid="strategies-table-section"
    >
      <div
        className="overflow-x-auto rounded-2xl border border-nx-border bg-nx-surface"
        data-testid="strategies-table"
      >
        <table className="w-full min-w-[640px] border-collapse text-left">
          <caption className="sr-only">
            Registered strategies with name, description, status, and P&amp;L.
          </caption>
          <thead className="border-b border-nx-border bg-nx-surface-raised">
            <tr>
              <Th>Name</Th>
              <Th>Description</Th>
              <Th>Status</Th>
              <Th align="right">P&amp;L</Th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <StrategyTableRow key={row.id} row={row} />
            ))}
          </tbody>
        </table>
      </div>

      <div className="mt-md flex items-center justify-between">
        <span className="font-mono text-caption text-nx-text-disabled">
          {rows.length} strateg{rows.length === 1 ? "y" : "ies"}
        </span>
        {refreshing && (
          <span
            className="flex items-center gap-xs font-mono text-caption text-nx-text-disabled"
            data-testid="strategies-refreshing"
          >
            <RefreshCw size={12} strokeWidth={1.5} className="animate-spin" />
            Refreshing…
          </span>
        )}
      </div>
    </section>
  );
}

interface StrategyTableRowProps {
  row: StrategyRow;
}

function StrategyTableRow({ row }: StrategyTableRowProps) {
  const pnlTone =
    row.pnl == null
      ? "neutral"
      : row.pnl > 0
        ? "positive"
        : row.pnl < 0
          ? "negative"
          : "neutral";

  return (
    <tr
      data-testid="strategy-row"
      data-strategy-id={row.id}
      className="border-b border-nx-border transition-colors last:border-b-0 hover:bg-nx-surface-raised"
    >
      <td className="px-lg py-md">
        <div className="flex flex-col gap-2xs">
          <span className="font-body text-body text-nx-text-display">
            {row.name}
          </span>
          <span className="font-mono text-caption text-nx-text-disabled">
            {row.id}
          </span>
        </div>
      </td>
      <td className="px-lg py-md">
        <p className="max-w-md font-body text-body-sm text-nx-text-secondary">
          {row.description || (
            <span className="text-nx-text-disabled">No description</span>
          )}
        </p>
      </td>
      <td className="px-lg py-md">
        <StatusBadge status={row.status} label={row.statusLabel} />
      </td>
      <td className="px-lg py-md text-right">
        <div className="flex flex-col items-end gap-2xs">
          <span
            className={clsx(
              "font-mono text-body tabular-nums",
              pnlTone === "positive" && "text-nx-success",
              pnlTone === "negative" && "text-nx-accent",
              pnlTone === "neutral" && "text-nx-text-primary",
            )}
          >
            {row.pnl == null ? "—" : formatCurrency(row.pnl)}
          </span>
          {row.pnlPct != null && (
            <span
              className={clsx(
                "font-mono text-caption tabular-nums",
                pnlTone === "positive" && "text-nx-success",
                pnlTone === "negative" && "text-nx-accent",
                pnlTone === "neutral" && "text-nx-text-secondary",
              )}
            >
              {formatPct(row.pnlPct)}
            </span>
          )}
        </div>
      </td>
    </tr>
  );
}

interface ThProps {
  children: React.ReactNode;
  align?: "left" | "right";
}

function Th({ children, align = "left" }: ThProps) {
  return (
    <th
      scope="col"
      className={clsx(
        "px-lg py-md font-mono text-label uppercase tracking-wider text-nx-text-secondary",
        align === "right" ? "text-right" : "text-left",
      )}
    >
      {children}
    </th>
  );
}

// ---------------------------------------------------------------------------
// States
// ---------------------------------------------------------------------------

function LoadingState() {
  return (
    <div
      aria-label="Strategies loading"
      aria-busy="true"
      data-testid="strategies-loading"
      className="overflow-hidden rounded-2xl border border-nx-border bg-nx-surface"
    >
      <div className="border-b border-nx-border bg-nx-surface-raised px-lg py-md">
        <div className="h-3 w-24 animate-pulse rounded-full bg-nx-border-visible" />
      </div>
      {[0, 1, 2, 3, 4].map((i) => (
        <div
          key={i}
          className="flex animate-pulse items-center gap-lg border-b border-nx-border px-lg py-md last:border-b-0"
        >
          <div className="h-4 w-40 rounded-full bg-nx-border-visible" />
          <div className="h-3 flex-1 rounded-full bg-nx-border" />
          <div className="h-5 w-20 rounded-full bg-nx-border-visible" />
          <div className="h-4 w-24 rounded-full bg-nx-border" />
        </div>
      ))}
    </div>
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
      aria-label="Strategies error"
      data-testid="strategies-error"
      className="flex flex-col items-start gap-md rounded-2xl border border-nx-accent/40 bg-nx-surface-raised p-lg"
    >
      <div className="flex items-center gap-xs text-nx-accent">
        <AlertTriangle size={16} strokeWidth={1.75} />
        <span className="font-mono text-label uppercase tracking-wider">
          Couldn&apos;t load strategies
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

function EmptyState() {
  return (
    <section
      aria-label="No strategies"
      data-testid="strategies-empty"
      className="flex flex-col items-center justify-center gap-md rounded-2xl border border-dashed border-nx-border-visible bg-nx-surface p-4xl text-center"
    >
      <span className="font-mono text-label uppercase tracking-wider text-nx-text-disabled">
        No strategies registered
      </span>
      <p className="max-w-md font-body text-body-sm text-nx-text-secondary">
        Install a strategy plugin from the Marketplace to see it listed here
        with its status and P&amp;L.
      </p>
    </section>
  );
}
