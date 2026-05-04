import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

const mockCompleteStep = vi.fn();
const mockSkip = vi.fn();
const mockSetSetupData = vi.fn();
let mockCurrentStep = "welcome";
let mockIsNeeded = true;
let mockCurrentStepIndex = 0;

vi.mock("../../hooks/useOnboarding", () => ({
  useOnboarding: () => ({
    currentStep: mockCurrentStep,
    currentStepIndex: mockCurrentStepIndex,
    isNeeded: mockIsNeeded,
    completeStep: mockCompleteStep,
    skip: mockSkip,
    setSetupData: mockSetSetupData,
  }),
}));

import { OnboardingManager } from "../../components/onboarding/OnboardingManager";

describe("OnboardingManager", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockCurrentStep = "welcome";
    mockIsNeeded = true;
    mockCurrentStepIndex = 0;
  });

  it("renders nothing when onboarding is not needed", () => {
    mockIsNeeded = false;
    const { container } = render(<OnboardingManager />);
    expect(container.innerHTML).toBe("");
  });

  it("renders WelcomeModal when current step is welcome", () => {
    mockCurrentStep = "welcome";
    mockCurrentStepIndex = 0;
    render(<OnboardingManager />);
    expect(screen.getByText("WELCOME TO NEXUS")).toBeInTheDocument();
  });

  it("renders SetupWizard when current step is setup", () => {
    mockCurrentStep = "setup";
    mockCurrentStepIndex = 1;
    render(<OnboardingManager />);
    expect(screen.getByText("Quick Setup")).toBeInTheDocument();
  });

  it("renders FeatureTour when current step is tour", () => {
    mockCurrentStep = "tour";
    mockCurrentStepIndex = 2;
    render(<OnboardingManager />);
    expect(screen.getByText(/FEATURE 1 OF 6/)).toBeInTheDocument();
  });

  it("calls completeStep when welcome continue is clicked", async () => {
    mockCurrentStep = "welcome";
    mockCurrentStepIndex = 0;
    render(<OnboardingManager />);
    await userEvent.click(screen.getByText("GET STARTED"));
    expect(mockCompleteStep).toHaveBeenCalledWith("welcome");
  });

  it("calls skip when skip is clicked from welcome", async () => {
    mockCurrentStep = "welcome";
    mockCurrentStepIndex = 0;
    render(<OnboardingManager />);
    await userEvent.click(screen.getByText("SKIP TOUR"));
    expect(mockSkip).toHaveBeenCalledOnce();
  });

  it("calls skip when skip is clicked from setup", async () => {
    mockCurrentStep = "setup";
    mockCurrentStepIndex = 1;
    render(<OnboardingManager />);
    await userEvent.click(screen.getByText("SKIP"));
    expect(mockSkip).toHaveBeenCalledOnce();
  });

  it("calls skip when skip is clicked from tour", async () => {
    mockCurrentStep = "tour";
    mockCurrentStepIndex = 2;
    render(<OnboardingManager />);
    await userEvent.click(screen.getByText("SKIP TOUR"));
    expect(mockSkip).toHaveBeenCalledOnce();
  });
});
