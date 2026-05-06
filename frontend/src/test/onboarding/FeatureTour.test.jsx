import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { FeatureTour } from "../../components/onboarding/FeatureTour";

describe("FeatureTour", () => {
  it("renders when open", () => {
    render(
      <FeatureTour
        open
        onComplete={vi.fn()}
        onSkip={vi.fn()}
        currentStepIndex={2}
      />
    );
    expect(screen.getByText(/FEATURE 1 OF 6/)).toBeInTheDocument();
  });

  it("does not render when closed", () => {
    const { container } = render(
      <FeatureTour
        open={false}
        onComplete={vi.fn()}
        onSkip={vi.fn()}
        currentStepIndex={2}
      />
    );
    expect(container.innerHTML).toBe("");
  });

  it("shows first feature title", () => {
    render(
      <FeatureTour
        open
        onComplete={vi.fn()}
        onSkip={vi.fn()}
        currentStepIndex={2}
      />
    );
    expect(screen.getByText("Dashboard")).toBeInTheDocument();
  });

  it("navigates to next feature", async () => {
    render(
      <FeatureTour
        open
        onComplete={vi.fn()}
        onSkip={vi.fn()}
        currentStepIndex={2}
      />
    );
    await userEvent.click(screen.getByText("NEXT"));
    expect(screen.getByText(/FEATURE 2 OF 6/)).toBeInTheDocument();
    expect(screen.getByText("Market Watch")).toBeInTheDocument();
  });

  it("shows back button after first feature", async () => {
    render(
      <FeatureTour
        open
        onComplete={vi.fn()}
        onSkip={vi.fn()}
        currentStepIndex={2}
      />
    );
    await userEvent.click(screen.getByText("NEXT"));
    expect(screen.getByText("BACK")).toBeInTheDocument();
  });

  it("goes back to previous feature", async () => {
    render(
      <FeatureTour
        open
        onComplete={vi.fn()}
        onSkip={vi.fn()}
        currentStepIndex={2}
      />
    );
    await userEvent.click(screen.getByText("NEXT"));
    await userEvent.click(screen.getByText("BACK"));
    expect(screen.getByText(/FEATURE 1 OF 6/)).toBeInTheDocument();
  });

  it("shows finish button on last feature", async () => {
    render(
      <FeatureTour
        open
        onComplete={vi.fn()}
        onSkip={vi.fn()}
        currentStepIndex={2}
      />
    );
    for (let i = 0; i < 5; i++) {
      await userEvent.click(screen.getByText("NEXT"));
    }
    expect(screen.getByText("FINISH")).toBeInTheDocument();
  });

  it("calls onComplete when finish is clicked", async () => {
    const onComplete = vi.fn();
    render(
      <FeatureTour
        open
        onComplete={onComplete}
        onSkip={vi.fn()}
        currentStepIndex={2}
      />
    );
    for (let i = 0; i < 5; i++) {
      await userEvent.click(screen.getByText("NEXT"));
    }
    await userEvent.click(screen.getByText("FINISH"));
    expect(onComplete).toHaveBeenCalledOnce();
  });

  it("calls onSkip when skip is clicked", async () => {
    const onSkip = vi.fn();
    render(
      <FeatureTour
        open
        onComplete={vi.fn()}
        onSkip={onSkip}
        currentStepIndex={2}
      />
    );
    await userEvent.click(screen.getByText("SKIP TOUR"));
    expect(onSkip).toHaveBeenCalledOnce();
  });
});
