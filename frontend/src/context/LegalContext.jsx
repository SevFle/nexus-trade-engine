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
  const { data } = useLegalDocuments();
  const documents = data ?? [];
  const acceptMutation = useAcceptLegal();

  useEffect(() => {
    if (!Array.isArray(documents)) return;
    const required = documents.filter(
      (d) => d.needs_re_acceptance || (d.requires_acceptance && !d.accepted)
    );
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
