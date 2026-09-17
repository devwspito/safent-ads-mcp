import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiClient, voidResponseSchema } from "@/api/client";
import { cloudflareConnectionStatusSchema, type ConnectCloudflareTokenInput } from "@/api/schemas/cloudflare";

const CLOUDFLARE_CONNECTION_KEY = ["integrations", "cloudflare"];
const DISCONNECT_CONFIRMATION_PHRASE = "DESCONECTAR";

export function useCloudflareConnection() {
  return useQuery({
    queryKey: CLOUDFLARE_CONNECTION_KEY,
    queryFn: () => apiClient.get("/integrations/cloudflare", cloudflareConnectionStatusSchema),
  });
}

/** Sin `useConfirmedMutation`: el backend valida contra Cloudflare y guarda en el mismo
 * paso, nunca pide un segundo tecleo de reautenticación (a diferencia de las credenciales
 * de VENDOR de Google/Meta). */
export function useConnectCloudflareToken() {
  const queryClient = useQueryClient();
  return useMutation({
    retry: false,
    mutationFn: (input: ConnectCloudflareTokenInput) =>
      apiClient.post("/integrations/cloudflare/token", cloudflareConnectionStatusSchema, input),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: CLOUDFLARE_CONNECTION_KEY }),
  });
}

export function useDisconnectCloudflareToken() {
  const queryClient = useQueryClient();
  return useMutation({
    retry: false,
    mutationFn: () =>
      apiClient.delete("/integrations/cloudflare/token", voidResponseSchema, {
        typed_confirmation: DISCONNECT_CONFIRMATION_PHRASE,
      }),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: CLOUDFLARE_CONNECTION_KEY }),
  });
}
