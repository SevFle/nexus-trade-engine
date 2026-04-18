import { Link } from "react-router-dom";
import { useLegalDocuments } from "../../hooks/useLegal";

export function LegalFooter() {
  const { data: documents } = useLegalDocuments();

  const legalDocs = (documents || []).map((doc) => ({
    slug: doc.slug,
    title: doc.title,
  }));

  const defaultLinks = [
    { slug: "risk-disclaimer", title: "Risk Disclaimer" },
    { slug: "terms-of-service", title: "Terms of Service" },
    { slug: "privacy-policy", title: "Privacy Policy" },
    { slug: "eula", title: "EULA" },
    { slug: "marketplace-eula", title: "Marketplace EULA" },
  ];

  const links = legalDocs.length > 0 ? legalDocs : defaultLinks;

  return (
    <footer className="border-t border-nx-border py-lg px-xl mt-auto">
      <div className="max-w-7xl mx-auto flex items-center justify-between">
        <div className="flex items-center gap-lg flex-wrap">
          {links.map((doc) => (
            <Link
              key={doc.slug}
              to={`/legal/${doc.slug}`}
              className="text-label font-mono uppercase text-nx-text-disabled hover:text-nx-text-secondary transition-colors"
            >
              {doc.title}
            </Link>
          ))}
        </div>
        <span className="text-label font-mono text-nx-text-disabled">
          {"NEXUS TRADE ENGINE // LEGAL"}
        </span>
      </div>
    </footer>
  );
}
