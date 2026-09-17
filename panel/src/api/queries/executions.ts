import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiClient } from "@/api/client";
import { batchUndoResponseSchema, executionSchema, executionsResponseSchema, undoResponseSchema, type Execution, type ExecutionsResponse } from "@/api/schemas/executions";
import { useMe } from "./auth";
import { useBusinessFilter } from "@/hooks/useBusinessFilter";

/**
 * 10 s solo mientras hay algo en vuelo (esta consulta ya filtra `outcome: "UNKNOWN"` en el
 * servidor, así que "en vuelo" es sencillamente "la lista no está vacía"); en reposo, 60 s
 * — medido en producción (16-sep): cada petición cuesta ~0,2-0,3 s, sondear cada 10 s sin
 * nada pendiente es coste sin ninguna decisión que tomar más rápido.
 */
export function unconfirmedExecutionsInterval(data: ExecutionsResponse | undefined): number {
  return (data?.items.length ?? 0) > 0 ? 10_000 : 60_000;
}

export function useUnconfirmedExecutions(businessId: string) {
  return useQuery({
    queryKey: ["executions-unconfirmed", businessId],
    queryFn: () => apiClient.get("/executions", executionsResponseSchema, { business_id: businessId, outcome: "UNKNOWN", limit: 200 }),
    enabled: Boolean(businessId),
    refetchInterval: (query) => unconfirmedExecutionsInterval(query.state.data),
  });
}

export function useExecution(executionId: string, proposalId: string) {
  const { data: me } = useMe();
  const { businessId } = useBusinessFilter(me?.businesses);
  return useQuery<Execution>({
    queryKey: ["execution", businessId, proposalId, executionId],
    queryFn: async () => {
      const execution = await apiClient.get(`/executions/${encodeURIComponent(executionId)}`, executionSchema);
      if (execution.execution_id !== executionId || execution.proposal_id !== proposalId) {
        throw new Error("La ejecución recibida no corresponde a esta propuesta.");
      }
      return execution;
    },
    enabled: Boolean(executionId && businessId),
    // Only read the persisted result. A refresh must never repeat the remote write.
    refetchInterval: (query) => {
      if (query.state.status === "error") return false;
      const outcome = query.state.data?.outcome;
      return outcome && ["CLAIMED", "RUNNING", "UNKNOWN"].includes(outcome) ? 10_000 : false;
    },
  });
}

export function useUndoExecution(businessId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: { executionId: string; reason: string }) =>
      apiClient.post(`/executions/${input.executionId}/undo`, undoResponseSchema, { reason: input.reason }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["proposals", businessId] });
      void queryClient.invalidateQueries({ queryKey: ["decision-log", businessId] });
    },
  });
}

/** Deshacer un lote (≤25) — un solo asidero para las 45 s de gracia del lote (rest-api.md §Ejecución). */
export function useUndoExecutionsBatch(businessId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: { executionIds: string[]; reason: string }) =>
      apiClient.post("/executions/undo", batchUndoResponseSchema, { execution_ids: input.executionIds, reason: input.reason }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["proposals", businessId] });
      void queryClient.invalidateQueries({ queryKey: ["decision-log", businessId] });
    },
  });
}
