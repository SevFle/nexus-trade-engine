export function InlineStatus({ status, children, className }) {
  const prefix = {
    ok: "[OK]",
    error: "[ERROR]",
    loading: "[LOADING...]",
    saved: "[SAVED]",
    warning: "[WARN]",
  };

  return (
    <span className={`text-label font-mono uppercase text-nx-text-secondary ${className || ""}`}>
      {prefix[status] || `[${status.toUpperCase()}]`} {children}
    </span>
  );
}
