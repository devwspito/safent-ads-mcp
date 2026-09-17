import { useQuery } from "@tanstack/react-query";
import { apiClient } from "@/api/client";
import { entityChildrenResponseSchema, type EntityChildrenResponse } from "@/api/schemas";

export function useEntityChildren(entityRef: string | null) {
  return useQuery<EntityChildrenResponse>({
    queryKey: ["entity-children", entityRef],
    queryFn: () => apiClient.get(`/entities/${encodeURIComponent(entityRef ?? "")}/children`, entityChildrenResponseSchema),
    enabled: Boolean(entityRef),
    placeholderData: (previous) => previous,
  });
}
