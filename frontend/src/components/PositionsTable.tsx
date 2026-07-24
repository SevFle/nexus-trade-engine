/**
 * PositionsTable — presentational table of open portfolio positions.
 *
 * Given an array of {@link Position} rows it renders a sortable table with
 * per-column currency formatting. It is deliberately stateless with respect
 * to data fetching: callers (e.g. the Portfolio dashboard) own the query and
 * pass the resolved positions in. This mirrors the split used by the
 * Strategies listing page, where the page component holds the React Query
 * concerns and a table component renders the rows.
 *
 * Sorting is local UI state — clicking a column header re-sorts the rows
 * in place. Numeric columns sort numerically (not lexically); the symbol
 * column sorts alphabetically. P&L is tone-coloured (green/red) so a glance
 * is enough to see winners and losers.
 *
 * Number formatting uses `Intl.NumberFormat` so the values are locale-aware
 * and follow the same currency conventions as the rest of the dashboard
 * (en-US / USD).
 */
import { useMemo, useState } from "react";
import clsx from "clsx";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

/** A single open position row. */
export interface Position {
  /** Ticker symbol, e.g. "AAPL". */
  symbol: string;
  /** Number of shares/units held. May be fractional. */
  quantity: number;
  /** Average cost per share, in account currency. */
  avg_cost: number;
  /** Current market value of the holding, in account currency. */
  market_value: number;
  /** Unrealised profit/loss for the position, in account currency. */
  unrealized_pnl: number;
}

export type PositionSortKey = keyof Pick<
  Position,
  "symbol" | "quantity" | "avg_cost" | "market_value" | "unrealized_pnl"
>;

export type SortDirection = "asc" | "desc";

export interface PositionsTableProps {
  /** Positions to render. An empty array renders the empty state. */
  positions: Position[];
  /** Optional extra classes for the root <section>. */
  className?: string;
}

/** P&L tone used for colouring numeric cells. */
type Tone = "positive" | "negative" | "neutral";

// ---------------------------------------------------------------------------
// Formatting helpers
// ---------------------------------------------------------------------------

const currencyFormatter = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  maximumFractionDigits: 2,
});

// P&L is shown with an explicit +/− so a glance distinguishes gains from
// losses even before the colour registers.
const signedCurrencyFormatter = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  maximumFractionDigits: 2,
  signDisplay: "always",
});

const quantityFormatter = new Intl.NumberFormat("en-US", {
  maximumFractionDigits: 4,
});

/** Format a plain currency value, falling back to an em-dash on non-finite. */
export function formatCurrency(value: number): string {
  return Number.isFinite(value) ? currencyFormatter.format(value) : "—";
}

/** Format a signed currency value (always carries +/−). */
export function formatSignedCurrency(value: number): string {
  return Number.isFinite(value) ? signedCurrencyFormatter.format(value) : "—";
}

/** Format a share quantity, falling back to an em-dash on non-finite. */
export function formatQuantity(value: number): string {
  return Number.isFinite(value) ? quantityFormatter.format(value) : "—";
}

