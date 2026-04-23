import { useAttributions } from "../../hooks/useLegal";
import { AttributionBadge } from "./AttributionBadge";

const FALLBACK_ATTRIBUTIONS = [
  { provider: "Polygon.io", description: "Market data provided by Polygon.io", url: "https://polygon.io" },
  { provider: "FMP", description: "Financial data from FMP", url: "https://financialmodelingprep.com" },
];

export function AttributionStrip({ className }) {
  const { data, isLoading } = useAttributions();
  const attributions = Array.isArray(data) && data.length > 0 ? data : FALLBACK_ATTRIBUTIONS;

  if (isLoading) return null;

  return (
    <div className={`flex items-center gap-lg flex-wrap ${className || ""}`}>
      {attributions.map((attr) => (
        <AttributionBadge
          key={attr.provider}
          provider={attr.provider}
          description={attr.description}
          url={attr.url}
        />
      ))}
    </div>
  );
}
