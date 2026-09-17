import type { PropsWithChildren } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ApiRequestError } from "@/api/client";
import { useRevokeGrantFlow } from "./useRevokeGrantFlow";

function wrapper({ children }: PropsWithChildren) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

function confirmationRequired(token: string) {
  return new ApiRequestError("Revisa y confirma esta acción.", "CONFIRMATION_REQUIRED", 428, {
    confirmation_token: token,
    expires_at: new Date(Date.now() + 60_000).toISOString(),
  });
}

function reauthRequired() {
  return new ApiRequestError("Confirma que eres tú.", "REAUTH_REQUIRED", 401);
}

describe("useRevokeGrantFlow", () => {
  it("ya fresco, el 428 del primer intento (sin cabeceras) abre la confirmación sin una segunda llamada", async () => {
    const revoke = vi.fn().mockRejectedValue(confirmationRequired("tok-1"));
    const onRevoked = vi.fn();
    const { result } = renderHook(
      () => useRevokeGrantFlow({ revoke, federatedLoginAvailable: true, onRevoked }),
      { wrapper },
    );

    act(() => result.current.presence.start({ grantId: "grant_1" }));

    await waitFor(() => expect(result.current.isConfirmationOpen).toBe(true));
    expect(result.current.presence.isPromptOpen).toBe(false);
    expect(revoke).toHaveBeenCalledTimes(1);
    expect(revoke).toHaveBeenCalledWith({ grantId: "grant_1", reauthToken: undefined });
  });

  it("tras el código TOTP, la llamada CON reauthToken que devuelve 428 es la que se confirma (2 llamadas, no 3, antes del diálogo)", async () => {
    const revoke = vi.fn().mockRejectedValueOnce(reauthRequired()).mockRejectedValueOnce(confirmationRequired("tok-2"));
    const onRevoked = vi.fn();
    const { result } = renderHook(
      () => useRevokeGrantFlow({ revoke, federatedLoginAvailable: true, onRevoked }),
      { wrapper },
    );

    act(() => result.current.presence.start({ grantId: "grant_1" }));
    await waitFor(() => expect(result.current.presence.isPromptOpen).toBe(true));
    expect(revoke).toHaveBeenCalledTimes(1);

    act(() => result.current.presence.confirm("123456"));

    await waitFor(() => expect(result.current.isConfirmationOpen).toBe(true));
    expect(result.current.presence.isPromptOpen).toBe(false);
    // Antes de este arreglo, aquí habría 3 llamadas: la que gana el 428 se descartaba y
    // `startConfirmation` repetía la misma petición sin cabeceras para pedir un token nuevo.
    expect(revoke).toHaveBeenCalledTimes(2);
    expect(revoke).toHaveBeenNthCalledWith(2, { grantId: "grant_1", reauthToken: "123456" });
  });

  it("confirmar en el diálogo envía el token de esa misma llamada — 3 peticiones en total, no 4", async () => {
    const revoke = vi
      .fn()
      .mockRejectedValueOnce(reauthRequired())
      .mockRejectedValueOnce(confirmationRequired("tok-3"))
      .mockResolvedValueOnce(undefined);
    const onRevoked = vi.fn();
    const { result } = renderHook(
      () => useRevokeGrantFlow({ revoke, federatedLoginAvailable: true, onRevoked }),
      { wrapper },
    );

    act(() => result.current.presence.start({ grantId: "grant_1" }));
    await waitFor(() => expect(result.current.presence.isPromptOpen).toBe(true));
    act(() => result.current.presence.confirm("123456"));
    await waitFor(() => expect(result.current.isConfirmationOpen).toBe(true));

    act(() => result.current.confirm());

    await waitFor(() => expect(onRevoked).toHaveBeenCalledTimes(1));
    expect(revoke).toHaveBeenCalledTimes(3);
    expect(revoke).toHaveBeenNthCalledWith(3, { grantId: "grant_1", confirmationToken: "tok-3" });
    expect(result.current.isConfirmationOpen).toBe(false);
  });

  it("un 5xx al confirmar dentro del diálogo lo deja abierto con el error (no la señal de identificación perdida)", async () => {
    const revoke = vi
      .fn()
      .mockRejectedValueOnce(confirmationRequired("tok-4"))
      .mockRejectedValueOnce(new ApiRequestError("fallo", "SERVER_ERROR", 500));
    const onRevoked = vi.fn();
    const { result } = renderHook(
      () => useRevokeGrantFlow({ revoke, federatedLoginAvailable: true, onRevoked }),
      { wrapper },
    );

    act(() => result.current.presence.start({ grantId: "grant_1" }));
    await waitFor(() => expect(result.current.isConfirmationOpen).toBe(true));

    act(() => result.current.confirm());

    await waitFor(() => expect(result.current.confirmError).not.toBeNull());
    expect(result.current.isConfirmationOpen).toBe(true);
    expect(onRevoked).not.toHaveBeenCalled();
  });
});
