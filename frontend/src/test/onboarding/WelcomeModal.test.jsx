import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { WelcomeModal } from "../../components/onboarding/WelcomeModal";

describe("WelcomeModal", () => {
  it("renders when open", () => {
    render(
      <WelcomeModal
        open
        onContinue={vi.fn()}
        onSkip={vi.fn()}
        currentStepIndex={0}
      />
    );
    expect(screen.getByText("WELCOME TO NEXUS")).toBeInTheDocument();
  });

  it("does not render when closed", () => {
    render(
      <WelcomeModal
        open={false}
        onContinue={vi.fn()}
        onSkip={vi.fn()}
        currentStepIndex={0}
      />
    );
    expect(screen.queryByText("WELCOME TO NEXUS")).not.toBeInTheDocument();
  });

  it("renders get started button", () => {
    render(
      <WelcomeModal
        open
        onContinue={vi.fn()}
        onSkip={vi.fn()}
        currentStepIndex={0}
      />
    );
    expect(screen.getByText("GET STARTED")).toBeInTheDocument();
  });

  it("renders skip tour button", () => {
    render(
      <WelcomeModal
        open
        onContinue={vi.fn()}
        onSkip={vi.fn()}
        currentStepIndex={0}
      />
    );
    expect(screen.getByText("SKIP TOUR")).toBeInTheDocument();
  });

  it("calls onContinue when get started is clicked", async () => {
    const onContinue = vi.fn();
    render(
      <WelcomeModal
        open
        onContinue={onContinue}
        onSkip={vi.fn()}
        currentStepIndex={0}
      />
    );
    await userEvent.click(screen.getByText("GET STARTED"));
    expect(onContinue).toHaveBeenCalledOnce();
  });

  it("calls onSkip when skip tour is clicked", async () => {
    const onSkip = vi.fn();
    render(
      <WelcomeModal
        open
        onContinue={vi.fn()}
        onSkip={onSkip}
        currentStepIndex={0}
      />
    );
    await userEvent.click(screen.getByText("SKIP TOUR"));
    expect(onSkip).toHaveBeenCalledOnce();
  });

  it("shows restart info text", () => {
    render(
      <WelcomeModal
        open
        onContinue={vi.fn()}
        onSkip={vi.fn()}
        currentStepIndex={0}
      />
    );
    expect(
      screen.getByText(/restart the tour anytime from Settings/i)
    ).toBeInTheDocument();
  });
});
