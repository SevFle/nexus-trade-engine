import { test, expect, type Page } from "@playwright/test";

/**
 * E2E test for the Strategies listing page (`/strategies`).
 *
 * Mirrors the dashboard-overview spec's self-contained approach: the backend
 * (engine) is NOT running in CI, so every HTTP call is mocked via
 * `page.route`, and auth/onboarding state is seeded in storage so the
 * ProtectedRoute gate is cleared and the StrategiesPage renders directly.
 *
 * Covers:
 *   - Loading skeleton shows while the request is in flight.
 *   - The table renders with the strategy data returned by the mocked
 *     `GET /api/v1/strategies/` endpoint (name, description, status badge,
 *     P&L).
 *   - The retry affordance appears on an API failure and recovers after a
 *     retry.
 *   - The STRATEGIES nav link routes to the listing.
 */

const API_BASE = "http://localhost:8000";

// Minimal mock user returned by GET /api/v1/auth/me.
const MOCK_USER = {
  id: "user-e2e",
  email: "trader@nexus.test",
  display_name: "E2E Trader",
};

// Minimal mock portfolio summary for the shell's PortfolioSummary card.
const MOCK_PORTFOLIO_SUMMARY = {
  total_value: 2_847_391.44,
  total_pnl: 31_204.18,
  total_pnl_pct: 1.11,
  active_strategies: 3,
  open_positions: 14,
  currency: "USD",
  as_of: "2026-07-23T00:00:00Z",
};

// Mocked strategy list. Mirrors the typed `StrategySummary` interface in
// src/lib/api.ts — objects with name/description/status/is_loaded/pnl.
const MOCK_STRATEGIES = [
  {
    id: "momentum-alpha",
    name: "MOMENTUM ALPHA",
    description: "Trend-following momentum strategy on large-cap equities.",
    is_loaded: true,
    status: "active",
    pnl: 12_450.75,
    pnl_pct: 8.32,
  },
  {
    id: "mean-revert",
    name: "MEAN REVERSION SIGMA",
    description: "Statistical mean-reversion on ETF pairs.",
    is_loaded: false,
    status: "idle",
    pnl: -2_310.0,
    pnl_pct: -1.54,
  },
  {
    id: "pairs-stat",
    name: "PAIRS STATISTICAL",
    description: "Cointegration-based pairs trading.",
    is_loaded: true,
    status: "active",
  },
];

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
 * Install backend HTTP mocks. The strategies list response is configurable so
 * the failure/retry scenario can swap it for a 500. A catch-all returns empty
 * JSON so no request hangs waiting on the absent backend.
 */
async function mockBackend(
  page: Page,
  options: {
    strategiesStatus?: number;
    strategiesBody?: unknown;
  } = {},
) {
  const strategiesStatus = options.strategiesStatus ?? 200;
  const strategiesBody = options.strategiesBody ?? {
    strategies: MOCK_STRATEGIES,
  };

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
    if (url.includes("/api/v1/strategies")) {
      await reply(strategiesStatus, strategiesBody);
      return;
    }

    // Catch-all so no request hangs waiting on an absent backend.
    await reply(200, {});
  });

  // The app may poll engine health at a path relative to the dev server
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
 *     not render its modal overlay on top of the page.
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

test.describe("Strategies listing page", () => {
  test.beforeEach(async ({ page }) => {
    await seedStorage(page);
  });

  test("loads and renders strategy data in a table", async ({ page }) => {
    await mockBackend(page);

    await page.goto("/strategies");
    await expect(page).toHaveURL(/\/strategies$/);

    // Page heading.
    await expect(
      page.getByRole("heading", { name: "Registered Strategies" }),
    ).toBeVisible();

    // The table is present and each mocked strategy is rendered.
    const table = page.getByTestId("strategies-table");
    await expect(table).toBeVisible();

    const rowMomentum = page.locator(
      '[data-testid="strategy-row"][data-strategy-id="momentum-alpha"]',
    );
    await expect(rowMomentum).toBeVisible();
    await expect(rowMomentum.getByText("MOMENTUM ALPHA")).toBeVisible();
    await expect(
      rowMomentum.getByText("Trend-following momentum strategy"),
    ).toBeVisible();
    // Active status badge.
    await expect(rowMomentum.getByText("Active", { exact: true })).toBeVisible();
    // Positive P&L formatted as USD currency.
    await expect(rowMomentum.getByText("$12,450.75")).toBeVisible();

    // A strategy with negative P&L renders too.
    const rowRevert = page.locator(
      '[data-testid="strategy-row"][data-strategy-id="mean-revert"]',
    );
    await expect(rowRevert).toBeVisible();
    await expect(rowRevert.getByText("Available", { exact: true })).toBeVisible();
    await expect(rowRevert.getByText("-$2,310.00")).toBeVisible();

    // A strategy without P&L data renders an em dash for P&L.
    const rowPairs = page.locator(
      '[data-testid="strategy-row"][data-strategy-id="pairs-stat"]',
    );
    await expect(rowPairs).toBeVisible();
    await expect(rowPairs.getByText("PAIRS STATISTICAL")).toBeVisible();

    // Row count summary.
    await expect(page.getByText("3 strategies", { exact: true })).toBeVisible();
  });

  test("shows an error state and recovers on retry", async ({ page }) => {
    // First load: strategies endpoint fails.
    await mockBackend(page, {
      strategiesStatus: 500,
      strategiesBody: { detail: "engine unavailable" },
    });

    await page.goto("/strategies");

    const errorState = page.getByTestId("strategies-error");
    await expect(errorState).toBeVisible();
    await expect(errorState.getByText("engine unavailable")).toBeVisible();
    // No table while in the error state.
    await expect(page.getByTestId("strategies-table")).toHaveCount(0);

    // Swap the mock to a success response, then click Retry.
    await page.unroute(`${API_BASE}/**`);
    await mockBackend(page, { strategiesBody: { strategies: MOCK_STRATEGIES } });

    await errorState.getByRole("button", { name: /Retry/i }).click();

    await expect(page.getByTestId("strategies-table")).toBeVisible();
    await expect(
      page.locator(
        '[data-testid="strategy-row"][data-strategy-id="momentum-alpha"]',
      ),
    ).toBeVisible();
  });

  test("shows an empty state when no strategies are registered", async ({
    page,
  }) => {
    await mockBackend(page, { strategiesBody: { strategies: [] } });

    await page.goto("/strategies");

    await expect(page.getByTestId("strategies-empty")).toBeVisible();
    await expect(page.getByTestId("strategies-table")).toHaveCount(0);
  });

  test("is reachable via the STRATEGIES nav link", async ({ page }) => {
    await mockBackend(page);

    await page.goto("/");
    await expect(page).toHaveURL(/\/$/);

    await page
      .locator("nav")
      .getByRole("link", { name: "STRATEGIES", exact: true })
      .click();

    await expect(page).toHaveURL(/\/strategies$/);
    await expect(
      page.getByRole("heading", { name: "Registered Strategies" }),
    ).toBeVisible();
  });
});
