import React from "react";
import { BrowserRouter, Routes, Route, Navigate, Outlet } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { AuthProvider } from "./auth/AuthContext";
import { ProtectedRoute } from "./auth/ProtectedRoute";
import { Shell } from "./components/layout/Shell";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { LegalProvider } from "./context/LegalContext";
import { ConsentModal } from "./components/legal/ConsentModal";
import { OnboardingManager } from "./components/onboarding/OnboardingManager";
import { ThemeProvider } from "./providers/ThemeProvider";
import { ToastProvider } from "./providers/ToastProvider";
import { ToastContainer } from "./components/ui/Toast";
import { WebSocketProvider } from "./providers/WebSocketProvider";
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
import DashboardPage from "./pages/dashboard/DashboardPage";
import StrategyListPage from "./pages/strategies/StrategyListPage";
import StrategyDetailPage from "./pages/strategies/StrategyDetailPage";
import StrategyCreatePage from "./pages/strategies/StrategyCreatePage";
import PortfolioOverviewPage from "./pages/portfolio/PortfolioOverviewPage";
import PositionsPage from "./pages/portfolio/PositionsPage";
import PerformancePage from "./pages/portfolio/PerformancePage";
import RiskAnalysisPage from "./pages/portfolio/RiskAnalysisPage";
import BacktestListPage from "./pages/backtest/BacktestListPage";
import BacktestConfigPage from "./pages/backtest/BacktestConfigPage";
import BacktestResultsPage from "./pages/backtest/BacktestResultsPage";
import SettingsPage from "./pages/settings/SettingsPage";

const WS_URL = import.meta.env.VITE_WS_URL || "ws://localhost:8000/ws";

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
      <OnboardingManager />
      <Shell>
        <ErrorBoundary scope="page">
          <Outlet />
        </ErrorBoundary>
      </Shell>
    </LegalProvider>
  );
}

export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <AuthProvider>
          <ThemeProvider>
            <ToastProvider>
              <WebSocketProvider url={WS_URL}>
                <ToastContainer />
                <ErrorBoundary scope="app">
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
                      <Route path="/dashboard" element={<DashboardPage />} />
                      <Route path="/market-watch" element={<MarketWatch />} />
                      <Route path="/strategies" element={<Strategies />} />
                      <Route path="/strategies/v2" element={<StrategyListPage />} />
                      <Route path="/strategies/create" element={<StrategyCreatePage />} />
                      <Route path="/strategies/:id" element={<StrategyDetailPage />} />
                      <Route path="/backtest" element={<Backtest />} />
                      <Route path="/backtest/v2" element={<BacktestListPage />} />
                      <Route path="/backtest/config" element={<BacktestConfigPage />} />
                      <Route path="/backtest/:id" element={<BacktestResultsPage />} />
                      <Route path="/marketplace" element={<Marketplace />} />
                      <Route path="/positions" element={<Positions />} />
                      <Route path="/portfolio" element={<PortfolioOverviewPage />} />
                      <Route path="/portfolio/positions" element={<PositionsPage />} />
                      <Route path="/portfolio/performance" element={<PerformancePage />} />
                      <Route path="/portfolio/risk" element={<RiskAnalysisPage />} />
                      <Route path="/costs" element={<CostAnalysis />} />
                      <Route path="/risk" element={<RiskMonitor />} />
                      <Route path="/dev" element={<DevConsole />} />
                      <Route path="/settings" element={<Settings />} />
                      <Route path="/settings/v2" element={<SettingsPage />} />
                      <Route path="/legal/:slug" element={<LegalDocument />} />
                      <Route path="*" element={<Navigate to="/" replace />} />
                    </Route>
                  </Routes>
                </ErrorBoundary>
              </WebSocketProvider>
            </ToastProvider>
          </ThemeProvider>
        </AuthProvider>
      </BrowserRouter>
    </QueryClientProvider>
  );
}
