import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { BrowserRouter } from "react-router-dom";
import { Footer } from "../../components/layout/Footer";

function renderWithRouter(ui) {
  return render(<BrowserRouter>{ui}</BrowserRouter>);
}

describe("Footer", () => {
  it("renders all legal document links", () => {
    renderWithRouter(<Footer />);

    const expectedLinks = [
      "Risk Disclaimer",
      "Terms of Service",
      "Privacy Policy",
      "EULA",
      "Marketplace EULA",
    ];

    for (const label of expectedLinks) {
      expect(screen.getByText(label)).toBeInTheDocument();
    }
  });

  it("renders links with correct legal document paths", () => {
    renderWithRouter(<Footer />);

    const links = screen.getAllByRole("link");
    const legalLinks = links.filter((link) =>
      link.getAttribute("href")?.startsWith("/legal/")
    );

    expect(legalLinks.length).toBe(5);

    const hrefs = legalLinks.map((l) => l.getAttribute("href"));
    expect(hrefs).toContain("/legal/risk-disclaimer");
    expect(hrefs).toContain("/legal/terms-of-service");
    expect(hrefs).toContain("/legal/privacy-policy");
    expect(hrefs).toContain("/legal/eula");
    expect(hrefs).toContain("/legal/marketplace-eula");
  });

  it("renders navigation with aria-label for accessibility", () => {
    renderWithRouter(<Footer />);
    expect(screen.getByLabelText("Legal documents")).toBeInTheDocument();
  });
});
