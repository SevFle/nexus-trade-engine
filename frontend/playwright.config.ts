import { defineConfig } from "@playwright/test";

/**
 * Playwright E2E config for the Nexus Trade React frontend.
 *
 * Scope: a single happy-path smoke test against the Vite dev server. The
 * backend (engine) is NOT required — the spec mocks the HTTP API via
 * `page.route` and seeds auth/onboarding state, so the test is fully
 * self-contained and deterministic in CI.
 */
const PORT = 3000;
const baseURL = `http://localhost:${PORT}`;

export default defineConfig({
  testDir: "./tests/e2e",
  fullyParallel: true,
  // Fail fast on `test.only` in CI; allow it locally for debugging.
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  workers: process.env.CI ? 1 : undefined,
  reporter: process.env.CI ? "line" : "list",
  timeout: 60_000,
  expect: { timeout: 10_000 },

  use: {
    baseURL,
    headless: true,
    trace: "on-first-retry",
  },

  projects: [
    {
      name: "chromium",
      use: {
        browserName: "chromium",
        viewport: { width: 1280, height: 720 },
      },
    },
  ],

  // Spin up the Vite dev server automatically; reuse one that is already
  // running locally so iterating on the test is fast.
  webServer: {
    command: "npm run dev",
    url: baseURL,
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
  },
});
