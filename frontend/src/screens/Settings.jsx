import { Link } from "react-router-dom";
import { useLegalDocuments, useAcceptances } from "../hooks/useLegal";
import { StatRow } from "../components/primitives/StatRow";
import { StatusBadge } from "../components/primitives/StatusBadge";
import { LoadingSpinner } from "../components/feedback/LoadingSpinner";

export default function Settings() {
  const { data: documents, isLoading: docsLoading } = useLegalDocuments();
  const { data: acceptances, isLoading: accLoading } = useAcceptances();

  if (docsLoading || accLoading) {
    return (
      <div className="flex items-center justify-center py-4xl">
        <LoadingSpinner />
      </div>
    );
  }

  const acceptanceMap = new Map(
    (acceptances || []).map((a) => [a.document_slug, a])
  );

  return (
    <div className="text-nx-text-primary p-xl">
      <div className="max-w-5xl mx-auto">
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
          <div className="border border-nx-border rounded-2xl overflow-hidden">
            {(documents || []).map((doc) => {
              const acceptance = acceptanceMap.get(doc.slug);
              const isAccepted = acceptance?.version === doc.current_version;

              return (
                <div
                  key={doc.slug}
                  className="flex items-center justify-between p-lg border-b border-nx-border last:border-b-0"
                >
                  <div className="flex items-center gap-md flex-1">
                    <Link
                      to={`/legal/${doc.slug}`}
                      className="text-body font-body text-nx-text-primary hover:text-nx-interactive transition-colors"
                    >
                      {doc.title}
                    </Link>
                  </div>
                  <div className="flex items-center gap-lg">
                    <span className="text-label font-mono text-nx-text-disabled">
                      v{doc.current_version}
                    </span>
                    {doc.requires_acceptance && (
                      isAccepted ? (
                        <StatusBadge status="ok">ACCEPTED</StatusBadge>
                      ) : (
                        <StatusBadge status="warning">ACTION NEEDED</StatusBadge>
                      )
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        </section>

        <section>
          <span className="text-label font-mono uppercase text-nx-text-secondary block mb-md">
            ACCEPTANCE HISTORY
          </span>
          <div className="border border-nx-border rounded-2xl overflow-hidden">
            {(acceptances || []).length === 0 ? (
              <div className="p-lg text-nx-text-disabled text-body-sm font-body">
                No acceptance records found.
              </div>
            ) : (
              (acceptances || []).map((acc) => (
                <div
                  key={`${acc.document_slug}-${acc.accepted_at}`}
                  className="flex items-center justify-between p-md border-b border-nx-border last:border-b-0"
                >
                  <span className="text-body-sm font-body text-nx-text-primary">
                    {acc.document_title || acc.document_slug}
                  </span>
                  <div className="flex items-center gap-md">
                    <span className="text-label font-mono text-nx-text-disabled">
                      v{acc.version}
                    </span>
                    <span className="text-label font-mono text-nx-text-disabled tabular-nums">
                      {new Date(acc.accepted_at).toLocaleDateString("en-US", {
                        year: "numeric",
                        month: "short",
                        day: "numeric",
                        hour: "2-digit",
                        minute: "2-digit",
                      })}
                    </span>
                  </div>
                </div>
              ))
            )}
          </div>
        </section>
      </div>
    </div>
  );
}
