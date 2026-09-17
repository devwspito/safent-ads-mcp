/** `contracts/rest-api.md` §Onboarding. Estado independiente de `platformApps.ts`/
 * `connections.ts` a propósito -- mismo criterio que esos dos, cada fixture manda
 * en su propio recurso; los tests que quieran los tres coherentes los actualizan
 * a la vez con sus respectivos `reset*`/`set*`. */
import type { OnboardingStatus, OnboardingStepId, OnboardingStepStatus } from "@/api/schemas/onboarding";

function defaultState(): OnboardingStatus {
  return {
    steps: [
      { id: "google_app", status: "pending", blocking_reason: null },
      { id: "google_account", status: "blocked", blocking_reason: "google_app_not_configured" },
      { id: "meta_app", status: "pending", blocking_reason: null },
      { id: "meta_account", status: "blocked", blocking_reason: "meta_app_not_configured" },
    ],
    complete: false,
  };
}

let state = defaultState();

export function resetOnboardingFixtures() {
  state = defaultState();
}

export function getOnboardingStatus(): OnboardingStatus {
  return state;
}

export function setOnboardingStep(id: OnboardingStepId, status: OnboardingStepStatus) {
  const steps = state.steps.map((step) =>
    step.id === id
      ? { ...step, status, blocking_reason: status === "blocked" ? step.blocking_reason : null }
      : step,
  );
  const complete = steps.some((step) => step.id.endsWith("_account") && step.status === "done");
  state = { steps, complete };
}
