import { useState } from "react";

const STRATEGY_TYPES = [
  {
    id: "algorithmic",
    label: "Fixed Algorithm",
    icon: "⚙️",
    color: "#3B82F6",
    example: "Mean Reversion, RSI Crossover, Bollinger Bands",
    desc: "Deterministic rules. Same input always produces the same output. Fast, auditable, no external dependencies.",
    code: `class MeanReversionStrategy(IStrategy):
    
    def evaluate(self, portfolio, market, costs):
        signals = []
        for symbol in self.watchlist:
            price = market.latest(symbol)
            sma = market.sma(symbol, period=50)
            std = market.std(symbol, period=50)
            
            if price < sma - 2 * std:
                cost = costs.estimate_total(
                    symbol, Side.BUY, self.position_size
                )
                if self._expected_return(price, sma) > cost:
                    signals.append(Signal.buy(symbol))
                    
        return signals`,
    traits: ["Deterministic", "Low latency", "No API costs", "Fully auditable"],
  },
  {
    id: "ml",
    label: "Neural Network / ML",
    icon: "🧠",
    color: "#8B5CF6",
    example: "LSTM price prediction, Transformer, CNN on charts, XGBoost ensemble",
    desc: "Developer trains and bundles their own model. The plugin loads weights at init and runs inference on each tick. GPU optional.",
    code: `class TransformerStrategy(IStrategy):
    
    def initialize(self, config):
        self.model = load_model(config.model_path)
        self.scaler = load_scaler(config.scaler_path)
        self.threshold = config.get("confidence", 0.7)
    
    def evaluate(self, portfolio, market, costs):
        features = self._build_features(market)
        scaled = self.scaler.transform(features)
        prediction = self.model.predict(scaled)
        
        signals = []
        for sym, pred in prediction.items():
            if pred.direction == UP and pred.confidence > self.threshold:
                net = pred.expected_return - costs.estimate_pct(sym)
                if net > self.min_net_return:
                    signals.append(Signal.buy(sym, weight=pred.confidence))
                    
        return signals`,
    traits: ["Custom models", "GPU support", "Bundled weights", "Adaptive"],
  },
  {
    id: "llm",
    label: "LLM-Powered",
    icon: "💬",
    color: "#10B981",
    example: "GPT/Claude for sentiment, earnings call analysis, news-driven allocation",
    desc: "Calls external LLM APIs for reasoning. Developer manages their own API keys, prompt engineering, and response parsing.",
    code: `class LLMSentimentStrategy(IStrategy):
    
    def initialize(self, config):
        self.llm = LLMClient(
            provider=config.llm_provider,  # "anthropic" | "openai" | ...
            api_key=config.api_key,        # developer's own key
            model=config.model_name
        )
        self.prompt_template = load_prompt(config.prompt_path)
    
    def evaluate(self, portfolio, market, costs):
        news = market.get_news(hours=24)
        
        response = self.llm.chat(
            self.prompt_template.format(
                news=news,
                portfolio=portfolio.summary(),
                budget=costs.estimate_budget()
            )
        )
        
        parsed = self._parse_signals(response)
        return self._apply_cost_filter(parsed, costs)`,
    traits: ["External API calls", "Developer's keys", "Prompt-driven", "Reasoning"],
  },
  {
    id: "hybrid",
    label: "Hybrid / Multi-Model",
    icon: "🔀",
    color: "#F59E0B",
    example: "RL agent + LLM reasoning + technical rules combined",
    desc: "Mix anything. An RL agent for position sizing, an LLM for macro context, fixed rules for risk. The developer orchestrates.",
    code: `class HybridStrategy(IStrategy):
    
    def initialize(self, config):
        self.rl_agent = RLAgent.load(config.rl_model_path)
        self.llm = LLMClient(config.llm_config)
        self.rules = TechnicalRules(config.rules)
    
    def evaluate(self, portfolio, market, costs):
        # Layer 1: Fixed rules filter the universe
        candidates = self.rules.screen(market)
        
        # Layer 2: LLM assesses macro context
        macro = self.llm.assess_macro(
            market.get_macro_indicators()
        )
        
        # Layer 3: RL agent decides sizing
        signals = []
        for sym in candidates:
            state = self._build_state(sym, market, macro)
            action = self.rl_agent.act(state)
            
            if action.should_trade:
                net = action.expected - costs.estimate_total(sym)
                if net > 0:
                    signals.append(Signal(
                        sym, action.side, weight=action.size
                    ))
                    
        return signals`,
    traits: ["Any combination", "Multi-model", "Full orchestration", "Maximum flexibility"],
  },
];

