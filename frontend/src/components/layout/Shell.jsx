import React from "react";
import { NavLink, useLocation } from "react-router-dom";
import { LayoutDashboard, Zap, Rewind, Store, BarChart3, Shield, Terminal, DollarSign } from "lucide-react";
import clsx from "clsx";
import { useTheme } from "../../hooks/useTheme";

const navItems = [
  { to: "/", label: "DASHBOARD", icon: LayoutDashboard },
  { to: "/strategies", label: "STRATEGIES", icon: Zap },
  { to: "/backtest", label: "BACKTEST", icon: Rewind },
  { to: "/marketplace", label: "MARKETPLACE", icon: Store },
  { to: "/positions", label: "POSITIONS", icon: BarChart3 },
  { to: "/costs", label: "COST ANALYSIS", icon: DollarSign },
  { to: "/risk", label: "RISK MONITOR", icon: Shield },
  { to: "/dev", label: "DEV CONSOLE", icon: Terminal },
];

function Sidebar({ collapsed, onToggle }) {
  const location = useLocation();

  return (
    <nav
      className={clsx(
        "h-screen flex flex-col border-r border-nx-border transition-all duration-300",
        "bg-nx-surface",
        collapsed ? "w-16" : "w-60",
      )}
    >
      <div className="p-md border-b border-nx-border flex items-center justify-between">
        {!collapsed && (
          <span className="text-label font-mono uppercase text-nx-text-display tracking-widest">
            Nexus
          </span>
        )}
        <button
          type="button"
          onClick={onToggle}
          className="text-nx-text-secondary hover:text-nx-text-display text-sm"
        >
          {collapsed ? "+" : "-"}
        </button>
      </div>

      <div className="flex-1 py-sm">
        {navItems.map(({ to, label, icon: Icon }) => {
          const isActive = location.pathname === to || (to !== "/" && location.pathname.startsWith(to));
          return (
            <NavLink
              key={to}
              to={to}
              end={to === "/"}
              className={clsx(
                "flex items-center gap-md px-md py-sm mx-xs text-label font-mono transition-colors",
                isActive
                  ? "text-nx-text-display border-l-2 border-nx-accent bg-nx-accent-subtle"
                  : "text-nx-text-secondary hover:text-nx-text-primary",
              )}
            >
              <Icon size={16} strokeWidth={1.5} />
              {!collapsed && <span>{label}</span>}
            </NavLink>
          );
        })}
      </div>

      <div className="p-md border-t border-nx-border">
        {!collapsed && (
          <span className="text-caption font-mono text-nx-text-disabled">v0.1.0</span>
        )}
      </div>
    </nav>
  );
}

export function Shell({ children }) {
  const [collapsed, setCollapsed] = React.useState(false);
  const { mode, toggle } = useTheme();

  return (
    <div className="flex min-h-screen bg-nx-black">
      <Sidebar collapsed={collapsed} onToggle={() => setCollapsed((c) => !c)} />
      <main className="flex-1 overflow-auto">
        <div className="flex justify-end p-sm">
          <button
            type="button"
            onClick={toggle}
            className="text-label font-mono uppercase text-nx-text-secondary hover:text-nx-text-display"
          >
            [{mode === "dark" ? "LIGHT" : "DARK"}]
          </button>
        </div>
        {children}
      </main>
    </div>
  );
}
