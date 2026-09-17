import { useMutation, useQueryClient } from "@tanstack/react-query";
import { apiClient } from "@/api/client";
import { deleteEntityResponseSchema, pauseResumeResponseSchema } from "@/api/schemas/entityLifecycle";

function invalidateEntityQueries(queryClient: ReturnType<typeof useQueryClient>, businessId: string) {
  void queryClient.invalidateQueries({ queryKey: ["cockpit", businessId] });
  void queryClient.invalidateQueries({ queryKey: ["portfolio", businessId] });
  void queryClient.invalidateQueries({ queryKey: ["entity-children"] });
  void queryClient.invalidateQueries({ queryKey: ["decision-log", businessId] });
}

/** El clic del propietario ES la autorización — sin propuesta intermedia (design.md §4.2/§7.2). */
export function usePauseEntity(businessId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (entityRef: string) => apiClient.post(`/entities/${encodeURIComponent(entityRef)}/pause`, pauseResumeResponseSchema),
    onSuccess: () => invalidateEntityQueries(queryClient, businessId),
  });
}

export function useResumeEntity(businessId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (entityRef: string) => apiClient.post(`/entities/${encodeURIComponent(entityRef)}/resume`, pauseResumeResponseSchema),
    onSuccess: () => invalidateEntityQueries(queryClient, businessId),
  });
}

/** Irreversible en la plataforma: nunca tiene ventana de deshacer (design.md §7.3). */
export function useDeleteEntity(businessId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (entityRef: string) => apiClient.post(`/entities/${encodeURIComponent(entityRef)}/delete`, deleteEntityResponseSchema),
    onSuccess: () => invalidateEntityQueries(queryClient, businessId),
  });
}
