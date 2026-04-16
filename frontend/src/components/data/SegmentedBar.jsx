import clsx from "clsx";

export function SegmentedBar({ value, max, height = 8, status = "neutral", segments = 20, className }) {
  const filled = Math.round((value / max) * segments);
  const clampedFilled = Math.min(filled, segments);
  const overflow = filled > segments;

  const fillColor = {
    neutral: "bg-nx-text-display",
    success: "bg-nx-success",
    warning: "bg-nx-warning",
    error: "bg-nx-accent",
  };

  return (
    <div className={clsx("w-full flex gap-xs", className)} style={{ height }}>
      {Array.from({ length: segments }).map((_, i) => (
        <div
          key={i}
          className={clsx(
            "flex-1",
            i < clampedFilled
              ? overflow
                ? "bg-nx-accent"
                : fillColor[status]
              : "bg-nx-border",
          )}
        />
      ))}
    </div>
  );
}