/** Map a numeric P&L to a colour tone. */
function pnlTone(pnl: number): Tone {
  return pnl > 0 ? "positive" : pnl < 0 ? "negative" : "neutral";
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function PositionsTable({
  positions,
  className,
}: PositionsTableProps) {
  const [sortKey, setSortKey] = useState<PositionSortKey>("market_value");
  const [sortDir, setSortDir] = useState<SortDirection>("desc");

  // Sort a defensive copy so the prop is never mutated regardless of what
  // callers do with the array afterwards.
  const sortedRows = useMemo(() => {
    const copy = [...positions];
    copy.sort((a, b) => {
      const av = a[sortKey];
      const bv = b[sortKey];
      let cmp: number;
      if (sortKey === "symbol") {
        cmp = String(av).localeCompare(String(bv));
      } else {
        cmp = (av as number) - (bv as number);
      }
      return sortDir === "asc" ? cmp : -cmp;
    });
    return copy;
  }, [positions, sortKey, sortDir]);

  function handleSort(key: PositionSortKey) {
    if (key === sortKey) {
      setSortDir((dir) => (dir === "asc" ? "desc" : "asc"));
    } else {
      setSortKey(key);
      // Default to descending for numeric columns (largest values first) and
      // ascending for the symbol column (A→Z), matching user expectations.
      setSortDir(key === "symbol" ? "asc" : "desc");
    }
  }

  return (
    <section aria-label="Open positions" className={className}>
      {sortedRows.length === 0 ? (
        <EmptyState />
      ) : (
        <div
          data-testid="positions-table"
          className="overflow-x-auto rounded-2xl border border-nx-border bg-nx-surface"
        >
          <table className="w-full min-w-[640px] border-collapse text-left">
            <caption className="sr-only">
              Open positions with symbol, quantity, average cost, market value,
              and unrealised P&amp;L.
            </caption>
            <thead className="border-b border-nx-border bg-nx-surface-raised">
              <tr>
                <SortableTh
                  label="Symbol"
                  sortKey="symbol"
                  activeKey={sortKey}
                  direction={sortDir}
                  onSort={handleSort}
                />
                <SortableTh
                  label="Quantity"
                  sortKey="quantity"
                  align="right"
                  activeKey={sortKey}
                  direction={sortDir}
                  onSort={handleSort}
                />
                <SortableTh
                  label="Avg Cost"
                  sortKey="avg_cost"
                  align="right"
                  activeKey={sortKey}
                  direction={sortDir}
                  onSort={handleSort}
                />
                <SortableTh
                  label="Market Value"
                  sortKey="market_value"
                  align="right"
                  activeKey={sortKey}
                  direction={sortDir}
                  onSort={handleSort}
                />
                <SortableTh
                  label="Unrealized P&L"
                  sortKey="unrealized_pnl"
                  align="right"
                  activeKey={sortKey}
                  direction={sortDir}
                  onSort={handleSort}
                />
              </tr>
            </thead>
            <tbody>
              {sortedRows.map((row) => (
                <PositionRow key={row.symbol} row={row} />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

// ---------------------------------------------------------------------------
// Row
// ---------------------------------------------------------------------------

interface PositionRowProps {
  row: Position;
}

function PositionRow({ row }: PositionRowProps) {
  const tone = pnlTone(row.unrealized_pnl);

  return (
    <tr
      data-testid="position-row"
      data-symbol={row.symbol}
      className="border-b border-nx-border transition-colors last:border-b-0 hover:bg-nx-surface-raised"
    >
      <td className="px-lg py-md">
        <span className="font-body text-body text-nx-text-display">
          {row.symbol}
        </span>
      </td>
      <td className="px-lg py-md text-right font-mono text-body tabular-nums text-nx-text-primary">
        {formatQuantity(row.quantity)}
      </td>
      <td className="px-lg py-md text-right font-mono text-body tabular-nums text-nx-text-primary">
        {formatCurrency(row.avg_cost)}
      </td>
      <td className="px-lg py-md text-right font-mono text-body tabular-nums text-nx-text-primary">
        {formatCurrency(row.market_value)}
      </td>
      <td
        className="px-lg py-md text-right font-mono text-body tabular-nums"
        data-testid={`pnl-${row.symbol}`}
      >
        <span
          className={clsx(
            tone === "positive" && "text-nx-success",
            tone === "negative" && "text-nx-accent",
            tone === "neutral" && "text-nx-text-primary",
          )}
        >
          {formatSignedCurrency(row.unrealized_pnl)}
        </span>
      </td>
    </tr>
  );
}

// ---------------------------------------------------------------------------
// Sortable header
// ---------------------------------------------------------------------------

interface SortableThProps {
  label: string;
  sortKey: PositionSortKey;
  activeKey: PositionSortKey;
  direction: SortDirection;
  align?: "left" | "right";
  onSort: (key: PositionSortKey) => void;
}

function SortableTh({
  label,
  sortKey,
  activeKey,
  direction,
  align = "left",
  onSort,
}: SortableThProps) {
  const isActive = sortKey === activeKey;
  const ariaSort = isActive
    ? direction === "asc"
      ? "ascending"
      : "descending"
    : "none";

  return (
    <th
      scope="col"
      aria-sort={ariaSort}
      data-testid={`th-${sortKey}`}
      className={clsx(
        "px-lg py-md font-mono text-label uppercase tracking-wider text-nx-text-secondary",
        align === "right" ? "text-right" : "text-left",
      )}
    >
      <button
        type="button"
        onClick={() => onSort(sortKey)}
        aria-label={`Sort by ${label}`}
        className={clsx(
          "inline-flex items-center gap-2xs uppercase tracking-wider",
          align === "right" && "flex-row-reverse",
          isActive ? "text-nx-text-primary" : "text-nx-text-secondary",
          "transition-colors hover:text-nx-text-primary",
        )}
      >
        {label}
        <SortGlyph active={isActive} direction={direction} />
      </button>
    </th>
  );
}

interface SortGlyphProps {
  active: boolean;
  direction: SortDirection;
}

/** A tiny caret that reflects the current sort direction. */
function SortGlyph({ active, direction }: SortGlyphProps) {
  const glyph = !active ? "↕" : direction === "asc" ? "↑" : "↓";
  return (
    <span
      aria-hidden="true"
      className={clsx(
        "text-caption",
        active ? "text-nx-text-primary" : "text-nx-text-disabled",
      )}
    >
      {glyph}
    </span>
  );
}

// ---------------------------------------------------------------------------
// Empty state
// ---------------------------------------------------------------------------

function EmptyState() {
  return (
    <div
      aria-label="No positions"
      data-testid="positions-empty"
      className="flex flex-col items-center justify-center gap-md rounded-2xl border border-dashed border-nx-border-visible bg-nx-surface p-4xl text-center"
    >
      <span className="font-mono text-label uppercase tracking-wider text-nx-text-disabled">
        No open positions
      </span>
      <p className="max-w-md font-body text-body-sm text-nx-text-secondary">
        Positions opened by your active strategies will appear here with their
        average cost, market value, and unrealised P&amp;L.
      </p>
    </div>
  );
}

export default PositionsTable;
