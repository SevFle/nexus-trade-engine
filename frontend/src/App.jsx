import React, { useState, useEffect, useCallback } from "react";
import { BrowserRouter, Routes, Route, useNavigate, useLocation } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Shell } from "./components/layout/Shell";
import { LegalFooter } from "./components/legal/LegalFooter";
import { ConsentModal } from "./components/legal/ConsentModal";
import { useLegalDocuments, useAcceptances } from "./hooks/useLegal";

import Dashboard from "./screens/Dashboard";
import Strategies from "./screens/Strategies";
import Backtest from "./screens/Backtest";
import Marketplace from "./screens/Marketplace";
import Positions from "./screens/Positions";
import CostAnalysis from "./screens/CostAnalysis";
import RiskMonitor from "./screens/RiskMonitor";
import DevConsole from "./screens/DevConsole";
import Settings from "./screens/Settings";
import LegalDocumentPage from "./screens/LegalDocumentPage";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: (failureCount, error) => {
        if (error?.status === 451) return false;
        return failureCount < 3;
      },
      staleTime: 5 * 60 * 1000,
    },
  },
});

function useConsentCheck() {
  const { data: documents } = useLegalDocuments();
  const { data: acceptances } = useAcceptances();
  const [showConsent, setShowConsent] = useState(false);

  useEffect(() => {
    if (!documents || !acceptances) return;
    const acceptedMap = new Map(
      (acceptances || []).map((a) => [a.document_slug, a.version])
    );
    const pending = (documents || []).filter(
      (doc) => doc.requires_acceptance && acceptedMap.get(doc.slug) !== doc.current_version
    );
    if (pending.length > 0) {
      setShowConsent(true);
    }
  }, [documents, acceptances]);

  return { showConsent, setShowConsent };
}

function Http451Handler() {
  const navigate = useNavigate();

  useEffect(() => {
    const handle451 = (event) => {
      if (event.detail?.pendingDocuments) {
        navigate("/", { state: { consentRequired: true } });
      }
    };
    window.addEventListener("legal-consent-required", handle451);
    return () => window.removeEventListener("legal-consent-required", handle451);
  }, [navigate]);

  return null;
}

function AppContent() {
  const { showConsent, setShowConsent } = useConsentCheck();
  const location = useLocation();
  const consentFromState = location.state?.consentRequired;
  const isConsentVisible = showConsent || consentFromState;

  const handleConsentComplete = useCallback(() => {
    setShowConsent(false);
    window.history.replaceState({}, document.title);
  }, [setShowConsent]);

  return (
    <Shell>
      <div className="flex flex-col min-h-full">
        <div className="flex-1">
          <Routes>
            <Route path="/" element={<Dashboard />} />
            <Route path="/strategies" element={<Strategies />} />
            <Route path="/backtest" element={<Backtest />} />
            <Route path="/marketplace" element={<Marketplace />} />
            <Route path="/positions" element={<Positions />} />
            <Route path="/costs" element={<CostAnalysis />} />
            <Route path="/risk" element={<RiskMonitor />} />
            <Route path="/dev" element={<DevConsole />} />
            <Route path="/settings" element={<Settings />} />
            <Route path="/legal/:slug" element={<LegalDocumentPage />} />
          </Routes>
        </div>
        <LegalFooter />
      </div>
      <Http451Handler />
      {isConsentVisible && (
        <ConsentModal onComplete={handleConsentComplete} />
      )}
    </Shell>
  );
}

export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <AppContent />
      </BrowserRouter>
    </QueryClientProvider>
  );
}
