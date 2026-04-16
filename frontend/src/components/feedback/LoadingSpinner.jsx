export function LoadingSpinner({ className }) {
  return (
    <div className={`flex items-center gap-sm text-nx-text-secondary ${className || ""}`}>
      <div className="flex gap-2xs">
        {Array.from({ length: 8 }).map((_, i) => (
          <div
            key={i}
            className="w-2xs h-sm bg-nx-border-visible"
            style={{ opacity: i < 3 ? 1 : 0.3 }}
          />
        ))}
      </div>
      <span className="text-label font-mono uppercase">[LOADING...]</span>
    </div>
  );
}
