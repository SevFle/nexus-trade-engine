import { useState } from "react";

const CATEGORIES = ["ALL", "MOMENTUM", "MEAN REVERSION", "ARBITRAGE", "MACRO", "ML/AI"];

const MOCK_STRATEGIES = [
  {
    id: "strat-001",
    name: "Momentum Alpha v3.2",
    author: "NEXUS CORE",
    tags: ["MOMENTUM", "EQUITIES", "DAILY"],
    sharpe: 2.34,
    installs: 12847,
    status: "verified",
  },
  {
    id: "strat-002",
    name: "Statistical Pairs Engine",
    author: "QUANT LABS",
    tags: ["MEAN REVERSION", "PAIRS", "INTRADAY"],
    sharpe: 1.89,
    installs: 8431,
    status: "verified",
  },
  {
    id: "strat-003",
    name: "Vol Surface Arbitrage",
    author: "DERIV RESEARCH",
    tags: ["ARBITRAGE", "OPTIONS", "VOLATILITY"],
    sharpe: 3.12,
    installs: 3204,
    status: "community",
  },
];

export default function Marketplace() {
  const [search, setSearch] = useState("");
  const [activeCategory, setActiveCategory] = useState("ALL");

  const filtered = MOCK_STRATEGIES.filter((s) => {
    const matchesSearch =
      s.name.toLowerCase().includes(search.toLowerCase()) ||
      s.author.toLowerCase().includes(search.toLowerCase());
    const matchesCategory =
      activeCategory === "ALL" ||
      s.tags.some((t) => t.includes(activeCategory.replace("/", "")));
    return matchesSearch && matchesCategory;
  });

  return (
    <div className="text-nx-text-primary p-xl">
      <div className="max-w-6xl mx-auto">
        <header className="mb-3xl">
          <span className="text-label font-mono uppercase text-nx-text-secondary block mb-sm">
            MARKETPLACE
          </span>
          <h1 className="text-display-md font-display text-nx-text-display">
            STRATEGY CATALOG
          </h1>
        </header>

        <section className="mb-2xl">
          <div className="flex items-center gap-md mb-lg">
            <div className="flex-1 relative">
              <input
                type="text"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                placeholder="SEARCH STRATEGIES..."
                className="w-full bg-nx-surface border border-nx-border rounded-xl px-lg py-md text-body-sm font-mono text-nx-text-primary placeholder:text-nx-text-disabled focus:outline-none focus:border-nx-border-visible"
              />
            </div>
          </div>
          <div className="flex gap-xs flex-wrap">
            {CATEGORIES.map((cat) => (
              <button
                type="button"
                key={cat}
                onClick={() => setActiveCategory(cat)}
                className={`px-md py-xs text-label font-mono uppercase border rounded-full transition-colors ${
                  activeCategory === cat
                    ? "bg-nx-text-display text-nx-black border-nx-text-display"
                    : "bg-transparent text-nx-text-secondary border-nx-border hover:border-nx-border-visible"
                }`}
              >
                {cat}
              </button>
            ))}
          </div>
        </section>

        <section className="grid grid-cols-3 gap-md">
          {filtered.map((strategy) => (
            <div
              key={strategy.id}
              className="bg-nx-surface border border-nx-border rounded-2xl p-lg flex flex-col"
            >
              <div className="flex items-start justify-between mb-md">
                <div>
                  <h3 className="text-subheading font-body text-nx-text-primary mb-xs">
                    {strategy.name}
                  </h3>
                  <span className="text-label font-mono uppercase text-nx-text-secondary">
                    BY {strategy.author}
                  </span>
                </div>
                <span
                  className={`text-label font-mono uppercase px-sm py-2xs border rounded ${
                    strategy.status === "verified"
                      ? "text-nx-success border-nx-success"
                      : "text-nx-text-secondary border-nx-border"
                  }`}
                >
                  {strategy.status.toUpperCase()}
                </span>
              </div>

              <div className="flex gap-xs flex-wrap mb-lg">
                {strategy.tags.map((tag) => (
                  <span
                    key={tag}
                    className="text-label font-mono uppercase text-nx-text-disabled bg-nx-surface-raised px-sm py-2xs rounded"
                  >
                    {tag}
                  </span>
                ))}
              </div>

              <div className="mt-auto flex items-baseline justify-between pt-md border-t border-nx-border">
                <div>
                  <span className="text-label font-mono uppercase text-nx-text-secondary block">
                    SHARPE
                  </span>
                  <span className="text-heading font-display text-nx-success tabular-nums">
                    {strategy.sharpe.toFixed(2)}
                  </span>
                </div>
                <div className="text-right">
                  <span className="text-label font-mono uppercase text-nx-text-secondary block">
                    INSTALLS
                  </span>
                  <span className="text-body-sm font-mono text-nx-text-primary tabular-nums">
                    {strategy.installs.toLocaleString()}
                  </span>
                </div>
              </div>
            </div>
          ))}
        </section>

        {filtered.length === 0 && (
          <div className="flex items-center justify-center py-4xl">
            <span className="text-label font-mono uppercase text-nx-text-disabled">
              NO STRATEGIES MATCH YOUR FILTERS
            </span>
          </div>
        )}
      </div>
    </div>
  );
}
