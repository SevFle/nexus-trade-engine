import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { SetupWizard } from "../../components/onboarding/SetupWizard";

describe("SetupWizard", () => {
  it("renders when open", () => {
    render(
      <SetupWizard
        open
        onComplete={vi.fn()}
        onSkip={vi.fn()}
        currentStepIndex={1}
      />
    );
    expect(screen.getByText("Quick Setup")).toBeInTheDocument();
  });

  it("shows experience levels on first step", () => {
    render(
      <SetupWizard
        open
        onComplete={vi.fn()}
        onSkip={vi.fn()}
        currentStepIndex={1}
      />
    );
    expect(screen.getByText("Beginner")).toBeInTheDocument();
    expect(screen.getByText("Intermediate")).toBeInTheDocument();
    expect(screen.getByText("Advanced")).toBeInTheDocument();
  });

  it("disables next button when nothing is selected", () => {
    render(
      <SetupWizard
        open
        onComplete={vi.fn()}
        onSkip={vi.fn()}
        currentStepIndex={1}
      />
    );
    const nextBtn = screen.getByText("NEXT");
    expect(nextBtn).toBeDisabled();
  });

  it("enables next button after selecting experience", async () => {
    render(
      <SetupWizard
        open
        onComplete={vi.fn()}
        onSkip={vi.fn()}
        currentStepIndex={1}
      />
    );
    await userEvent.click(screen.getByText("Beginner"));
    expect(screen.getByText("NEXT")).not.toBeDisabled();
  });

  it("advances to markets step after selecting experience", async () => {
    render(
      <SetupWizard
        open
        onComplete={vi.fn()}
        onSkip={vi.fn()}
        currentStepIndex={1}
      />
    );
    await userEvent.click(screen.getByText("Intermediate"));
    await userEvent.click(screen.getByText("NEXT"));
    expect(screen.getByText("Equities")).toBeInTheDocument();
    expect(screen.getByText("Crypto")).toBeInTheDocument();
  });

  it("allows multiple market selection", async () => {
    render(
      <SetupWizard
        open
        onComplete={vi.fn()}
        onSkip={vi.fn()}
        currentStepIndex={1}
      />
    );
    await userEvent.click(screen.getByText("Beginner"));
    await userEvent.click(screen.getByText("NEXT"));
    await userEvent.click(screen.getByText("Equities"));
    await userEvent.click(screen.getByText("Crypto"));
    expect(screen.getByText("NEXT")).not.toBeDisabled();
  });

  it("advances to risk profile step", async () => {
    render(
      <SetupWizard
        open
        onComplete={vi.fn()}
        onSkip={vi.fn()}
        currentStepIndex={1}
      />
    );
    await userEvent.click(screen.getByText("Beginner"));
    await userEvent.click(screen.getByText("NEXT"));
    await userEvent.click(screen.getByText("Equities"));
    await userEvent.click(screen.getByText("NEXT"));
    expect(screen.getByText("Conservative")).toBeInTheDocument();
    expect(screen.getByText("Moderate")).toBeInTheDocument();
    expect(screen.getByText("Aggressive")).toBeInTheDocument();
  });

  it("shows finish button on last step", async () => {
    render(
      <SetupWizard
        open
        onComplete={vi.fn()}
        onSkip={vi.fn()}
        currentStepIndex={1}
      />
    );
    await userEvent.click(screen.getByText("Beginner"));
    await userEvent.click(screen.getByText("NEXT"));
    await userEvent.click(screen.getByText("Equities"));
    await userEvent.click(screen.getByText("NEXT"));
    expect(screen.getByText("FINISH")).toBeInTheDocument();
  });

  it("calls onComplete with data after finishing all steps", async () => {
    const onComplete = vi.fn();
    const onSaveData = vi.fn();
    render(
      <SetupWizard
        open
        onComplete={onComplete}
        onSkip={vi.fn()}
        currentStepIndex={1}
        onSaveData={onSaveData}
      />
    );
    await userEvent.click(screen.getByText("Advanced"));
    await userEvent.click(screen.getByText("NEXT"));
    await userEvent.click(screen.getByText("Crypto"));
    await userEvent.click(screen.getByText("NEXT"));
    await userEvent.click(screen.getByText("Aggressive"));
    await userEvent.click(screen.getByText("FINISH"));
    expect(onComplete).toHaveBeenCalledOnce();
    expect(onSaveData).toHaveBeenCalledWith({
      experience: "advanced",
      markets: ["crypto"],
      riskTolerance: "aggressive",
    });
  });

  it("calls onSkip when skip is clicked", async () => {
    const onSkip = vi.fn();
    render(
      <SetupWizard
        open
        onComplete={vi.fn()}
        onSkip={onSkip}
        currentStepIndex={1}
      />
    );
    await userEvent.click(screen.getByText("SKIP"));
    expect(onSkip).toHaveBeenCalledOnce();
  });

  it("shows back button on steps after the first", async () => {
    render(
      <SetupWizard
        open
        onComplete={vi.fn()}
        onSkip={vi.fn()}
        currentStepIndex={1}
      />
    );
    await userEvent.click(screen.getByText("Beginner"));
    await userEvent.click(screen.getByText("NEXT"));
    expect(screen.getByText("BACK")).toBeInTheDocument();
  });

  it("goes back to previous step", async () => {
    render(
      <SetupWizard
        open
        onComplete={vi.fn()}
        onSkip={vi.fn()}
        currentStepIndex={1}
      />
    );
    await userEvent.click(screen.getByText("Beginner"));
    await userEvent.click(screen.getByText("NEXT"));
    await userEvent.click(screen.getByText("BACK"));
    expect(screen.getByText("Beginner")).toBeInTheDocument();
  });
});
