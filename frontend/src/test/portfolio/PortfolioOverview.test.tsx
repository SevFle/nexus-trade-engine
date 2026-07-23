import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";

// Mock the typed API client singleton so the page never hits the network.
// `vi.hoisted` runs the factory before `vi.mock` is hoisted, so the mock fn
// is defined by the time the mocked module is evaluated.
const { getPortfolioSummary } = vi.hoisted(() => ({
  getPortfolioSummary: vi.fn(),
}));

vi.mock("../../lib/api", () => ({
  apiClient: { getPortfolioSummary },
}));

// Import AFTER the mock is registered so the page picks up the stubbed client.
import PortfolioOverview from "../../pages/PortfolioOverview";

function renderOverview() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <PortfolioOverview />
    </QueryClientProvider>,
  );
}

function withProviders(node: ReactNode) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return (
    <QueryClientProvider client={queryClient}>{node}</QueryClientProvider>
  );
}

describe("PortfolioOverview", () => {
  beforeEach(() => {
    getPortfolioSummary.mockReset();
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  it("renders the page heading", () => {
    getPortfolioSummary.mockResolvedValue({ as_of: "2099-01-01T00:00:00Z" });
    renderOverview();
    expect(
      screen.getByRole("heading", { name: "Portfolio Overview" }),
    ).toBeInTheDocument();
  });

  it("shows the loading skeleton while the query is pending", () => {
    // Never-resolving promise keeps the query in pending state.
    getPortfolioSummary.mockReturnValue(new Promise(() => {}));
    renderOverview();
    expect(screen.getByTestId("portfolio-summary-loading")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Retry" })).not.toBeTruthy();
  });

  it("renders the three summary cards once data arrives", async () => {
    getPortfolioSummary.mockResolvedValue({
      total_value: 150000,
      total_pnl: 5000,
      total_pnl_pct: 3.45,
      active_strategies: 4,
      open_positions: 12,
      currency: "USD",
      as_of: "2099-01-01T00:00:00Z",
    });

    renderOverview();

    await waitFor(() => {
      expect(screen.getByTestId("portfolio-summary-cards")).toBeInTheDocument();
    });

    // Total portfolio value card.
    expect(screen.getByTestId("portfolio-card-total-portfolio-value")).toBeInTheDocument();
    expect(screen.getByText("$150,000.00")).toBeInTheDocument();
    expect(screen.getByText(/12 open positions/)).toBeInTheDocument();

    // P&L card — positive tone, +$5,000.00 and +3.45%.
    expect(screen.getByTestId("portfolio-card-p-l")).toBeInTheDocument();
    expect(screen.getByText("+$5,000.00")).toBeInTheDocument();
    expect(screen.getByText("+3.45%")).toBeInTheDocument();

    // Active strategies card.
    expect(screen.getByTestId("portfolio-card-active-strategies")).toBeInTheDocument();
    expect(screen.getByText("4")).toBeInTheDocument();
  });

  it("tones the P&L card negatively for a loss", async () => {
    getPortfolioSummary.mockResolvedValue({
      total_value: 95000,
      total_pnl: -2500,
      total_pnl_pct: -2.5,
      active_strategies: 1,
      open_positions: 1,
      currency: "USD",
      as_of: "2099-01-01T00:00:00Z",
    });

    const { container } = renderOverview();

    await waitFor(() => {
      expect(screen.getByTestId("portfolio-summary-cards")).toBeInTheDocument();
    });

    expect(screen.getByText("-$2,500.00")).toBeInTheDocument();
    // A down-trend icon should render for the negative P&L.
    expect(container.querySelector("svg.lucide-trending-down")).not.toBeNull();
    expect(container.querySelector("svg.lucide-trending-up")).toBeNull();
  });

  it("tones the P&L card positively for a gain", async () => {
    getPortfolioSummary.mockResolvedValue({
      total_value: 100000,
      total_pnl: 100,
      total_pnl_pct: 0.1,
      active_strategies: 0,
      open_positions: 0,
      currency: "USD",
      as_of: "2099-01-01T00:00:00Z",
    });

    const { container } = renderOverview();

    await waitFor(() => {
      expect(screen.getByTestId("portfolio-summary-cards")).toBeInTheDocument();
    });

    expect(container.querySelector("svg.lucide-trending-up")).not.toBeNull();
    expect(container.querySelector("svg.lucide-trending-down")).toBeNull();
  });

  it("renders the error state with a retry control on failure", async () => {
    getPortfolioSummary.mockRejectedValue(new Error("boom: 503"));

    renderOverview();

    await waitFor(() => {
      expect(screen.getByTestId("portfolio-summary-error")).toBeInTheDocument();
    });

    expect(screen.getByText("boom: 503")).toBeInTheDocument();
    const retry = screen.getByRole("button", { name: /Retry/ });
    expect(retry).toBeInTheDocument();

    // Retry resolves and the cards render.
    getPortfolioSummary.mockResolvedValue({
      total_value: 1,
      total_pnl: 0,
      total_pnl_pct: 0,
      active_strategies: 0,
      open_positions: 0,
      currency: "USD",
      as_of: "2099-01-01T00:00:00Z",
    });
    fireEvent.click(retry);

    await waitFor(() => {
      expect(screen.getByTestId("portfolio-summary-cards")).toBeInTheDocument();
    });
  });

  it("calls apiClient.getPortfolioSummary on mount", () => {
    getPortfolioSummary.mockResolvedValue({
      total_value: 0,
      total_pnl: 0,
      total_pnl_pct: 0,
      active_strategies: 0,
      open_positions: 0,
      currency: "USD",
      as_of: "2099-01-01T00:00:00Z",
    });
    renderOverview();
    expect(getPortfolioSummary).toHaveBeenCalledTimes(1);
  });

  it("preserves provider semantics for nested renders", () => {
    // Sanity check that withProviders produces a valid element tree.
    getPortfolioSummary.mockResolvedValue({
      total_value: 0,
      total_pnl: 0,
      total_pnl_pct: 0,
      active_strategies: 0,
      open_positions: 0,
      currency: "USD",
      as_of: "2099-01-01T00:00:00Z",
    });
    const { unmount } = render(withProviders(<PortfolioOverview />));
    expect(
      screen.getByRole("heading", { name: "Portfolio Overview" }),
    ).toBeInTheDocument();
    unmount();
  });
});
