import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiClient } from "@/api/client";
import { hardCapsUpdateBody, hardCapsViewSchema, type HardCapsUpdate, type HardCapsView } from "@/api/schemas/hardCaps";

const HARD_CAPS_ROOT_KEY = ["hard-caps"];

function hardCapsKey(platformAccountId: string) {
  return [...HARD_CAPS_ROOT_KEY, platformAccountId];
}

function hardCapsPath(platformAccountId: string) {
  return `/accounts/${encodeURIComponent(platformAccountId)}/hard-caps`;
}

/** Sesión de panel por cookie y nada más: esta superficie no tiene vía bearer ni herramienta MCP. */
interface SensitiveHeaders {
  reauthToken?: string;
  confirmationToken?: string;
}

/**
 * `X-Reauth-Token` solo en la llamada que prueba presencia; `X-Action-Confirmation` solo en el
 * reenvío confirmado. Nunca los dos: el código TOTP se quema por acción, y el reenvío es la
 * MISMA acción que la evidencia recién escrita ya satisface.
 */
function sensitiveHeaders({ reauthToken, confirmationToken }: SensitiveHeaders): Record<string, string> {
  return {
    ...(reauthToken ? { "X-Reauth-Token": reauthToken } : {}),
    ...(confirmationToken ? { "X-Action-Confirmation": confirmationToken } : {}),
  };
}

/** `GET /api/v1/accounts/{platform_account_id}/hard-caps` — el efectivo ya resuelto por el bróker. */
export function useAccountHardCaps(platformAccountId: string) {
  return useQuery({
    queryKey: hardCapsKey(platformAccountId),
    queryFn: () => apiClient.get(hardCapsPath(platformAccountId), hardCapsViewSchema),
    enabled: Boolean(platformAccountId),
    retry: false,
  });
}

/**
 * Un cambio de tope mueve `accounts_used` y `cap_changes_today` del sobre, que es común a todas
 * las cuentas: se refresca la vista entera, no solo la fila que se tocó.
 */
function useApplied(platformAccountId: string) {
  const queryClient = useQueryClient();
  return (view: HardCapsView) => {
    queryClient.setQueryData(hardCapsKey(platformAccountId), view);
    void queryClient.invalidateQueries({ queryKey: HARD_CAPS_ROOT_KEY });
  };
}

/**
 * `PUT` — tres importes y la divisa del sobre. La cadena es 401 `REAUTH_REQUIRED` (solo al subir)
 * y después 428 `CONFIRMATION_REQUIRED` siempre: la orquesta `useHardCapsFlow`, que reenvía este
 * mismo cuerpo byte a byte con la prueba de un solo uso.
 */
export function useSetAccountHardCaps(platformAccountId: string) {
  const onApplied = useApplied(platformAccountId);
  return useMutation({
    retry: false,
    mutationFn: ({ caps, reauthToken, confirmationToken }: SensitiveHeaders & { caps: HardCapsUpdate }) =>
      apiClient.put(
        hardCapsPath(platformAccountId),
        hardCapsViewSchema,
        hardCapsUpdateBody(caps),
        undefined,
        sensitiveHeaders({ reauthToken, confirmationToken }),
      ),
    onSuccess: onApplied,
  });
}

/** `DELETE` — vuelve a la entrada del fichero; sin ella, la cuenta queda sin poder escribir. */
export function useDeleteAccountHardCaps(platformAccountId: string) {
  const onApplied = useApplied(platformAccountId);
  return useMutation({
    retry: false,
    mutationFn: ({ reauthToken, confirmationToken }: SensitiveHeaders) =>
      apiClient.delete(
        hardCapsPath(platformAccountId),
        hardCapsViewSchema,
        undefined,
        undefined,
        sensitiveHeaders({ reauthToken, confirmationToken }),
      ),
    onSuccess: onApplied,
  });
}
