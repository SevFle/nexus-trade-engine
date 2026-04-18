import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { LiveTradingModal } from "../../components/legal/LiveTradingModal";

vi.mock("../../hooks/useLegal", () => ({
  useAcceptLegal: () => ({
    mutateAsync: vi.fn().mockResolvedValue({ accepted: [] }),
  }),
}));

function renderLiveTradingModal(props = {}) {
  const queryClient = new QueryClient({
    defaultOptions: { mutations: { retry: false } },
  });

  return render(
    <QueryClientProvider client={queryClient}>
      <LiveTradingModal
        open={true}
        onAccept={vi.fn()}
        onClose={vi.fn()}
        {...props}
      />
    </QueryClientProvider>
  );
}

describe("LiveTradingModal", () => {
  it("renders when open", () => {
    renderLiveTradingModal();
    expect(
      screen.getByText("LIVE TRADING RISK ACKNOWLEDGMENT")
    ).toBeInTheDocument();
  });

  it("does not render when closed", () => {
    renderLiveTradingModal({ open: false });
    expect(
      screen.queryByText("LIVE TRADING RISK ACKNOWLEDGMENT")
    ).not.toBeInTheDocument();
  });

  it("shows risk disclosures", () => {
    renderLiveTradingModal();
    expect(
      screen.getByText(/live trading carries significant financial risk/i)
    ).toBeInTheDocument();
    expect(
      screen.getByText(/Past performance does not guarantee future results/i)
    ).toBeInTheDocument();
  });

  it("disables begin button when checkbox is not checked", () => {
    renderLiveTradingModal();
    const beginBtn = screen.getByText("BEGIN LIVE TRADING");
    expect(beginBtn).toBeDisabled();
  });

  it("enables begin button after checking the risk checkbox", async () => {
    renderLiveTradingModal();
    const checkbox = screen.getByRole("checkbox");
    fireEvent.click(checkbox);

    const beginBtn = screen.getByText("BEGIN LIVE TRADING");
    await waitFor(() => expect(beginBtn).not.toBeDisabled());
  });

  it("shows cancel button", () => {
    renderLiveTradingModal();
    expect(screen.getByText("CANCEL")).toBeInTheDocument();
  });

  it("calls onClose when cancel is clicked", () => {
    const onClose = vi.fn();
    renderLiveTradingModal({ onClose });
    fireEvent.click(screen.getByText("CANCEL"));
    expect(onClose).toHaveBeenCalled();
  });

  it("shows risk acceptance label", () => {
    renderLiveTradingModal();
    expect(
      screen.getByText(/I understand the risks of live trading/i)
    ).toBeInTheDocument();
  });
});
