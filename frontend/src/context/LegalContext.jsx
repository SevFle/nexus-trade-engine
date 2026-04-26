import React, {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { useLegalDocuments, useAcceptLegal } from "../hooks/useLegal";

const LegalContext = createContext(null);

export function LegalProvider({ children }) {
  const [showConsentModal, setShowConsentModal] = useState(false);
  const [pendingDocs, setPendingDocs] = useState([]);
  const hasConsentedRef = useRef(false);
  const { data } = useLegalDocuments();
  const documents = data ?? [];
  const acceptMutation = useAcceptLegal();

  const requiredPending = useMemo(() => {
    if (!Array.isArray(documents)) return [];
    return documents.filter(
      (d) => d.needs_re_acceptance || (d.requires_acceptance && !d.accepted)
    );
  }, [documents]);

  useEffect(() => {
    if (requiredPending.length > 0 && !hasConsentedRef.current) {
      setPendingDocs(requiredPending);
      setShowConsentModal(true);
    }
  }, [requiredPending]);

  useEffect(() => {
    const handler = (e) => {
      setPendingDocs(e.detail);
      setShowConsentModal(true);
    };
    window.addEventListener("legal:consent-required", handler);
    return () => window.removeEventListener("legal:consent-required", handler);
  }, []);

  const handleAccept = useCallback(async () => {
    hasConsentedRef.current = true;
    const acceptances = pendingDocs.map((d) => ({
      document_slug: d.slug,
      document_version: d.current_version ?? d.version,
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
