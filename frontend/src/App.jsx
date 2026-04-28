import React from "react";
import { BrowserRouter, Routes, Route, Navigate, Outlet } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { AuthProvider } from "./auth/AuthContext";
import { ProtectedRoute } from "./auth/ProtectedRoute";
import { Shell } from "./components/layout/Shell";
import { LegalProvider } from "./context/LegalContext";
import { ConsentModal } from "./components/legal/ConsentModal";
import Login from "./pages/Login";
import Register from "./pages/Register";
import OAuthCallback from "./pages/OAuthCallback";
import Dashboard from "./screens/Dashboard";
import MarketWatch from "./screens/MarketWatch";
import Strategies from "./screens/Strategies";
import Backtest from "./screens/Backtest";
import Marketplace from "./screens/Marketplace";
import Positions from "./screens/Positions";
import CostAnalysis from "./screens/CostAnalysis";
import RiskMonitor from "./screens/RiskMonitor";
import DevConsole from "./screens/DevConsole";
import Settings from "./screens/Settings";
import LegalDocument from "./screens/LegalDocument";
import Onboarding from "./screens/Onboarding";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: false,
      refetchOnWindowFocus: false,
    },
  },
});

function ShellLayout() {
  return (
    <LegalProvider>
      <ConsentModal />
      <Shell>
        <Outlet />
      </Shell>
    </LegalProvider>
  );
}

export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <AuthProvider>
          <Routes>
            <Route path="/login" element={<Login />} />
            <Route path="/register" element={<Register />} />
            <Route path="/auth/callback" element={<OAuthCallback />} />
            <Route path="/onboarding" element={<Onboarding />} />
            <Route
              element={
                <ProtectedRoute>
                  <ShellLayout />
                </ProtectedRoute>
              }
            >
              <Route path="/" element={<Dashboard />} />
              <Route path="/market-watch" element={<MarketWatch />} />
              <Route path="/strategies" element={<Strategies />} />
              <Route path="/backtest" element={<Backtest />} />
              <Route path="/marketplace" element={<Marketplace />} />
              <Route path="/positions" element={<Positions />} />
              <Route path="/costs" element={<CostAnalysis />} />
              <Route path="/risk" element={<RiskMonitor />} />
              <Route path="/dev" element={<DevConsole />} />
              <Route path="/settings" element={<Settings />} />
              <Route path="/legal/:slug" element={<LegalDocument />} />
              <Route path="*" element={<Navigate to="/" replace />} />
            </Route>
          </Routes>
        </AuthProvider>
      </BrowserRouter>
    </QueryClientProvider>
  );
}
