import { useState } from "react";
import { HeroMetric } from "../components/primitives/HeroMetric";
import { StatRow } from "../components/primitives/StatRow";
import { Sparkline } from "../components/data/Sparkline";
import { StatusBadge } from "../components/primitives/StatusBadge";
import { DisclaimerBanner } from "../components/legal/DisclaimerBanner";
import { AttributionStrip } from "../components/legal/AttributionStrip";

const MOCK_RESULTS = {
  sharpe: "2.34",
  maxDrawdown: "-12.7%",
  cagr: "28.4%",
  winRate: "63.2%",
  totalTrades: 847,
  profitFactor: "1.89",
  avgWin: "$3,421.00",
  avgLoss: "-$1,812.00",
  calmarRatio: "2.23",
};

const EQUITY_CURVE = Array.from({ length: 120 }, (_, i) => ({
  value: 1000000 + i * 8500 + Math.sin(i * 0.3) * 40000 + Math.random() * 15000,
}));

const MOCK_CONFIG = {
  strategy: "MOMENTUM ALPHA v3.2",
  start: "2024-01-01",
  end: "2026-03-31",
  initialCapital: "$1,000,000",
  universe: "SPY, QQQ, IWM, DIA",
  frequency: "DAILY",
  benchmark: "SPY BUY & HOLD",
};

export default function Backtest() {
  const [hasResults] = useState(true);

  if (!hasResults) {
    return (
      <div className="text-nx-text-primary p-xl flex items-center justify-center">
        <span className="text-label font-mono uppercase text-nx-text-secondary">
          CONFIGURE AND RUN BACKTEST TO VIEW RESULTS
        </span>
      </div>
    );
  }

  return (
    <div className="text-nx-text-primary p-xl">
      <div className="max-w-6xl mx-auto">
        <div className="mb-3xl">
          <DisclaimerBanner variant="warning">
            Past performance does not guarantee future results. Backtests are
            subject to look-ahead bias, selection bias, and survivorship bias.
            Simulated results may differ materially from actual trading.
          </DisclaimerBanner>
        </div>
        <header className="mb-3xl">
          <div className="flex items-center gap-md mb-sm">
            <span className="text-label font-mono uppercase text-nx-text-secondary">
              BACKTEST STUDIO
            </span>
            <StatusBadge status="ok">COMPLETE</StatusBadge>
          </div>
          <h1 className="text-display-md font-display text-nx-text-display">
            {MOCK_CONFIG.strategy}
          </h1>
          <span className="text-label font-mono uppercase text-nx-text-disabled mt-xs block">
            {MOCK_CONFIG.start} -- {MOCK_CONFIG.end} {"// "} {MOCK_CONFIG.frequency}
          </span>
        </header>

        <section className="grid grid-cols-4 gap-md mb-3xl">
          <div>
            <StatRow label="SHARPE RATIO" value={MOCK_RESULTS.sharpe} status="success" />
          </div>
          <div>
            <StatRow label="MAX DRAWDOWN" value={MOCK_RESULTS.maxDrawdown} status="error" />
          </div>
          <div>
            <StatRow label="CAGR" value={MOCK_RESULTS.cagr} status="success" />
          </div>
          <div>
            <StatRow label="WIN RATE" value={MOCK_RESULTS.winRate} status="success" />
          </div>
        </section>

        <section className="mb-3xl">
          <span className="text-label font-mono uppercase text-nx-text-secondary block mb-md">
            EQUITY CURVE
          </span>
          <div className="border border-nx-border rounded-2xl p-lg bg-nx-surface">
            <Sparkline data={EQUITY_CURVE} color="var(--text-display)" height={200} />
          </div>
        </section>

        <section className="grid grid-cols-2 gap-xl mb-3xl">
          <div>
            <span className="text-label font-mono uppercase text-nx-text-secondary block mb-md">
              PERFORMANCE BREAKDOWN
            </span>
            <StatRow label="TOTAL TRADES" value={String(MOCK_RESULTS.totalTrades)} />
            <StatRow label="PROFIT FACTOR" value={MOCK_RESULTS.profitFactor} status="success" />
            <StatRow label="AVG WIN" value={MOCK_RESULTS.avgWin} status="success" />
            <StatRow label="AVG LOSS" value={MOCK_RESULTS.avgLoss} status="error" />
            <StatRow label="CALMAR RATIO" value={MOCK_RESULTS.calmarRatio} status="success" />
          </div>

          <div>
            <span className="text-label font-mono uppercase text-nx-text-secondary block mb-md">
              CONFIGURATION
            </span>
            <StatRow label="INITIAL CAPITAL" value={MOCK_CONFIG.initialCapital} />
            <StatRow label="UNIVERSE" value={MOCK_CONFIG.universe} />
            <StatRow label="FREQUENCY" value={MOCK_CONFIG.frequency} />
            <StatRow label="BENCHMARK" value={MOCK_CONFIG.benchmark} />
            <StatRow label="PERIOD" value={`${MOCK_CONFIG.start} -> ${MOCK_CONFIG.end}`} />
          </div>
        </section>

        <AttributionStrip className="mb-3xl" />
      </div>
    </div>
  );
}
