import { describe, it, expect, beforeEach } from "vitest";
import { renderHook, act } from "@testing-library/react";
import { useOnboarding } from "../../hooks/useOnboarding";

describe("useOnboarding", () => {
  beforeEach(() => {
    localStorage.clear();
  });

  it("starts with welcome as current step", () => {
    const { result } = renderHook(() => useOnboarding());
    expect(result.current.currentStep).toBe("welcome");
  });

  it("starts with 0 progress", () => {
    const { result } = renderHook(() => useOnboarding());
    expect(result.current.progress).toBe(0);
  });

  it("is needed when not all steps are complete", () => {
    const { result } = renderHook(() => useOnboarding());
    expect(result.current.isNeeded).toBe(true);
  });

  it("completes a step", () => {
    const { result } = renderHook(() => useOnboarding());
    act(() => {
      result.current.completeStep("welcome");
    });
    expect(result.current.isComplete("welcome")).toBe(true);
    expect(result.current.currentStep).toBe("setup");
  });

  it("tracks progress through steps", () => {
    const { result } = renderHook(() => useOnboarding());
    act(() => {
      result.current.completeStep("welcome");
    });
    expect(result.current.progress).toBeCloseTo(1 / 3);
    act(() => {
      result.current.completeStep("setup");
    });
    expect(result.current.progress).toBeCloseTo(2 / 3);
  });

  it("marks all complete after finishing all steps", () => {
    const { result } = renderHook(() => useOnboarding());
    act(() => {
      result.current.completeStep("welcome");
      result.current.completeStep("setup");
      result.current.completeStep("tour");
    });
    expect(result.current.allComplete).toBe(true);
    expect(result.current.isNeeded).toBe(false);
    expect(result.current.currentStep).toBeNull();
  });

  it("sets isSkipped when skip is called", () => {
    const { result } = renderHook(() => useOnboarding());
    act(() => {
      result.current.skip();
    });
    expect(result.current.isSkipped).toBe(true);
    expect(result.current.isNeeded).toBe(false);
  });

  it("resets state", () => {
    const { result } = renderHook(() => useOnboarding());
    act(() => {
      result.current.completeStep("welcome");
      result.current.skip();
    });
    expect(result.current.isSkipped).toBe(true);
    act(() => {
      result.current.reset();
    });
    expect(result.current.isSkipped).toBe(false);
    expect(result.current.currentStep).toBe("welcome");
  });

  it("saves setup data", () => {
    const { result } = renderHook(() => useOnboarding());
    act(() => {
      result.current.setSetupData({
        experience: "advanced",
        markets: ["crypto"],
        riskTolerance: "aggressive",
      });
    });
    expect(result.current.setupData).toEqual({
      experience: "advanced",
      markets: ["crypto"],
      riskTolerance: "aggressive",
    });
  });

  it("persists state to localStorage", () => {
    const { result } = renderHook(() => useOnboarding());
    act(() => {
      result.current.completeStep("welcome");
    });
    const stored = JSON.parse(localStorage.getItem("nexus-onboarding"));
    expect(stored.completed).toContain("welcome");
  });

  it("restores state from localStorage", () => {
    localStorage.setItem(
      "nexus-onboarding",
      JSON.stringify({ completed: ["welcome", "setup"], skipped: false, setupData: null })
    );
    const { result } = renderHook(() => useOnboarding());
    expect(result.current.currentStep).toBe("tour");
    expect(result.current.progress).toBeCloseTo(2 / 3);
  });

  it("ignores duplicate completeStep calls", () => {
    const { result } = renderHook(() => useOnboarding());
    act(() => {
      result.current.completeStep("welcome");
      result.current.completeStep("welcome");
    });
    expect(result.current.isComplete("welcome")).toBe(true);
  });

  it("returns correct steps", () => {
    const { result } = renderHook(() => useOnboarding());
    expect(result.current.steps).toEqual(["welcome", "setup", "tour"]);
  });
});
