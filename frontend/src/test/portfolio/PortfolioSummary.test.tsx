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
    expect(
      screen.getByRole("region", { name: "Portfolio summary" }),
    ).toBeInTheDocument();
  });

  it("derives a positive tone from a positive pnlDirection regardless of the dayPnl string", () => {
    // dayPnl has no "+" prefix (simulating locale formatting) yet the tone
    // must still be positive because pnlDirection > 0. This guards against
    // regressing back to string-prefix matching.
    const { container } = render(
      <PortfolioSummary
        placeholder={false}
        dayPnl="31,204.18"
        dayPnlPct="1.11%"
        pnlDirection={31204.18}
      />,
    );
    expect(container.querySelectorAll("svg.lucide-trending-up")).toHaveLength(1);
    expect(container.querySelector("svg.lucide-trending-down")).toBeNull();
  });

  it("derives a negative tone from a negative pnlDirection", () => {
    const { container } = render(
      <PortfolioSummary
        placeholder={false}
        dayPnl="-$10.00"
        dayPnlPct="-1.00%"
        pnlDirection={-10}
      />,
    );
    expect(container.querySelectorAll("svg.lucide-trending-down")).toHaveLength(1);
    expect(container.querySelector("svg.lucide-trending-up")).toBeNull();
  });

  it("renders a neutral tone when pnlDirection is zero", () => {
    const { container } = render(
      <PortfolioSummary
        placeholder={false}
        dayPnl="$0.00"
        dayPnlPct="0.00%"
        pnlDirection={0}
      />,
    );
    expect(container.querySelector("svg.lucide-trending-up")).toBeNull();
    expect(container.querySelector("svg.lucide-trending-down")).toBeNull();
  });

  it("falls back to a neutral tone when pnlDirection is omitted", () => {
    const { container } = render(
      <PortfolioSummary placeholder={false} dayPnl="$5.00" />,
    );
    expect(container.querySelector("svg.lucide-trending-up")).toBeNull();
    expect(container.querySelector("svg.lucide-trending-down")).toBeNull();
  });

  it("stays neutral in the placeholder state even with a positive pnlDirection", () => {
    const { container } = render(
      <PortfolioSummary placeholder pnlDirection={1000} />,
    );
    expect(container.querySelector("svg.lucide-trending-up")).toBeNull();
    expect(container.querySelector("svg.lucide-trending-down")).toBeNull();
  });
});
