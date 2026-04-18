import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

vi.mock("../../hooks/useLegal", () => ({
  useLegalDocument: () => ({
    data: { content: "# Risk Disclaimer\n\nTrading involves risk." },
  }),
  useAcceptLegal: () => ({
    mutateAsync: vi.fn().mockResolvedValue({ accepted: [] }),
  }),
}));

vi.mock("../../context/LegalContext", () => {
  const pendingDocs = [
    { slug: "risk-disclaimer", title: "Risk Disclaimer", version: "1.0.0" },
    { slug: "terms-of-service", title: "Terms of Service", version: "1.0.0" },
  ];
  return {
    useLegalContext: () => ({
      showConsentModal: true,
      pendingDocs,
      handleAccept: vi.fn().mockResolvedValue(undefined),
      triggerConsent: vi.fn(),
    }),
  };
});

import { ConsentModal } from "../../components/legal/ConsentModal";

function renderConsentModal() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });

  return render(
    <QueryClientProvider client={queryClient}>
      <ConsentModal />
    </QueryClientProvider>
  );
}

describe("ConsentModal", () => {
  it("renders when consent is required", () => {
    renderConsentModal();
    expect(screen.getByText("LEGAL ACCEPTANCE REQUIRED")).toBeInTheDocument();
  });

  it("renders all pending document tabs as buttons", () => {
    renderConsentModal();
    const buttons = screen.getAllByRole("button");
    const tabTexts = buttons.map((b) => b.textContent);
    expect(tabTexts).toContain("Risk Disclaimer");
    expect(tabTexts).toContain("Terms of Service");
  });

  it("has accept button disabled when checkbox is not checked", () => {
    renderConsentModal();
    const acceptBtn = screen.getByText("I ACCEPT");
    expect(acceptBtn).toBeDisabled();
  });

  it("enables accept button after checking the box", async () => {
    renderConsentModal();
    const checkbox = screen.getByRole("checkbox");
    fireEvent.click(checkbox);

    await waitFor(() => {
      expect(screen.getByRole("checkbox")).toBeChecked();
    });
    await waitFor(() => {
      expect(screen.getByText("I ACCEPT")).not.toBeDisabled();
    });
  });

  it("shows consent message text", () => {
    renderConsentModal();
    expect(
      screen.getByText(/review and accept the following legal documents/i)
    ).toBeInTheDocument();
  });

  it("shows agreement checkbox label", () => {
    renderConsentModal();
    expect(
      screen.getByText(/I have read and understood the above documents/i)
    ).toBeInTheDocument();
  });
});
