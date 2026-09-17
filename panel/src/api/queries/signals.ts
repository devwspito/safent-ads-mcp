import { useQuery } from "@tanstack/react-query";
import { apiClient } from "@/api/client";
import { signalsResponseSchema, type SignalsResponse } from "@/api/schemas";

export interface SignalsFilters {
  business_id: string;
  platform?: string;
  kind?: string;
  min_strength?: number;
  since?: string;
}

export function useSignals(filters: SignalsFilters) {
  return useQuery<SignalsResponse>({
    queryKey: ["signals", filters],
    queryFn: () =>
      apiClient.get("/signals", signalsResponseSchema, {
        business_id: filters.business_id,
        platform: filters.platform,
        kind: filters.kind,
        min_strength: filters.min_strength,
        since: filters.since,
        limit: 50,
      }),
    enabled: Boolean(filters.business_id),
    placeholderData: (previous) => previous,
  });
}
