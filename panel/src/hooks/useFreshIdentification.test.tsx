import type { PropsWithChildren } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiRequestError } from "@/api/client";
import { setMockFederatedLoginAvailable } from "@/mocks/handlers";
import { navigateTo } from "@/utils/navigation";
import { useFreshIdentification } from "./useFreshIdentification";

vi.mock("@/utils/navigation", () => ({ navigateTo: vi.fn(() => true) }));

function wrapper({ children }: PropsWithChildren) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

describe("useFreshIdentification", () => {
  beforeEach(() => {
    setMockFederatedLoginAvailable(true);
    vi.mocked(navigateTo).mockReturnValue(true);
  });

  afterEach(() => {
    setMockFederatedLoginAvailable(false);
    vi.restoreAllMocks();
  });

  it("un intento sin cabecera que tiene éxito no abre el prompt", async () => {
    const mutate = vi.fn().mockResolvedValue({ ok: true });
    const onSuccess = vi.fn();
    const { result } = renderHook(() => useFreshIdentification({ mutate, onSuccess, federatedLoginAvailable: true }), { wrapper });

    act(() => result.current.start({ label: "acción" }));

    await waitFor(() => expect(onSuccess).toHaveBeenCalledWith({ ok: true }, { label: "acción" }));
    expect(mutate).toHaveBeenCalledTimes(1);
    expect(mutate).toHaveBeenCalledWith({ label: "acción" });
    expect(result.current.isPromptOpen).toBe(false);
  });

  it("un 401 REAUTH_REQUIRED sin `details` abre el prompt con métodos ['totp'] (comportamiento legado)", async () => {
    const mutate = vi.fn().mockRejectedValue(new ApiRequestError("Falta prueba de presencia.", "REAUTH_REQUIRED", 401));
    const { result } = renderHook(() => useFreshIdentification({ mutate, federatedLoginAvailable: true }), { wrapper });

    act(() => result.current.start({}));

    await waitFor(() => expect(result.current.isPromptOpen).toBe(true));
    expect(result.current.methods).toEqual(["totp"]);
  });

  it("un 401 REAUTH_REQUIRED con `details.methods` abre el prompt con esa variante exacta", async () => {
    const mutate = vi
      .fn()
      .mockRejectedValue(
        new ApiRequestError("Confirma que eres tú.", "REAUTH_REQUIRED", 401, { methods: ["federated"] }),
      );
    const { result } = renderHook(() => useFreshIdentification({ mutate, federatedLoginAvailable: true }), { wrapper });

    act(() => result.current.start({}));

    await waitFor(() => expect(result.current.isPromptOpen).toBe(true));
    expect(result.current.methods).toEqual(["federated"]);
  });

  it("un error que no es REAUTH_REQUIRED no abre el prompt y se expone en `errorMessage`", async () => {
    const mutate = vi.fn().mockRejectedValue(new ApiRequestError("fallo", "SERVER_ERROR", 500));
    const { result } = renderHook(() => useFreshIdentification({ mutate, federatedLoginAvailable: true }), { wrapper });

    act(() => result.current.start({}));

    await waitFor(() => expect(result.current.errorMessage).not.toBeNull());
    expect(result.current.isPromptOpen).toBe(false);
  });

  it("confirmar añade el código guardado en start y llama a mutate una sola vez", async () => {
    const mutate = vi
      .fn()
      .mockRejectedValueOnce(new ApiRequestError("Falta código.", "REAUTH_REQUIRED", 401))
      .mockResolvedValueOnce({ ok: true });
    const onSuccess = vi.fn();
    const { result } = renderHook(() => useFreshIdentification({ mutate, onSuccess, federatedLoginAvailable: true }), { wrapper });

    act(() => result.current.start({ label: "acción" }));
    await waitFor(() => expect(result.current.isPromptOpen).toBe(true));

    act(() => result.current.confirm("123456"));

    await waitFor(() => expect(result.current.isPromptOpen).toBe(false));
    expect(mutate).toHaveBeenCalledTimes(2);
    expect(mutate).toHaveBeenNthCalledWith(2, { label: "acción" }, "123456");
    expect(onSuccess).toHaveBeenCalledWith({ ok: true }, { label: "acción" });
  });

  it("un código incorrecto (401 REAUTH_REQUIRED) deja el prompt abierto con un error accionable", async () => {
    const mutate = vi
      .fn()
      .mockRejectedValueOnce(new ApiRequestError("Falta código.", "REAUTH_REQUIRED", 401))
      .mockRejectedValueOnce(new ApiRequestError("Codigo TOTP invalido.", "REAUTH_REQUIRED", 401));
    const { result } = renderHook(() => useFreshIdentification({ mutate, federatedLoginAvailable: true }), { wrapper });

    act(() => result.current.start({}));
    await waitFor(() => expect(result.current.isPromptOpen).toBe(true));
    act(() => result.current.confirm("000000"));

    await waitFor(() => expect(result.current.errorMessage).not.toBeNull());
    expect(result.current.isPromptOpen).toBe(true);
    expect(result.current.errorMessage).toMatch(/código actual/i);
  });

  it("un bloqueo por intentos (429) pide esperar, no reintentar en bucle", async () => {
    const mutate = vi
      .fn()
      .mockRejectedValueOnce(new ApiRequestError("Falta código.", "REAUTH_REQUIRED", 401))
      .mockRejectedValueOnce(new ApiRequestError("Demasiados intentos recientes.", "ACCOUNT_LOCKED", 429));
    const { result } = renderHook(() => useFreshIdentification({ mutate, federatedLoginAvailable: true }), { wrapper });

    act(() => result.current.start({}));
    await waitFor(() => expect(result.current.isPromptOpen).toBe(true));
    act(() => result.current.confirm("123456"));

    await waitFor(() => expect(result.current.errorMessage).not.toBeNull());
    expect(result.current.isPromptOpen).toBe(true);
    expect(result.current.errorMessage).toMatch(/espera/i);
  });

  it("cancelar cierra el prompt sin llamar a mutate de nuevo", async () => {
    const mutate = vi.fn().mockRejectedValue(new ApiRequestError("Falta código.", "REAUTH_REQUIRED", 401));
    const { result } = renderHook(() => useFreshIdentification({ mutate, federatedLoginAvailable: true }), { wrapper });

    act(() => result.current.start({}));
    await waitFor(() => expect(result.current.isPromptOpen).toBe(true));
    act(() => result.current.cancel());

    expect(result.current.isPromptOpen).toBe(false);
    expect(mutate).toHaveBeenCalledTimes(1);
  });

  it("confirmWithGoogle navega a la URL de autorización cuando `POST /auth/federated/start` responde 200", async () => {
    const mutate = vi.fn().mockRejectedValue(
      new ApiRequestError("Confirma que eres tú.", "REAUTH_REQUIRED", 401, { methods: ["federated"] }),
    );
    const { result } = renderHook(() => useFreshIdentification({ mutate, txnId: "txn_1", federatedLoginAvailable: true }), { wrapper });

    act(() => result.current.start({}));
    await waitFor(() => expect(result.current.isPromptOpen).toBe(true));

    act(() => result.current.confirmWithGoogle());

    await waitFor(() => expect(navigateTo).toHaveBeenCalledTimes(1));
    const [calledUrl] = vi.mocked(navigateTo).mock.calls[0]!;
    expect(calledUrl).toContain("txn_id=txn_1");
    expect(result.current.federatedStartError).toBeNull();
  });

  it("`federatedLoginAvailable: false` nunca ofrece Google, aunque el 401 traiga `methods: ['federated']`", async () => {
    const mutate = vi.fn().mockRejectedValue(
      new ApiRequestError("Confirma que eres tú.", "REAUTH_REQUIRED", 401, { methods: ["federated"] }),
    );
    const { result } = renderHook(
      () => useFreshIdentification({ mutate, federatedLoginAvailable: false }),
      { wrapper },
    );

    act(() => result.current.start({}));

    await waitFor(() => expect(result.current.errorMessage).not.toBeNull());
    expect(result.current.isPromptOpen).toBe(false);
    expect(result.current.methods).toEqual([]);
  });

  it("`federatedLoginAvailable: false` con `methods: ['totp', 'federated']` deja solo el código TOTP", async () => {
    const mutate = vi.fn().mockRejectedValue(
      new ApiRequestError("Confirma que eres tú.", "REAUTH_REQUIRED", 401, { methods: ["totp", "federated"] }),
    );
    const { result } = renderHook(
      () => useFreshIdentification({ mutate, federatedLoginAvailable: false }),
      { wrapper },
    );

    act(() => result.current.start({}));

    await waitFor(() => expect(result.current.isPromptOpen).toBe(true));
    expect(result.current.methods).toEqual(["totp"]);
  });

  it("confirmWithGoogle no navega si el login federado está apagado (404) y ofrece reintentar", async () => {
    setMockFederatedLoginAvailable(false);
    const mutate = vi.fn().mockRejectedValue(
      new ApiRequestError("Confirma que eres tú.", "REAUTH_REQUIRED", 401, { methods: ["federated"] }),
    );
    const { result } = renderHook(() => useFreshIdentification({ mutate, federatedLoginAvailable: true }), { wrapper });

    act(() => result.current.start({}));
    await waitFor(() => expect(result.current.isPromptOpen).toBe(true));
    act(() => result.current.confirmWithGoogle());

    await waitFor(() => expect(result.current.federatedStartError).not.toBeNull());
    expect(navigateTo).not.toHaveBeenCalled();

    act(() => result.current.retryFederatedStart());
    expect(result.current.federatedStartError).toBeNull();
  });
});
