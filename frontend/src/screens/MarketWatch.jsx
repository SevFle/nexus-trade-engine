import React from "react";
import { useQuery } from "@tanstack/react-query";
import { CandlestickChart, LineChart, RefreshCw } from "lucide-react";
import clsx from "clsx";
import { getBars, getQuote } from "../api/marketData";
import { PriceChart } from "../components/data/PriceChart";
import { StatusBadge } from "../components/primitives/StatusBadge";
import { LoadingSpinner } from "../components/feedback/LoadingSpinner";
import { EmptyState } from "../components/feedback/EmptyState";

const PERIODS = [
  { id: "5d", label: "5D", interval: "1d" },
  { id: "1mo", label: "1M", interval: "1d" },
  { id: "3mo", label: "3M", interval: "1d" },
  { id: "1y", label: "1Y", interval: "1d" },
  { id: "5y", label: "5Y", interval: "1wk" },
  { id: "max", label: "MAX", interval: "1mo" },
];

const PROVIDERS = [
  { id: "", label: "AUTO" },
  { id: "yahoo", label: "YAHOO" },
  { id: "polygon", label: "POLYGON" },
  { id: "alpaca", label: "ALPACA" },
  { id: "binance", label: "BINANCE" },
  { id: "coingecko", label: "COINGECKO" },
  { id: "oanda", label: "OANDA" },
];

function PeriodPicker({ value, onChange }) {
  return (
    <div className="inline-flex items-center gap-xs bg-nx-surface border border-nx-border rounded-md p-xs">
      {PERIODS.map((p) => {
        const isActive = p.id === value;
        return (
          <button
            type="button"
            key={p.id}
            onClick={() => onChange(p)}
            className={clsx(
              "px-sm py-xs text-label font-mono uppercase rounded transition-colors",
              isActive
                ? "bg-nx-accent-subtle text-nx-text-display"
                : "text-nx-text-secondary hover:text-nx-text-primary",
            )}
          >
            {p.label}
          </button>
        );
      })}
    </div>
  );
}

function ChartModePicker({ value, onChange }) {
  return (
    <div className="inline-flex items-center gap-xs bg-nx-surface border border-nx-border rounded-md p-xs">
      <button
        type="button"
        onClick={() => onChange("line")}
        title="Line"
        className={clsx(
          "px-sm py-xs rounded flex items-center gap-xs text-label font-mono uppercase transition-colors",
          value === "line"
            ? "bg-nx-accent-subtle text-nx-text-display"
            : "text-nx-text-secondary hover:text-nx-text-primary",
        )}
      >
        <LineChart size={14} strokeWidth={1.5} />
        LINE
      </button>
      <button
        type="button"
        onClick={() => onChange("candle")}
        title="Candle"
        className={clsx(
          "px-sm py-xs rounded flex items-center gap-xs text-label font-mono uppercase transition-colors",
          value === "candle"
            ? "bg-nx-accent-subtle text-nx-text-display"
            : "text-nx-text-secondary hover:text-nx-text-primary",
        )}
      >
        <CandlestickChart size={14} strokeWidth={1.5} />
        CANDLE
      </button>
    </div>
  );
}

