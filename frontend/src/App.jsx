import React from "react";
import { BrowserRouter, Routes, Route } from "react-router-dom";
import { Shell } from "./components/layout/Shell";
import Dashboard from "./screens/Dashboard";
import Strategies from "./screens/Strategies";
import Backtest from "./screens/Backtest";
import Marketplace from "./screens/Marketplace";
import Positions from "./screens/Positions";
import CostAnalysis from "./screens/CostAnalysis";
import RiskMonitor from "./screens/RiskMonitor";
import DevConsole from "./screens/DevConsole";

export default function App() {
  return (
    <BrowserRouter>
      <Shell>
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/strategies" element={<Strategies />} />
          <Route path="/backtest" element={<Backtest />} />
          <Route path="/marketplace" element={<Marketplace />} />
          <Route path="/positions" element={<Positions />} />
          <Route path="/costs" element={<CostAnalysis />} />
          <Route path="/risk" element={<RiskMonitor />} />
          <Route path="/dev" element={<DevConsole />} />
        </Routes>
      </Shell>
    </BrowserRouter>
  );
}
