import { z } from "zod";
import { apiClient } from "@/api/client";
import { meResponseSchema } from "@/api/schemas";

export interface InitialBusinessInput {
  name: string;
  timezone: string;
  reference_currency: string;
}

const initialBusinessSchema = z.object({ business_id: z.string().uuid(), slug: z.string(), name: z.string() });

// No mutation retries: a lost response is resolved by reading the authenticated owner first.
export function createInitialBusiness(input: InitialBusinessInput) {
  return apiClient.post("/onboarding/business", initialBusinessSchema, input);
}

export function readInitialBusinessSession() {
  return apiClient.get("/auth/me", meResponseSchema);
}
