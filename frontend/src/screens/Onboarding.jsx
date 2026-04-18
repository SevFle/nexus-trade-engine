import { useState, useEffect, useMemo } from "react";
import { useNavigate } from "react-router-dom";
import DOMPurify from "dompurify";
import { marked } from "marked";
import { useLegalDocuments, useLegalDocument, useAcceptLegal } from "../hooks/useLegal";
import { LoadingSpinner } from "../components/feedback/LoadingSpinner";

export default function Onboarding() {
  const navigate = useNavigate();
  const documents = useLegalDocuments().data || [];
  const isLoading = useLegalDocuments().isLoading;
  const acceptMutation = useAcceptLegal();
  const [currentIdx, setCurrentIdx] = useState(0);
  const [accepted, setAccepted] = useState({});

  const requiredDocs = useMemo(
    () => documents.filter((d) => d.requires_acceptance),
    [documents]
  );

  const currentDoc = requiredDocs[currentIdx];
  const { data: docDetail } = useLegalDocument(currentDoc?.slug);

  const htmlContent = useMemo(() => {
    const raw = docDetail?.content_markdown;
    if (!raw) return "";
    const html = marked.parse(raw, { async: false });
    return DOMPurify.sanitize(html);
  }, [docDetail?.content_markdown]);

  const allAccepted = requiredDocs.every((d) => accepted[d.slug]);

  useEffect(() => {
    if (!isLoading && requiredDocs.length === 0) {
      navigate("/");
    }
  }, [isLoading, requiredDocs.length, navigate]);

  const handleAcceptDoc = () => {
    const doc = requiredDocs[currentIdx];
    setAccepted((prev) => ({ ...prev, [doc.slug]: true }));
    if (currentIdx < requiredDocs.length - 1) {
      setCurrentIdx((prev) => prev + 1);
    }
  };

  const handleFinish = async () => {
    const acceptances = requiredDocs.map((d) => ({
      document_slug: d.slug,
      document_version: d.current_version,
    }));
    await acceptMutation.mutateAsync(acceptances);
    navigate("/");
  };

  if (isLoading) {
    return (
      <div className="flex items-center justify-center min-h-screen bg-nx-black">
        <LoadingSpinner />
      </div>
    );
  }

  if (requiredDocs.length === 0) {
    return null;
  }

  return (
    <div className="flex items-center justify-center min-h-screen bg-nx-black p-xl">
      <div className="w-full max-w-2xl">
        <header className="mb-3xl text-center">
          <span className="text-label font-mono uppercase text-nx-text-secondary block mb-sm">
            WELCOME TO NEXUS
          </span>
          <h1 className="text-display-lg font-display text-nx-text-display">
            LEGAL AGREEMENTS
          </h1>
          <p className="text-body font-body text-nx-text-secondary mt-md">
            Please review and accept the following documents to get started.
          </p>
        </header>

        <div className="flex gap-xs justify-center mb-lg flex-wrap">
          {requiredDocs.map((doc, i) => (
            <button
              key={doc.slug}
              type="button"
              onClick={() => setCurrentIdx(i)}
              className={`px-md py-xs text-label font-mono uppercase border rounded-full transition-colors ${
                i === currentIdx
                  ? "bg-nx-text-display text-nx-black border-nx-text-display"
                  : accepted[doc.slug]
                    ? "bg-nx-success/20 text-nx-success border-nx-success"
                    : "bg-transparent text-nx-text-secondary border-nx-border"
              }`}
            >
              {accepted[doc.slug] ? "\u2713 " : ""}
              {doc.title || doc.slug}
            </button>
          ))}
        </div>

        {currentDoc && (
          <div className="bg-nx-surface border border-nx-border rounded-2xl p-xl mb-lg">
            <div className="flex items-center justify-between mb-lg">
              <h2 className="text-subheading font-display text-nx-text-display">
                {currentDoc.title || currentDoc.slug}
              </h2>
              <span className="text-label font-mono uppercase text-nx-text-disabled">
                v{currentDoc.current_version}
              </span>
            </div>
            <div className="max-h-64 overflow-y-auto mb-lg text-body-sm font-body text-nx-text-primary">
              {htmlContent ? (
                <div
                  className="prose prose-sm prose-invert max-w-none [&_h1]:text-subheading [&_h1]:font-display [&_h1]:text-nx-text-display [&_h1]:mb-md [&_h2]:text-body [&_h2]:font-display [&_h2]:text-nx-text-primary [&_h2]:mb-sm [&_p]:mb-sm [&_ul]:list-disc [&_ul]:pl-md"
                  dangerouslySetInnerHTML={{ __html: htmlContent }}
                />
              ) : (
                <div className="flex items-center justify-center py-xl">
                  <LoadingSpinner />
                </div>
              )}
            </div>
            <button
              type="button"
              onClick={handleAcceptDoc}
              className="w-full px-2xl py-md text-label font-mono uppercase rounded-full border bg-nx-text-display text-nx-black border-nx-text-display hover:bg-nx-text-primary transition-colors"
            >
              I ACCEPT &mdash; {currentDoc.title || currentDoc.slug}
            </button>
          </div>
        )}

        <button
          type="button"
          disabled={!allAccepted}
          onClick={handleFinish}
          className={`w-full px-2xl py-md text-label font-mono uppercase rounded-full border transition-colors ${
            allAccepted
              ? "bg-nx-accent text-nx-black border-nx-accent hover:bg-nx-accent/80"
              : "bg-nx-text-disabled/30 text-nx-text-disabled border-nx-text-disabled/30 cursor-not-allowed"
          }`}
        >
          {allAccepted ? "CONTINUE TO NEXUS" : "ACCEPT ALL DOCUMENTS TO CONTINUE"}
        </button>
      </div>
    </div>
  );
}
