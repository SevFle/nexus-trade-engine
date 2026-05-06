import { useOnboarding } from "../../hooks/useOnboarding";
import { WelcomeModal } from "./WelcomeModal";
import { SetupWizard } from "./SetupWizard";
import { FeatureTour } from "./FeatureTour";

export function OnboardingManager() {
  const {
    currentStep,
    currentStepIndex,
    isNeeded,
    completeStep,
    skip,
    setSetupData,
  } = useOnboarding();

  if (!isNeeded) return null;

  if (currentStep === "welcome") {
    return (
      <WelcomeModal
        open
        onContinue={() => completeStep("welcome")}
        onSkip={skip}
        currentStepIndex={currentStepIndex}
      />
    );
  }

  if (currentStep === "setup") {
    return (
      <SetupWizard
        open
        onComplete={() => completeStep("setup")}
        onSkip={skip}
        currentStepIndex={currentStepIndex}
        onSaveData={setSetupData}
      />
    );
  }

  if (currentStep === "tour") {
    return (
      <FeatureTour
        open
        onComplete={() => completeStep("tour")}
        onSkip={skip}
        currentStepIndex={currentStepIndex}
      />
    );
  }

  return null;
}
