import js from "@eslint/js";
import globals from "globals";
import tseslint from "typescript-eslint";

export default tseslint.config(
  {
    // Build artefacts and tooling config files at the repo root are not
    // part of the lint surface. vite.config.ts in particular uses TS
    // syntax that we intentionally don't type-check here.
    ignores: [
      "dist/**",
      "node_modules/**",
      "vite.config.ts",
      "eslint.config.js",
      "postcss.config.js",
      "tailwind.config.js",
    ],
  },

  // All first-party sources get the JS recommended baseline plus a shared
  // browser/module environment. JSX parsing is enabled so the legacy
  // .jsx class components and the newer .tsx function components both
  // lint cleanly under one rule set.
  {
    files: ["src/**/*.{js,jsx,ts,tsx}"],
    extends: [js.configs.recommended],
    languageOptions: {
      ecmaVersion: "latest",
      sourceType: "module",
      globals: {
        ...globals.browser,
        ...globals.es2021,
      },
      parserOptions: {
        ecmaFeatures: { jsx: true },
      },
    },
    rules: {
      "no-unused-vars": [
        "warn",
        { argsIgnorePattern: "^_", varsIgnorePattern: "^React$" },
      ],
    },
  },

  // TypeScript sources override: swap espree for the TS-aware parser so
  // type annotations, interfaces and `as` casts parse. Unused locals are
  // handed to the TS rule — the base `no-unused-vars` misreports around
  // type-only syntax — and `no-undef` is turned off because the TS
  // compiler already enforces referenced identifiers and the JS rule
  // produces false positives against type-only references.
  {
    files: ["src/**/*.{ts,tsx}"],
    plugins: { "@typescript-eslint": tseslint.plugin },
    languageOptions: {
      parser: tseslint.parser,
      parserOptions: {
        ecmaFeatures: { jsx: true },
      },
    },
    rules: {
      "no-undef": "off",
      "no-unused-vars": "off",
      "@typescript-eslint/no-unused-vars": [
        "warn",
        { argsIgnorePattern: "^_", varsIgnorePattern: "^React$" },
      ],
    },
  },
);
