import { useMemo } from "react";
import { marked } from "marked";
import DOMPurify from "dompurify";
import { useLegalDocument } from "../../hooks/useLegal";
import { LoadingSpinner } from "../feedback/LoadingSpinner";

export function LegalDocumentViewer({ slug }) {
  const { data, isLoading, error } = useLegalDocument(slug);

  const htmlContent = useMemo(() => {
    if (!data?.content_markdown) return "";
    const raw = marked.parse(data.content_markdown, { async: false });
    return DOMPurify.sanitize(raw);
  }, [data?.content_markdown]);

  if (isLoading) {
    return (
      <div className="flex items-center justify-center py-4xl">
        <LoadingSpinner />
      </div>
    );
  }

  if (error) {
    return (
      <div className="text-nx-accent text-body-sm font-body p-xl">
        Failed to load document.
      </div>
    );
  }

  return (
    <div className="max-w-4xl mx-auto p-xl">
      {data?.title && (
        <header className="mb-3xl">
          <h1 className="text-display-md font-display text-nx-text-display">
            {data.title}
          </h1>
          {data?.version && (
            <span className="text-label font-mono uppercase text-nx-text-disabled mt-xs block">
              VERSION {data.version}
            </span>
          )}
        </header>
      )}
      <div
        className="prose prose-invert max-w-none text-body font-body text-nx-text-primary [&_h1]:text-display-sm [&_h1]:font-display [&_h1]:text-nx-text-display [&_h1]:mb-lg [&_h2]:text-subheading [&_h2]:font-display [&_h2]:text-nx-text-primary [&_h2]:mb-md [&_h3]:text-body [&_h3]:font-display [&_h3]:text-nx-text-primary [&_h3]:mb-sm [&_p]:mb-md [&_ul]:list-disc [&_ul]:pl-lg [&_ol]:list-decimal [&_ol]:pl-lg [&_li]:mb-xs [&_strong]:text-nx-text-display"
        dangerouslySetInnerHTML={{ __html: htmlContent }}
      />
    </div>
  );
}
