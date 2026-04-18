import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RiskAcceptanceModal } from "./RiskAcceptanceModal";

function renderWithProviders(ui) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>{ui}</QueryClientProvider>
  );
}

describe("RiskAcceptanceModal", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("renders with live trading risk content", () => {
    renderWithProviders(
      <RiskAcceptanceModal
        documentSlug="risk-disclaimer"
        version="1.0"
        onAccepted={vi.fn()}
        onCancel={vi.fn()}
      />
    );

    expect(screen.getByText(/live trading risk acknowledgment/i)).toBeInTheDocument();
    expect(screen.getByText(/real money will be at risk/i)).toBeInTheDocument();
  });

  it("has cancel and accept buttons", () => {
    renderWithProviders(
      <RiskAcceptanceModal
        documentSlug="risk-disclaimer"
        version="1.0"
        onAccepted={vi.fn()}
        onCancel={vi.fn()}
      />
    );

    expect(
      screen.getByRole("button", { name: /cancel/i })
    ).toBeEnabled();
    expect(
      screen.getByRole("button", { name: /i accept this risk/i })
    ).toBeDisabled();
  });

  it("enables accept button after checking checkbox", async () => {
    const user = userEvent.setup();
    renderWithProviders(
      <RiskAcceptanceModal
        documentSlug="risk-disclaimer"
        version="1.0"
        onAccepted={vi.fn()}
        onCancel={vi.fn()}
      />
    );

    const checkbox = screen.getByRole("checkbox", {
      name: /i accept the risks of live trading/i,
    });
    await user.click(checkbox);

    expect(
      screen.getByRole("button", { name: /i accept this risk/i })
    ).toBeEnabled();
  });

  it("calls onCancel when cancel is clicked", async () => {
    const user = userEvent.setup();
    const onCancel = vi.fn();
    renderWithProviders(
      <RiskAcceptanceModal
        documentSlug="risk-disclaimer"
        version="1.0"
        onAccepted={vi.fn()}
        onCancel={onCancel}
      />
    );

    await user.click(screen.getByRole("button", { name: /cancel/i }));
    expect(onCancel).toHaveBeenCalledOnce();
  });

  it("calls accept API and onAccepted when accepted", async () => {
    const user = userEvent.setup();
    const onAccepted = vi.fn();
    vi.spyOn(globalThis, "fetch").mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({}),
    });

    renderWithProviders(
      <RiskAcceptanceModal
        documentSlug="risk-disclaimer"
        version="1.0"
        onAccepted={onAccepted}
        onCancel={vi.fn()}
      />
    );

    await user.click(
      screen.getByRole("checkbox", { name: /i accept the risks of live trading/i })
    );
    await user.click(
      screen.getByRole("button", { name: /i accept this risk/i })
    );

    await waitFor(() => expect(onAccepted).toHaveBeenCalledOnce());
  });

  it("has proper ARIA attributes", () => {
    renderWithProviders(
      <RiskAcceptanceModal
        documentSlug="risk-disclaimer"
        version="1.0"
        onAccepted={vi.fn()}
        onCancel={vi.fn()}
      />
    );

    const dialog = screen.getByRole("dialog");
    expect(dialog).toHaveAttribute("aria-modal", "true");
    expect(dialog).toHaveAttribute("aria-label", "Live trading risk acceptance");
  });
});
