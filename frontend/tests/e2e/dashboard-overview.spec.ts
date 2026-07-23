import { test, expect, type Page } from "@playwright/test";

/**
 * Happy-path E2E smoke test for the dashboard overview.
 *
 * The React app is gated behind auth (ProtectedRoute) and talks to a backend
 * at http://localhost:8000 that is not running in CI. To keep this test
 * self-contained and deterministic we:
 *
 *   1. Mock every backend HTTP call (auth, legal, portfolio, health) with
 *      `page.route`, including CORS preflight responses, and
 *   2. Seed sessionStorage with a fake access token plus a completed
 *      onboarding flag so the protected dashboard renders directly — no auth
 *      redirect, no onboarding/consent modal overlays.
 *
 * Scope is deliberately tight: ONE test asserting the core shell of the
 * overview page (title, portfolio summary card, nav bar) plus a single
 * client-side navigation hop.
 */

const API_BASE = "http://localhost:8000";

// Minimal mock user returned by GET /api/v1/auth/me.
const MOCK_USER = {
  id: "user-e2e",
  email: "trader@nexus.test",
  display_name: "E2E Trader",
};

// Minimal mock portfolio summary for the /portfolio navigation hop.
// Shape mirrors the typed `PortfolioSummaryData` interface in src/lib/api.ts.
const MOCK_PORTFOLIO_SUMMARY = {
  total_value: 2_847_391.44,
  total_pnl: 31_204.18,
  total_pnl_pct: 1.11,
  active_strategies: 7,
  open_positions: 14,
  currency: "USD",
  as_of: "2026-07-23T00:00:00Z",
};

/**
 * CORS headers for mocked responses. Some client calls use
 * `credentials: "include"`, so we cannot use a wildcard origin — we echo back
 * the request's `Origin` (falling back to `*`).
 */
function corsHeaders(origin: string | null) {
  return {
    "Access-Control-Allow-Origin": origin ?? "*",
    "Access-Control-Allow-Methods": "GET,POST,PUT,PATCH,DELETE,OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, Authorization",
    "Access-Control-Allow-Credentials": "true",
    "Access-Control-Max-Age": "86400",
  };
}

/**
 * Install backend HTTP mocks. Covers auth bootstrap, legal documents (empty
 * so no consent modal), legal attributions, and the portfolio summary used on
 * the navigation target. A catch-all returns empty JSON so no request hangs
 * waiting on the absent backend.
 */
async function mockBackend(page: Page) {
  await page.route(`${API_BASE}/**`, async (route) => {
    const request = route.request();
    const origin = request.headers()["origin"] ?? null;

    if (request.method() === "OPTIONS") {
      await route.fulfill({ status: 204, headers: corsHeaders(origin) });
      return;
    }

    const url = request.url();
    const reply = (status: number, body: unknown) =>
      route.fulfill({
        status,
        contentType: "application/json",
        headers: corsHeaders(origin),
        body: JSON.stringify(body),
      });

    if (url.includes("/api/v1/auth/providers")) {
      await reply(200, { providers: ["local"] });
      return;
    }
    if (url.includes("/api/v1/auth/me")) {
      await reply(200, MOCK_USER);
      return;
    }
    if (url.includes("/api/v1/legal/documents")) {
      // No pending documents => the consent modal never shows.
      await reply(200, { documents: [] });
      return;
    }
    if (url.includes("/api/v1/legal/attributions")) {
      await reply(200, []);
      return;
    }
    if (url.includes("/api/v1/portfolio/summary")) {
      await reply(200, MOCK_PORTFOLIO_SUMMARY);
      return;
    }

    // Catch-all so no request hangs waiting on an absent backend.
    await reply(200, {});
  });

  // The dashboard polls engine health at a path relative to the dev server
  // (http://localhost:3000/health), so it needs its own route.
  await page.route("**/health", async (route) => {
    const origin = route.request().headers()["origin"] ?? null;
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      headers: corsHeaders(origin),
      body: JSON.stringify({ status: "ok" }),
    });
  });
}

/**
 * Seed browser storage before the app boots:
 *   - sessionStorage: fake access/refresh tokens + expiry so AuthProvider's
 *     bootstrap calls /auth/me (which we mock) and authenticates the session,
 *     clearing the ProtectedRoute gate.
 *   - localStorage: onboarding marked complete so the OnboardingManager does
 *     not render its modal overlay on top of the dashboard.
 */
async function seedStorage(page: Page) {
  await page.addInitScript(() => {
    const now = Date.now();
    sessionStorage.setItem("nexus_access_token", "e2e-fake-access-token");
    sessionStorage.setItem("nexus_refresh_token", "e2e-fake-refresh-token");
    sessionStorage.setItem("nexus_token_expiry", String(now + 3_600_000));
    localStorage.setItem(
      "nexus-onboarding",
      JSON.stringify({
        completed: ["welcome", "setup", "tour"],
        skipped: false,
        setupData: null,
      }),
    );
  });
}

test.beforeEach(async ({ page }) => {
  await seedStorage(page);
  await mockBackend(page);
});

test.describe("Dashboard overview", () => {
  test("renders title, portfolio summary card, and navigation", async ({
    page,
  }) => {
    await page.goto("/");

    // --- Page title (from index.html) ---
    await expect(page).toHaveTitle(/Nexus Trade Engine/);

    // --- Auth gate cleared: still on the dashboard, not redirected to /login ---
    await expect(page).toHaveURL(/\/$/);

    // --- Navigation bar (sidebar) ---
    const nav = page.locator("nav");
    await expect(nav).toBeVisible();
    await expect(
      nav.getByRole("link", { name: "DASHBOARD", exact: true }),
    ).toBeVisible();
    await expect(
      nav.getByRole("link", { name: "PORTFOLIO", exact: true }),
    ).toBeVisible();
    await expect(
      nav.getByRole("link", { name: "STRATEGIES", exact: true }),
    ).toBeVisible();

    // --- Portfolio summary card (rendered in the shell above the routed page) ---
    const summary = page.getByTestId("portfolio-summary");
    await expect(summary).toBeVisible();
    await expect(summary.getByText("Portfolio Summary")).toBeVisible();
    // The shell renders it in placeholder mode, which surfaces a tag + labels.
    await expect(summary.getByText("placeholder", { exact: true })).toBeVisible();
    await expect(summary.getByText("Total Value")).toBeVisible();
    await expect(summary.getByText("Open Positions")).toBeVisible();

    // --- Dashboard overview body ---
    await expect(page.getByText("PORTFOLIO VALUE").first()).toBeVisible();

    // --- Navigation: hop to /portfolio via the sidebar and confirm routing ---
    await nav.getByRole("link", { name: "PORTFOLIO", exact: true }).click();
    await expect(page).toHaveURL(/\/portfolio$/);
    await expect(
      page.getByRole("heading", { name: "Portfolio Overview" }),
    ).toBeVisible();
  });
});
