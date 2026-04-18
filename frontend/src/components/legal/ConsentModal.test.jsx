import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ConsentModal } from "./ConsentModal";

function renderWithProviders(ui) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>{ui}</QueryClientProvider>
  );
}

const MOCK_DOCUMENTS = [
  {
    slug: "risk-disclaimer",
    title: "Risk Disclaimer",
    current_version: "1.0",
    requires_acceptance: true,
    content: "<p>Risk disclaimer content</p>",
  },
  {
    slug: "terms-of-service",
    title: "Terms of Service",
    current_version: "2.0",
    requires_acceptance: true,
    content: "<p>Terms of service content</p>",
  },
];

describe("ConsentModal", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("renders loading state", () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(
      () => new Promise(() => {})
    );
    renderWithProviders(<ConsentModal onComplete={vi.fn()} />);
    expect(screen.getByText(/loading legal documents/i)).toBeInTheDocument();
  });

  it("calls onComplete when no documents require acceptance", async () => {
    const onComplete = vi.fn();
    vi.spyOn(globalThis, "fetch").mockResolvedValue({
      ok: true,
      json: () => Promise.resolve([]),
    });
    renderWithProviders(<ConsentModal onComplete={onComplete} />);
    await waitFor(() => expect(onComplete).toHaveBeenCalled());
  });

  it("renders first document and accept button", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation((url) => {
      if (url.includes("/legal/documents")) {
        return Promise.resolve({
          ok: true,
          json: () => Promise.resolve(MOCK_DOCUMENTS),
        });
      }
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve([]),
      });
    });

    renderWithProviders(<ConsentModal onComplete={vi.fn()} />);

    await waitFor(() => {
      expect(screen.getByText("Risk Disclaimer")).toBeInTheDocument();
    });
    expect(screen.getByText("1 / 2")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /i understand and accept/i })
    ).toBeDisabled();
  });

  it("enables accept button after checking checkbox", async () => {
    const user = userEvent.setup();
    vi.spyOn(globalThis, "fetch").mockImplementation((url) => {
      if (url.includes("/legal/documents")) {
        return Promise.resolve({
          ok: true,
          json: () => Promise.resolve(MOCK_DOCUMENTS),
        });
      }
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve([]),
      });
    });

    renderWithProviders(<ConsentModal onComplete={vi.fn()} />);

    await waitFor(() => {
      expect(screen.getByText("Risk Disclaimer")).toBeInTheDocument();
    });

    const checkbox = screen.getByRole("checkbox", {
      name: /i understand and accept the terms/i,
    });
    expect(checkbox).not.toBeChecked();

    await user.click(checkbox);
    expect(checkbox).toBeChecked();

    const button = screen.getByRole("button", { name: /i understand and accept/i });
    expect(button).toBeEnabled();
  });

  it("progresses to next document on accept and calls onComplete at end", async () => {
    const user = userEvent.setup();
    const onComplete = vi.fn();
    let acceptCallCount = 0;

    vi.spyOn(globalThis, "fetch").mockImplementation((url, options) => {
      if (url.includes("/legal/documents") && !url.includes("/accept")) {
        return Promise.resolve({
          ok: true,
          json: () => Promise.resolve(MOCK_DOCUMENTS),
        });
      }
      if (url.includes("/legal/accept")) {
        acceptCallCount++;
        return Promise.resolve({
          ok: true,
          json: () => Promise.resolve({}),
        });
      }
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve([]),
      });
    });

    renderWithProviders(<ConsentModal onComplete={onComplete} />);

    await waitFor(() => {
      expect(screen.getByText("Risk Disclaimer")).toBeInTheDocument();
    });

    await user.click(
      screen.getByRole("checkbox", { name: /i understand and accept the terms/i })
    );
    await user.click(
      screen.getByRole("button", { name: /i understand and accept/i })
    );

    await waitFor(() => {
      expect(screen.getByText("Terms of Service")).toBeInTheDocument();
    });
    expect(screen.getByText("2 / 2")).toBeInTheDocument();

    await user.click(
      screen.getByRole("checkbox", { name: /i understand and accept the terms/i })
    );
    await user.click(
      screen.getByRole("button", { name: /i understand and accept/i })
    );

    await waitFor(() => {
      expect(onComplete).toHaveBeenCalled();
    });
    expect(acceptCallCount).toBe(2);
  });

  it("has proper ARIA attributes for accessibility", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation((url) => {
      if (url.includes("/legal/documents")) {
        return Promise.resolve({
          ok: true,
          json: () => Promise.resolve(MOCK_DOCUMENTS),
        });
      }
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve([]),
      });
    });

    renderWithProviders(<ConsentModal onComplete={vi.fn()} />);

    await waitFor(() => {
      expect(screen.getByText("Risk Disclaimer")).toBeInTheDocument();
    });

    const dialog = screen.getByRole("dialog");
    expect(dialog).toHaveAttribute("aria-modal", "true");
    expect(dialog).toHaveAttribute("aria-label", "Legal consent: Risk Disclaimer");
  });
});
