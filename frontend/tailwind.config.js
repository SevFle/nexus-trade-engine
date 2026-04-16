export default {
  content: ["./index.html", "./src/**/*.{js,jsx}"],
  darkMode: "class",
  theme: {
    extend: {
      colors: {
        nx: {
          black: "var(--black)",
          surface: "var(--surface)",
          "surface-raised": "var(--surface-raised)",
          border: "var(--border)",
          "border-visible": "var(--border-visible)",
          "text-disabled": "var(--text-disabled)",
          "text-secondary": "var(--text-secondary)",
          "text-primary": "var(--text-primary)",
          "text-display": "var(--text-display)",
          success: "var(--success)",
          warning: "var(--warning)",
          accent: "var(--accent)",
          "accent-subtle": "var(--accent-subtle)",
          interactive: "var(--interactive)",
        },
      },
      fontFamily: {
        display: ['"Doto"', '"Space Mono"', "monospace"],
        body: ['"Space Grotesk"', '"DM Sans"', "system-ui", "sans-serif"],
        mono: ['"Space Mono"', '"JetBrains Mono"', '"SF Mono"', "monospace"],
      },
      fontSize: {
        "display-xl": ["72px", { lineHeight: "1", letterSpacing: "-0.03em" }],
        "display-lg": ["48px", { lineHeight: "1.1", letterSpacing: "-0.02em" }],
        "display-md": ["36px", { lineHeight: "1.1", letterSpacing: "-0.02em" }],
        heading: ["24px", { lineHeight: "1.3", letterSpacing: "-0.01em" }],
        subheading: ["18px", { lineHeight: "1.4" }],
        body: ["16px", { lineHeight: "1.5" }],
        "body-sm": ["14px", { lineHeight: "1.5", letterSpacing: "0.01em" }],
        caption: ["12px", { lineHeight: "1.4", letterSpacing: "0.04em" }],
        label: ["11px", { lineHeight: "1.4", letterSpacing: "0.08em" }],
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
      borderRadius: {
        "2xl": "16px",
        "3xl": "24px",
      },
      transitionDuration: {
        DEFAULT: "200ms",
      },
      transitionTimingFunction: {
        DEFAULT: "cubic-bezier(0.25, 0.1, 0.25, 1)",
      },
    },
  },
  plugins: [],
};
