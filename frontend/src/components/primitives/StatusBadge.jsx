import clsx from "clsx";

export function StatusBadge({ status, children, className }) {
  const styles = {
    ok: "text-nx-success",
    error: "text-nx-accent",
    warning: "text-nx-warning",
    loading: "text-nx-text-secondary",
    neutral: "text-nx-text-disabled",
  };

  return (
    <span
      className={clsx(
        "text-label font-mono uppercase",
        styles[status] || styles.neutral,
        className,
      )}
    >
      [{children}]
    </span>
  );
}
