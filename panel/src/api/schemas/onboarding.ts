/** `contracts/rest-api.md` §Onboarding: `GET /api/v1/onboarding`. */
import { z } from "zod";

export const onboardingStepIdSchema = z.enum([
  "google_app",
  "google_account",
  "meta_app",
  "meta_account",
]);
export type OnboardingStepId = z.infer<typeof onboardingStepIdSchema>;

export const onboardingStepStatusSchema = z.enum(["done", "pending", "blocked"]);
export type OnboardingStepStatus = z.infer<typeof onboardingStepStatusSchema>;

export const onboardingStepSchema = z.object({
  id: onboardingStepIdSchema,
  status: onboardingStepStatusSchema,
  blocking_reason: z.string().nullable(),
});
export type OnboardingStep = z.infer<typeof onboardingStepSchema>;

export const onboardingStatusSchema = z.object({
  steps: z.array(onboardingStepSchema),
  complete: z.boolean(),
});
export type OnboardingStatus = z.infer<typeof onboardingStatusSchema>;
