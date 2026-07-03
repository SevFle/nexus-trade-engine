import { useState } from "react";

const LAYERS = [
  {
    id: "ui",
    label: "PRESENTATION LAYER",
    color: "#0A1628",
    accent: "#3B82F6",
    modules: [
      { name: "Dashboard", desc: "Portfolio overview, P&L, live charts, alerts" },
      { name: "Strategy Lab", desc: "Install, configure, A/B test strategies" },
      { name: "Backtest Studio", desc: "Historical evaluation with full cost model" },
      { name: "Marketplace", desc: "Browse, rate, install community strategies" },
      { name: "Risk Monitor", desc: "Drawdown, exposure, VaR, position limits" },
    ],
  },
  {
    id: "api",
    label: "API GATEWAY & ORCHESTRATION",
    color: "#0F1D32",
    accent: "#F59E0B",
    modules: [
      { name: "REST / WebSocket API", desc: "External & internal communication layer" },
      { name: "Auth & RBAC", desc: "API keys, OAuth, role-based access" },
      { name: "Rate Limiter", desc: "Protect core engine from overload" },
      { name: "Event Bus", desc: "Pub/sub for decoupled module communication" },
    ],
  },
  {
    id: "engine",
    label: "CORE TRADING ENGINE",
    color: "#111B2E",
    accent: "#10B981",
    modules: [
      { name: "Order Manager", desc: "Order lifecycle: create → route → fill → reconcile" },
      { name: "Execution Modes", desc: "Backtest · Paper Trade · Live — same interface" },
      { name: "Cost Model", desc: "Fees, spread, slippage, tax (FIFO/LIFO), wash sales" },
      { name: "Risk Engine", desc: "Pre-trade checks, position limits, circuit breakers" },
      { name: "Portfolio Tracker", desc: "Real-time NAV, allocation, P&L attribution" },
    ],
  },
  {
    id: "plugin",
    label: "STRATEGY PLUGIN SYSTEM",
    color: "#0D1A2D",
    accent: "#8B5CF6",
    modules: [
      { name: "Plugin SDK", desc: "IStrategy interface, lifecycle hooks, sandboxed runtime" },
      { name: "Strategy Registry", desc: "Version control, hot-reload, dependency resolution" },
      { name: "Signal Bus", desc: "Strategies emit BUY/SELL/HOLD → engine consumes" },
      { name: "Config Schema", desc: "Each plugin declares tunable params via JSON Schema" },
      { name: "Evaluation Engine", desc: "Sharpe, Sortino, max DD, win rate, cost-adjusted returns" },
    ],
  },
  {
    id: "data",
    label: "DATA & PERSISTENCE LAYER",
    color: "#091525",
    accent: "#EF4444",
    modules: [
      { name: "Market Data Feeds", desc: "Historical OHLCV, tick data, order book, alt data" },
      { name: "Time-Series DB", desc: "TimescaleDB / QuestDB for candles & indicators" },
      { name: "Relational DB", desc: "PostgreSQL for users, portfolios, orders, config" },
      { name: "Cache Layer", desc: "Redis for live quotes, session state, signals" },
      { name: "Blob Store", desc: "Trained ML models, strategy artifacts, logs" },
    ],
  },
];

const EXECUTION_MODES = [
  {
    mode: "BACKTEST",
    icon: "⏪",
    color: "#3B82F6",
    desc: "Historical simulation with full cost model. Iterate fast.",
    flow: ["Load historical data", "Run strategy tick-by-tick", "Apply fees/tax/slippage per trade", "Generate performance report"],
  },
  {
    mode: "PAPER TRADE",
    icon: "📋",
    color: "#F59E0B",
    desc: "Live market data, simulated execution. Validate in real-time.",
    flow: ["Subscribe to live feeds", "Strategy generates signals", "Mock order fills with realistic slippage", "Track simulated P&L live"],
  },
  {
    mode: "LIVE TRADE",
    icon: "🔴",
    color: "#EF4444",
    desc: "Real money, real broker. Same interface, same risk checks.",
    flow: ["Same signal pipeline as paper", "Route orders to broker API", "Reconcile fills vs expected", "Enforce hard risk limits"],
  },
];

