import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiClient } from "@/api/client";
import { killSwitchStateSchema, type BrakeMode, type BrakeScopeKind, type KillSwitchState } from "@/api/schemas";

export function useKillSwitch(businessId: string) {
  return useQuery<KillSwitchState>({
    queryKey: ["kill-switch", businessId],
    queryFn: () => apiClient.get("/kill-switch", killSwitchStateSchema, { business_id: businessId }),
    enabled: Boolean(businessId),
    refetchInterval: 60_000,
  });
}

export interface SetKillSwitchInput {
  scope_kind: BrakeScopeKind;
  scope_id: string | null;
  mode: BrakeMode;
  engaged: boolean;
  reason: string;
  typed_confirmation?: string;
}

/**
 * Los frenos son acumulables por ámbito (rest-api.md §Ejecución, deshacer y freno): esta
 * mutación engancha o suelta UN brake concreto (scope_kind/scope_id/mode), nunca "el freno"
 * en singular. Surte efecto sin esperar al ciclo.
 */
export function useSetKillSwitch(businessId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: SetKillSwitchInput) => apiClient.post("/kill-switch", killSwitchStateSchema, input, { business_id: businessId }),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["kill-switch", businessId] }),
  });
}
