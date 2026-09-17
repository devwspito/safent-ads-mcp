import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiClient, voidResponseSchema } from "@/api/client";
import {
  mcpOauthConsentActionResponseSchema,
  mcpOauthConsentSchema,
  mcpOauthGrantsResponseSchema,
} from "@/api/schemas/mcpOauth";

const GRANTS_KEY = ["mcp-oauth", "grants"];

/** `GET /api/v1/mcp-oauth/consent/{txn_id}` — 401 sin sesión, 404/410 caducado o resuelto. */
export function useMcpOauthConsent(txnId: string | null) {
  return useQuery({
    queryKey: ["mcp-oauth", "consent", txnId],
    queryFn: () => apiClient.get(`/mcp-oauth/consent/${txnId}`, mcpOauthConsentSchema),
    enabled: txnId !== null,
    retry: false,
  });
}

/**
 * `X-Reauth-Token` (TOTP fresco, C-41) o identificación federada fresca sin cabecera
 * (contracts/federated-login.md §2): `useFreshIdentification` intenta primero SIN
 * `reauthToken` — el 401 lo produce la dependencia del servidor, antes del caso de uso.
 */
export function useApproveMcpOauthConsent(txnId: string) {
  return useMutation({
    mutationFn: ({ reauthToken }: { reauthToken?: string }) =>
      apiClient.post(
        `/mcp-oauth/consent/${txnId}/approve`,
        mcpOauthConsentActionResponseSchema,
        undefined,
        undefined,
        reauthToken ? { "X-Reauth-Token": reauthToken } : undefined,
      ),
  });
}

/** Sin TOTP: declinar no cambia la postura de seguridad. */
export function useDenyMcpOauthConsent(txnId: string) {
  return useMutation({
    mutationFn: () => apiClient.post(`/mcp-oauth/consent/${txnId}/deny`, mcpOauthConsentActionResponseSchema),
  });
}

/** `GET /api/v1/mcp-oauth/grants` — sección "Aplicaciones con acceso". */
export function useMcpOauthGrants() {
  return useQuery({
    queryKey: GRANTS_KEY,
    queryFn: () => apiClient.get("/mcp-oauth/grants", mcpOauthGrantsResponseSchema),
  });
}

/**
 * `POST /api/v1/mcp-oauth/grants/{grant_id}/revoke` — identificación fresca **y** confirmación de
 * acción (contracts/federated-login.md §2): la primera llamada sin cabeceras 401 (sin frescura) o
 * 428 (frescura sin confirmar); `useFreshIdentification` prueba la presencia, la respuesta 428 pasa
 * a `ActionConfirmationDialog`, y solo entonces esta misma mutación reenvía `confirmationToken`.
 */
export function useRevokeMcpOauthGrant() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ grantId, reauthToken, confirmationToken }: { grantId: string; reauthToken?: string; confirmationToken?: string }) =>
      apiClient.post(
        `/mcp-oauth/grants/${grantId}/revoke`,
        voidResponseSchema,
        undefined,
        undefined,
        {
          ...(reauthToken ? { "X-Reauth-Token": reauthToken } : {}),
          ...(confirmationToken ? { "X-Action-Confirmation": confirmationToken } : {}),
        },
      ),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: GRANTS_KEY }),
  });
}
