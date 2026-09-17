import { useQuery } from "@tanstack/react-query";
import { apiClient } from "@/api/client";
import { decisionLogResponseSchema, decisionLogVerifySchema, type DecisionLogVerify } from "@/api/schemas/decisionLog";

export interface DecisionLogFilters {
  business_id: string;
  since?: string;
  until?: string;
  event_type?: string;
  entity_ref?: string;
}

export function useDecisionLog(filters: DecisionLogFilters) {
  return useQuery({
    queryKey: ["decision-log", filters.business_id, filters.since, filters.until, filters.event_type, filters.entity_ref],
    queryFn: () =>
      apiClient.get("/decision-log", decisionLogResponseSchema, {
        business_id: filters.business_id,
        since: filters.since,
        until: filters.until,
        event_type: filters.event_type,
        entity_ref: filters.entity_ref,
        limit: 100,
      }),
    enabled: Boolean(filters.business_id),
    placeholderData: (previous) => previous,
  });
}

export function useDecisionLogVerify(businessId: string) {
  return useQuery<DecisionLogVerify>({
    queryKey: ["decision-log-verify", businessId],
    queryFn: () => apiClient.get("/decision-log/verify", decisionLogVerifySchema),
    enabled: Boolean(businessId),
    refetchInterval: 60_000,
  });
}