const PLUGIN_INTERFACE = `interface IStrategy {
  // Identity
  readonly id: string;
  readonly name: string;
  readonly version: string;
  readonly author: string;

  // Lifecycle
  initialize(config: StrategyConfig): Promise<void>;
  onMarketData(data: MarketTick): void;
  onOrderFill(fill: OrderFill): void;
  dispose(): void;

  // Signals
  evaluate(
    portfolio: PortfolioSnapshot,
    marketState: MarketState,
    costModel: ICostModel
  ): Signal[];

  // Metadata
  getConfigSchema(): JSONSchema;
  getRequiredDataFeeds(): DataFeed[];
  getMinHistoryBars(): number;
}`;

const COST_MODEL = `interface ICostModel {
  // Per-trade costs
  calcCommission(order: Order): Money;
  calcSpread(symbol: string, side: Side): Money;
  calcSlippage(order: Order, book: OrderBook): Money;

  // Tax engine
  calcTax(trade: ClosedTrade, method: "FIFO"|"LIFO"): Money;
  checkWashSale(trade: Trade, history: Trade[]): boolean;
  calcDividendTax(dividend: Dividend): Money;

  // Summary
  calcTotalCost(order: Order): CostBreakdown;
  calcNetReturn(grossReturn: Money): Money;
}`;

