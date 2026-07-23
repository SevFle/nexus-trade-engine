import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import {
  render,
  screen,
  fireEvent,
  waitFor,
  cleanup,
} from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
// Mock the typed API client singleton so the page never hits the network.
// `vi.hoisted` runs the factory before `vi.mock` is hoisted, so the mock fns
// are defined by the time the mocked module is evaluated.
const { listStrategies, runBacktest } = vi.hoisted(() => ({
  listStrategies: vi.fn(),
  runBacktest: vi.fn(),
}));

vi.mock("../../lib/api", () => ({
  apiClient: { listStrategies, runBacktest },
  ApiError: class ApiError extends Error {
    readonly status: number;
    constructor(message: string, status: number) {
      super(message);
      this.name = "ApiError";
      this.status = status;
    }
  },
}));

// Import AFTER the mock is registered so the page picks up the stubbed client.
import BacktestPage from "../../pages/BacktestPage";

function newQueryClient() {
  return new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });
}

function renderPage() {
  return render(
    <QueryClientProvider client={newQueryClient()}>
      <BacktestPage />
    </QueryClientProvider>,
  );
}

function fillValidForm() {
  fireEvent.change(screen.getByTestId("backtest-strategy-select"), {
    target: { value: "momentum-alpha" },
  });
  fireEvent.change(screen.getByTestId("backtest-symbol-input"), {
    target: { value: "aapl" },
  });
  fireEvent.change(screen.getByTestId("backtest-capital-input"), {
    target: { value: "50000" },
  });
  fireEvent.change(screen.getByTestId("backtest-start-date"), {
    target: { value: "2024-01-01" },
  });
  fireEvent.change(screen.getByTestId("backtest-end-date"), {
    target: { value: "2024-12-31" },
  });
}

const STRATEGIES = {
  strategies: [
    { id: "momentum-alpha", name: "Momentum Alpha" },
    { id: "mean-revert", name: "Mean Reversion" },
  ],
};

