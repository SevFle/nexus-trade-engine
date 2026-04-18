import clsx from "clsx";

const KNOWN_PROVIDERS = {
  polygon: { label: "Polygon.io", description: "Market data provided by" },
  fmp: { label: "FMP", description: "Financial data from" },
  alpha_vantage: { label: "Alpha Vantage", description: "Market data from" },
  yahoo: { label: "Yahoo Finance", description: "Data sourced from" },
};

export function AttributionBadges({ providers = [], className }) {
  if (!providers || providers.length === 0) return null;

  return (
    <div className={clsx("flex items-center gap-md flex-wrap", className)}>
      {providers.map((provider) => {
        const known = KNOWN_PROVIDERS[provider.slug] || {
          label: provider.name || provider.slug,
          description: "Data provided by",
        };
        return (
          <span
            key={provider.slug}
            className="inline-flex items-center gap-xs px-sm py-2xs bg-nx-surface border border-nx-border rounded text-label font-mono text-nx-text-disabled"
          >
            <span>{known.description}</span>
            <span className="text-nx-text-secondary">{known.label}</span>
          </span>
        );
      })}
    </div>
  );
}
