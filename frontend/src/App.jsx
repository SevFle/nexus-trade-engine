import React from "react";
import { BrowserRouter, Routes, Route, NavLink, Navigate } from "react-router-dom";
import { AuthProvider, useAuth } from "./contexts/AuthContext";
import { ProtectedRoute } from "./components/ProtectedRoute";
import Login from "./screens/Login";
import Register from "./screens/Register";
import AuthCallback from "./screens/AuthCallback";

const API = import.meta.env.VITE_API_URL || "http://localhost:8000";

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

const navItems = [
  { to: "/", label: "Dashboard", icon: "\uD83D\uDCCA" },
  { to: "/strategies", label: "Strategies", icon: "\u26A1" },
  { to: "/backtest", label: "Backtest", icon: "\u23EA" },
  { to: "/marketplace", label: "Marketplace", icon: "\uD83C\uDFEA" },
];

function UserMenu() {
  const { user, logout } = useAuth();

  if (!user) return null;

  return (
    <div className="flex items-center gap-3">
      <span className="text-xs text-gray-400">
        {user.display_name || user.email}
      </span>
      <button
        type="button"
        onClick={logout}
        className="text-xs text-gray-500 hover:text-white transition-colors"
      >
        Sign out
      </button>
    </div>
  );
}

function Layout({ children }) {
  return (
    <div className="min-h-screen bg-gray-950 text-white flex">
      <nav className="w-56 bg-gray-900 border-r border-gray-800 flex flex-col">
        <div className="p-4 border-b border-gray-800">
          <div className="flex items-center gap-2">
            <span className="text-lg font-bold bg-gradient-to-r from-blue-500 to-purple-500 bg-clip-text text-transparent">
              NEXUS
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
        <div className="p-3 border-t border-gray-800">
          <UserMenu />
        </div>
      </nav>

      <main className="flex-1 overflow-auto">{children}</main>
    </div>
  );
}

function AppRoutes() {
  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route path="/register" element={<Register />} />
      <Route path="/auth/callback/:provider" element={<AuthCallback />} />
      <Route
        path="/"
        element={
          <ProtectedRoute>
            <Layout>
              <Dashboard />
            </Layout>
          </ProtectedRoute>
        }
      />
      <Route
        path="/strategies"
        element={
          <ProtectedRoute>
            <Layout>
              <Strategies />
            </Layout>
          </ProtectedRoute>
        }
      />
      <Route
        path="/backtest"
        element={
          <ProtectedRoute>
            <Layout>
              <Backtest />
            </Layout>
          </ProtectedRoute>
        }
      />
      <Route
        path="/marketplace"
        element={
          <ProtectedRoute>
            <Layout>
              <Marketplace />
            </Layout>
          </ProtectedRoute>
        }
      />
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}

export default function App() {
  return (
    <BrowserRouter>
      <AuthProvider>
        <AppRoutes />
      </AuthProvider>
    </BrowserRouter>
  );
}
