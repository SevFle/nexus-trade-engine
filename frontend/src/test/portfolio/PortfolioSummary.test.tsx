import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { PortfolioSummary } from "../../components/portfolio/PortfolioSummary";

describe("PortfolioSummary", () => {
  it("renders the placeholder state by default", () => {
    render(<PortfolioSummary />);
    expect(screen.getByText("Portfolio Summary")).toBeInTheDocument();
    expect(screen.getByText("placeholder")).toBeInTheDocument();
    expect(screen.getByTestId("portfolio-summary")).toBeInTheDocument();
    // Default metrics render an em-dash until real data arrives.
    expect(screen.getAllByText("—").length).toBeGreaterThan(0);
  });

  it("hides the placeholder tag when placeholder is false", () => {
    render(
      <PortfolioSummary
        placeholder={false}
        totalValue="$1,000.00"
        dayPnl="+$10.00"
        dayPnlPct="+1.00%"
        openPositions={3}
        healthScore={99}
      />,
    );
    expect(screen.queryByText("placeholder")).toBeNull();
    expect(screen.getByText("$1,000.00")).toBeInTheDocument();
    expect(screen.getByText("+$10.00")).toBeInTheDocument();
    expect(screen.getByText("+1.00%")).toBeInTheDocument();
    expect(screen.getByText("3")).toBeInTheDocument();
    expect(screen.getByText("99")).toBeInTheDocument();
  });

  it("renders an accessible region label", () => {
    render(<PortfolioSummary />);
    expect(screen.getByRole("region", { name: "Portfolio summary" })).toBeInTheDocument();
  });
});
