import { useParams, Link } from "react-router-dom";
import { useLegalDocument } from "../hooks/useLegal";
import { LoadingSpinner } from "../components/feedback/LoadingSpinner";
import { ArrowLeft } from "lucide-react";

export default function LegalDocumentPage() {
  const { slug } = useParams();
  const { data: doc, isLoading, error } = useLegalDocument(slug);

  if (isLoading) {
    return (
      <div className="flex items-center justify-center py-4xl">
        <LoadingSpinner />
      </div>
    );
  }

  if (error) {
    return (
      <div className="text-nx-text-primary p-xl">
        <div className="max-w-3xl mx-auto">
          <p className="text-nx-accent text-body-sm font-body">
            Failed to load document. Please try again later.
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className="text-nx-text-primary p-xl">
      <div className="max-w-3xl mx-auto">
        <Link
          to="/settings"
          className="inline-flex items-center gap-sm text-label font-mono uppercase text-nx-text-secondary hover:text-nx-text-primary transition-colors mb-xl"
        >
          <ArrowLeft size={14} />
          BACK TO SETTINGS
        </Link>

        <header className="mb-3xl">
          <h1 className="text-display-md font-display text-nx-text-display">
            {doc?.title || slug}
          </h1>
          {doc?.current_version && (
            <span className="text-label font-mono text-nx-text-disabled mt-xs block">
              VERSION {doc.current_version}
            </span>
          )}
        </header>

        <div className="border-t border-nx-border pt-xl">
          {doc?.content ? (
            <div
              className="prose prose-invert max-w-none text-body-sm font-body text-nx-text-primary"
              dangerouslySetInnerHTML={{ __html: doc.content }}
            />
          ) : (
            <p className="text-nx-text-secondary text-body-sm font-body">
              Document content is not available.
            </p>
          )}
        </div>
      </div>
    </div>
  );
}
