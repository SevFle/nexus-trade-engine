import { useState, useEffect, useCallback } from "react";
import { useLegalDocuments, useAcceptLegal } from "../../hooks/useLegal";

export function ConsentModal({ onComplete, requiredOnly = true }) {
  const { data: documents, isLoading } = useLegalDocuments();
  const acceptMutation = useAcceptLegal();
  const [currentIndex, setCurrentIndex] = useState(0);
  const [checked, setChecked] = useState(false);

  const docs = requiredOnly
    ? (documents || []).filter((d) => d.requires_acceptance)
    : documents || [];

  const currentDoc = docs[currentIndex];

  const handleAccept = useCallback(() => {
    if (!checked || !currentDoc) return;
    acceptMutation.mutate(
      [{ document_slug: currentDoc.slug, version: currentDoc.current_version }],
      {
        onSuccess: () => {
          setChecked(false);
          if (currentIndex < docs.length - 1) {
            setCurrentIndex((i) => i + 1);
          } else {
            onComplete?.();
          }
        },
      }
    );
  }, [checked, currentDoc, acceptMutation, currentIndex, docs.length, onComplete]);

  useEffect(() => {
    const handleKeyDown = (e) => {
      if (e.key === "Escape") e.stopPropagation();
    };
    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, []);

  if (isLoading) {
    return (
      <div
        role="dialog"
        aria-modal="true"
        aria-label="Loading legal documents"
        className="fixed inset-0 z-50 flex items-center justify-center bg-black/80"
      >
        <div className="text-nx-text-secondary text-label font-mono uppercase animate-pulse">
          [LOADING LEGAL DOCUMENTS...]
        </div>
      </div>
    );
  }

  if (!docs.length) {
    onComplete?.();
    return null;
  }

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label={`Legal consent: ${currentDoc?.title || ""}`}
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/80"
    >
      <div
        className="bg-nx-surface border border-nx-border rounded-2xl w-full max-w-2xl max-h-[80vh] flex flex-col"
        role="document"
      >
        <div className="p-lg border-b border-nx-border flex items-center justify-between">
          <div>
            <span className="text-label font-mono uppercase text-nx-text-secondary block">
              LEGAL CONSENT REQUIRED
            </span>
            <h2 className="text-heading font-display text-nx-text-display mt-xs">
              {currentDoc?.title}
            </h2>
          </div>
          <span className="text-label font-mono text-nx-text-disabled">
            {currentIndex + 1} / {docs.length}
          </span>
        </div>

        <div className="flex-1 overflow-auto p-lg">
          {currentDoc?.content ? (
            <div
              className="prose prose-invert text-nx-text-primary text-body-sm font-body"
              dangerouslySetInnerHTML={{ __html: currentDoc.content }}
            />
          ) : (
            <div className="text-nx-text-secondary text-body-sm font-body">
              Document content loading...
            </div>
          )}
        </div>

        <div className="p-lg border-t border-nx-border">
          <label className="flex items-center gap-md cursor-pointer mb-lg">
            <input
              type="checkbox"
              checked={checked}
              onChange={(e) => setChecked(e.target.checked)}
              className="w-4 h-4 rounded border-nx-border-visible bg-nx-surface accent-nx-interactive"
              aria-label="I understand and accept the terms"
            />
            <span className="text-body-sm font-body text-nx-text-primary">
              I have read and understand this document. I accept the terms outlined above.
            </span>
          </label>

          <button
            type="button"
            onClick={handleAccept}
            disabled={!checked || acceptMutation.isPending}
            className={`w-full px-2xl py-md text-label font-mono uppercase rounded-full border transition-colors ${
              checked && !acceptMutation.isPending
                ? "bg-nx-interactive text-white border-nx-interactive hover:opacity-90"
                : "bg-nx-text-disabled/20 text-nx-text-disabled border-nx-border cursor-not-allowed"
            }`}
          >
            {acceptMutation.isPending ? "ACCEPTING..." : "I UNDERSTAND AND ACCEPT"}
          </button>
        </div>
      </div>
    </div>
  );
}
