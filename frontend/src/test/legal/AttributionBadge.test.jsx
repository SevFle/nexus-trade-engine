import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { AttributionBadge } from "../../components/legal/AttributionBadge";

describe("AttributionBadge", () => {
  it("renders provider name when no description", () => {
    render(<AttributionBadge provider="TestProvider" />);
    expect(screen.getByText("TestProvider")).toBeInTheDocument();
  });

  it("renders description when provided", () => {
    render(
      <AttributionBadge provider="TestProvider" description="Data by TestProvider" />
    );
    expect(screen.getByText("Data by TestProvider")).toBeInTheDocument();
  });

  it("renders as link when url is provided", () => {
    render(
      <AttributionBadge
        provider="Polygon"
        description="Market data"
        url="https://polygon.io"
      />
    );
    const link = screen.getByRole("link");
    expect(link).toHaveAttribute("href", "https://polygon.io");
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", "noopener noreferrer");
  });

  it("renders as span when no url", () => {
    render(<AttributionBadge provider="LocalData" description="Local data" />);
    expect(screen.queryByRole("link")).toBeNull();
    expect(screen.getByText("Local data")).toBeInTheDocument();
  });
});
