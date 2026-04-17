import { useState } from "react";
import { StatusBadge } from "../components/primitives/StatusBadge";
import { StatRow } from "../components/primitives/StatRow";

const STRATEGIES = [
  { id: "momentum-alpha", name: "MOMENTUM ALPHA v3.2", status: "idle" },
  { id: "mean-revert", name: "MEAN REVERSION SIGMA", status: "idle" },
  { id: "pairs-stat", name: "PAIRS STATISTICAL v1.7", status: "idle" },
];

const MODES = ["BACKTEST", "PAPER", "LIVE"];

const MOCK_PARAMS = [
  { key: "LOOKBACK_PERIOD", value: "14", type: "int" },
  { key: "THRESHOLD", value: "1.5", type: "float" },
  { key: "POSITION_SIZE", value: "0.10", type: "float" },
  { key: "STOP_LOSS_PCT", value: "2.0", type: "float" },
  { key: "TAKE_PROFIT_PCT", value: "4.0", type: "float" },
  { key: "MAX_POSITIONS", value: "5", type: "int" },
];

export default function Strategies() {
  const [activeStrategy, setActiveStrategy] = useState(STRATEGIES[0].id);
  const [mode, setMode] = useState("BACKTEST");
  const [params, setParams] = useState(
    Object.fromEntries(MOCK_PARAMS.map((p) => [p.key, p.value]))
  );
  const [running, setRunning] = useState(false);

  const strategy = STRATEGIES.find((s) => s.id === activeStrategy);

  const handleRun = () => {
    setRunning(true);
    setTimeout(() => setRunning(false), 3000);
  };

  return (
    <div className="text-nx-text-primary p-xl">
      <div className="max-w-5xl mx-auto">
        <header className="mb-3xl">
          <span className="text-label font-mono uppercase text-nx-text-secondary block mb-sm">
            STRATEGY RUNNER
          </span>
          <h1 className="text-display-md font-display text-nx-text-display">
            {strategy.name}
          </h1>
        </header>

        <section className="mb-2xl">
          <span className="text-label font-mono uppercase text-nx-text-secondary block mb-md">
            EXECUTION MODE
          </span>
          <div className="flex gap-xs">
            {MODES.map((m) => (
              <button
                type="button"
                key={m}
                onClick={() => setMode(m)}
                className={`px-lg py-sm text-label font-mono uppercase border rounded-full transition-colors ${
                  mode === m
                    ? "bg-nx-text-display text-nx-black border-nx-text-display"
                    : "bg-transparent text-nx-text-secondary border-nx-border hover:border-nx-border-visible"
                }`}
              >
                {m}
              </button>
            ))}
          </div>
        </section>

        <section className="mb-2xl">
          <span className="text-label font-mono uppercase text-nx-text-secondary block mb-md">
            STRATEGY SELECT
          </span>
          <div className="flex flex-col gap-xs">
            {STRATEGIES.map((s) => (
              <button
                type="button"
                key={s.id}
                onClick={() => setActiveStrategy(s.id)}
                className={`flex items-center justify-between p-md border rounded-2xl text-left transition-colors ${
                  activeStrategy === s.id
                    ? "bg-nx-surface-raised border-nx-border-visible"
                    : "bg-nx-surface border-nx-border hover:border-nx-border-visible"
                }`}
              >
                <span className="text-body font-body text-nx-text-primary">
                  {s.name}
                </span>
                <StatusBadge status="neutral">IDLE</StatusBadge>
              </button>
            ))}
          </div>
        </section>

        <section className="mb-2xl bg-nx-surface border border-nx-border rounded-2xl p-lg">
          <span className="text-label font-mono uppercase text-nx-text-secondary block mb-md">
            PARAMETERS
          </span>
          <div className="flex flex-col">
            {MOCK_PARAMS.map((p) => (
              <div
                key={p.key}
                className="flex items-center justify-between py-sm border-b border-nx-border last:border-b-0"
              >
                <span className="text-label font-mono uppercase text-nx-text-secondary">
                  {p.key}
                </span>
                <input
                  type="text"
                  value={params[p.key]}
                  onChange={(e) =>
                    setParams((prev) => ({ ...prev, [p.key]: e.target.value }))
                  }
                  className="bg-nx-surface-raised border border-nx-border text-body-sm font-mono text-nx-text-primary tabular-nums px-sm py-2xs rounded text-right w-24"
                />
              </div>
            ))}
          </div>
        </section>

        <section className="flex items-center gap-md">
          <button
            type="button"
            onClick={handleRun}
            disabled={running}
            className={`px-2xl py-md text-label font-mono uppercase rounded-full border transition-colors ${
              running
                ? "bg-nx-text-disabled text-nx-black border-nx-text-disabled cursor-not-allowed"
                : "bg-nx-text-display text-nx-black border-nx-text-display hover:bg-nx-text-primary"
            }`}
          >
            {running ? "RUNNING..." : "RUN STRATEGY"}
          </button>
          {running && (
            <StatusBadge status="loading">EXECUTING</StatusBadge>
          )}
        </section>
      </div>
    </div>
  );
}
