import { useState } from "react";
import { Modal } from "../feedback/Modal";
import { ProgressBar } from "./ProgressBar";
import { SkipForward } from "lucide-react";
import clsx from "clsx";

const STEPS_LABELS = ["Welcome", "Setup", "Tour"];

const EXPERIENCE_LEVELS = [
  { value: "beginner", label: "Beginner", desc: "New to algorithmic trading" },
  { value: "intermediate", label: "Intermediate", desc: "Some experience with automated strategies" },
  { value: "advanced", label: "Advanced", desc: "Experienced quant or developer" },
];

const MARKETS = [
  { value: "equities", label: "Equities" },
  { value: "crypto", label: "Crypto" },
  { value: "forex", label: "Forex" },
  { value: "futures", label: "Futures" },
  { value: "options", label: "Options" },
];

const RISK_PROFILES = [
  { value: "conservative", label: "Conservative", desc: "Capital preservation first" },
  { value: "moderate", label: "Moderate", desc: "Balanced risk and reward" },
  { value: "aggressive", label: "Aggressive", desc: "Maximize returns, accept higher drawdowns" },
];

export function SetupWizard({ open, onComplete, onSkip, currentStepIndex, onSaveData }) {
  const [wizardStep, setWizardStep] = useState(0);
  const [experience, setExperience] = useState("");
  const [markets, setMarkets] = useState([]);
  const [riskTolerance, setRiskTolerance] = useState("");

  const wizardSteps = [
    { key: "experience", label: "EXPERIENCE" },
    { key: "markets", label: "MARKETS" },
    { key: "risk", label: "RISK PROFILE" },
  ];

  const canProceed = () => {
    if (wizardStep === 0) return experience !== "";
    if (wizardStep === 1) return markets.length > 0;
    if (wizardStep === 2) return riskTolerance !== "";
    return false;
  };

  const handleNext = () => {
    if (wizardStep < wizardSteps.length - 1) {
      setWizardStep((s) => s + 1);
    } else {
      const data = { experience, markets, riskTolerance };
      if (onSaveData) onSaveData(data);
      onComplete();
    }
  };

  const handleBack = () => {
    if (wizardStep > 0) setWizardStep((s) => s - 1);
  };

  const toggleMarket = (value) => {
    setMarkets((prev) =>
      prev.includes(value) ? prev.filter((m) => m !== value) : [...prev, value]
    );
  };

  return (
    <Modal open={open} title="Quick Setup" maxWidth="max-w-lg">
      <div className="mb-lg">
        <span className="text-label font-mono uppercase text-nx-text-secondary block mb-xs">
          STEP {wizardStep + 1} OF {wizardSteps.length}
        </span>
        <h2 className="text-subheading font-display text-nx-text-display mb-lg">
          {wizardSteps[wizardStep].label}
        </h2>
        <ProgressBar
          steps={STEPS_LABELS}
          currentStepIndex={currentStepIndex}
          className="mb-lg"
        />
      </div>

      {wizardStep === 0 && (
        <div className="flex flex-col gap-sm mb-xl">
          {EXPERIENCE_LEVELS.map((level) => (
            <button
              key={level.value}
              type="button"
              onClick={() => setExperience(level.value)}
              className={clsx(
                "w-full text-left p-lg rounded-lg border transition-colors",
                experience === level.value
                  ? "bg-nx-accent-subtle border-nx-accent text-nx-text-display"
                  : "bg-nx-surface border-nx-border hover:border-nx-border-visible text-nx-text-primary"
              )}
            >
              <span className="text-body font-body block">{level.label}</span>
              <span className="text-caption font-mono text-nx-text-secondary">{level.desc}</span>
            </button>
          ))}
        </div>
      )}

      {wizardStep === 1 && (
        <div className="flex flex-wrap gap-sm mb-xl">
          {MARKETS.map((market) => (
            <button
              key={market.value}
              type="button"
              onClick={() => toggleMarket(market.value)}
              className={clsx(
                "px-lg py-md rounded-full border text-label font-mono uppercase transition-colors",
                markets.includes(market.value)
                  ? "bg-nx-text-display text-nx-black border-nx-text-display"
                  : "bg-transparent text-nx-text-secondary border-nx-border hover:border-nx-border-visible"
              )}
            >
              {market.label}
            </button>
          ))}
        </div>
      )}

      {wizardStep === 2 && (
        <div className="flex flex-col gap-sm mb-xl">
          {RISK_PROFILES.map((profile) => (
            <button
              key={profile.value}
              type="button"
              onClick={() => setRiskTolerance(profile.value)}
              className={clsx(
                "w-full text-left p-lg rounded-lg border transition-colors",
                riskTolerance === profile.value
                  ? "bg-nx-accent-subtle border-nx-accent text-nx-text-display"
                  : "bg-nx-surface border-nx-border hover:border-nx-border-visible text-nx-text-primary"
              )}
            >
              <span className="text-body font-body block">{profile.label}</span>
              <span className="text-caption font-mono text-nx-text-secondary">{profile.desc}</span>
            </button>
          ))}
        </div>
      )}

      <div className="flex items-center justify-between">
        <div className="flex gap-sm">
          {wizardStep > 0 && (
            <button
              type="button"
              onClick={handleBack}
              className="nx-btn-secondary"
            >
              BACK
            </button>
          )}
          <button
            type="button"
            onClick={onSkip}
            className="nx-btn-ghost flex items-center gap-xs"
          >
            <SkipForward size={14} strokeWidth={1.5} />
            SKIP
          </button>
        </div>
        <button
          type="button"
          disabled={!canProceed()}
          onClick={handleNext}
          className={clsx(
            "nx-btn-primary",
            !canProceed() && "opacity-50 cursor-not-allowed"
          )}
        >
          {wizardStep === wizardSteps.length - 1 ? "FINISH" : "NEXT"}
        </button>
      </div>
    </Modal>
  );
}
