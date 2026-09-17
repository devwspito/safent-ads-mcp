import { useQuery } from "@tanstack/react-query";
import { apiClient } from "@/api/client";
import { cockpitViewSchema, type CockpitView, type CockpitWindow } from "@/api/schemas/cockpit";

/**
 * `window="today"` es la única fuente de gasto y leads de HOY por campaña (Campañas,
 * design.md §4): `/portfolio` sólo da ventanas de 7/14/30 días. Resultados usa `/portfolio` en
 * su lugar, que sí trae el selector 7/14/30 y la serie de 14 días para la tendencia (§5).
 */
export function useCockpit(businessId: string, window: CockpitWindow) {
  return useQuery<CockpitView>({
    queryKey: ["cockpit", businessId, window],
    queryFn: () => apiClient.get("/cockpit", cockpitViewSchema, { business_id: businessId, window }),
    enabled: Boolean(businessId),
    refetchInterval: 60_000,
    placeholderData: (previous, query) => query?.queryKey[1] === businessId ? previous : undefined,
  });
}
