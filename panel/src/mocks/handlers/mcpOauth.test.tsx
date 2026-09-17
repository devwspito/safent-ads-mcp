import type { PropsWithChildren } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";
import { ApiRequestError } from "@/api/client";
import { useApproveMcpOauthConsent, useRevokeMcpOauthGrant } from "@/api/queries/mcpOauth";
import { MOCK_CONSENT_TXN_ID, markMockFederatedPresenceFresh, resetMcpOauthFixtures, setMockSessionForTests } from "@/mocks/handlers";
import { MOCK_TOTP_CODE } from "@/mocks/fixtures/businesses";

function wrapper({ children }: PropsWithChildren) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

/**
 * Contrato final del backend (T060): un TOTP que prueba presencia deja una fila de confirmación
 * POR ACCIÓN (mismo método + ruta), nunca de sesión — a diferencia de la identificación federada,
 * que sigue siendo de sesión y sirve para cualquier acción dentro de la ventana. Estas pruebas
 * viven aquí (contra el mock HTTP directamente, sin montar ninguna pantalla) porque cruzan dos
 * páginas distintas — "Aplicaciones con acceso" (revocar) y el consentimiento MCP (aprobar).
 */
describe("mcp-oauth mock — identificación fresca por acción, no de sesión", () => {
  beforeEach(() => {
    setMockSessionForTests(true);
    resetMcpOauthFixtures();
  });

  it("un TOTP que prueba presencia para revocar el grant A no aprueba un consentimiento distinto", async () => {
    const revoke = renderHook(() => useRevokeMcpOauthGrant(), { wrapper });
    await act(async () => {
      // Establece frescura TOTP para `revoke:grant_claude_code`; el 428 que sigue es esperado
      // aquí (no confirmamos la acción, solo probamos presencia).
      await revoke.result.current.mutateAsync({ grantId: "grant_claude_code", reauthToken: MOCK_TOTP_CODE }).catch(() => {});
    });

    const approve = renderHook(() => useApproveMcpOauthConsent(MOCK_CONSENT_TXN_ID), { wrapper });
    await expect(approve.result.current.mutateAsync({})).rejects.toMatchObject({
      code: "REAUTH_REQUIRED",
      status: 401,
    });
  });

  it("un TOTP que prueba presencia para revocar el grant A no revoca el grant B", async () => {
    const revokeA = renderHook(() => useRevokeMcpOauthGrant(), { wrapper });
    await act(async () => {
      await revokeA.result.current.mutateAsync({ grantId: "grant_claude_code", reauthToken: MOCK_TOTP_CODE }).catch(() => {});
    });

    const revokeB = renderHook(() => useRevokeMcpOauthGrant(), { wrapper });
    await expect(revokeB.result.current.mutateAsync({ grantId: "grant_codex" })).rejects.toMatchObject({
      code: "REAUTH_REQUIRED",
      status: 401,
    });
  });

  it("la identificación federada se consume al completar una revocación: la siguiente acción vuelve a pedir presencia", async () => {
    markMockFederatedPresenceFresh();
    const revokeA = renderHook(() => useRevokeMcpOauthGrant(), { wrapper });

    let confirmationToken: string | undefined;
    try {
      // Fresco por federación, pero sin confirmar todavía -> 428 (nunca un 401 aquí: la
      // identificación federada sí es de sesión).
      await act(async () => {
        await revokeA.result.current.mutateAsync({ grantId: "grant_claude_code" });
      });
      throw new Error("se esperaba un 428 CONFIRMATION_REQUIRED");
    } catch (error) {
      expect(error).toBeInstanceOf(ApiRequestError);
      const apiError = error as ApiRequestError;
      expect(apiError.status).toBe(428);
      confirmationToken = apiError.details?.confirmation_token as string;
    }
    expect(confirmationToken).toBeTruthy();

    await act(async () => {
      await revokeA.result.current.mutateAsync({ grantId: "grant_claude_code", confirmationToken });
    });

    const revokeB = renderHook(() => useRevokeMcpOauthGrant(), { wrapper });
    await expect(revokeB.result.current.mutateAsync({ grantId: "grant_codex" })).rejects.toMatchObject({
      code: "REAUTH_REQUIRED",
      status: 401,
    });
  });
});
