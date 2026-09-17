import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { z } from "zod";
import { apiClient } from "@/api/client";
import {
  federatedStartResponseSchema,
  federatedStatusResponseSchema,
  meResponseSchema,
  type FederatedStartResponse,
  type MeResponse,
} from "@/api/schemas";

const voidSchema = z.undefined();

export function useMe() {
  return useQuery<MeResponse>({
    queryKey: ["auth", "me"],
    queryFn: () => apiClient.get("/auth/me", meResponseSchema),
    retry: false,
    staleTime: 5 * 60_000,
  });
}

export function useLogin() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: { email: string; password: string }) =>
      apiClient.post("/auth/login", voidSchema, input),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["auth", "me"] });
    },
  });
}

export function useLogout() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => apiClient.post("/auth/logout", voidSchema),
    onSuccess: () => {
      queryClient.setQueryData(["auth", "me"], undefined);
      void queryClient.invalidateQueries();
    },
  });
}

/**
 * `GET /auth/federated/status` (contracts/federated-login.md §1) — decide si `LoginPage`
 * pinta «Entrar con Google». 404, error de red o carga en curso deben leerse como "no
 * disponible" en el llamador; por eso no hay `retry` (la ausencia no es un fallo transitorio).
 */
export function useFederatedStatus() {
  return useQuery({
    queryKey: ["auth", "federated-status"],
    queryFn: () => apiClient.get("/auth/federated/status", federatedStatusResponseSchema),
    retry: false,
    staleTime: 60_000,
  });
}

/**
 * `POST /auth/federated/start` — el propósito (`entrar` / `re-identificar`) lo decide el
 * servidor según haya o no sesión; el cliente solo aporta el `txn_id` de la transacción de
 * consentimiento a la que volver, si hay una.
 */
export function useStartFederatedLogin() {
  return useMutation<FederatedStartResponse, unknown, string | null>({
    mutationFn: (txnId) =>
      apiClient.post("/auth/federated/start", federatedStartResponseSchema, { txn_id: txnId }),
  });
}
