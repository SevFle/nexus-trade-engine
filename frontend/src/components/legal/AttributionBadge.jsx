import { ExternalLink } from "lucide-react";

export function AttributionBadge({ provider, description, url }) {
  return (
    <span className="inline-flex items-center gap-xs text-label font-mono uppercase text-nx-text-disabled">
      <span className="w-1.5 h-1.5 rounded-full bg-nx-text-disabled shrink-0" />
      {url ? (
        <a
          href={url}
          target="_blank"
          rel="noopener noreferrer"
          className="hover:text-nx-text-secondary transition-colors inline-flex items-center gap-xs"
        >
          {description || provider}
          <ExternalLink size={10} strokeWidth={1.5} />
        </a>
      ) : (
        <span>{description || provider}</span>
      )}
    </span>
  );
}
