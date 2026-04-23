import { useState, useEffect, useMemo } from "react";
import { marked } from "marked";
import DOMPurify from "dompurify";
import { Modal } from "../feedback/Modal";
import { useLegalContext } from "../../context/LegalContext";
import { useLegalDocument } from "../../hooks/useLegal";

export function ConsentModal() {
  const { showConsentModal, pendingDocs, handleAccept } = useLegalContext();
  const [selectedSlug, setSelectedSlug] = useState(null);
  const [checked, setChecked] = useState(false);

  const activeSlug = selectedSlug || pendingDocs[0]?.slug;

  const { data: docContent } = useLegalDocument(activeSlug);

  const htmlContent = useMemo(() => {
    if (!docContent?.content_markdown) return "";
    const raw = marked.parse(docContent.content_markdown, { async: false });
    return DOMPurify.sanitize(raw);
  }, [docContent?.content_markdown]);

  useEffect(() => {
    setChecked(false);
  }, [selectedSlug, pendingDocs]);

  if (!showConsentModal || pendingDocs.length === 0) return null;

  return (
    <Modal open title={null} maxWidth="max-w-2xl">
      <div className="mb-lg">
        <h2 className="text-subheading font-display text-nx-text-display mb-xs">
          LEGAL ACCEPTANCE REQUIRED
        </h2>
        <p className="text-body-sm font-body text-nx-text-secondary">
          You must review and accept the following legal documents before
          continuing.
        </p>
      </div>

      <div className="flex gap-xs mb-lg flex-wrap">
        {pendingDocs.map((doc) => (
          <button
            key={doc.slug}
            type="button"
            onClick={() => setSelectedSlug(doc.slug)}
            className={`px-md py-xs text-label font-mono uppercase border rounded-full transition-colors ${
              activeSlug === doc.slug
                ? "bg-nx-text-display text-nx-black border-nx-text-display"
                : "bg-transparent text-nx-text-secondary border-nx-border hover:border-nx-border-visible"
            }`}
          >
            {doc.title || doc.slug}
          </button>
        ))}
      </div>

      <div className="bg-nx-surface-raised border border-nx-border rounded-2xl p-lg mb-lg max-h-64 overflow-y-auto">
        {htmlContent ? (
          <div
            className="prose prose-sm prose-invert max-w-none text-body-sm font-body text-nx-text-primary [&_h1]:text-subheading [&_h1]:font-display [&_h1]:text-nx-text-display [&_h1]:mb-md [&_h2]:text-body [&_h2]:font-display [&_h2]:text-nx-text-primary [&_h2]:mb-sm [&_p]:mb-sm [&_ul]:list-disc [&_ul]:pl-md"
            dangerouslySetInnerHTML={{ __html: htmlContent }}
          />
        ) : (
          <span className="text-label font-mono uppercase text-nx-text-disabled">
            LOADING DOCUMENT...
          </span>
        )}
      </div>

      <label className="flex items-center gap-md mb-lg cursor-pointer select-none">
        <input
          type="checkbox"
          checked={checked}
          onChange={(e) => setChecked(e.target.checked)}
          className="w-4 h-4 rounded border-nx-border accent-nx-text-display"
        />
        <span className="text-body-sm font-body text-nx-text-primary">
          I have read and understood the above documents and agree to their
          terms.
        </span>
      </label>

      <button
        type="button"
        disabled={!checked}
        onClick={handleAccept}
        className={`w-full px-2xl py-md text-label font-mono uppercase rounded-full border transition-colors ${
          checked
            ? "bg-nx-text-display text-nx-black border-nx-text-display hover:bg-nx-text-primary"
            : "bg-nx-text-disabled/30 text-nx-text-disabled border-nx-text-disabled/30 cursor-not-allowed"
        }`}
      >
        I ACCEPT
      </button>
    </Modal>
  );
}
