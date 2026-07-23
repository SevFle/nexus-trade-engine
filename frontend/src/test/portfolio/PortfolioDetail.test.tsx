import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import {
  QueryClient,
  QueryClientProvider,
} from "@tanstack/react-query";
import {
  MemoryRouter,
  Route,
  Routes,
} from "react-router-dom";

// Mock the typed API client singleton so the page never hits the network.
// `vi.hoisted` runs the factory before `vi.mock` is hoisted, so the mock fn
// is defined by the time the mocked module is evaluated.
const { getPortfolio } = vi.hoisted(() => ({
  getPortfolio: vi.fn(),
}));

vi.mock("../../lib/api", () => ({
  apiClient: { getPortfolio },
}));

// Import AFTER the mock is registered so the page picks up the stubbed client.
import PortfolioDetail from "../../pages/PortfolioDetail";

const PORTFOLIO_ID = "p-123";

const PORTFOLIO_FIXTURE = {
  id: PORTFOLIO_ID,
  name: "Nexus Alpha",
  description: "Flagship momentum strategy",
  initial_capital: 100000,
  created_at: "2025-01-01T00:00:00Z",
  equity_curve: [
    { timestamp: "2025-01-01T00:00:00Z", equity: 100000 },
    { timestamp: "2025-02-01T00:00:00Z", equity: 104000 },
    { timestamp: "2025-03-01T00:00:00Z", equity: 109500 },
  ],
  allocations: [
    { name: "AAPL", value: 60000 },
    { name: "MSFT", value: 30000 },
    { name: "GOOG", value: 15000 },
  ],
};

function renderDetail(initialPath = `/portfolio/${PORTFOLIO_ID}`) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[initialPath]}>
        <Routes>
          <Route path="/portfolio/:id" element={<PortfolioDetail />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("PortfolioDetail", () => {
  beforeEach(() => {
    getPortfolio.mockReset();
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  it("fetches the portfolio by route id and renders the header + metrics", async () => {
    getPortfolio.mockResolvedValue(PORTFOLIO_FIXTURE);

    renderDetail();

    expect(getPortfolio).toHaveBeenCalledWith(PORTFOLIO_ID);
    expect(
      await screen.findByRole("heading", { name: "Nexus Alpha" }),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Flagship momentum strategy"),
    ).toBeInTheDocument();
    // Initial capital metric + created date metric render from core fields.
    expect(screen.getByText("$100,000.00")).toBeInTheDocument();
    expect(
      screen.getByTestId("portfolio-detail-metric-created"),
    ).toBeInTheDocument();
  });

  it("renders the equity curve and allocation charts with derived data", async () => {
    getPortfolio.mockResolvedValue(PORTFOLIO_FIXTURE);

    renderDetail();

    await screen.findByTestId("portfolio-equity-curve");
    expect(screen.getByTestId("portfolio-equity-chart")).toBeInTheDocument();
    // Change summary: 100000 -> 109500 = +9500 (+9.50%).
    expect(screen.getByTestId("portfolio-equity-change").textContent).toContain(
      "+9.50%",
    );

    // Allocation pie + legend.
    expect(screen.getByTestId("portfolio-allocation-chart")).toBeInTheDocument();
    const legend = screen.getByTestId("portfolio-allocation-legend");
    expect(legend).toBeInTheDocument();
    // Each allocation slice renders a legend row keyed by symbol.
    expect(
      screen.getByTestId("portfolio-allocation-row-aapl"),
    ).toBeInTheDocument();
    expect(
      screen.getByTestId("portfolio-allocation-row-msft"),
    ).toBeInTheDocument();
    expect(
      screen.getByTestId("portfolio-allocation-row-goog"),
    ).toBeInTheDocument();
    // Weights are computed against the total (105000): AAPL 60000 ~= 57.1%.
    expect(screen.getByText("57.1%")).toBeInTheDocument();
  });

  it("shows the loading skeleton while the query is pending", () => {
    // Never-resolving promise keeps the query in pending state.
    getPortfolio.mockReturnValue(new Promise(() => {}));
    renderDetail();
    expect(screen.getByTestId("portfolio-detail-loading")).toBeInTheDocument();
  });

  it("renders the error state with a retry control on failure", async () => {
    getPortfolio.mockRejectedValue(new Error("boom: 503"));

    renderDetail();

    await waitFor(() => {
      expect(screen.getByTestId("portfolio-detail-error")).toBeInTheDocument();
    });
    expect(screen.getByText("boom: 503")).toBeInTheDocument();
    const retry = screen.getByRole("button", { name: /Retry/ });
    expect(retry).toBeInTheDocument();

    // Retry resolves and the body renders.
    getPortfolio.mockResolvedValue(PORTFOLIO_FIXTURE);
    fireEvent.click(retry);

    await waitFor(() => {
      expect(
        screen.getByRole("heading", { name: "Nexus Alpha" }),
      ).toBeInTheDocument();
    });
  });

  it("falls back to empty states when the backend omits chart fields", async () => {
    // Metadata-only response (current backend shape): no equity_curve or allocations.
    getPortfolio.mockResolvedValue({
      id: PORTFOLIO_ID,
      name: "Bare Portfolio",
      description: "",
      initial_capital: 50000,
      created_at: "2025-06-01T00:00:00Z",
    });

    renderDetail();

    await screen.findByRole("heading", { name: "Bare Portfolio" });
    // Both chart sections show their empty states instead of rendering charts.
    const emptyStates = screen.getAllByTestId("portfolio-chart-empty");
    expect(emptyStates).toHaveLength(2);
    expect(screen.queryByTestId("portfolio-equity-chart")).toBeNull();
    expect(screen.queryByTestId("portfolio-allocation-chart")).toBeNull();
    // Holdings metric reflects zero allocations.
    expect(
      screen.getByTestId("portfolio-detail-metric-holdings"),
    ).toHaveTextContent("0");
  });
});
