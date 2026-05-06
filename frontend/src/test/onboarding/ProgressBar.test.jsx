import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { ProgressBar } from "../../components/onboarding/ProgressBar";

describe("ProgressBar", () => {
  it("renders all step labels", () => {
    render(
      <ProgressBar
        steps={["Welcome", "Setup", "Tour"]}
        currentStepIndex={0}
      />
    );
    expect(screen.getByText("Welcome", { hidden: true })).toBeInTheDocument();
    expect(screen.getByText("Setup", { hidden: true })).toBeInTheDocument();
    expect(screen.getByText("Tour", { hidden: true })).toBeInTheDocument();
  });

  it("shows check marks for completed steps", () => {
    render(
      <ProgressBar
        steps={["Welcome", "Setup", "Tour"]}
        currentStepIndex={2}
      />
    );
    const checks = screen.getAllByText("\u2713");
    expect(checks.length).toBe(2);
  });

  it("shows percentage at 33%", () => {
    render(
      <ProgressBar
        steps={["Welcome", "Setup", "Tour"]}
        currentStepIndex={1}
      />
    );
    expect(screen.getByText("33%")).toBeInTheDocument();
  });

  it("shows 0% when at start", () => {
    render(
      <ProgressBar
        steps={["Welcome", "Setup", "Tour"]}
        currentStepIndex={0}
      />
    );
    expect(screen.getByText("0%")).toBeInTheDocument();
  });

  it("shows 100% when complete", () => {
    render(
      <ProgressBar
        steps={["Welcome", "Setup", "Tour"]}
        currentStepIndex={3}
      />
    );
    expect(screen.getByText("100%")).toBeInTheDocument();
  });
});