function LayerCard({ layer, isExpanded, onToggle }) {
  return (
    <div
      style={{
        background: layer.color,
        border: `1px solid ${layer.accent}22`,
        borderLeft: `3px solid ${layer.accent}`,
        borderRadius: 8,
        marginBottom: 6,
        cursor: "pointer",
        transition: "all 0.2s ease",
      }}
      onClick={onToggle}
    >
      <div style={{ padding: "12px 16px", display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <span style={{ color: layer.accent, fontSize: 11, fontFamily: "'JetBrains Mono', monospace", fontWeight: 700, letterSpacing: 2 }}>
            {layer.label}
          </span>
        </div>
        <span style={{ color: layer.accent, fontSize: 14, transform: isExpanded ? "rotate(180deg)" : "rotate(0)", transition: "transform 0.2s" }}>▼</span>
      </div>
      {isExpanded && (
        <div style={{ padding: "0 16px 14px", display: "flex", flexWrap: "wrap", gap: 6 }}>
          {layer.modules.map((m) => (
            <div
              key={m.name}
              style={{
                background: `${layer.accent}0D`,
                border: `1px solid ${layer.accent}22`,
                borderRadius: 6,
                padding: "8px 12px",
                flex: "1 1 calc(50% - 6px)",
                minWidth: 180,
              }}
            >
              <div style={{ color: layer.accent, fontSize: 12, fontWeight: 700, fontFamily: "'JetBrains Mono', monospace" }}>{m.name}</div>
              <div style={{ color: "#94A3B8", fontSize: 11, marginTop: 3, lineHeight: 1.4 }}>{m.desc}</div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

export default function TradingArchitecture() {
  const [expandedLayer, setExpandedLayer] = useState("engine");
  const [activeTab, setActiveTab] = useState("architecture");
  const [activeMode, setActiveMode] = useState(0);

  const tabs = [
    { id: "architecture", label: "Architecture" },
    { id: "modes", label: "Execution Modes" },
    { id: "plugin", label: "Plugin SDK" },
    { id: "costs", label: "Cost Model" },
  ];

  return (
    <div style={{
      background: "#060E1A",
      minHeight: "100vh",
      color: "#E2E8F0",
      fontFamily: "'Segoe UI', -apple-system, sans-serif",
    }}>
      {/* Header */}
      <div style={{
        padding: "24px 20px 16px",
        borderBottom: "1px solid #1E293B",
        background: "linear-gradient(180deg, #0A1628 0%, #060E1A 100%)",
      }}>
        <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 4 }}>
          <div style={{
            width: 28, height: 28, borderRadius: 6,
            background: "linear-gradient(135deg, #3B82F6, #8B5CF6)",
            display: "flex", alignItems: "center", justifyContent: "center",
            fontSize: 14, fontWeight: 900, color: "#fff",
          }}>Δ</div>
          <span style={{ fontSize: 18, fontWeight: 800, letterSpacing: -0.5, color: "#F8FAFC" }}>
            NEXUS TRADE ENGINE
          </span>
        </div>
        <p style={{ color: "#64748B", fontSize: 11, margin: 0, fontFamily: "'JetBrains Mono', monospace", letterSpacing: 1 }}>
          AI-NATIVE PLUGIN TRADING FRAMEWORK
        </p>
      </div>

      {/* Tabs */}
      <div style={{ display: "flex", gap: 0, borderBottom: "1px solid #1E293B", background: "#0A1628" }}>
        {tabs.map((t) => (
          <button
            key={t.id}
            onClick={() => setActiveTab(t.id)}
            style={{
              flex: 1,
              padding: "10px 8px",
              background: activeTab === t.id ? "#111B2E" : "transparent",
              color: activeTab === t.id ? "#F8FAFC" : "#64748B",
              border: "none",
              borderBottom: activeTab === t.id ? "2px solid #3B82F6" : "2px solid transparent",
              fontSize: 11,
              fontWeight: 700,
              cursor: "pointer",
              fontFamily: "'JetBrains Mono', monospace",
              letterSpacing: 0.5,
            }}
          >
            {t.label}
          </button>
        ))}
      </div>

      {/* Content */}
      <div style={{ padding: "12px 12px 24px" }}>

        {/* --- ARCHITECTURE TAB --- */}
        {activeTab === "architecture" && (
          <div>
            <p style={{ color: "#94A3B8", fontSize: 12, lineHeight: 1.6, margin: "8px 0 14px" }}>
              Five-layer architecture. Each layer is independently scalable. Strategies plug into the Plugin layer and communicate via the Signal Bus.
            </p>
            {LAYERS.map((layer) => (
              <LayerCard
                key={layer.id}
                layer={layer}
                isExpanded={expandedLayer === layer.id}
                onToggle={() => setExpandedLayer(expandedLayer === layer.id ? null : layer.id)}
              />
            ))}
            <div style={{
              marginTop: 14, padding: 14, background: "#0A1628",
              border: "1px solid #1E293B", borderRadius: 8,
            }}>
              <div style={{ color: "#F59E0B", fontSize: 11, fontWeight: 700, fontFamily: "'JetBrains Mono', monospace", marginBottom: 8, letterSpacing: 1 }}>
                DATA FLOW
              </div>
              <div style={{ fontSize: 12, color: "#94A3B8", lineHeight: 1.8, fontFamily: "'JetBrains Mono', monospace" }}>
                <div>Market Feed → <span style={{ color: "#EF4444" }}>Data Layer</span></div>
                <div style={{ paddingLeft: 12 }}>→ <span style={{ color: "#8B5CF6" }}>Strategy Plugin</span> .evaluate()</div>
                <div style={{ paddingLeft: 24 }}>→ Signal[] → <span style={{ color: "#10B981" }}>Core Engine</span></div>
                <div style={{ paddingLeft: 36 }}>→ CostModel.calcTotalCost()</div>
                <div style={{ paddingLeft: 36 }}>→ RiskEngine.validate()</div>
                <div style={{ paddingLeft: 36 }}>→ OrderManager.execute()</div>
                <div style={{ paddingLeft: 48 }}>→ <span style={{ color: "#3B82F6" }}>Broker / Simulator</span></div>
              </div>
            </div>
          </div>
        )}

        {/* --- EXECUTION MODES TAB --- */}
        {activeTab === "modes" && (
          <div>
            <p style={{ color: "#94A3B8", fontSize: 12, lineHeight: 1.6, margin: "8px 0 14px" }}>
              One strategy, three execution modes. The <strong style={{ color: "#10B981" }}>IStrategy</strong> interface is identical across all modes — only the execution backend changes.
            </p>
            <div style={{ display: "flex", gap: 6, marginBottom: 14 }}>
              {EXECUTION_MODES.map((m, i) => (
                <button
                  key={m.mode}
                  onClick={() => setActiveMode(i)}
                  style={{
                    flex: 1,
                    padding: "10px 8px",
                    background: activeMode === i ? `${m.color}22` : "#0A1628",
                    border: activeMode === i ? `1px solid ${m.color}` : "1px solid #1E293B",
                    borderRadius: 8,
                    color: activeMode === i ? m.color : "#64748B",
                    fontSize: 11,
                    fontWeight: 800,
                    cursor: "pointer",
                    fontFamily: "'JetBrains Mono', monospace",
                  }}
                >
                  <div style={{ fontSize: 18, marginBottom: 4 }}>{m.icon}</div>
                  {m.mode}
                </button>
              ))}
            </div>
            {(() => {
              const m = EXECUTION_MODES[activeMode];
              return (
                <div style={{
                  background: "#0A1628",
                  border: `1px solid ${m.color}33`,
                  borderRadius: 8,
                  padding: 16,
                }}>
                  <p style={{ color: "#CBD5E1", fontSize: 13, margin: "0 0 14px", lineHeight: 1.5 }}>{m.desc}</p>
                  <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                    {m.flow.map((step, i) => (
                      <div key={i} style={{ display: "flex", alignItems: "center", gap: 10 }}>
                        <div style={{
                          width: 24, height: 24, borderRadius: "50%",
                          background: `${m.color}22`, border: `1px solid ${m.color}55`,
                          display: "flex", alignItems: "center", justifyContent: "center",
                          fontSize: 10, fontWeight: 800, color: m.color, flexShrink: 0,
                        }}>{i + 1}</div>
                        <span style={{ color: "#CBD5E1", fontSize: 12 }}>{step}</span>
                      </div>
                    ))}
                  </div>
                </div>
              );
            })()}
            <div style={{
              marginTop: 14, padding: 14, background: "#10B98112",
              border: "1px solid #10B98133", borderRadius: 8,
            }}>
              <div style={{ color: "#10B981", fontSize: 11, fontWeight: 700, fontFamily: "'JetBrains Mono', monospace", marginBottom: 6 }}>
                KEY PRINCIPLE
              </div>
              <p style={{ color: "#CBD5E1", fontSize: 12, margin: 0, lineHeight: 1.5 }}>
                Strategy code never knows which mode it's running in. The engine swaps the execution backend behind the same interface. This means a strategy tested in backtest behaves identically in live — no code changes needed.
              </p>
            </div>
          </div>
        )}

        {/* --- PLUGIN SDK TAB --- */}
        {activeTab === "plugin" && (
          <div>
            <p style={{ color: "#94A3B8", fontSize: 12, lineHeight: 1.6, margin: "8px 0 14px" }}>
              Every strategy implements this interface. Third-party devs build against the SDK, publish to the marketplace, and users install with one click.
            </p>
            <div style={{
              background: "#0A1628",
              border: "1px solid #8B5CF633",
              borderRadius: 8,
              padding: 14,
              marginBottom: 12,
            }}>
              <div style={{ color: "#8B5CF6", fontSize: 11, fontWeight: 700, fontFamily: "'JetBrains Mono', monospace", marginBottom: 10, letterSpacing: 1 }}>
                IStrategy INTERFACE
              </div>
              <pre style={{
                color: "#CBD5E1",
                fontSize: 10,
                fontFamily: "'JetBrains Mono', monospace",
                lineHeight: 1.6,
                margin: 0,
                whiteSpace: "pre-wrap",
                wordBreak: "break-word",
              }}>{PLUGIN_INTERFACE}</pre>
            </div>
            <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
              {[
                { title: "Sandboxed Execution", desc: "Each strategy runs in an isolated container. No filesystem or network access. Can only interact via the SDK interface.", color: "#EF4444" },
                { title: "Hot Reload", desc: "Update strategy code without restarting the engine. Version rollback supported.", color: "#F59E0B" },
                { title: "Config via JSON Schema", desc: "Strategies declare their tunable parameters. The UI auto-generates settings forms.", color: "#3B82F6" },
                { title: "Cost-Aware by Design", desc: "The evaluate() method receives the ICostModel so strategies can factor in costs before emitting signals.", color: "#10B981" },
              ].map((item) => (
                <div key={item.title} style={{
                  background: "#0A1628",
                  border: `1px solid ${item.color}22`,
                  borderLeft: `3px solid ${item.color}`,
                  borderRadius: 6,
                  padding: "10px 14px",
                }}>
                  <div style={{ color: item.color, fontSize: 11, fontWeight: 700, fontFamily: "'JetBrains Mono', monospace" }}>{item.title}</div>
                  <div style={{ color: "#94A3B8", fontSize: 11, marginTop: 3, lineHeight: 1.4 }}>{item.desc}</div>
                </div>
              ))}
            </div>
          </div>
        )}

        {/* --- COST MODEL TAB --- */}
        {activeTab === "costs" && (
          <div>
            <p style={{ color: "#94A3B8", fontSize: 12, lineHeight: 1.6, margin: "8px 0 14px" }}>
              The cost model is a first-class citizen — not an afterthought. Every signal passes through it before execution.
            </p>
            <div style={{
              background: "#0A1628",
              border: "1px solid #EF444433",
              borderRadius: 8,
              padding: 14,
              marginBottom: 12,
            }}>
              <div style={{ color: "#EF4444", fontSize: 11, fontWeight: 700, fontFamily: "'JetBrains Mono', monospace", marginBottom: 10, letterSpacing: 1 }}>
                ICostModel INTERFACE
              </div>
              <pre style={{
                color: "#CBD5E1",
                fontSize: 10,
                fontFamily: "'JetBrains Mono', monospace",
                lineHeight: 1.6,
                margin: 0,
                whiteSpace: "pre-wrap",
                wordBreak: "break-word",
              }}>{COST_MODEL}</pre>
            </div>
            <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
              {[
                { category: "Transaction Costs", items: "Commission, Spread (bid-ask), Slippage (market impact), Exchange fees", pct: "0.05–0.30%", color: "#F59E0B" },
                { category: "Tax Engine", items: "Short-term vs long-term gains, FIFO/LIFO lot selection, Wash sale detection, Dividend withholding", pct: "15–37%", color: "#EF4444" },
                { category: "Hidden Costs", items: "Opportunity cost of cash, Currency conversion, Margin interest, Data feed fees", pct: "Variable", color: "#8B5CF6" },
              ].map((c) => (
                <div key={c.category} style={{
                  background: "#0A1628",
                  border: `1px solid ${c.color}22`,
                  borderRadius: 8,
                  padding: 14,
                }}>
                  <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 6 }}>
                    <span style={{ color: c.color, fontSize: 12, fontWeight: 700, fontFamily: "'JetBrains Mono', monospace" }}>{c.category}</span>
                    <span style={{ color: c.color, fontSize: 10, fontFamily: "'JetBrains Mono', monospace", opacity: 0.7 }}>{c.pct}</span>
                  </div>
                  <div style={{ color: "#94A3B8", fontSize: 11, lineHeight: 1.5 }}>{c.items}</div>
                </div>
              ))}
            </div>
            <div style={{
              marginTop: 14, padding: 14, background: "#F59E0B12",
              border: "1px solid #F59E0B33", borderRadius: 8,
            }}>
              <div style={{ color: "#F59E0B", fontSize: 11, fontWeight: 700, fontFamily: "'JetBrains Mono', monospace", marginBottom: 6 }}>
                WHY THIS MATTERS
              </div>
              <p style={{ color: "#CBD5E1", fontSize: 12, margin: 0, lineHeight: 1.5 }}>
                A strategy showing 12% annual return in a naive backtest might only deliver 6% after all costs. By injecting the cost model into every evaluation, strategies learn to trade less frequently and more efficiently. This is your real edge.
              </p>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