function formatPrice(value) {
  if (value == null) return "—";
  const n = Number(value);
  if (!Number.isFinite(n)) return "—";
  return n.toLocaleString(undefined, {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

function formatPct(value) {
  if (!Number.isFinite(value)) return "—";
  const sign = value > 0 ? "+" : "";
  return `${sign}${value.toFixed(2)}%`;
}

function deriveSummary(bars, quote) {
  if (!bars || bars.length === 0) return null;
  const last = bars[bars.length - 1];
  const first = bars[0];
  const lastPrice = quote?.price ?? Number(last.close);
  const change = lastPrice - Number(first.close);
  const changePct = (change / Number(first.close)) * 100;
  return {
    lastPrice,
    change,
    changePct,
    asOf: last.timestamp,
  };
}

export default function MarketWatch() {
  const [draft, setDraft] = React.useState("AAPL");
  const [symbol, setSymbol] = React.useState("AAPL");
  const [period, setPeriod] = React.useState(PERIODS[3]); // 1y default
  const [provider, setProvider] = React.useState("");
  const [mode, setMode] = React.useState("line");

  const queryKey = React.useMemo(
    () => ["market-data", "bars", symbol, period.id, period.interval, provider || "auto"],
    [symbol, period, provider],
  );

  const barsQuery = useQuery({
    queryKey,
    queryFn: () =>
      getBars(symbol, {
        period: period.id,
        interval: period.interval,
        provider: provider || undefined,
      }),
    enabled: Boolean(symbol),
    staleTime: 60_000,
  });

  const quoteQuery = useQuery({
    queryKey: ["market-data", "quote", symbol, provider || "auto"],
    queryFn: () => getQuote(symbol, { provider: provider || undefined }),
    enabled: Boolean(symbol),
    staleTime: 30_000,
    retry: false,
  });

  function handleSubmit(e) {
    e.preventDefault();
    const next = draft.trim().toUpperCase();
    if (!next) return;
    setSymbol(next);
  }

  const summary = deriveSummary(barsQuery.data?.bars, quoteQuery.data);
  const changeIsPositive = summary ? summary.change >= 0 : true;
  const providerLabel =
    barsQuery.data?.provider || quoteQuery.data?.provider || "—";
  const assetClass = barsQuery.data?.asset_class || "—";

  return (
    <div className="text-nx-text-primary p-xl">
      <div className="max-w-7xl mx-auto">
        <header className="mb-xl">
          <span className="text-label font-mono uppercase text-nx-text-secondary block mb-sm">
            MARKET WATCH
          </span>
          <span className="text-display-xl font-display text-nx-text-display tabular-nums block">
            {symbol}
          </span>
          {summary && (
            <div className="flex items-baseline gap-md mt-md">
              <span className="text-heading font-display text-nx-text-display tabular-nums">
                {formatPrice(summary.lastPrice)}
              </span>
              <span
                className={clsx(
                  "text-body font-mono tabular-nums",
                  changeIsPositive ? "text-nx-success" : "text-nx-accent",
                )}
              >
                {summary.change >= 0 ? "+" : ""}
                {formatPrice(summary.change)} ({formatPct(summary.changePct)})
              </span>
              <StatusBadge status="ok">{providerLabel.toUpperCase()}</StatusBadge>
              <span className="text-label font-mono uppercase text-nx-text-disabled">
                {assetClass}
              </span>
            </div>
          )}
        </header>

        <form
          onSubmit={handleSubmit}
          className="flex flex-wrap items-center gap-md mb-lg"
        >
          <label className="flex items-center gap-sm">
            <span className="text-label font-mono uppercase text-nx-text-secondary">
              SYMBOL
            </span>
            <input
              type="text"
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              placeholder="AAPL, BTC-USD, EURUSD=X"
              maxLength={32}
              className="bg-nx-surface border border-nx-border rounded-md px-md py-sm font-mono uppercase tracking-wide text-nx-text-display placeholder:text-nx-text-disabled focus:outline-none focus:border-nx-accent"
              aria-label="Symbol"
            />
          </label>
          <label className="flex items-center gap-sm">
            <span className="text-label font-mono uppercase text-nx-text-secondary">
              PROVIDER
            </span>
            <select
              value={provider}
              onChange={(e) => setProvider(e.target.value)}
              className="bg-nx-surface border border-nx-border rounded-md px-sm py-sm font-mono uppercase text-nx-text-primary focus:outline-none focus:border-nx-accent"
              aria-label="Provider"
            >
              {PROVIDERS.map((p) => (
                <option key={p.id || "auto"} value={p.id}>
                  {p.label}
                </option>
              ))}
            </select>
          </label>
          <button
            type="submit"
            className="bg-nx-accent-subtle text-nx-text-display border border-nx-border rounded-md px-md py-sm font-mono uppercase text-label hover:bg-nx-accent hover:text-white transition-colors flex items-center gap-xs"
          >
            <RefreshCw size={12} strokeWidth={1.5} />
            LOAD
          </button>
        </form>

        <div className="flex flex-wrap items-center justify-between gap-md mb-md">
          <PeriodPicker value={period.id} onChange={setPeriod} />
          <ChartModePicker value={mode} onChange={setMode} />
        </div>

        <section className="bg-nx-surface border border-nx-border rounded-2xl p-md">
          {barsQuery.isPending ? (
            <div className="flex items-center justify-center" style={{ height: 380 }}>
              <LoadingSpinner />
            </div>
          ) : barsQuery.isError ? (
            <EmptyState
              title="Could not load bars"
              description={barsQuery.error?.message || "Provider request failed."}
            />
          ) : barsQuery.data?.bars?.length === 0 ? (
            <EmptyState
              title="No bars available"
              description={`No data for ${symbol} on the selected period.`}
            />
          ) : (
            <PriceChart bars={barsQuery.data?.bars} mode={mode} height={380} />
          )}
        </section>
      </div>
    </div>
  );
}
