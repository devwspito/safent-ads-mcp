import { useQuery } from "@tanstack/react-query";
import { apiClient } from "@/api/client";
import { onboardingStatusSchema } from "@/api/schemas/onboarding";

const ONBOARDING_KEY = ["onboarding"];

export function useOnboardingStatus() {
  return useQuery({
    queryKey: ONBOARDING_KEY,
    queryFn: () => apiClient.get("/onboarding", onboardingStatusSchema),
  });
}
