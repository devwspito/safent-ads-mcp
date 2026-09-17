import { useQuery } from "@tanstack/react-query";
import { apiClient } from "@/api/client";
import { freshnessResponseSchema, type FreshnessResponse } from "@/api/schemas";

export function useFreshness(businessId: string) {
  return useQuery<FreshnessResponse>({
    queryKey: ["freshness", businessId],
    queryFn: () => apiClient.get("/freshness", freshnessResponseSchema, { business_id: businessId }),
    enabled: Boolean(businessId),
    refetchInterval: 60_000,
  });
}
