import { describe, expect, it, beforeEach, vi } from "vitest";
import { http, HttpResponse } from "msw";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { API_BASE } from "@/mocks/handlers/apiBase";
import {
  resetCloudflareFixtures,
  resetConnectionsFixtures,
  resetOnboardingFixtures,
  resetPlatformAppsFixtures,
  setMockSessionForTests,
} from "@/mocks/handlers";
import { server } from "@/mocks/server";
import { listPlatformApps } from "@/mocks/fixtures/platformApps";
import { ConexionesPage } from "./ConexionesPage";
import { hasAdsSetupHost, requestAdsSetup } from "@/utils/adsSetupHost";

vi.mock("@/utils/adsSetupHost", () => ({ hasAdsSetupHost: vi.fn(() => false), requestAdsSetup: vi.fn(() => true) }));

function renderPage(entry = "/conexiones?business_id=biz_ejemplo") {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[entry]}>
        <ConexionesPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("ConexionesPage", () => {
  beforeEach(() => {
    vi.mocked(hasAdsSetupHost).mockReturnValue(false);
    vi.mocked(requestAdsSetup).mockReset().mockReturnValue(true);
    setMockSessionForTests(true);
    resetPlatformAppsFixtures();
    resetConnectionsFixtures();
    resetOnboardingFixtures();
    resetCloudflareFixtures();
  });

  it("pide el número de Google Ads cuando la conexión está gestionada por Safent", async () => {
    server.use(http.get(`${API_BASE}/platform-apps`, () => HttpResponse.json({
      items: listPlatformApps().items.map(item => item.platform === "google" ? { ...item, client_id_masked: null } : item),
    })));
    renderPage();
    const field = await screen.findByLabelText("Número de cuenta de Google Ads");
    expect(field).toBeRequired();
    expect(field).toHaveAttribute("placeholder", "123-456-7890");
    expect(screen.queryByLabelText(/Número de cuenta.*Meta/)).not.toBeInTheDocument();
  });

  it("mantiene los cuatro pasos técnicos reales dentro de Administración, después de Conectar", async () => {
    const user = userEvent.setup();
    renderPage();
    const googleApp = await screen.findByText("Cliente OAuth de Google Ads");
    const details = screen.getByText("Ver configuración técnica").closest("details")!;
    const progress = googleApp.closest("section")!;
    expect(details).toContainElement(progress);
    expect(within(progress).getAllByRole("listitem")).toHaveLength(4);
    expect(googleApp).not.toBeVisible();
    expect(screen.getByRole("heading", { name: "Conectar tus cuentas" }).compareDocumentPosition(progress) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    await user.click(screen.getByText("Ver configuración técnica"));
    expect(googleApp).toBeVisible();
    expect(within(progress).getAllByText("Pendiente")).toHaveLength(2);
    expect(within(progress).getAllByText("Bloqueado")).toHaveLength(2);
  });

  it("conserva el error de progreso y su reintento dentro de Administración", async () => {
    server.use(http.get(`${API_BASE}/onboarding`, () => HttpResponse.json({}, { status: 503 })));
    const user = userEvent.setup();
    renderPage();
    const error = await screen.findByText("No se ha podido comprobar el progreso de la configuración.");
    const details = screen.getByText("Ver configuración técnica").closest("details")!;
    expect(details).toContainElement(error);
    expect(error).not.toBeVisible();
    await user.click(screen.getByText("Ver configuración técnica"));
    expect(error).toBeVisible();
    expect(within(details).getByRole("button", { name: "Reintentar" })).toBeEnabled();
  });

  it("Preparar la conexión · Administración queda plegado bajo Ver configuración técnica, no suelto en la vista principal", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("Google Ads — Negocio Ejemplo");
    const connect = screen.getByRole("heading", { name: "Conectar tus cuentas" });
    const summary = screen.getByText("Ver configuración técnica");
    const details = summary.closest("details")!;
    const administration = within(details).getByText("Preparar la conexión · Administración");
    expect(details).toContainElement(administration);
    expect(connect.compareDocumentPosition(details) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(administration).not.toBeVisible();
    expect(screen.getByText(/La configuración de las aplicaciones está guardada/)).not.toBeVisible();
    expect(details).not.toHaveAttribute("open");
    for (const button of screen.getAllByRole("button", { name: "Reemplazar" })) expect(button).not.toBeVisible();
    summary.focus();
    expect(summary).toHaveFocus();
    // jsdom does not implement native summary's Enter default action.
    // Keep native details/summary semantics and test its actual click toggle.
    await user.click(summary);
    expect(details).toHaveAttribute("open");
    expect(administration).toBeVisible();
    expect(screen.getByText(/La configuración de las aplicaciones está guardada/)).toBeVisible();
    expect(screen.getAllByRole("button", { name: "Reemplazar" })).toHaveLength(2);
    expect(screen.getByText(/Guardar|Guardarlos no conecta cuentas/)).toBeVisible();
    expect(screen.getByText(/puerto dinámico no está verificado para Meta/)).toBeVisible();
  });

  it("un error al leer la configuración no habilita ni conexión ni edición técnica", async () => {
    server.use(http.get(`${API_BASE}/platform-apps`, () => HttpResponse.json({ error: { code: "UNAVAILABLE", message: "No disponible" } }, { status: 503 })));
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("Google Ads — Negocio Ejemplo");
    await waitFor(() => expect(screen.getAllByRole("button", { name: "Conectar" }).every(button => button.hasAttribute("disabled"))).toBe(true));
    await user.click(screen.getByText("Ver configuración técnica"));
    expect(screen.getByLabelText("Client ID")).toBeDisabled();
    expect(screen.getByLabelText("App ID")).toBeDisabled();
  });

  it("muestra la salud del token y las palancas no disponibles por cuenta", async () => {
    renderPage();
    expect(await screen.findByText("Google Ads — Negocio Ejemplo")).toBeInTheDocument();
    expect(screen.getByText("Meta Ads — Negocio Ejemplo")).toBeInTheDocument();
    expect(screen.getByText(/Creación de campañas/)).toBeInTheDocument();
  });

  it("conectar es un flujo por plataforma, no por cuenta, con estado explícito", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("Google Ads — Negocio Ejemplo");

    const connectButtons = screen.getAllByRole("button", { name: "Conectar" });
    expect(connectButtons).toHaveLength(2);
    await user.click(connectButtons[0]!);

    expect(await screen.findByText("Esperando a la plataforma…")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByText("Conexión completada.")).toBeInTheDocument());
  });

  it("cancelar en la plataforma deja la sesión en estado de error, con el motivo del servidor", async () => {
    server.use(
      http.post(`${API_BASE}/platform-accounts/:provider/reconnect/start`, () =>
        HttpResponse.json(
          { session_id: "rc_cancelada", authorize_url: "https://ads.example/oauth/authorize", expires_at: new Date(Date.now() + 600_000).toISOString() },
          { status: 201 },
        ),
      ),
      http.get(`${API_BASE}/platform-accounts/:provider/reconnect/status`, () =>
        HttpResponse.json({ state: "error", error_code: "OAUTH_PROVIDER_DENIED", message: "La plataforma rechazó la conexión." }),
      ),
    );

    const user = userEvent.setup();
    renderPage();
    await screen.findByText("Google Ads — Negocio Ejemplo");

    const connectButtons = screen.getAllByRole("button", { name: "Conectar" });
    await user.click(connectButtons[0]!);

    expect(await screen.findByText("La plataforma rechazó la conexión.")).toBeInTheDocument();
  });

  it("un proyecto de Google Cloud con acceso de prueba muestra la acción real para revisar su acceso", async () => {
    server.use(
      http.post(`${API_BASE}/platform-accounts/:provider/reconnect/start`, () =>
        HttpResponse.json(
          { session_id: "rc_test_access", authorize_url: "https://ads.example/oauth/authorize", expires_at: new Date(Date.now() + 600_000).toISOString() },
          { status: 201 },
        ),
      ),
      http.get(`${API_BASE}/platform-accounts/:provider/reconnect/status`, () =>
        HttpResponse.json({
          state: "error",
          error_code: "GOOGLE_PROJECT_ACCESS_LEVEL_TEST",
          message:
            "El proyecto de Google Cloud de tu cliente OAuth solo tiene acceso de prueba. Solicita acceso a cuentas reales (Explorer, Básico o Estándar) en https://console.cloud.google.com/google/ads-apis/overview y vuelve a conectar. No necesitas un token de desarrollador.",
        }),
      ),
    );

    const user = userEvent.setup();
    renderPage();
    await screen.findByText("Google Ads — Negocio Ejemplo");

    const connectButtons = screen.getAllByRole("button", { name: "Conectar" });
    await user.click(connectButtons[0]!);

    const actionLink = await screen.findByRole("link", { name: "Revisar acceso en Google Cloud Console" });
    expect(actionLink).toHaveAttribute("href", "https://console.cloud.google.com/google/ads-apis/overview");
    expect(actionLink).toHaveAttribute("target", "_blank");
    expect(actionLink).toHaveAttribute("rel", "noopener noreferrer");
  });

  it("no muestra el enlace de acceso de Google para otros errores de conexión", async () => {
    server.use(
      http.post(`${API_BASE}/platform-accounts/:provider/reconnect/start`, () =>
        HttpResponse.json(
          { session_id: "rc_denied", authorize_url: "https://ads.example/oauth/authorize", expires_at: new Date(Date.now() + 600_000).toISOString() },
          { status: 201 },
        ),
      ),
      http.get(`${API_BASE}/platform-accounts/:provider/reconnect/status`, () =>
        HttpResponse.json({ state: "error", error_code: "OAUTH_PROVIDER_DENIED", message: "La plataforma rechazó la conexión." }),
      ),
    );

    const user = userEvent.setup();
    renderPage();
    await screen.findByText("Google Ads — Negocio Ejemplo");

    const connectButtons = screen.getAllByRole("button", { name: "Conectar" });
    await user.click(connectButtons[0]!);

    await screen.findByText("La plataforma rechazó la conexión.");
    expect(screen.queryByRole("link", { name: /Revisar acceso/ })).not.toBeInTheDocument();
  });

  it("quitar exige escribir QUITAR y refresca la salud del token", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("Google Ads — Negocio Ejemplo");

    const quitarButtons = screen.getAllByRole("button", { name: "Quitar" });
    await user.click(quitarButtons[0]!);

    const dialog = await screen.findByRole("dialog", { name: /Quitar Google Ads/ });
    const confirmButton = within(dialog).getByRole("button", { name: "Quitar" });
    expect(confirmButton).toBeDisabled();

    await user.type(within(dialog).getByLabelText(/Escribe QUITAR/), "QUITAR");
    expect(confirmButton).toBeEnabled();
    await user.click(confirmButton);

    const card = (await screen.findByText("Google Ads — Negocio Ejemplo")).closest("div")!.parentElement as HTMLElement;
    await user.click(within(card).getByText("Ver detalles técnicos"));
    expect(await within(card).findByText(/Token revocado/)).toBeInTheDocument();
  });

  it("el token de sistema de Meta va enmascarado y nunca vuelve al DOM", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("Google Ads — Negocio Ejemplo");

    const tokenInput = screen.getByLabelText("Token de sistema de Meta Business");
    await user.click(screen.getByText("Otra forma de conectar Meta"));
    expect(tokenInput).toHaveAttribute("type", "password");

    await user.type(tokenInput, "pasted-system-user-token");
    await user.click(screen.getByRole("button", { name: "Usar token de sistema" }));

    expect(await screen.findByText("1 cuenta conectada.")).toBeInTheDocument();
    expect(document.body.textContent).not.toContain("pasted-system-user-token");
  });

  // El emparejamiento de Telegram se prueba en AjustesPage.test.tsx (sección Avisos,
  // design.md §6.3): TelegramPairingCard vive ahí ahora, no en Tus cuentas.

  it("una plataforma pendiente ofrece una acción que abre su configuración", async () => {
    server.use(
      http.get(`${API_BASE}/platform-apps`, () =>
        HttpResponse.json({
          items: [
            {
              platform: "google",
              configured: false,
              client_id_masked: null,
              login_customer_id_masked: null,
              redirect_uri: "https://ads.example.ts.net/api/v1/platform-accounts/google/reconnect/callback",
              updated_at: null,
            },
            {
              platform: "meta",
              configured: true,
              client_id_masked: "****3210",
              login_customer_id_masked: null,
              redirect_uri: "https://ads.example.ts.net/api/v1/platform-accounts/meta/reconnect/callback",
              updated_at: new Date().toISOString(),
            },
          ],
        }),
      ),
    );
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("Google Ads — Negocio Ejemplo");

    const connectButtons = screen.getAllByRole("button", { name: "Conectar" });
    expect(connectButtons).toHaveLength(1);
    expect(connectButtons[0]).toBeEnabled();
    expect(screen.queryByLabelText(/Número de cuenta de Google Ads/)).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Configurar Google paso a paso" }));
    expect(screen.getByLabelText("Client ID")).toBeVisible();
    expect(screen.queryByText(/Pide a quien administra/)).not.toBeInTheDocument();
  });

  it("en Safent lleva a Composio desde Meta y no muestra credenciales directas ni retorno local", async () => {
    vi.mocked(hasAdsSetupHost).mockReturnValue(true);
    server.use(http.get(`${API_BASE}/platform-apps`, () => HttpResponse.json({
      items: listPlatformApps().items.map(item => item.platform === "meta" ? { ...item, configured: false } : item),
    })));
    const user = userEvent.setup();
    const start = vi.fn();
    server.use(http.post(`${API_BASE}/platform-accounts/:provider/reconnect/start`, () => { start(); return HttpResponse.json({}); }));
    renderPage();
    const setup = await screen.findByRole("button", { name: "Configurar Meta paso a paso" });
    expect(screen.getByText(/Necesitas el App ID y el App Secret/)).toBeVisible();
    expect(screen.queryByText("Ver configuración técnica")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("App secret")).not.toBeInTheDocument();
    expect(screen.queryByText(/URI de retorno/)).not.toBeInTheDocument();
    expect(screen.queryByText("Otra forma de conectar Meta")).not.toBeInTheDocument();
    await user.click(setup);
    expect(requestAdsSetup).toHaveBeenCalledTimes(1);
    expect(requestAdsSetup).toHaveBeenCalledWith("meta");
    expect(start).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Conectar" })).toBeEnabled();
  });

  it("en Safent permite revisar una configuración existente sin reemplazarla", async () => {
    vi.mocked(hasAdsSetupHost).mockReturnValue(true);
    const user = userEvent.setup();
    renderPage();
    const review = await screen.findByRole("button", { name: "Revisar configuración de Google" });
    await user.click(review);
    expect(requestAdsSetup).toHaveBeenCalledTimes(1);
    expect(requestAdsSetup).toHaveBeenCalledWith("google");
    expect(screen.queryByRole("button", { name: "Reemplazar" })).not.toBeInTheDocument();
  });

  it("si la capacidad del host se revoca, explica cómo volver a la guía", async () => {
    vi.mocked(hasAdsSetupHost).mockReturnValue(true);
    vi.mocked(requestAdsSetup).mockReturnValue(false);
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByRole("button", { name: "Revisar configuración de Meta" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("No pudimos abrir la guía");
  });

  it("al volver de la guía enfoca Meta pero no inicia OAuth", async () => {
    vi.mocked(hasAdsSetupHost).mockReturnValue(true);
    const start = vi.fn();
    server.use(http.post(`${API_BASE}/platform-accounts/:provider/reconnect/start`, () => { start(); return HttpResponse.json({}); }));
    renderPage("/conexiones?business_id=biz_ejemplo&provider=meta");
    await screen.findByText("Meta Ads — Negocio Ejemplo");
    expect(document.activeElement).toHaveAttribute("data-provider", "meta");
    expect(start).not.toHaveBeenCalled();
  });

  it("no presenta un error de lectura como si faltaran credenciales", async () => {
    vi.mocked(hasAdsSetupHost).mockReturnValue(true);
    server.use(http.get(`${API_BASE}/platform-apps`, () => HttpResponse.json({}, { status: 503 })));
    renderPage();
    await screen.findByRole("button", { name: "Reintentar" });
    expect(screen.queryByRole("button", { name: /Configurar .* paso a paso/ })).not.toBeInTheDocument();
    expect(screen.queryByText(/Necesitas el App ID/)).not.toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "Conectar" }).every(button => button.hasAttribute("disabled"))).toBe(true);
  });

  it("muestra la tarjeta Cloudflare en su propia sección, siempre visible", async () => {
    const user = userEvent.setup();
    renderPage();
    expect(await screen.findByRole("heading", { name: "Integraciones" })).toBeInTheDocument();
    expect(screen.getByText("Cloudflare")).toBeInTheDocument();
    expect(await screen.findByText("No conectado")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Conectar Cloudflare" }));

    expect(screen.getByRole("link", { name: "Crear token en Cloudflare" })).toHaveAttribute(
      "href",
      "https://dash.cloudflare.com/profile/api-tokens",
    );
  });
});
