import { useLegalDocuments, useMyAcceptances } from "../hooks/useLegal";
import { LoadingSpinner } from "../components/feedback/LoadingSpinner";
import { Link } from "react-router-dom";

export default function Settings() {
  const documents = useLegalDocuments().data || [];
  const docsLoading = useLegalDocuments().isLoading;
  const { data: acceptancesData, isLoading: accLoading } = useMyAcceptances();
  const acceptances = acceptancesData?.acceptances || [];

  return (
    <div className="text-nx-text-primary p-xl">
      <div className="max-w-4xl mx-auto">
        <header className="mb-3xl">
          <span className="text-label font-mono uppercase text-nx-text-secondary block mb-sm">
            SETTINGS
          </span>
          <h1 className="text-display-md font-display text-nx-text-display">
            LEGAL &amp; COMPLIANCE
          </h1>
        </header>

        <section className="mb-3xl">
          <span className="text-label font-mono uppercase text-nx-text-secondary block mb-md">
            LEGAL DOCUMENTS
          </span>
          {docsLoading ? (
            <div className="flex items-center justify-center py-lg">
              <LoadingSpinner />
            </div>
          ) : (
            <div className="bg-nx-surface border border-nx-border rounded-2xl">
              {documents.map((doc) => (
                <Link
                  key={doc.slug}
                  to={`/legal/${doc.slug}`}
                  className="flex items-center justify-between p-lg border-b border-nx-border last:border-b-0 hover:bg-nx-surface-raised transition-colors"
                >
                  <span className="text-body font-body text-nx-text-primary">
                    {doc.title || doc.slug}
                  </span>
                  <span className="text-label font-mono uppercase text-nx-text-disabled">
                    v{doc.current_version}
                  </span>
                </Link>
              ))}
              {documents.length === 0 && (
                <div className="p-lg text-center">
                  <span className="text-label font-mono uppercase text-nx-text-disabled">
                    NO DOCUMENTS AVAILABLE
                  </span>
                </div>
              )}
            </div>
          )}
        </section>

        <section>
          <span className="text-label font-mono uppercase text-nx-text-secondary block mb-md">
            ACCEPTANCE HISTORY
          </span>
          {accLoading ? (
            <div className="flex items-center justify-center py-lg">
              <LoadingSpinner />
            </div>
          ) : acceptances.length === 0 ? (
            <span className="text-label font-mono uppercase text-nx-text-disabled">
              NO ACCEPTANCE RECORDS FOUND
            </span>
          ) : (
            <div className="bg-nx-surface border border-nx-border rounded-2xl">
              {acceptances.map((acc) => (
                <div
                  key={acc.id || `${acc.document_slug}-${acc.accepted_at}`}
                  className="flex items-center justify-between p-lg border-b border-nx-border last:border-b-0"
                >
                  <div>
                    <span className="text-body font-body text-nx-text-primary block">
                      {acc.document_slug}
                    </span>
                    <span className="text-label font-mono text-nx-text-disabled">
                      v{acc.document_version}
                    </span>
                  </div>
                  <span className="text-label font-mono text-nx-text-secondary">
                    {new Date(acc.accepted_at).toLocaleDateString(undefined, {
                      year: "numeric",
                      month: "short",
                      day: "numeric",
                      hour: "2-digit",
                      minute: "2-digit",
                    })}
                  </span>
                </div>
              ))}
            </div>
          )}
        </section>
      </div>
    </div>
  );
}
