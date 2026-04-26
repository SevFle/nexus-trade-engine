/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{js,jsx}"],
  darkMode: "class",
  theme: {
    extend: {
      colors: {
        nx: {
          black: "rgb(var(--nx-black) / <alpha-value>)",
          surface: "rgb(var(--nx-surface) / <alpha-value>)",
          "surface-raised": "rgb(var(--nx-surface-raised) / <alpha-value>)",
          border: "rgb(var(--nx-border) / <alpha-value>)",
          "border-visible": "rgb(var(--nx-border-visible) / <alpha-value>)",

          "text-disabled": "rgb(var(--nx-text-disabled) / <alpha-value>)",
          "text-secondary": "rgb(var(--nx-text-secondary) / <alpha-value>)",
          "text-primary": "rgb(var(--nx-text-primary) / <alpha-value>)",
          "text-display": "rgb(var(--nx-text-display) / <alpha-value>)",

          /* Status — identical in both modes */
          success: "rgb(var(--nx-success) / <alpha-value>)",
          warning: "rgb(var(--nx-warning) / <alpha-value>)",
          accent: "rgb(var(--nx-accent) / <alpha-value>)",
          "accent-subtle": "rgb(var(--nx-accent) / 0.15)",
          interactive: "rgb(var(--nx-interactive) / <alpha-value>)",
        },
      },
      spacing: {
        "2xs": "2px",
        xs: "4px",
        sm: "8px",
        md: "16px",
        lg: "24px",
        xl: "32px",
        "2xl": "48px",
        "3xl": "64px",
        "4xl": "96px",
      },
      fontFamily: {
        display: [
          "Doto",
          "Space Mono",
          "ui-monospace",
          "SFMono-Regular",
          "monospace",
        ],
        body: [
          "Space Grotesk",
          "DM Sans",
          "ui-sans-serif",
          "system-ui",
          "-apple-system",
          "sans-serif",
        ],
        mono: [
          "Space Mono",
          "JetBrains Mono",
          "ui-monospace",
          "SFMono-Regular",
          "Menlo",
          "monospace",
        ],
      },
      fontSize: {
        label: ["11px", { lineHeight: "16px", letterSpacing: "0.08em" }],
        caption: ["12px", { lineHeight: "16px", letterSpacing: "0.04em" }],
        "body-sm": ["14px", { lineHeight: "20px", letterSpacing: "0.01em" }],
        body: ["16px", { lineHeight: "24px" }],
        subheading: ["18px", { lineHeight: "24px" }],
        heading: ["24px", { lineHeight: "32px", letterSpacing: "-0.01em" }],
        "display-md": ["36px", { lineHeight: "40px", letterSpacing: "-0.02em" }],
        "display-lg": ["48px", { lineHeight: "52px", letterSpacing: "-0.02em" }],
        "display-xl": ["72px", { lineHeight: "76px", letterSpacing: "-0.03em" }],
      },
      borderRadius: {
        none: "0",
        xs: "4px",
        sm: "8px",
        md: "8px",
        lg: "16px",
        full: "999px",
      },
      transitionTimingFunction: {
        "nx-out": "cubic-bezier(0.25, 0.1, 0.25, 1)",
      },
      transitionDuration: {
        nx: "200ms",
      },
      keyframes: {
        "nx-tick": {
          "0%": { opacity: "0.6" },
          "100%": { opacity: "1" },
        },
      },
      animation: {
        "nx-tick": "nx-tick 200ms cubic-bezier(0.25, 0.1, 0.25, 1)",
      },
      backgroundImage: {
        "nx-dot-grid":
          "radial-gradient(circle, rgb(var(--nx-border-visible)) 1px, transparent 1px)",
      },
      backgroundSize: {
        "nx-dot": "16px 16px",
      },
    },
  },
  plugins: [],
};
