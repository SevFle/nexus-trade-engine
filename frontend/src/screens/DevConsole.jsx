import { useState } from "react";
import { InlineStatus } from "../components/feedback/InlineStatus";
import { StatusBadge } from "../components/primitives/StatusBadge";

const FILE_TREE = [
  { name: "src/", type: "dir", depth: 0 },
  { name: "index.ts", type: "file", depth: 1 },
  { name: "strategy.ts", type: "file", depth: 1, active: true },
  { name: "indicators/", type: "dir", depth: 1 },
  { name: "rsi.ts", type: "file", depth: 2 },
  { name: "macd.ts", type: "file", depth: 2 },
  { name: "bollinger.ts", type: "file", depth: 2 },
  { name: "utils/", type: "dir", depth: 1 },
  { name: "math.ts", type: "file", depth: 2 },
  { name: "risk.ts", type: "file", depth: 2 },
  { name: "tests/", type: "dir", depth: 1 },
  { name: "strategy.test.ts", type: "file", depth: 2 },
  { name: "package.json", type: "file", depth: 0 },
  { name: "nexus.config.toml", type: "file", depth: 0 },
];

const EDITOR_CONTENT = `import { Strategy, Signal } from "@nexus/sdk";
import { RSI, EMA } from "./indicators/rsi";

export class MomentumAlpha extends Strategy {
  readonly name = "MOMENTUM ALPHA v3.2";
  readonly version = "3.2.0";

  private rsi: RSI;
  private ema: EMA;

  configure() {
    return {
      universe: ["SPY", "QQQ", "IWM", "DIA"],
      lookback: this.params.lookback ?? 14,
      threshold: this.params.threshold ?? 1.5,
      positionSize: this.params.positionSize ?? 0.10,
    };
  }

  async evaluate(ctx: MarketContext): Promise<Signal[]> {
    const signals: Signal[] = [];

    for (const symbol of this.config.universe) {
      const rsi = this.rsi.compute(ctx.bars(symbol));
      const trend = this.ema.compute(ctx.bars(symbol));

      if (rsi < 30 && trend > 0) {
        signals.push(Signal.long(symbol, this.config.positionSize));
      } else if (rsi > 70 && trend < 0) {
        signals.push(Signal.short(symbol, this.config.positionSize));
      }
    }

    return signals;
  }
}`;

const CONSOLE_OUTPUT = [
  { id: "out-1", type: "info", text: "[14:32:01] Compiling plugin momentum-alpha..." },
  { id: "out-2", type: "info", text: "[14:32:01] TypeScript 5.4.2 // Target ES2022" },
  { id: "out-3", type: "ok", text: "[14:32:02] Build successful (312ms)" },
  { id: "out-4", type: "info", text: "[14:32:02] Running test suite..." },
  { id: "out-5", type: "ok", text: "[14:32:03] PASS strategy.test.ts (14 tests)" },
  { id: "out-6", type: "ok", text: "[14:32:03] Tests: 14 passed, 0 failed, 14 total" },
  { id: "out-7", type: "ok", text: "[14:32:03] Coverage: 94.2% statements, 88.1% branches" },
  { id: "out-8", type: "info", text: "[14:32:03] Validating against Nexus SDK v2.8.1..." },
  { id: "out-9", type: "ok", text: "[14:32:03] Plugin validation passed" },
  { id: "out-10", type: "info", text: "[14:32:04] Ready to deploy" },
];

export default function DevConsole() {
  const [activeFile, setActiveFile] = useState("strategy.ts");
  const [selectedTest, setSelectedTest] = useState("PASS");

  return (
    <div className="text-nx-text-primary p-xl">
      <div className="max-w-7xl mx-auto">
        <header className="mb-2xl flex items-center justify-between">
          <div>
            <span className="text-label font-mono uppercase text-nx-text-secondary block mb-sm">
              PLUGIN DEV CONSOLE
            </span>
            <h1 className="text-heading font-display text-nx-text-display">
              MOMENTUM ALPHA v3.2
            </h1>
          </div>
          <div className="flex items-center gap-md">
            <InlineStatus status="saved">ALL FILES SAVED</InlineStatus>
            <StatusBadge status={selectedTest === "PASS" ? "ok" : "error"}>
              {selectedTest}
            </StatusBadge>
            <span className="text-label font-mono uppercase text-nx-text-disabled">
              SDK v2.8.1
            </span>
          </div>
        </header>

        <section className="flex gap-md" style={{ height: "calc(100vh - 200px)" }}>
          <div className="w-56 shrink-0 bg-nx-surface border border-nx-border rounded-2xl overflow-hidden flex flex-col">
            <div className="p-md border-b border-nx-border">
              <span className="text-label font-mono uppercase text-nx-text-secondary">
                EXPLORER
              </span>
            </div>
            <div className="flex-1 overflow-auto p-sm">
              {FILE_TREE.map((file) => (
                <button
                  type="button"
                  key={file.name}
                  onClick={() => file.type === "file" && setActiveFile(file.name)}
                  className={`w-full text-left text-label font-mono py-xs px-sm rounded transition-colors ${
                    file.type === "dir"
                      ? "text-nx-text-secondary uppercase"
                      : file.name === activeFile
                        ? "bg-nx-surface-raised text-nx-text-primary"
                        : "text-nx-text-disabled hover:text-nx-text-secondary"
                  }`}
                  style={{ paddingLeft: `${file.depth * 16 + 8}px` }}
                >
                  {file.name}
                </button>
              ))}
            </div>
          </div>

          <div className="flex-1 flex flex-col gap-md min-w-0">
            <div className="flex-1 bg-nx-surface border border-nx-border rounded-2xl overflow-hidden flex flex-col">
              <div className="flex items-center gap-sm px-md py-sm border-b border-nx-border">
                <span className="text-label font-mono uppercase text-nx-text-secondary">
                  {activeFile}
                </span>
              </div>
              <div className="flex-1 overflow-auto p-md">
                <pre className="text-body-sm font-mono text-nx-text-primary leading-relaxed whitespace-pre">
                  {EDITOR_CONTENT}
                </pre>
              </div>
            </div>

            <div className="h-48 bg-nx-surface border border-nx-border rounded-2xl overflow-hidden flex flex-col">
              <div className="flex items-center gap-sm px-md py-sm border-b border-nx-border">
                <span className="text-label font-mono uppercase text-nx-text-secondary">
                  OUTPUT
                </span>
              </div>
              <div className="flex-1 overflow-auto p-md">
                {CONSOLE_OUTPUT.map((line) => (
                  <div key={line.id} className="text-label font-mono leading-relaxed">
                    <span
                      className={
                        line.type === "ok"
                          ? "text-nx-success"
                          : line.type === "error"
                            ? "text-nx-accent"
                            : "text-nx-text-secondary"
                      }
                    >
                      {line.text}
                    </span>
                  </div>
                ))}
              </div>
            </div>
          </div>
        </section>
      </div>
    </div>
  );
}
