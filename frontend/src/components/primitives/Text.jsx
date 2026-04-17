import clsx from "clsx";

const variants = {
  "display-xl": "text-display-xl font-display",
  "display-lg": "text-display-lg font-display",
  "display-md": "text-display-md font-display",
  heading: "text-heading font-body font-medium",
  subheading: "text-subheading font-body",
  body: "text-body font-body",
  "body-sm": "text-body-sm font-body",
  caption: "text-caption font-mono",
  label: "text-label font-mono uppercase",
};

const colors = {
  display: "text-nx-text-display",
  primary: "text-nx-text-primary",
  secondary: "text-nx-text-secondary",
  disabled: "text-nx-text-disabled",
  accent: "text-nx-accent",
  success: "text-nx-success",
  warning: "text-nx-warning",
  interactive: "text-nx-interactive",
};

export function Text({ variant = "body", color = "primary", className, children, ...props }) {
  return (
    <span className={clsx(variants[variant], colors[color], className)} {...props}>
      {children}
    </span>
  );
}
