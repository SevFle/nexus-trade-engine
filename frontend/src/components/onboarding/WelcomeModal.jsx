import { Modal } from "../feedback/Modal";
import { ProgressBar } from "./ProgressBar";
import { Rocket, SkipForward } from "lucide-react";

const STEPS_LABELS = ["Welcome", "Setup", "Tour"];

export function WelcomeModal({ open, onContinue, onSkip, currentStepIndex }) {
  return (
    <Modal open={open} title="" maxWidth="max-w-lg">
      <div className="flex flex-col items-center text-center">
        <div className="w-16 h-16 rounded-full bg-nx-accent-subtle flex items-center justify-center mb-xl">
          <Rocket size={28} className="text-nx-accent" strokeWidth={1.5} />
        </div>

        <h1 className="text-display-md font-display text-nx-text-display mb-sm">
          WELCOME TO NEXUS
        </h1>
        <p className="text-body font-body text-nx-text-secondary mb-xl max-w-md">
          Your algorithmic trading command center. Let us show you around so
          you can start backtesting and running strategies in minutes.
        </p>

        <div className="w-full mb-xl">
          <ProgressBar
            steps={STEPS_LABELS}
            currentStepIndex={currentStepIndex}
          />
        </div>

        <div className="flex flex-col gap-sm w-full">
          <button
            type="button"
            onClick={onContinue}
            className="nx-btn-primary w-full"
          >
            GET STARTED
          </button>
          <button
            type="button"
            onClick={onSkip}
            className="nx-btn-ghost w-full flex items-center justify-center gap-xs"
          >
            <SkipForward size={14} strokeWidth={1.5} />
            SKIP TOUR
          </button>
        </div>

        <p className="text-caption font-mono text-nx-text-disabled mt-lg">
          You can restart the tour anytime from Settings
        </p>
      </div>
    </Modal>
  );
}
