import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiClient, voidResponseSchema } from "@/api/client";
import type { Platform } from "@/api/schemas";
import {
  platformAppStatusSchema,
  platformAppsResponseSchema,
  type SetGoogleAppCredentialsInput,
  type SetMetaAppCredentialsInput,
} from "@/api/schemas/platformApps";

const PLATFORM_APPS_KEY = ["platform-apps"];

export function usePlatformApps() {
  return useQuery({
    queryKey: PLATFORM_APPS_KEY,
    queryFn: () => apiClient.get("/platform-apps", platformAppsResponseSchema),
  });
}

interface SetPlatformAppCredentialsArgs {
  confirmationToken?: string;
}

/** Explicit owner confirmation bound to the exact request, never an OTP. */
export function useSetGoogleAppCredentials() {
  const queryClient = useQueryClient();
  return useMutation({
    retry: false,
    mutationFn: ({ confirmationToken, ...body }: SetGoogleAppCredentialsInput & SetPlatformAppCredentialsArgs) =>
      apiClient.put("/platform-apps/google", platformAppStatusSchema, body, undefined, {
        ...(confirmationToken ? { "X-Action-Confirmation": confirmationToken } : {}),
      }),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: PLATFORM_APPS_KEY }),
  });
}

export function useSetMetaAppCredentials() {
  const queryClient = useQueryClient();
  return useMutation({
    retry: false,
    mutationFn: ({ confirmationToken, ...body }: SetMetaAppCredentialsInput & SetPlatformAppCredentialsArgs) =>
      apiClient.put("/platform-apps/meta", platformAppStatusSchema, body, undefined, {
        ...(confirmationToken ? { "X-Action-Confirmation": confirmationToken } : {}),
      }),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: PLATFORM_APPS_KEY }),
  });
}

export function useDeletePlatformAppCredentials() {
  const queryClient = useQueryClient();
  return useMutation({
    retry: false,
    mutationFn: (platform: Platform) =>
      apiClient.delete(`/platform-apps/${platform}`, voidResponseSchema, {
        typed_confirmation: "ELIMINAR",
      }),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: PLATFORM_APPS_KEY }),
  });
}
