import React from "react";
import { BrowserRouter, Routes, Route, NavLink } from "react-router-dom";

const API = import.meta.env.VITE_API_URL || "http://localhost:8000";

// ── Pages (scaffolded) ──

function Dashboard() {
  const [health, setHealth] = React.useState(null);

  React.useEffect(() => {
    fetch(`${API}/health`)
      .then((r) => r.json())
      .then(setHealth)
      .catch(() => setHealth({ status: "unreachable" }));
  }, []);

  return (
    <div className="p-6">
      <h1 className="text-2xl font-bold mb-4">Dashboard</h1>
      <div className="bg-gray-800 rounded-lg p-4 mb-4">
        <h2 className="text-sm text-gray-400 uppercase mb-2">Engine Status</h2>
        {health ? (
          <pre className="text-sm text-green-400">{JSON.stringify(health, null, 2)}</pre>
        ) : (
          <p className="text-gray-500">Connecting...</p>
        )}
      </div>
      <p className="text-gray-400">Portfolio overview, P&L charts, and live positions will go here.</p>
    </div>
  );
}

function Strategies() {
  return (
    <div className="p-6">
      <h1 className="text-2xl font-bold mb-4">Strategy Lab</h1>
      <p className="text-gray-400">Install, configure, and A/B test trading strategies.</p>
    </div>
  );
}

function Backtest() {
  return (
    <div className="p-6">
      <h1 className="text-2xl font-bold mb-4">Backtest Studio</h1>
      <p className="text-gray-400">Run historical simulations with full cost modeling.</p>
    </div>
  );
}

function Marketplace() {
  return (
    <div className="p-6">
      <h1 className="text-2xl font-bold mb-4">Marketplace</h1>
      <p className="text-gray-400">Browse and install community strategy plugins.</p>
    </div>
  );
}

// ── Layout ──

const navItems = [
  { to: "/", label: "Dashboard", icon: "📊" },
  { to: "/strategies", label: "Strategies", icon: "⚡" },
  { to: "/backtest", label: "Backtest", icon: "⏪" },
  { to: "/marketplace", label: "Marketplace", icon: "🏪" },
];

function Layout({ children }) {
  return (
    <div className="min-h-screen bg-gray-950 text-white flex">
      {/* Sidebar */}
      <nav className="w-56 bg-gray-900 border-r border-gray-800 flex flex-col">
        <div className="p-4 border-b border-gray-800">
          <div className="flex items-center gap-2">
            <span className="text-lg font-bold bg-gradient-to-r from-blue-500 to-purple-500 bg-clip-text text-transparent">
              ⚡ NEXUS
            </span>
          </div>
          <p className="text-xs text-gray-500 mt-1">Trade Engine v0.1.0</p>
        </div>
        <div className="flex-1 p-3 space-y-1">
          {navItems.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.to === "/"}
              className={({ isActive }) =>
                `flex items-center gap-3 px-3 py-2 rounded-lg text-sm font-medium transition-colors ${
                  isActive
                    ? "bg-blue-600/20 text-blue-400"
                    : "text-gray-400 hover:text-white hover:bg-gray-800"
                }`
              }
            >
              <span>{item.icon}</span>
              {item.label}
            </NavLink>
          ))}
        </div>
      </nav>

      {/* Main content */}
      <main className="flex-1 overflow-auto">{children}</main>
    </div>
  );
}

// ── App ──

export default function App() {
  return (
    <BrowserRouter>
      <Layout>
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/strategies" element={<Strategies />} />
          <Route path="/backtest" element={<Backtest />} />
          <Route path="/marketplace" element={<Marketplace />} />
        </Routes>
      </Layout>
    </BrowserRouter>
  );
}
