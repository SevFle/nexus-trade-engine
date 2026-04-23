import React, {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useState,
} from "react";
import { useLegalDocuments, useAcceptLegal } from "../hooks/useLegal";

const LegalContext = createContext(null);

export function LegalProvider({ children }) {
  const [showConsentModal, setShowConsentModal] = useState(false);
  const [pendingDocs, setPendingDocs] = useState([]);
  const { data: documents = [] } = useLegalDocuments();
  const acceptMutation = useAcceptLegal();

  useEffect(() => {
    if (!Array.isArray(documents)) return;
    const required = documents.filter((d) => d.requires_acceptance);
    if (required.length > 0) {
      setPendingDocs(required);
      setShowConsentModal(true);
    }
  }, [documents]);

  useEffect(() => {
    const handler = (e) => {
      setPendingDocs(e.detail);
      setShowConsentModal(true);
    };
    window.addEventListener("legal:consent-required", handler);
    return () => window.removeEventListener("legal:consent-required", handler);
  }, []);

  const handleAccept = useCallback(async () => {
    const acceptances = pendingDocs.map((d) => ({
      document_slug: d.slug,
      version: d.version,
    }));
    await acceptMutation.mutateAsync(acceptances);
    setPendingDocs([]);
    setShowConsentModal(false);
  }, [pendingDocs, acceptMutation]);

  const triggerConsent = useCallback((docs) => {
    setPendingDocs(docs);
    setShowConsentModal(true);
  }, []);

  return (
    <LegalContext.Provider
      value={{ showConsentModal, pendingDocs, handleAccept, triggerConsent }}
    >
      {children}
    </LegalContext.Provider>
  );
}

export function useLegalContext() {
  const ctx = useContext(LegalContext);
  if (!ctx)
    throw new Error("useLegalContext must be used within LegalProvider");
  return ctx;
}
