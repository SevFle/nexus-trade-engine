import { useState, useCallback, useEffect } from "react";

const STORAGE_KEY = "nexus-onboarding";

const STEPS = ["welcome", "setup", "tour"];

const defaultState = {
  completed: [],
  skipped: false,
  setupData: null,
};

function loadState() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return { ...defaultState };
    return { ...defaultState, ...JSON.parse(raw) };
  } catch {
    return { ...defaultState };
  }
}

function saveState(state) {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
}

export function useOnboarding() {
  const [state, setState] = useState(loadState);

  useEffect(() => {
    saveState(state);
  }, [state]);

  const isComplete = (step) => state.completed.includes(step);
  const allComplete = STEPS.every((s) => state.completed.includes(s));
  const isSkipped = state.skipped;
  const isNeeded = !allComplete && !state.skipped;

  const currentStep = STEPS.find((s) => !state.completed.includes(s)) || null;
  const currentStepIndex = currentStep ? STEPS.indexOf(currentStep) : STEPS.length;
  const progress = STEPS.length > 0 ? currentStepIndex / STEPS.length : 1;

  const completeStep = useCallback((step) => {
    setState((prev) => {
      if (prev.completed.includes(step)) return prev;
      return { ...prev, completed: [...prev.completed, step] };
    });
  }, []);

  const skip = useCallback(() => {
    setState((prev) => ({ ...prev, skipped: true }));
  }, []);

  const reset = useCallback(() => {
    setState({ ...defaultState });
  }, []);

  const setSetupData = useCallback((data) => {
    setState((prev) => ({ ...prev, setupData: data }));
  }, []);

  return {
    steps: STEPS,
    currentStep,
    currentStepIndex,
    progress,
    isComplete,
    allComplete,
    isSkipped,
    isNeeded,
    completeStep,
    skip,
    reset,
    setupData: state.setupData,
    setSetupData,
  };
}
