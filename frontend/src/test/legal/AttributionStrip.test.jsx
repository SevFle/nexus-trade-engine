import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { AttributionStrip } from "../../components/legal/AttributionStrip";

vi.mock("../../hooks/useLegal", () => ({
  useAttributions: () => ({
    data: [
      { provider: "Polygon.io", description: "Market data by Polygon.io", url: "https://polygon.io" },
      { provider: "FMP", description: "Financial data from FMP", url: "https://financialmodelingprep.com" },
    ],
    isLoading: false,
  }),
}));

function renderStrip() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <AttributionStrip />
    </QueryClientProvider>
  );
}

describe("AttributionStrip", () => {
  it("renders attribution badges from API data", () => {
    renderStrip();
    expect(screen.getByText("Market data by Polygon.io")).toBeInTheDocument();
    expect(screen.getByText("Financial data from FMP")).toBeInTheDocument();
  });

  it("renders links for providers with URLs", () => {
    renderStrip();
    const links = screen.getAllByRole("link");
    expect(links.length).toBe(2);
    expect(links[0]).toHaveAttribute("href", "https://polygon.io");
    expect(links[1]).toHaveAttribute("href", "https://financialmodelingprep.com");
  });
});
