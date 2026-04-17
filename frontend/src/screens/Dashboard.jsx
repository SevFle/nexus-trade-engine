import { useState, useEffect } from "react";
import { StatRow } from "../components/primitives/StatRow";
import { StatusBadge } from "../components/primitives/StatusBadge";
import { Sparkline } from "../components/data/Sparkline";
import { InlineStatus } from "../components/feedback/InlineStatus";

const MOCK_PORTFOLIO = {
  value: "$2,847,391.44",
  dailyPnl: "+$31,204.18",
  dailyPnlPct: "+1.11%",
  totalReturn: "+42.67%",
  activePositions: 14,
  strategyHealth: 97,
};

const MOCK_SPARKLINE = Array.from({ length: 30 }, (_, i) => ({
  value: 2800000 + Math.sin(i * 0.4) * 80000 + i * 2000 + Math.random() * 20000,
}));

export default function Dashboard() {
  const [data, setData] = useState(MOCK_PORTFOLIO);
  const [health, setHealth] = useState(null);
  const [lastUpdate, setLastUpdate] = useState("2026-04-16T14:32:07Z");

  useEffect(() => {
    fetch("/health")
      .then((r) => r.json())
      .then(setHealth)
      .catch(() => setHealth({ status: "disconnected" }));
  }, []);

  useEffect(() => {
    const id = setInterval(() => {
      setLastUpdate(new Date().toISOString());
    }, 30000);
    return () => clearInterval(id);
  }, []);

  const widgets = [
    { label: "DAY P&L", value: data.dailyPnl, unit: "USD", status: "success" },
    { label: "TOTAL RETURN", value: data.totalReturn, unit: "SINCE INCEPTION", status: "success" },
    { label: "ACTIVE POSITIONS", value: String(data.activePositions), unit: "OPEN", status: "neutral" },
    { label: "STRATEGY HEALTH", value: String(data.strategyHealth), unit: "SCORE", status: "success" },
  ];

  return (
    <div className="text-nx-text-primary p-xl">
      <div className="max-w-7xl mx-auto">
        <header className="mb-3xl">
          <span className="text-label font-mono uppercase text-nx-text-secondary block mb-sm">
            PORTFOLIO VALUE
          </span>
          <span className="text-display-xl font-display text-nx-text-display tabular-nums block">
            {data.value}
          </span>
          <div className="flex items-baseline gap-md mt-md">
            <span className="text-heading font-display text-nx-success tabular-nums">
              {data.dailyPnl}
            </span>
            <span className="text-body font-mono text-nx-success tabular-nums">
              {data.dailyPnlPct}
            </span>
            <StatusBadge status="ok">TODAY</StatusBadge>
          </div>
          <div className="mt-lg" style={{ maxWidth: 480 }}>
            <Sparkline data={MOCK_SPARKLINE} color="var(--success)" height={48} />
          </div>
        </header>

        <section className="grid grid-cols-4 gap-md mb-3xl">
          {widgets.map((w) => (
            <div
              key={w.label}
              className="bg-nx-surface rounded-2xl p-lg border border-nx-border"
            >
              <span className="text-label font-mono uppercase text-nx-text-secondary block mb-sm">
                {w.label}
              </span>
              <span
                className={`text-heading font-display tabular-nums ${
                  w.status === "success"
                    ? "text-nx-success"
                    : w.status === "error"
                      ? "text-nx-accent"
                      : "text-nx-text-display"
                }`}
              >
                {w.value}
              </span>
              <span className="text-label font-mono uppercase text-nx-text-disabled ml-xs">
                {w.unit}
              </span>
            </div>
          ))}
        </section>

        <footer className="flex items-center justify-between border-t border-nx-border pt-md">
          <span className="text-label font-mono uppercase text-nx-text-disabled">
            LAST UPDATE: {new Date(lastUpdate).toLocaleTimeString("en-US", { hour12: false })}
          </span>
          {health ? (
            <InlineStatus status={health.status === "ok" ? "ok" : "error"}>
              {health.status === "ok" ? "ENGINE CONNECTED" : "ENGINE DISCONNECTED"}
            </InlineStatus>
          ) : (
            <InlineStatus status="loading">CONNECTING</InlineStatus>
          )}
        </footer>
      </div>
    </div>
  );
}
