import React, {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";
import { useLegalDocuments, useAcceptLegal } from "../hooks/useLegal";

const LegalContext = createContext(null);

export function LegalProvider({ children }) {
  const [showConsentModal, setShowConsentModal] = useState(false);
  const [pendingDocs, setPendingDocs] = useState([]);
  const documents = useLegalDocuments().data || [];
  const acceptMutation = useAcceptLegal();

  const requiredPending = useMemo(
    () =>
      documents.filter(
        (d) => d.needs_re_acceptance || (d.requires_acceptance && !d.accepted)
      ),
    [documents]
  );

  useEffect(() => {
    if (requiredPending.length > 0) {
      setPendingDocs(requiredPending);
      setShowConsentModal(true);
    }
  }, [requiredPending]);

  useEffect(() => {
    const handler = (e) => {
      const slugs = Array.isArray(e.detail) ? e.detail : [];
      const docs = slugs.map((slug) => {
        const found = documents.find((d) => d.slug === slug);
        return found || { slug, title: slug, current_version: "0.0.0" };
      });
      setPendingDocs(docs);
      setShowConsentModal(true);
    };
    window.addEventListener("legal:consent-required", handler);
    return () => window.removeEventListener("legal:consent-required", handler);
  }, [documents]);

  const handleAccept = useCallback(async () => {
    const acceptances = pendingDocs.map((d) => ({
      document_slug: d.slug,
      document_version: d.current_version,
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
