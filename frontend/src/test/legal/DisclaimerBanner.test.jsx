import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { DisclaimerBanner } from "../../components/legal/DisclaimerBanner";

describe("DisclaimerBanner", () => {
  it("renders children text", () => {
    render(<DisclaimerBanner>Backtesting has limitations</DisclaimerBanner>);
    expect(screen.getByText("Backtesting has limitations")).toBeInTheDocument();
  });

  it("renders with alert role for accessibility", () => {
    render(<DisclaimerBanner>Test warning</DisclaimerBanner>);
    expect(screen.getByRole("alert")).toBeInTheDocument();
  });

  it("renders warning variant by default", () => {
    const { container } = render(<DisclaimerBanner>Warning text</DisclaimerBanner>);
    const el = container.firstChild;
    expect(el.className).toContain("border-nx-warning");
  });

  it("renders info variant", () => {
    const { container } = render(
      <DisclaimerBanner variant="info">Info text</DisclaimerBanner>
    );
    const el = container.firstChild;
    expect(el.className).toContain("bg-nx-surface-raised");
  });

  it("renders danger variant", () => {
    const { container } = render(
      <DisclaimerBanner variant="danger">Danger text</DisclaimerBanner>
    );
    const el = container.firstChild;
    expect(el.className).toContain("border-nx-accent");
  });
});
