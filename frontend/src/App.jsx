import React from "react";
import { BrowserRouter, Routes, Route, Outlet } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Shell } from "./components/layout/Shell";
import { LegalProvider } from "./context/LegalContext";
import { ConsentModal } from "./components/legal/ConsentModal";
import Dashboard from "./screens/Dashboard";
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
    <Shell>
      <Outlet />
    </Shell>
  );
}

export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <LegalProvider>
          <ConsentModal />
          <Routes>
            <Route path="/onboarding" element={<Onboarding />} />
            <Route element={<ShellLayout />}>
              <Route path="/" element={<Dashboard />} />
              <Route path="/strategies" element={<Strategies />} />
              <Route path="/backtest" element={<Backtest />} />
              <Route path="/marketplace" element={<Marketplace />} />
              <Route path="/positions" element={<Positions />} />
              <Route path="/costs" element={<CostAnalysis />} />
              <Route path="/risk" element={<RiskMonitor />} />
              <Route path="/dev" element={<DevConsole />} />
              <Route path="/settings" element={<Settings />} />
              <Route path="/legal/:slug" element={<LegalDocument />} />
            </Route>
          </Routes>
        </LegalProvider>
      </BrowserRouter>
    </QueryClientProvider>
  );
}
