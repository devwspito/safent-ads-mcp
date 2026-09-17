import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiClient } from "@/api/client";
import { settingsSchema, type Settings, type SettingsUpdate, type Theme } from "@/api/schemas/settings";

export function useSettings(businessId: string) {
  return useQuery<Settings>({
    queryKey: ["settings", businessId],
    queryFn: () => apiClient.get("/settings", settingsSchema, { business_id: businessId }),
    enabled: Boolean(businessId),
  });
}

/** `timezone`/`currency` los dicta la plataforma (rest-api.md): no forman parte del cuerpo editable. */
export function useUpdateSettings(businessId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (update: SettingsUpdate) => apiClient.put("/settings", settingsSchema, { business_id: businessId, ...update }),
    onSuccess: (data) => queryClient.setQueryData(["settings", businessId], data),
  });
}

export type { Theme };
