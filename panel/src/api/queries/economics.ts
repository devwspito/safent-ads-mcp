import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiClient } from "@/api/client";
import { z } from "zod";
import {
  conversionsImportResultSchema,
  offeringEconomicsSchema,
  offeringsResponseSchema,
  webhookTokenResponseSchema,
  type OfferingEconomicsInput,
} from "@/api/schemas/economics";

export function useOfferings(businessId: string) {
  return useQuery({
    queryKey: ["offerings", businessId],
    queryFn: () => apiClient.get("/offerings", offeringsResponseSchema, { business_id: businessId }),
    enabled: Boolean(businessId),
  });
}

export function useUpdateOfferingEconomics(businessId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    retry: false,
    mutationFn: (input: { offeringId: string; economics: OfferingEconomicsInput }) =>
      apiClient.put(`/offerings/${input.offeringId}/economics`, offeringEconomicsSchema, input.economics, {
        business_id: businessId,
      }),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["offerings", businessId] }),
  });
}

export interface CreateOfferingInput {
  code: string;
  title: string;
  price_amount: string | null;
  price_currency: string | null;
}

const createdOfferingSchema = z.object({
  offering_id: z.string().uuid(), code: z.string(), title: z.string(),
  price_amount: z.string().nullable(), price_currency: z.string().nullable(), created: z.boolean(),
});

export function useCreateOffering(businessId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    retry: false,
    mutationFn: (input: CreateOfferingInput) => apiClient.post(
      "/offerings", createdOfferingSchema, input, { business_id: businessId },
    ),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["offerings", businessId] }),
  });
}

export function useImportConversions(businessId: string) {
  return useMutation({
    retry: false,
    mutationFn: (file: File) => {
      const formData = new FormData();
      formData.append("file", file);
      return apiClient.postForm("/conversions/import", conversionsImportResultSchema, formData, {
        business_id: businessId,
      });
    },
  });
}

/** Explicit owner confirmation bound to the exact request, never an OTP. */
export function useGenerateWebhookToken(businessId: string) {
  return useMutation({
    retry: false,
    mutationFn: ({ confirmationToken }: { confirmationToken?: string }) =>
      apiClient.post(
        "/conversions/webhook-token",
        webhookTokenResponseSchema,
        undefined,
        { business_id: businessId },
        { ...(confirmationToken ? { "X-Action-Confirmation": confirmationToken } : {}) },
      ),
  });
}
