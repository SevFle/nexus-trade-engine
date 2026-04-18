import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import {
  DisclaimerBanner,
  BacktestDisclaimer,
  PaperTradingDisclaimer,
  MarketplaceDisclaimer,
} from "./DisclaimerBanner";

describe("DisclaimerBanner", () => {
  it("renders children text", () => {
    render(<DisclaimerBanner>Test disclaimer text</DisclaimerBanner>);
    expect(screen.getByText("Test disclaimer text")).toBeInTheDocument();
  });

  it("has role alert", () => {
    render(<DisclaimerBanner>Test</DisclaimerBanner>);
    expect(screen.getByRole("alert")).toBeInTheDocument();
  });

  it("renders warning variant by default", () => {
    render(<DisclaimerBanner>Test</DisclaimerBanner>);
    const alert = screen.getByRole("alert");
    expect(alert.className).toContain("bg-nx-warning");
  });

  it("renders info variant", () => {
    render(<DisclaimerBanner variant="info">Test</DisclaimerBanner>);
    const alert = screen.getByRole("alert");
    expect(alert.className).toContain("bg-nx-interactive");
  });

  it("renders danger variant", () => {
    render(<DisclaimerBanner variant="danger">Test</DisclaimerBanner>);
    const alert = screen.getByRole("alert");
    expect(alert.className).toContain("bg-nx-accent");
  });
});

describe("BacktestDisclaimer", () => {
  it("renders correct disclaimer text", () => {
    render(<BacktestDisclaimer />);
    expect(
      screen.getByText(/past performance does not guarantee future results/i)
    ).toBeInTheDocument();
  });

  it("mentions look-ahead and selection bias", () => {
    render(<BacktestDisclaimer />);
    expect(screen.getByText(/look-ahead and selection bias/i)).toBeInTheDocument();
  });
});

describe("PaperTradingDisclaimer", () => {
  it("renders correct disclaimer text", () => {
    render(<PaperTradingDisclaimer />);
    expect(
      screen.getByText(/paper trading results may differ materially/i)
    ).toBeInTheDocument();
  });
});

describe("MarketplaceDisclaimer", () => {
  it("renders correct disclaimer text", () => {
    render(<MarketplaceDisclaimer />);
    expect(
      screen.getByText(/third-party code in your environment/i)
    ).toBeInTheDocument();
  });

  it("mentions Nexus is not responsible", () => {
    render(<MarketplaceDisclaimer />);
    expect(
      screen.getByText(/nexus is not responsible/i)
    ).toBeInTheDocument();
  });
});