const SDK_FEATURES = [
  {
    title: "What the SDK Provides",
    color: "#3B82F6",
    items: [
      { name: "Market Data Access", desc: "OHLCV, tick data, order book, indicators, news feeds, alternative data" },
      { name: "Portfolio Snapshot", desc: "Current positions, cash, unrealized P&L, allocation weights" },
      { name: "Cost Model", desc: "Pre-trade cost estimation so strategies can decide if a trade is worth it" },
      { name: "Logging & Metrics", desc: "Structured logging, custom metrics, performance attribution" },
      { name: "Config System", desc: "JSON Schema-driven params auto-rendered as UI forms" },
      { name: "Secrets Vault", desc: "Encrypted storage for API keys (LLM, data providers, etc.)" },
    ],
  },
  {
    title: "What the Developer Controls",
    color: "#10B981",
    items: [
      { name: "Internal Logic", desc: "Any algorithm, any model, any API — zero restrictions on approach" },
      { name: "Model Weights", desc: "Bundle trained models as artifacts. Load at init, run inference freely" },
      { name: "External APIs", desc: "Call LLMs, data vendors, custom services — developer manages their own keys" },
      { name: "Dependencies", desc: "Declare pip/npm deps in manifest. Installed in sandboxed runtime" },
      { name: "Data Preprocessing", desc: "Feature engineering, normalization, custom indicators — all internal" },
      { name: "Signal Logic", desc: "Full control over when, what, and how much to trade" },
    ],
  },
  {
    title: "What the Engine Enforces",
    color: "#EF4444",
    items: [
      { name: "Sandbox Boundary", desc: "No direct filesystem, no raw network (only declared API endpoints)" },
      { name: "Signal Format", desc: "Outputs must be valid Signal[] objects — the only contract" },
      { name: "Resource Limits", desc: "Max memory, CPU time, API call rate per evaluation cycle" },
      { name: "Risk Limits", desc: "Engine can veto signals that violate portfolio-level risk rules" },
      { name: "Audit Trail", desc: "Every signal, every cost calculation, every fill — logged immutably" },
      { name: "Version Pinning", desc: "Published strategies are immutable. New version = new release" },
    ],
  },
];

const MANIFEST_EXAMPLE = `# strategy.manifest.yaml

id: "hybrid-momentum-v2"
name: "AI Momentum Pro"
version: "2.1.0"
author: "dev@example.com"
license: "commercial"
min_engine_version: "1.4.0"

# What the strategy needs
runtime: "python:3.11"
dependencies:
  - torch>=2.0
  - anthropic>=0.25
  - ta-lib>=0.4

resources:
  max_memory: "2GB"
  gpu: optional
  
# Bundled artifacts
artifacts:
  - models/transformer_v2.pt
  - models/scaler.pkl
  - prompts/macro_analysis.txt

# External API access (whitelisted)
network:
  allowed_endpoints:
    - api.anthropic.com
    - api.openai.com
    - data.provider.com

# User-configurable parameters
config_schema:
  type: object
  properties:
    confidence_threshold:
      type: number
      default: 0.7
      min: 0.5
      max: 0.99
      description: "Min prediction confidence to trigger a trade"
    llm_provider:
      type: string
      enum: ["anthropic", "openai"]
      default: "anthropic"
    risk_per_trade:
      type: number
      default: 0.02
      description: "Max portfolio % risked per trade"

# Marketplace metadata
marketplace:
  category: "hybrid-ai"
  tags: ["momentum", "llm", "neural-net"]
  min_capital: 10000
  preferred_assets: ["US equities", "ETFs"]
  backtest_required: true`;

function CodeBlock({ code, color }) {
  return (
    <pre style={{
      background: "#060E1A",
      border: `1px solid ${color}22`,
      borderRadius: 8,
      padding: 14,
      color: "#CBD5E1",
      fontSize: 10,
      fontFamily: "'JetBrains Mono', monospace",
      lineHeight: 1.7,
      margin: 0,
      whiteSpace: "pre-wrap",
      wordBreak: "break-word",
      overflowX: "auto",
    }}>
      {code}
    </pre>
  );
}

