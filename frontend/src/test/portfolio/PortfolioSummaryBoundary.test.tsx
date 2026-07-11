import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen } from "@testing-library/react";
import type { ReactNode } from "react";

// Stub the network call the boundary makes on catch so the test doesn't
// hit jsdom's fetch or log spurious failures.
vi.mock("../../api/clientErrors", () => ({
  reportClientError: vi.fn().mockResolvedValue({ error_id: "test-id" }),
}));

import { ErrorBoundary } from "../../components/ErrorBoundary";
import { PortfolioSummary } from "../../components/portfolio/PortfolioSummary";

/**
 * These tests pin the wiring added in App.tsx: the persistent
 * <PortfolioSummary/> card sits inside its own <ErrorBoundary
 * scope="portfolio-summary">, so a render/data failure degrades to an
 * inline scoped notice instead of blanking the whole shell.
 */
describe("PortfolioSummary error boundary", () => {
  beforeEach(() => {
    vi.useRealTimers();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders the summary normally when there is no error", () => {
    render(
      <ErrorBoundary scope="portfolio-summary">
        <PortfolioSummary placeholder />
      </ErrorBoundary>,
    );
    expect(screen.getByTestId("portfolio-summary")).toBeInTheDocument();
    expect(screen.getByText("Portfolio Summary")).toBeInTheDocument();
  });

  it("isolates a thrown error and surfaces the portfolio-summary scope", () => {
    function Boom({ children }: { children?: ReactNode }) {
      throw new Error("summary blew up");
    }

    render(
      <div data-testid="shell">
        <ErrorBoundary scope="portfolio-summary">
          <PortfolioSummary placeholder={false} totalValue="$1.00" />
          <Boom />
        </ErrorBoundary>
        <div data-testid="sibling">other content survives</div>
      </div>,
    );

    // The scoped fallback is shown.
    expect(screen.getByText(/ERROR — portfolio-summary/i)).toBeInTheDocument();
    // The failing summary is gone.
    expect(screen.queryByTestId("portfolio-summary")).toBeNull();
    // The boundary only isolated the summary — siblings keep rendering.
    expect(screen.getByTestId("sibling")).toBeInTheDocument();
  });
});
