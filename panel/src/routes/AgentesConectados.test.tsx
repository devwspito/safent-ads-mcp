import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { http, HttpResponse } from "msw";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { API_BASE } from "@/mocks/handlers/apiBase";
import {
  resetConnectionsFixtures,
  resetMcpOauthFixtures,
  resetOnboardingFixtures,
  resetPlatformAppsFixtures,
  setMockFederatedLoginAvailable,
  setMockFreshIdentificationUntil,
  setMockSessionForTests,
} from "@/mocks/handlers";
import { MOCK_TOTP_CODE } from "@/mocks/fixtures/businesses";
import { MOCK_GRANT_WITHOUT_EXPIRY } from "@/mocks/fixtures/mcpOauth";
import { server } from "@/mocks/server";
import { navigateTo } from "@/utils/navigation";
import { ConexionesPage } from "./ConexionesPage";

vi.mock("@/utils/navigation", () => ({ navigateTo: vi.fn(() => true) }));

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/conexiones?business_id=biz_ejemplo"]}>
        <ConexionesPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function respondReauthRequired(methods: string[]) {
  server.use(
    http.post(`${API_BASE}/mcp-oauth/grants/:grantId/revoke`, () =>
      HttpResponse.json(
        { error: { code: "REAUTH_REQUIRED", message: "Confirma que eres tú.", details: { methods } } },
        { status: 401 },
      ),
    ),
  );
}

/** Espera a que `useMe()` resuelva — el botón queda deshabilitado mientras tanto (T062: la
 * sección no decide si ofrecer Google sin saber `federated_login_available` de verdad). */
async function findEnabledRevokeButton() {
  await screen.findByText("Claude Code");
  const [button] = await screen.findAllByRole("button", { name: "Quitar acceso" });
  await waitFor(() => expect(button).toBeEnabled());
  return button!;
}