describe("BacktestPage", () => {
  beforeEach(() => {
    listStrategies.mockReset();
    runBacktest.mockReset();
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it("renders the page heading and form", () => {
    listStrategies.mockReturnValue(new Promise(() => {}));
    renderPage();
    expect(
      screen.getByRole("heading", { name: "Run a Backtest" }),
    ).toBeInTheDocument();
    expect(screen.getByTestId("backtest-form")).toBeInTheDocument();
  });

  it("populates the strategy dropdown once data arrives", async () => {
    listStrategies.mockResolvedValue(STRATEGIES);
    renderPage();
    await waitFor(() => {
      expect(
        screen.getByRole("option", { name: "Momentum Alpha" }),
      ).toBeInTheDocument();
    });
    expect(
      screen.getByRole("option", { name: "Mean Reversion" }),
    ).toBeInTheDocument();
  });

  it("shows the strategies load error state on failure", async () => {
    listStrategies.mockRejectedValue(new Error("engine down"));
    renderPage();
    await waitFor(() => {
      expect(screen.getByTestId("backtest-strategies-error")).toBeInTheDocument();
    });
    expect(screen.getByText("engine down")).toBeInTheDocument();
    // The form should not render while strategies failed to load.
    expect(screen.queryByTestId("backtest-form")).not.toBeInTheDocument();
  });

  it("recovers the strategies list on retry", async () => {
    listStrategies.mockRejectedValueOnce(new Error("transient"));
    renderPage();
    await waitFor(() => {
      expect(screen.getByTestId("backtest-strategies-error")).toBeInTheDocument();
    });
    listStrategies.mockResolvedValue(STRATEGIES);
    fireEvent.click(screen.getByRole("button", { name: /Retry/i }));
    await waitFor(() => {
      expect(
        screen.getByRole("option", { name: "Momentum Alpha" }),
      ).toBeInTheDocument();
    });
  });

  it("shows inline validation errors and does not submit when invalid", async () => {
    listStrategies.mockResolvedValue(STRATEGIES);
    renderPage();
    await waitFor(() => {
      expect(
        screen.getByRole("option", { name: "Momentum Alpha" }),
      ).toBeInTheDocument();
    });

    // Clear the default symbol + capital so validation trips on every field.
    fireEvent.change(screen.getByTestId("backtest-symbol-input"), {
      target: { value: "" },
    });
    fireEvent.change(screen.getByTestId("backtest-capital-input"), {
      target: { value: "" },
    });

    fireEvent.click(screen.getByTestId("backtest-submit"));

    expect(screen.getByTestId("strategy_name-error")).toBeInTheDocument();
    expect(screen.getByTestId("symbol-error")).toBeInTheDocument();
    expect(screen.getByTestId("start_date-error")).toBeInTheDocument();
    expect(screen.getByTestId("end_date-error")).toBeInTheDocument();
    expect(screen.getByTestId("initial_capital-error")).toBeInTheDocument();
    expect(runBacktest).not.toHaveBeenCalled();
  });

  it("flags an inverted date range", async () => {
    listStrategies.mockResolvedValue(STRATEGIES);
    renderPage();
    await waitFor(() => {
      expect(
        screen.getByRole("option", { name: "Momentum Alpha" }),
      ).toBeInTheDocument();
    });

    fireEvent.change(screen.getByTestId("backtest-strategy-select"), {
      target: { value: "momentum-alpha" },
    });
    fireEvent.change(screen.getByTestId("backtest-start-date"), {
      target: { value: "2024-12-31" },
    });
    fireEvent.change(screen.getByTestId("backtest-end-date"), {
      target: { value: "2024-01-01" },
    });

    fireEvent.click(screen.getByTestId("backtest-submit"));

    expect(screen.getByTestId("end_date-error")).toHaveTextContent(
      /on or after the start date/i,
    );
    expect(runBacktest).not.toHaveBeenCalled();
  });

  it("submits a valid run and shows the success state with the backtest id", async () => {
    listStrategies.mockResolvedValue(STRATEGIES);
    runBacktest.mockResolvedValue({
      status: "accepted",
      backtest_id: "bt-123",
    });
    renderPage();
    await waitFor(() => {
      expect(
        screen.getByRole("option", { name: "Momentum Alpha" }),
      ).toBeInTheDocument();
    });

    fillValidForm();
    fireEvent.click(screen.getByTestId("backtest-submit"));

    await waitFor(() => {
      expect(screen.getByTestId("backtest-submit-success")).toBeInTheDocument();
    });

    // The request mirrors the documented BacktestSubmitRequest shape, with the
    // cost model passed through config.
    expect(runBacktest).toHaveBeenCalledTimes(1);
    expect(runBacktest).toHaveBeenCalledWith({
      strategy_name: "momentum-alpha",
      symbol: "AAPL",
      start_date: "2024-01-01",
      end_date: "2024-12-31",
      initial_capital: 50000,
      config: { cost_model: "default" },
    });
    // The returned backtest id is surfaced.
    expect(screen.getByText("bt-123")).toBeInTheDocument();
  });

  it("shows a submission error state when the mutation fails", async () => {
    listStrategies.mockResolvedValue(STRATEGIES);
    runBacktest.mockRejectedValue(new Error("engine blew up"));
    renderPage();
    await waitFor(() => {
      expect(
        screen.getByRole("option", { name: "Momentum Alpha" }),
      ).toBeInTheDocument();
    });

    fillValidForm();
    fireEvent.click(screen.getByTestId("backtest-submit"));

    await waitFor(() => {
      expect(screen.getByTestId("backtest-submit-error")).toBeInTheDocument();
    });
    expect(screen.getByText("engine blew up")).toBeInTheDocument();
    expect(screen.queryByTestId("backtest-submit-success")).not.toBeInTheDocument();
  });

  it("uppercases and trims the symbol on submit", async () => {
    listStrategies.mockResolvedValue(STRATEGIES);
    runBacktest.mockResolvedValue({
      status: "accepted",
      backtest_id: "bt-9",
    });
    renderPage();
    await waitFor(() => {
      expect(
        screen.getByRole("option", { name: "Momentum Alpha" }),
      ).toBeInTheDocument();
    });

    fireEvent.change(screen.getByTestId("backtest-strategy-select"), {
      target: { value: "momentum-alpha" },
    });
    fireEvent.change(screen.getByTestId("backtest-symbol-input"), {
      target: { value: "  msft  " },
    });
    fireEvent.change(screen.getByTestId("backtest-capital-input"), {
      target: { value: "100000" },
    });
    fireEvent.change(screen.getByTestId("backtest-start-date"), {
      target: { value: "2024-01-01" },
    });
    fireEvent.change(screen.getByTestId("backtest-end-date"), {
      target: { value: "2024-06-30" },
    });

    fireEvent.click(screen.getByTestId("backtest-submit"));

    await waitFor(() => expect(runBacktest).toHaveBeenCalled());
    expect(runBacktest.mock.calls[0][0].symbol).toBe("MSFT");
  });

  it("reset clears the form and the success banner", async () => {
    listStrategies.mockResolvedValue(STRATEGIES);
    runBacktest.mockResolvedValue({
      status: "accepted",
      backtest_id: "bt-reset",
    });
    renderPage();
    await waitFor(() => {
      expect(
        screen.getByRole("option", { name: "Momentum Alpha" }),
      ).toBeInTheDocument();
    });

    fillValidForm();
    fireEvent.click(screen.getByTestId("backtest-submit"));
    await waitFor(() => {
      expect(screen.getByTestId("backtest-submit-success")).toBeInTheDocument();
    });

    fireEvent.click(screen.getByTestId("backtest-reset"));

    await waitFor(() => {
      expect(
        screen.queryByTestId("backtest-submit-success"),
      ).not.toBeInTheDocument();
    });
    // Capital returns to the default; strategy clears back to the placeholder.
    expect(
      (screen.getByTestId("backtest-capital-input") as HTMLInputElement).value,
    ).toBe("100000");
    expect(
      (screen.getByTestId("backtest-strategy-select") as HTMLSelectElement)
        .value,
    ).toBe("");
  });

  it("renders provider semantics for nested renders", () => {
    listStrategies.mockReturnValue(new Promise(() => {}));
    const { unmount } = render(
      <QueryClientProvider client={newQueryClient()}>
        <BacktestPage />
      </QueryClientProvider>,
    );
    expect(
      screen.getByRole("heading", { name: "Run a Backtest" }),
    ).toBeInTheDocument();
    unmount();
    expect(screen.queryByTestId("backtest-form")).not.toBeInTheDocument();
  });
});