export default function PluginSDK() {
  const [activeType, setActiveType] = useState("algorithmic");
  const [activeTab, setActiveTab] = useState("types");
  const [expandedSection, setExpandedSection] = useState(0);

  const currentType = STRATEGY_TYPES.find((t) => t.id === activeType);

  const tabs = [
    { id: "types", label: "Strategy Types" },
    { id: "sdk", label: "SDK Contract" },
    { id: "manifest", label: "Manifest" },
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
            background: "linear-gradient(135deg, #8B5CF6, #10B981)",
            display: "flex", alignItems: "center", justifyContent: "center",
            fontSize: 14, fontWeight: 900, color: "#fff",
          }}>⚡</div>
          <span style={{ fontSize: 17, fontWeight: 800, letterSpacing: -0.5, color: "#F8FAFC" }}>
            PLUGIN SDK
          </span>
        </div>
        <p style={{ color: "#64748B", fontSize: 11, margin: 0, fontFamily: "'JetBrains Mono', monospace", letterSpacing: 1 }}>
          BUILD ANYTHING — ALGORITHMS · ML · LLMs · HYBRIDS
        </p>
      </div>

      {/* Tabs */}
      <div style={{ display: "flex", gap: 0, borderBottom: "1px solid #1E293B", background: "#0A1628" }}>
        {tabs.map((t) => (
          <button
            key={t.id}
            onClick={() => setActiveTab(t.id)}
            style={{
              flex: 1, padding: "10px 8px",
              background: activeTab === t.id ? "#111B2E" : "transparent",
              color: activeTab === t.id ? "#F8FAFC" : "#64748B",
              border: "none",
              borderBottom: activeTab === t.id ? "2px solid #8B5CF6" : "2px solid transparent",
              fontSize: 11, fontWeight: 700, cursor: "pointer",
              fontFamily: "'JetBrains Mono', monospace",
            }}
          >{t.label}</button>
        ))}
      </div>

      <div style={{ padding: "12px 12px 24px" }}>

        {/* STRATEGY TYPES */}
        {activeTab === "types" && (
          <div>
            <p style={{ color: "#94A3B8", fontSize: 12, lineHeight: 1.6, margin: "8px 0 14px" }}>
              The <code style={{ color: "#8B5CF6" }}>evaluate()</code> method is the only contract. What runs inside is entirely up to the developer.
            </p>

            {/* Type selector */}
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 6, marginBottom: 14 }}>
              {STRATEGY_TYPES.map((t) => (
                <button
                  key={t.id}
                  onClick={() => setActiveType(t.id)}
                  style={{
                    padding: "10px 10px",
                    background: activeType === t.id ? `${t.color}18` : "#0A1628",
                    border: activeType === t.id ? `1px solid ${t.color}` : "1px solid #1E293B",
                    borderRadius: 8,
                    color: activeType === t.id ? t.color : "#64748B",
                    fontSize: 11, fontWeight: 700, cursor: "pointer",
                    fontFamily: "'JetBrains Mono', monospace",
                    textAlign: "left",
                    transition: "all 0.15s ease",
                  }}
                >
                  <div style={{ fontSize: 18, marginBottom: 4 }}>{t.icon}</div>
                  {t.label}
                </button>
              ))}
            </div>

            {/* Detail card */}
            {currentType && (
              <div>
                <div style={{
                  background: "#0A1628",
                  border: `1px solid ${currentType.color}33`,
                  borderRadius: 8,
                  padding: 14,
                  marginBottom: 10,
                }}>
                  <div style={{ color: currentType.color, fontSize: 11, fontWeight: 700, fontFamily: "'JetBrains Mono', monospace", letterSpacing: 1, marginBottom: 4 }}>
                    {currentType.example}
                  </div>
                  <p style={{ color: "#CBD5E1", fontSize: 12, margin: "6px 0 12px", lineHeight: 1.5 }}>
                    {currentType.desc}
                  </p>
                  <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
                    {currentType.traits.map((trait) => (
                      <span key={trait} style={{
                        background: `${currentType.color}15`,
                        border: `1px solid ${currentType.color}33`,
                        borderRadius: 4,
                        padding: "3px 8px",
                        fontSize: 10,
                        color: currentType.color,
                        fontFamily: "'JetBrains Mono', monospace",
                      }}>{trait}</span>
                    ))}
                  </div>
                </div>
                <div style={{ color: "#64748B", fontSize: 10, fontFamily: "'JetBrains Mono', monospace", marginBottom: 6, letterSpacing: 1 }}>
                  EXAMPLE IMPLEMENTATION
                </div>
                <CodeBlock code={currentType.code} color={currentType.color} />
              </div>
            )}
          </div>
        )}

        {/* SDK CONTRACT */}
        {activeTab === "sdk" && (
          <div>
            <p style={{ color: "#94A3B8", fontSize: 12, lineHeight: 1.6, margin: "8px 0 14px" }}>
              Clear boundaries: the SDK gives, the developer builds, the engine enforces.
            </p>
            {SDK_FEATURES.map((section, i) => (
              <div key={section.title} style={{ marginBottom: 8 }}>
                <button
                  onClick={() => setExpandedSection(expandedSection === i ? -1 : i)}
                  style={{
                    width: "100%",
                    padding: "12px 14px",
                    background: "#0A1628",
                    border: `1px solid ${section.color}22`,
                    borderLeft: `3px solid ${section.color}`,
                    borderRadius: 8,
                    color: section.color,
                    fontSize: 12,
                    fontWeight: 700,
                    fontFamily: "'JetBrains Mono', monospace",
                    cursor: "pointer",
                    textAlign: "left",
                    display: "flex",
                    justifyContent: "space-between",
                    alignItems: "center",
                    letterSpacing: 0.5,
                  }}
                >
                  {section.title}
                  <span style={{ transform: expandedSection === i ? "rotate(180deg)" : "rotate(0)", transition: "transform 0.2s" }}>▼</span>
                </button>
                {expandedSection === i && (
                  <div style={{
                    background: "#0A162844",
                    border: `1px solid ${section.color}11`,
                    borderTop: "none",
                    borderRadius: "0 0 8px 8px",
                    padding: "8px 10px",
                  }}>
                    {section.items.map((item) => (
                      <div key={item.name} style={{
                        padding: "8px 10px",
                        borderBottom: "1px solid #1E293B44",
                      }}>
                        <div style={{ color: "#F8FAFC", fontSize: 12, fontWeight: 600 }}>{item.name}</div>
                        <div style={{ color: "#94A3B8", fontSize: 11, marginTop: 2, lineHeight: 1.4 }}>{item.desc}</div>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            ))}

            <div style={{
              marginTop: 14, padding: 14, background: "#8B5CF612",
              border: "1px solid #8B5CF633", borderRadius: 8,
            }}>
              <div style={{ color: "#8B5CF6", fontSize: 11, fontWeight: 700, fontFamily: "'JetBrains Mono', monospace", marginBottom: 6 }}>
                THE GOLDEN RULE
              </div>
              <p style={{ color: "#CBD5E1", fontSize: 12, margin: 0, lineHeight: 1.5 }}>
                <strong>Input:</strong> MarketState + PortfolioSnapshot + ICostModel<br />
                <strong>Output:</strong> Signal[]<br /><br />
                Everything between input and output is a black box that belongs to the developer. The engine doesn't care if you're using a 3-line moving average crossover or a 70B parameter LLM — it only sees the signals.
              </p>
            </div>
          </div>
        )}

        {/* MANIFEST */}
        {activeTab === "manifest" && (
          <div>
            <p style={{ color: "#94A3B8", fontSize: 12, lineHeight: 1.6, margin: "8px 0 14px" }}>
              Every plugin ships with a manifest declaring its identity, dependencies, resource needs, network access, config schema, and marketplace metadata.
            </p>
            <CodeBlock code={MANIFEST_EXAMPLE} color="#F59E0B" />
            <div style={{
              marginTop: 12, padding: 14, background: "#F59E0B12",
              border: "1px solid #F59E0B33", borderRadius: 8,
            }}>
              <div style={{ color: "#F59E0B", fontSize: 11, fontWeight: 700, fontFamily: "'JetBrains Mono', monospace", marginBottom: 6 }}>
                MANIFEST POWERS
              </div>
              <div style={{ color: "#CBD5E1", fontSize: 12, lineHeight: 1.6 }}>
                <div style={{ marginBottom: 4 }}>
                  <strong style={{ color: "#F59E0B" }}>Auto-UI:</strong> config_schema generates settings forms automatically
                </div>
                <div style={{ marginBottom: 4 }}>
                  <strong style={{ color: "#F59E0B" }}>Security:</strong> network whitelist prevents unauthorized data exfiltration
                </div>
                <div style={{ marginBottom: 4 }}>
                  <strong style={{ color: "#F59E0B" }}>Reproducibility:</strong> pinned deps + bundled artifacts = identical behavior everywhere
                </div>
                <div>
                  <strong style={{ color: "#F59E0B" }}>Marketplace:</strong> metadata drives search, filtering, and compatibility checks
                </div>
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