describe("Aplicaciones con acceso (ConexionesPage)", () => {
  beforeEach(() => {
    setMockSessionForTests(true);
    resetPlatformAppsFixtures();
    resetConnectionsFixtures();
    resetOnboardingFixtures();
    resetMcpOauthFixtures();
  });

  afterEach(() => {
    vi.restoreAllMocks();
    setMockFederatedLoginAvailable(false);
    setMockFreshIdentificationUntil(null);
  });

  it("lista las aplicaciones con sus alcances en palabras llanas y sus fechas", async () => {
    renderPage();

    const section = (await screen.findByRole("heading", { name: "Aplicaciones con acceso" })).closest("section");
    expect(section).not.toBeNull();
    const within_ = within(section as HTMLElement);

    expect(await within_.findByText("Claude Code")).toBeInTheDocument();
    expect(within_.getByText("Codex")).toBeInTheDocument();
    expect(within_.getAllByText(/ver negocios, cuentas, campañas y resultados/).length).toBeGreaterThan(0);
    expect(within_.getByText(/proponer campañas y cambios, que tú apruebas/)).toBeInTheDocument();
    expect(within_.getAllByText(/último uso nunca/).length).toBeGreaterThan(0);
    expect(within_.getAllByRole("button", { name: "Quitar acceso" })).toHaveLength(2);
  });

  it("un grant sin refresh token activo (expires_at null) se muestra como «sin caducidad»", async () => {
    server.use(
      http.get(`${API_BASE}/mcp-oauth/grants`, () =>
        HttpResponse.json({ grants: [MOCK_GRANT_WITHOUT_EXPIRY] }),
      ),
    );
    renderPage();

    expect(await screen.findByText("Codex sin refresco")).toBeInTheDocument();
    expect(screen.getByText(/caduca sin caducidad/)).toBeInTheDocument();
  });

  it("sin aplicaciones con acceso muestra el estado vacío", async () => {
    server.use(http.get(`${API_BASE}/mcp-oauth/grants`, () => HttpResponse.json({ grants: [] })));
    renderPage();

    expect(await screen.findByText("Ninguna aplicación con acceso")).toBeInTheDocument();
    expect(screen.getByText("Conecta Claude Code o Codex con el instalador del MCP.")).toBeInTheDocument();
  });

  describe("Quitar acceso: prueba de presencia (401) y confirmación de la acción (428) encadenadas", () => {
    it("TOTP correcto encadena ambos diálogos hasta el 204 y refresca la lista", async () => {
      const user = userEvent.setup();
      renderPage();

      await user.click(await findEnabledRevokeButton());
      const presenceDialog = await screen.findByRole("dialog", { name: "Confirmar quitar acceso" });
      expect(presenceDialog).toHaveTextContent("Claude Code");
      await user.type(within(presenceDialog).getByLabelText(/Código de verificación/), MOCK_TOTP_CODE);
      await user.click(within(presenceDialog).getByRole("button", { name: "Continuar" }));

      await waitFor(() => expect(presenceDialog).not.toBeInTheDocument());
      const confirmDialog = await screen.findByRole("dialog", { name: "Revisa y confirma esta acción" });
      expect(confirmDialog).toHaveTextContent(/Se quitará el acceso de Claude Code/);
      await user.click(within(confirmDialog).getByRole("button", { name: "Quitar acceso" }));

      await waitFor(() => expect(confirmDialog).not.toBeInTheDocument());
      await waitFor(() => expect(screen.queryByText("Claude Code")).not.toBeInTheDocument());
      expect(screen.getByText("Codex")).toBeInTheDocument();
    });

    it("el foco vuelve al botón «Quitar acceso» tras confirmar en el segundo diálogo (WCAG 2.4.3)", async () => {
      // Aislado de la lista real: revocar SIEMPRE devuelve el mismo grant, así que el botón que
      // abrió la cadena sigue en el documento y la comprobación de foco no compite con el
      // refresco de la lista (que en la prueba de arriba sí lo hace desaparecer).
      server.use(
        http.get(`${API_BASE}/mcp-oauth/grants`, () =>
          HttpResponse.json({
            grants: [
              {
                grant_id: "grant_claude_code",
                client_id: "client-1",
                client_name: "Claude Code",
                redirect_host: "127.0.0.1:54321",
                scopes: ["ads:read"],
                created_at: new Date().toISOString(),
                expires_at: null,
                last_used_at: null,
              },
            ],
          }),
        ),
      );
      const user = userEvent.setup();
      renderPage();

      const revokeButton = await findEnabledRevokeButton();
      await user.click(revokeButton);
      const presenceDialog = await screen.findByRole("dialog", { name: "Confirmar quitar acceso" });
      await user.type(within(presenceDialog).getByLabelText(/Código de verificación/), MOCK_TOTP_CODE);
      await user.click(within(presenceDialog).getByRole("button", { name: "Continuar" }));

      const confirmDialog = await screen.findByRole("dialog", { name: "Revisa y confirma esta acción" });
      await user.click(within(confirmDialog).getByRole("button", { name: "Quitar acceso" }));

      await waitFor(() => expect(confirmDialog).not.toBeInTheDocument());
      expect(document.activeElement).toBe(revokeButton);
    });

    it("un TOTP incorrecto deja el primer diálogo abierto con un error y nunca llega al segundo", async () => {
      const user = userEvent.setup();
      renderPage();

      await user.click(await findEnabledRevokeButton());
      const presenceDialog = await screen.findByRole("dialog", { name: "Confirmar quitar acceso" });
      await user.type(within(presenceDialog).getByLabelText(/Código de verificación/), "000000");
      await user.click(within(presenceDialog).getByRole("button", { name: "Continuar" }));

      expect(await within(presenceDialog).findByRole("alert")).toHaveTextContent(/código actual/i);
      expect(screen.getAllByRole("dialog")).toHaveLength(1);
      expect(screen.getByText("Claude Code")).toBeInTheDocument();
    });

    it("cancelar en el primer diálogo (presencia) deja la lista sin cambios y no abre la confirmación", async () => {
      const user = userEvent.setup();
      renderPage();

      await user.click(await findEnabledRevokeButton());
      const presenceDialog = await screen.findByRole("dialog", { name: "Confirmar quitar acceso" });
      await user.click(within(presenceDialog).getByRole("button", { name: "Cancelar" }));

      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
      expect(screen.getByText("Claude Code")).toBeInTheDocument();
    });

    it("cancelar en el segundo diálogo (428) deja la lista sin cambios y devuelve el foco al botón", async () => {
      const user = userEvent.setup();
      renderPage();

      const revokeButton = await findEnabledRevokeButton();
      await user.click(revokeButton);
      const presenceDialog = await screen.findByRole("dialog", { name: "Confirmar quitar acceso" });
      await user.type(within(presenceDialog).getByLabelText(/Código de verificación/), MOCK_TOTP_CODE);
      await user.click(within(presenceDialog).getByRole("button", { name: "Continuar" }));

      const confirmDialog = await screen.findByRole("dialog", { name: "Revisa y confirma esta acción" });
      await user.click(within(confirmDialog).getByRole("button", { name: "Cancelar" }));

      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
      expect(screen.getByText("Claude Code")).toBeInTheDocument();
      // WCAG 2.4.3: vuelve al botón de origen — no a "quien tenía el foco" cuando el primer
      // diálogo cerró y el segundo aún no había montado.
      expect(document.activeElement).toBe(revokeButton);
    });

    it("`methods: [\"federated\"]` pinta solo «Confirmar con Google» y no navega sin clic", async () => {
      const navigateToMock = vi.mocked(navigateTo);
      setMockFederatedLoginAvailable(true);
      respondReauthRequired(["federated"]);
      const user = userEvent.setup();
      renderPage();

      await user.click(await findEnabledRevokeButton());
      const dialog = await screen.findByRole("dialog", { name: "Confirmar quitar acceso" });

      expect(within(dialog).getByRole("button", { name: "Confirmar con Google" })).toBeInTheDocument();
      expect(within(dialog).queryByLabelText(/Código de verificación/)).not.toBeInTheDocument();
      expect(navigateToMock).not.toHaveBeenCalled();

      await user.click(within(dialog).getByRole("button", { name: "Confirmar con Google" }));

      await waitFor(() => expect(navigateToMock).toHaveBeenCalledTimes(1));
    });

    it("con `federated_login_available: false`, un `methods: [\"federated\"]` nunca ofrece Google", async () => {
      setMockFederatedLoginAvailable(false);
      respondReauthRequired(["federated"]);
      const user = userEvent.setup();
      renderPage();

      await user.click(await findEnabledRevokeButton());

      expect(await screen.findByRole("alert")).toBeInTheDocument();
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
      expect(screen.getByText("Claude Code")).toBeInTheDocument();
    });

    it("muestra «Identificado hasta las HH:MM» cuando `session.fresh_identification_until` sigue en el futuro", async () => {
      const freshUntil = new Date(Date.now() + 2 * 60_000).toISOString();
      setMockFreshIdentificationUntil(freshUntil);
      respondReauthRequired(["totp", "federated"]);
      const user = userEvent.setup();
      renderPage();

      await user.click(await findEnabledRevokeButton());
      const dialog = await screen.findByRole("dialog", { name: "Confirmar quitar acceso" });

      expect(within(dialog).getByText(/Identificado hasta las \d{2}:\d{2}/)).toBeInTheDocument();
    });
  });
});
