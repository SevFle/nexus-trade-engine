import { useState } from "react";
import { StatRow } from "../components/primitives/StatRow";
import { SegmentedBar } from "../components/data/SegmentedBar";
import { StatusBadge } from "../components/primitives/StatusBadge";

const RISK_SCORE = 34;

const RISK_METRICS = [
  { label: "MAX DRAWDOWN (REALIZED)", value: "-8.7%", barValue: 8.7, barMax: 25, status: "warning" },
  { label: "VALUE AT RISK (95%, 1D)", value: "-$42,710", barValue: 42710, barMax: 100000, status: "neutral" },
  { label: "EXPECTED SHORTFALL (CVaR)", value: "-$68,934", barValue: 68934, barMax: 100000, status: "warning" },
  { label: "PORTFOLIO CONCENTRATION (HHI)", value: "0.142", barValue: 142, barMax: 1000, status: "neutral" },
  { label: "CORRELATION RISK", value: "0.67", barValue: 67, barMax: 100, status: "warning" },
  { label: "LEVERAGE RATIO", value: "1.24x", barValue: 124, barMax: 300, status: "neutral" },
];

const ALERTS = [
  { id: 1, level: "warning", message: "TSLA SHORT POSITION EXCEEDS 8% PORTFOLIO WEIGHT", time: "14:28:41" },
  { id: 2, level: "warning", message: "SECTOR CONCENTRATION: TECHNOLOGY AT 62% OF NAV", time: "13:55:12" },
  { id: 3, level: "ok", message: "DAILY VAR LIMIT: OPERATING WITHIN BOUNDS", time: "12:00:00" },
  { id: 4, level: "ok", message: "MARGIN UTILIZATION AT 34% OF AVAILABLE", time: "11:04:33" },
];

export default function RiskMonitor() {
  const riskColor =
    RISK_SCORE < 30 ? "var(--success)" : RISK_SCORE < 60 ? "var(--warning)" : "var(--accent)";

  const riskLabel =
    RISK_SCORE < 30 ? "LOW" : RISK_SCORE < 60 ? "MODERATE" : RISK_SCORE < 80 ? "HIGH" : "CRITICAL";

  return (
    <div className="text-nx-text-primary p-xl">
      <div className="max-w-5xl mx-auto">
        <header className="mb-3xl flex items-start justify-between">
          <div>
            <span className="text-label font-mono uppercase text-nx-text-secondary block mb-sm">
              RISK MONITOR
            </span>
            <h1 className="text-display-md font-display text-nx-text-display">
              PORTFOLIO RISK
            </h1>
          </div>
          <div className="flex flex-col items-center gap-sm">
            <div
              className="w-32 h-32 rounded-full border-4 flex items-center justify-center"
              style={{ borderColor: riskColor }}
            >
              <span
                className="text-display-md font-display tabular-nums"
                style={{ color: riskColor }}
              >
                {RISK_SCORE}
              </span>
            </div>
            <span className="text-label font-mono uppercase text-nx-text-secondary">
              RISK SCORE {"//"} {riskLabel}
            </span>
          </div>
        </header>

        <section className="mb-3xl">
          <span className="text-label font-mono uppercase text-nx-text-secondary block mb-md">
            RISK METRICS
          </span>
          <div className="flex flex-col gap-lg">
            {RISK_METRICS.map((metric) => (
              <div key={metric.label}>
                <div className="flex items-baseline justify-between mb-xs">
                  <span className="text-label font-mono uppercase text-nx-text-secondary">
                    {metric.label}
                  </span>
                  <span
                    className={`text-body-sm font-mono tabular-nums ${
                      metric.status === "warning"
                        ? "text-nx-warning"
                        : metric.status === "error"
                          ? "text-nx-accent"
                          : "text-nx-text-primary"
                    }`}
                  >
                    {metric.value}
                  </span>
                </div>
                <SegmentedBar
                  value={metric.barValue}
                  max={metric.barMax}
                  status={metric.status}
                  height={4}
                />
              </div>
            ))}
          </div>
        </section>

        <section>
          <span className="text-label font-mono uppercase text-nx-text-secondary block mb-md">
            ACTIVE ALERTS
          </span>
          <div className="flex flex-col gap-xs">
            {ALERTS.map((alert) => (
              <div
                key={alert.id}
                className="flex items-center gap-md bg-nx-surface border border-nx-border rounded-xl p-md"
              >
                <StatusBadge status={alert.level}>
                  {alert.level.toUpperCase()}
                </StatusBadge>
                <span className="text-body-sm font-body text-nx-text-primary flex-1">
                  {alert.message}
                </span>
                <span className="text-label font-mono text-nx-text-disabled tabular-nums">
                  {alert.time}
                </span>
              </div>
            ))}
          </div>
        </section>
      </div>
    </div>
  );
}
