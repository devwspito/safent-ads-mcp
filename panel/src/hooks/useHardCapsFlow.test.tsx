import type { PropsWithChildren } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ApiRequestError } from "@/api/client";
import type { HardCapsUpdate, HardCapsView } from "@/api/schemas/hardCaps";
import { useHardCapsFlow } from "./useHardCapsFlow";

function wrapper({ children }: PropsWithChildren) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

const CAPS: HardCapsUpdate = {
  daily_cap_minor: 4000,
  monthly_cap_minor: 20_000,
  ceiling_minor: 9000,
  currency: "EUR",
};

const APPLIED: HardCapsView = {
  platform_account_id: "100-000-0002",
  source: "file_and_panel",
  writable: true,
  currency: "EUR",
  effective: { daily_cap_minor: 4000, monthly_cap_minor: 20_000, floor_minor: 200, ceiling_minor: 9000 },
  clamped_by: [],
  panel_state_available: true,
  envelope: null,
};

function confirmationRequired(token: string) {
  return new ApiRequestError("Revisa y confirma esta acción.", "CONFIRMATION_REQUIRED", 428, {
    confirmation_token: token,
    expires_at: new Date(Date.now() + 60_000).toISOString(),
  });
}

function reauthRequired() {
  return new ApiRequestError("Confirma que eres tú.", "REAUTH_REQUIRED", 401, { methods: ["totp"] });
}

function renderFlow(submit: ReturnType<typeof vi.fn>, onApplied = vi.fn()) {
  const rendered = renderHook(
    () => useHardCapsFlow({ submit, federatedLoginAvailable: false, onApplied }),
    { wrapper },
  );
  return { ...rendered, onApplied };
}

describe("useHardCapsFlow", () => {
  it("bajar un tope solo pide confirmar: ninguna prueba de presencia por el camino", async () => {
    const submit = vi.fn().mockRejectedValueOnce(confirmationRequired("tok-1")).mockResolvedValueOnce(APPLIED);
    const { result, onApplied } = renderFlow(submit);

    act(() => result.current.start({ kind: "set", caps: CAPS }));
    await waitFor(() => expect(result.current.isConfirmationOpen).toBe(true));
    expect(result.current.presence.isPromptOpen).toBe(false);

    act(() => result.current.confirm());
    await waitFor(() => expect(onApplied).toHaveBeenCalledWith(APPLIED));
    expect(submit).toHaveBeenCalledTimes(2);
    expect(submit).toHaveBeenNthCalledWith(1, { kind: "set", caps: CAPS }, { reauthToken: undefined });
    expect(submit).toHaveBeenNthCalledWith(2, { kind: "set", caps: CAPS }, { confirmationToken: "tok-1" });
  });

  it("subir encadena 401 y 428 con un solo código, y el reenvío confirmado no repite el código", async () => {
    const submit = vi
      .fn()
      .mockRejectedValueOnce(reauthRequired())
      .mockRejectedValueOnce(confirmationRequired("tok-2"))
      .mockResolvedValueOnce(APPLIED);
    const { result, onApplied } = renderFlow(submit);

    act(() => result.current.start({ kind: "set", caps: CAPS }));
    await waitFor(() => expect(result.current.presence.isPromptOpen).toBe(true));

    act(() => result.current.presence.confirm("123456"));
    await waitFor(() => expect(result.current.isConfirmationOpen).toBe(true));
    expect(result.current.presence.isPromptOpen).toBe(false);

    act(() => result.current.confirm());
    await waitFor(() => expect(onApplied).toHaveBeenCalledWith(APPLIED));
    expect(submit).toHaveBeenCalledTimes(3);
    expect(submit).toHaveBeenNthCalledWith(2, { kind: "set", caps: CAPS }, { reauthToken: "123456" });
    // El tercero lleva la prueba de un solo uso y NO el código: ya se quemó, y la confirmación
    // está ligada a esta misma acción.
    expect(submit).toHaveBeenNthCalledWith(3, { kind: "set", caps: CAPS }, { confirmationToken: "tok-2" });
  });

  it("el cuerpo del reenvío es el mismo objeto congelado, no uno reconstruido por el camino", async () => {
    const submit = vi.fn().mockRejectedValueOnce(confirmationRequired("tok-3")).mockResolvedValueOnce(APPLIED);
    const { result } = renderFlow(submit);

    act(() => result.current.start({ kind: "set", caps: { ...CAPS } }));
    await waitFor(() => expect(result.current.isConfirmationOpen).toBe(true));
    act(() => result.current.confirm());

    await waitFor(() => expect(submit).toHaveBeenCalledTimes(2));
    const first = submit.mock.calls[0]![0] as { caps: HardCapsUpdate };
    const second = submit.mock.calls[1]![0] as { caps: HardCapsUpdate };
    expect(JSON.stringify(second.caps)).toBe(JSON.stringify(first.caps));
    expect(Object.isFrozen(second.caps)).toBe(true);
  });

  it("un segundo clic con la petición en vuelo no reenvía la prueba de un solo uso", async () => {
    let release: (view: HardCapsView) => void = () => {};
    const submit = vi
      .fn()
      .mockRejectedValueOnce(confirmationRequired("tok-4"))
      .mockImplementationOnce(() => new Promise<HardCapsView>((resolve) => (release = resolve)));
    const { result } = renderFlow(submit);

    act(() => result.current.start({ kind: "set", caps: CAPS }));
    await waitFor(() => expect(result.current.isConfirmationOpen).toBe(true));

    act(() => result.current.confirm());
    act(() => result.current.confirm());
    expect(submit).toHaveBeenCalledTimes(2);

    await act(async () => release(APPLIED));
    expect(submit).toHaveBeenCalledTimes(2);
  });

  it("un rechazo del sobre se dice con su frase, sin abrir ningún diálogo", async () => {
    const submit = vi.fn().mockRejectedValue(new ApiRequestError("x", "ENVELOPE_EXCEEDED", 409));
    const { result } = renderFlow(submit);

    act(() => result.current.start({ kind: "set", caps: CAPS }));
    await waitFor(() =>
      expect(result.current.submitError).toBe("Ese importe supera el sobre declarado en config/caps.yaml."),
    );
    expect(result.current.isConfirmationOpen).toBe(false);
    expect(result.current.presence.isPromptOpen).toBe(false);
  });

  it("perder la identificación fresca entre los dos diálogos vuelve a la prueba de presencia", async () => {
    const submit = vi
      .fn()
      .mockRejectedValueOnce(confirmationRequired("tok-5"))
      .mockRejectedValueOnce(reauthRequired())
      .mockRejectedValueOnce(reauthRequired());
    const { result } = renderFlow(submit);

    act(() => result.current.start({ kind: "withdraw" }));
    await waitFor(() => expect(result.current.isConfirmationOpen).toBe(true));

    act(() => result.current.confirm());
    await waitFor(() => expect(result.current.presence.isPromptOpen).toBe(true));
    expect(result.current.isConfirmationOpen).toBe(false);
  });
});
