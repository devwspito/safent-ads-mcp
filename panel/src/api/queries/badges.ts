import { useQuery } from "@tanstack/react-query";
import { apiClient } from "@/api/client";
import { badgesResponseSchema, type BadgesResponse } from "@/api/schemas/badges";

/** Insignias de la barra lateral en una sola consulta — caché 60 s (rest-api.md §Auditoría y salud). */
export function useBadges(businessId: string, signalsSince?: string) {
  return useQuery<BadgesResponse>({
    queryKey: ["badges", businessId, signalsSince],
    queryFn: () => apiClient.get("/badges", badgesResponseSchema, { business_id: businessId, signals_since: signalsSince }),
    enabled: Boolean(businessId),
    refetchInterval: 60_000,
  });
}
