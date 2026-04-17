import { HeroMetric } from "../components/primitives/HeroMetric";
import { StatRow } from "../components/primitives/StatRow";
import { SegmentedBar } from "../components/data/SegmentedBar";

const COST_BREAKDOWN = [
  { label: "COMMISSION", value: 3240, max: 10000, status: "neutral", pct: "0.114%" },
  { label: "SPREAD", value: 5870, max: 10000, status: "warning", pct: "0.207%" },
  { label: "SLIPPAGE", value: 2130, max: 10000, status: "neutral", pct: "0.075%" },
  { label: "TAXES & FEES", value: 1420, max: 10000, status: "neutral", pct: "0.050%" },
];

const PER_TRADE = [
  { label: "AVG COMMISSION/TRADE", value: "$3.82", status: "neutral" },
  { label: "AVG SPREAD/TRADE", value: "$6.92", status: "warning" },
  { label: "AVG SLIPPAGE/TRADE", value: "$2.51", status: "neutral" },
  { label: "AVG TOTAL COST/TRADE", value: "$13.25", status: "warning" },
  { label: "COST PER $100K NOTIONAL", value: "$2.48", status: "neutral" },
  { label: "TOTAL TRADES ANALYZED", value: "847", status: "neutral" },
];

const MONTHLY_DRAG = [
  { label: "JAN 2026", value: "0.38%", status: "neutral" },
  { label: "FEB 2026", value: "0.41%", status: "neutral" },
  { label: "MAR 2026", value: "0.52%", status: "warning" },
  { label: "APR 2026", value: "0.44%", status: "neutral" },
];

export default function CostAnalysis() {
  return (
    <div className="text-nx-text-primary p-xl">
      <div className="max-w-5xl mx-auto">
        <header className="mb-3xl">
          <span className="text-label font-mono uppercase text-nx-text-secondary block mb-sm">
            COST ANALYSIS
          </span>
          <HeroMetric value="0.446" unit="% ANNUAL DRAG" status="warning" />
          <span className="text-label font-mono uppercase text-nx-text-disabled block mt-sm">
            TOTAL IMPLEMENTATION SHORTFALL OVER 847 TRADES
          </span>
        </header>

        <section className="mb-3xl">
          <span className="text-label font-mono uppercase text-nx-text-secondary block mb-lg">
            COST BREAKDOWN
          </span>
          <div className="flex flex-col gap-xl">
            {COST_BREAKDOWN.map((cost) => (
              <div key={cost.label}>
                <div className="flex items-baseline justify-between mb-sm">
                  <span className="text-label font-mono uppercase text-nx-text-secondary">
                    {cost.label}
                  </span>
                  <span className="text-body-sm font-mono tabular-nums text-nx-text-primary">
                    ${cost.value.toLocaleString()}
                    <span className="text-label text-nx-text-disabled ml-xs">
                      {cost.pct} OF NAV
                    </span>
                  </span>
                </div>
                <SegmentedBar
                  value={cost.value}
                  max={cost.max}
                  status={cost.status}
                  height={6}
                />
              </div>
            ))}
          </div>
        </section>

        <section className="grid grid-cols-2 gap-xl mb-3xl">
          <div>
            <span className="text-label font-mono uppercase text-nx-text-secondary block mb-md">
              PER-TRADE AVERAGES
            </span>
            {PER_TRADE.map((row) => (
              <StatRow
                key={row.label}
                label={row.label}
                value={row.value}
                status={row.status}
              />
            ))}
          </div>

          <div>
            <span className="text-label font-mono uppercase text-nx-text-secondary block mb-md">
              MONTHLY COST DRAG
            </span>
            {MONTHLY_DRAG.map((row) => (
              <StatRow
                key={row.label}
                label={row.label}
                value={row.value}
                status={row.status}
              />
            ))}
            <div className="mt-lg p-md bg-nx-surface border border-nx-border rounded-xl">
              <span className="text-label font-mono uppercase text-nx-text-secondary block mb-xs">
                RECOMMENDATION
              </span>
              <span className="text-body-sm font-body text-nx-text-primary">
                Spread costs are 44% of total drag. Consider switching to maker
                orders or negotiating tier-2 exchange fees to reduce by an
                estimated 0.09% annually.
              </span>
            </div>
          </div>
        </section>
      </div>
    </div>
  );
}
