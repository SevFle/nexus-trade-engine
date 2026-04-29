import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter } from "react-router-dom";
import Settings from "../../screens/Settings";

vi.mock("../../hooks/useLegal", () => ({
  useLegalDocuments: () => ({
    data: [
      { slug: "risk-disclaimer", title: "Risk Disclaimer", version: "1.0.0", requires_acceptance: true },
      { slug: "terms-of-service", title: "Terms of Service", version: "2.0.0", requires_acceptance: true },
    ],
    isLoading: false,
  }),
  useMyAcceptances: () => ({
    data: {
      acceptances: [
        {
          document_slug: "risk-disclaimer",
          document_title: "Risk Disclaimer",
          version: "1.0.0",
          accepted_at: "2026-04-18T10:00:00Z",
        },
      ],
    },
    isLoading: false,
  }),
}));

vi.mock("../../components/feedback/LoadingSpinner", () => ({
  LoadingSpinner: () => <div data-testid="loading-spinner" />,
}));

function renderSettings() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });

  return render(
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <Settings />
      </BrowserRouter>
    </QueryClientProvider>
  );
}

describe("Settings Legal Section", () => {
  it("renders the legal and compliance header", () => {
    renderSettings();
    expect(screen.getByText("LEGAL & COMPLIANCE")).toBeInTheDocument();
  });

  it("renders legal documents list with both documents", () => {
    renderSettings();
    const riskElements = screen.getAllByText("Risk Disclaimer");
    const tosElements = screen.getAllByText("Terms of Service");
    expect(riskElements.length).toBeGreaterThanOrEqual(1);
    expect(tosElements.length).toBeGreaterThanOrEqual(1);
  });

  it("renders document versions", () => {
    renderSettings();
    const v1Elements = screen.getAllByText("v1.0.0");
    const v2Elements = screen.getAllByText("v2.0.0");
    expect(v1Elements.length).toBeGreaterThanOrEqual(1);
    expect(v2Elements.length).toBeGreaterThanOrEqual(1);
  });

  it("renders acceptance history section header", () => {
    renderSettings();
    expect(screen.getByText("ACCEPTANCE HISTORY")).toBeInTheDocument();
  });

  it("renders document links to legal document pages", () => {
    renderSettings();
    const links = screen.getAllByRole("link");
    const legalLinks = links.filter((l) =>
      l.getAttribute("href")?.startsWith("/legal/")
    );
    expect(legalLinks.length).toBeGreaterThanOrEqual(2);
    const hrefs = legalLinks.map((l) => l.getAttribute("href"));
    expect(hrefs).toContain("/legal/risk-disclaimer");
    expect(hrefs).toContain("/legal/terms-of-service");
  });

  it("renders acceptance date in history", () => {
    renderSettings();
    expect(screen.getByText(/Apr 18, 2026/)).toBeInTheDocument();
  });
});
