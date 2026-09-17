import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiClient, voidResponseSchema } from "@/api/client";
import type { Platform } from "@/api/schemas";
import {
  metaSystemUserTokenResponseSchema,
  platformAccountsResponseSchema,
  reconnectStartResponseSchema,
  reconnectStatusResponseSchema,
  telegramPairingSchema,
  telegramPairingStartResponseSchema,
  telegramTestMessageResponseSchema,
} from "@/api/schemas/connections";

function platformAccountsKey(businessId: string) {
  return ["platform-accounts", businessId];
}

export function usePlatformAccounts(businessId: string) {
  return useQuery({
    queryKey: platformAccountsKey(businessId),
    queryFn: () => apiClient.get("/platform-accounts", platformAccountsResponseSchema, { business_id: businessId }),
    enabled: Boolean(businessId),
  });
}

/**
 * "Conectar" es por PLATAFORMA, no por cuenta (rest-api.md §Conexiones): Google/Meta autorizan
 * al propietario y el descubrimiento de cuentas puede devolver varias de una vez.
 */
export function useStartReconnect(businessId: string) {
  return useMutation({
    retry: false,
    mutationFn: ({ provider, googleCustomerId }: { provider: Platform; googleCustomerId?: string }) =>
      apiClient.post(`/platform-accounts/${provider}/reconnect/start`, reconnectStartResponseSchema,
        provider === "google" && googleCustomerId ? { google_customer_id: googleCustomerId } : undefined, {
        business_id: businessId,
      }),
  });
}

export function useReconnectStatus(businessId: string, provider: Platform | null, sessionId: string | null, ownerId: string) {
  return useQuery({
    queryKey: ["reconnect-status", ownerId, businessId, provider, sessionId],
    queryFn: () =>
      apiClient.get(`/platform-accounts/${provider}/reconnect/status`, reconnectStatusResponseSchema, {
        business_id: businessId,
        session_id: sessionId ?? undefined,
      }),
    enabled: Boolean(ownerId && businessId && provider && sessionId),
    retry: false,
    refetchOnMount: "always",
    // The card handles both native window focus and document visibility, and
    // deduplicates those events with any request already in flight.
    refetchOnWindowFocus: false,
    refetchInterval: (query) => (!query.state.error && query.state.data?.state === "waiting" ? 400 : false),
  });
}

/** `{id}` es aquí un `AccountRef` real (`google:123`), no la plataforma — terminal, sólo se sale reconectando. */
export function useRevokePlatformAccount(businessId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    retry: false,
    mutationFn: (platformAccountId: string) =>
      apiClient.post(`/platform-accounts/${platformAccountId}/revoke`, voidResponseSchema),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: platformAccountsKey(businessId) }),
  });
}

/** Vía alternativa de Meta (System User de empresa): sin `state`/PKCE, el bróker valida el token antes de crear ninguna cuenta. */
export function useRegisterMetaSystemUserToken(businessId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    retry: false,
    mutationFn: (token: string) =>
      apiClient.post("/platform-accounts/meta/system-user-token", metaSystemUserTokenResponseSchema, { token }, { business_id: businessId }),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: platformAccountsKey(businessId) }),
  });
}

export function useTelegramPairing(businessId: string) {
  return useQuery({
    queryKey: ["telegram-pairing", businessId],
    queryFn: () => apiClient.get("/telegram/pairing", telegramPairingSchema, { business_id: businessId }),
    enabled: Boolean(businessId),
    refetchInterval: (query) => (query.state.data?.status === "pending" ? 400 : false),
  });
}

/** Explicit owner confirmation bound to the exact request, never an OTP. */
export function useStartTelegramPairing(businessId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    retry: false,
    mutationFn: ({ confirmationToken }: { confirmationToken?: string }) =>
      apiClient.post("/telegram/pairing/start", telegramPairingStartResponseSchema, undefined, undefined, {
        ...(confirmationToken ? { "X-Action-Confirmation": confirmationToken } : {}),
      }),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["telegram-pairing", businessId] }),
  });
}

export function useUnpairTelegram(businessId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    retry: false,
    mutationFn: (typedConfirmation: string) =>
      apiClient.delete("/telegram/pairing", voidResponseSchema, { typed_confirmation: typedConfirmation }),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["telegram-pairing", businessId] }),
  });
}

export function useSendTelegramTestMessage() {
  return useMutation({
    retry: false,
    mutationFn: () => apiClient.post("/telegram/pairing/test-message", telegramTestMessageResponseSchema),
  });
}
