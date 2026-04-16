export function EmptyState({ title, description, className }) {
  return (
    <div className={`flex flex-col items-center justify-center py-4xl ${className || ""}`}>
      <div
        className="w-3xl h-3xl mb-2xl"
        style={{
          backgroundImage: "radial-gradient(circle, var(--border-visible) 1px, transparent 1px)",
          backgroundSize: "12px 12px",
          opacity: 0.4,
        }}
      />
      <p className="text-heading font-body text-nx-text-secondary mb-sm">{title}</p>
      {description && (
        <p className="text-body-sm font-body text-nx-text-disabled max-w-md text-center">
          {description}
        </p>
      )}
    </div>
  );
}
