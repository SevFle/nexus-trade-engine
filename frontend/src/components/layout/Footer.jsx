import { Link } from "react-router-dom";

const LEGAL_LINKS = [
  { to: "/legal/risk-disclaimer", label: "Risk Disclaimer" },
  { to: "/legal/terms-of-service", label: "Terms of Service" },
  { to: "/legal/privacy-policy", label: "Privacy Policy" },
  { to: "/legal/eula", label: "EULA" },
  { to: "/legal/marketplace-eula", label: "Marketplace EULA" },
  { to: "/legal/data-provider-attributions", label: "Data Provider Attributions" },
];

export function Footer() {
  return (
    <footer className="border-t border-nx-border bg-nx-surface px-xl py-lg">
      <div className="flex items-center justify-between flex-wrap gap-md">
        <nav aria-label="Legal documents" className="flex items-center gap-lg flex-wrap">
          {LEGAL_LINKS.map((link) => (
            <Link
              key={link.to}
              to={link.to}
              className="text-label font-mono uppercase text-nx-text-disabled hover:text-nx-text-secondary transition-colors"
            >
              {link.label}
            </Link>
          ))}
        </nav>
        <span className="text-label font-mono uppercase text-nx-text-disabled">
          NEXUS TRADE ENGINE v0.1.0
        </span>
      </div>
    </footer>
  );
}
