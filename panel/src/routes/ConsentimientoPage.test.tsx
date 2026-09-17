import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { http, HttpResponse, delay } from "msw";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useSearchParams } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { API_BASE } from "@/mocks/handlers/apiBase";
import {
  MOCK_CONSENT_TXN_ID,
  resetMcpOauthFixtures,
  setMockFederatedLoginAvailable,
  setMockFreshIdentificationUntil,
  setMockSessionForTests,
} from "@/mocks/handlers";
import { approveMockConsent } from "@/mocks/fixtures/mcpOauth";
import { MOCK_TOTP_CODE } from "@/mocks/fixtures/businesses";
import { server } from "@/mocks/server";
import { navigateTo } from "@/utils/navigation";
import { ConsentimientoPage } from "./ConsentimientoPage";

vi.mock("@/utils/navigation", () => ({ navigateTo: vi.fn(() => true) }));

function LoginStub() {
  const [searchParams] = useSearchParams();
  return <p>login-next:{searchParams.get("next")}</p>;
}

function renderPage(path = `/oauth/autorizar?txn=${MOCK_CONSENT_TXN_ID}`) {
  window.history.pushState({}, "", path);
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/oauth/autorizar" element={<ConsentimientoPage />} />
          <Route path="/login" element={<LoginStub />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("ConsentimientoPage", () => {
  beforeEach(() => {
    setMockSessionForTests(true);
    resetMcpOauthFixtures();
  });

  afterEach(() => {
    vi.restoreAllMocks();
    setMockFederatedLoginAvailable(false);
    // Corre SIEMPRE, incluso si el test de abajo falla antes de llegar a su
    // propia limpieza -- un <meta> que sobrevive a un test roto contamina
    // los siguientes (mismo document.head en todo el fichero).
    document.querySelectorAll('meta[name="ads-instance-name"]').forEach(el => el.remove());
    setMockFreshIdentificationUntil(null);
  });

  /** `useMe()` decide `federatedLoginAvailable` (T052 fix): «Autorizar» queda deshabilitado
   * hasta que resuelva, para no ofrecer/retirar Google a media carga (sin parpadeo). */
  async function findEnabledAutorizarButton() {
    const button = await screen.findByRole("button", { name: "Autorizar" });
    await waitFor(() => expect(button).toBeEnabled());
    return button;
  }

  function respondReauthRequired(methods: string[]) {
    server.use(
      http.post(`${API_BASE}/mcp-oauth/consent/:txnId/approve`, () =>
        HttpResponse.json(
          { error: { code: "REAUTH_REQUIRED", message: "Confirma que eres tú.", details: { methods } } },
          { status: 401 },
        ),
      ),
    );
  }

  it("pinta el nombre del cliente y los alcances en palabras llanas", async () => {
    renderPage();

    expect(await screen.findByText("Claude Code", { exact: false })).toBeInTheDocument();
    expect(screen.getByText(/ver negocios, cuentas, campañas y resultados/)).toBeInTheDocument();
    expect(screen.getByText(/proponer campañas y cambios, que tú apruebas/)).toBeInTheDocument();
    expect(screen.getByText("127.0.0.1:54321")).toBeInTheDocument();
  });

  it("el título dice a qué instancia se accede, con el defecto genérico sin inyección", async () => {
    renderPage();

    expect(await screen.findByText(/quiere acceder a Ads MCP/)).toBeInTheDocument();
  });

  it("el título usa la identidad de la instancia inyectada por el servidor, nunca un literal", async () => {
    const meta = document.createElement("meta");
    meta.name = "ads-instance-name";
    meta.content = "Acme Ads MCP";
    document.head.append(meta);

    renderPage();

    expect(await screen.findByText(/quiere acceder a Acme Ads MCP/)).toBeInTheDocument();
  });

  it("escapa el nombre del cliente: se pinta como texto, nunca como HTML", async () => {
    server.use(
      http.get(`${API_BASE}/mcp-oauth/consent/:txnId`, () =>
        HttpResponse.json({
          txn_id: MOCK_CONSENT_TXN_ID,
          client_id: "client-hostil",
          client_name: "<img src=x onerror=alert(1)>",
          redirect_host: "127.0.0.1:9999",
          scopes: [{ name: "ads:read", label: "Leer tu cartera, señales y registro" }],
          expires_at: new Date(Date.now() + 60_000).toISOString(),
        }),
      ),
    );
    renderPage();

    expect(await screen.findByText("<img src=x onerror=alert(1)>", { exact: false })).toBeInTheDocument();
    expect(document.querySelector("img")).not.toBeInTheDocument();
  });

  it("quita `?txn=` de la URL nada más leerlo (C-58)", async () => {
    renderPage();
    await screen.findByText("Claude Code", { exact: false });

    expect(window.location.search).toBe("");
  });

  it("estado de carga antes de que llegue la respuesta", async () => {
    server.use(
      http.get(`${API_BASE}/mcp-oauth/consent/:txnId`, async () => {
        await delay(50);
        return HttpResponse.json({
          txn_id: MOCK_CONSENT_TXN_ID,
          client_id: "client-1",
          client_name: "Claude Code",
          redirect_host: "127.0.0.1:54321",
          scopes: [{ name: "ads:read", label: "Leer tu cartera, señales y registro" }],
          expires_at: new Date(Date.now() + 60_000).toISOString(),
        });
      }),
    );
    renderPage();

    expect(screen.queryByText("Claude Code", { exact: false })).not.toBeInTheDocument();
    expect(await screen.findByText("Claude Code", { exact: false })).toBeInTheDocument();
  });

  it("un error de servidor muestra el estado de error con Reintentar", async () => {
    server.use(
      http.get(`${API_BASE}/mcp-oauth/consent/:txnId`, () =>
        HttpResponse.json({ error: { code: "SERVER_ERROR", message: "fallo" } }, { status: 500 }),
      ),
    );
    renderPage();

    expect(await screen.findByRole("button", { name: "Reintentar" })).toBeInTheDocument();
  });

  it("txn caducado o desconocido muestra el mensaje de caducidad", async () => {
    renderPage("/oauth/autorizar?txn=txn_no_existe");

    expect(await screen.findByText(/Esta solicitud ha caducado/)).toBeInTheDocument();
  });

  it("sin sesión redirige a /login con `next` apuntando de vuelta a esta solicitud", async () => {
    setMockSessionForTests(false);
    renderPage();

    const login = await screen.findByText(/login-next:/);
    expect(login.textContent).toBe(`login-next:/oauth/autorizar?txn=${MOCK_CONSENT_TXN_ID}`);
  });

  it("Autorizar pide TOTP y navega a redirect_to al confirmar", async () => {
    const navigateToMock = vi.mocked(navigateTo);
    const user = userEvent.setup();
    renderPage();

    await user.click(await findEnabledAutorizarButton());
    const dialog = await screen.findByRole("dialog", { name: "Confirmar acceso" });
    await user.type(within(dialog).getByLabelText(/Código de verificación/), MOCK_TOTP_CODE);
    await user.click(within(dialog).getByRole("button", { name: "Autorizar" }));

    await waitFor(() => expect(navigateToMock).toHaveBeenCalledTimes(1));
    const [calledUrl] = navigateToMock.mock.calls[0]!;
    expect(calledUrl.startsWith("http://127.0.0.1:54321/callback?")).toBe(true);
    expect(calledUrl).toContain("code=");
    expect(dialog).not.toBeInTheDocument();
  });

  it("un TOTP incorrecto no navega y muestra el error dentro del prompt", async () => {
    const navigateToMock = vi.mocked(navigateTo);
    const user = userEvent.setup();
    renderPage();

    await user.click(await findEnabledAutorizarButton());
    const dialog = await screen.findByRole("dialog", { name: "Confirmar acceso" });
    await user.type(within(dialog).getByLabelText(/Código de verificación/), "000000");
    await user.click(within(dialog).getByRole("button", { name: "Autorizar" }));

    expect(await within(dialog).findByRole("alert")).toHaveTextContent(/código actual/i);
    expect(navigateToMock).not.toHaveBeenCalled();
  });

  it("Cancelar navega a redirect_to sin pedir TOTP", async () => {
    const navigateToMock = vi.mocked(navigateTo);
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole("button", { name: "Cancelar" }));

    await waitFor(() => expect(navigateToMock).toHaveBeenCalledTimes(1));
    const [calledUrl] = navigateToMock.mock.calls[0]!;
    expect(calledUrl).toContain("error=access_denied");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("un redirect_to con esquema inseguro no navega y muestra la tarjeta de error", async () => {
    const navigateToMock = vi.mocked(navigateTo);
    navigateToMock.mockReturnValueOnce(false);
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole("button", { name: "Cancelar" }));

    await waitFor(() => expect(navigateToMock).toHaveBeenCalledTimes(1));
    expect(
      await screen.findByText(/No hemos podido volver a tu terminal automáticamente/, { exact: false }),
    ).toBeInTheDocument();
    expect(screen.queryByText("Listo, vuelve a tu terminal.")).not.toBeInTheDocument();
  });

  describe("prueba de presencia federada (contracts/federated-login.md §2 y §4)", () => {
    it("con identificación fresca, Autorizar navega en un solo clic sin abrir ningún prompt", async () => {
      const navigateToMock = vi.mocked(navigateTo);
      server.use(
        http.post(`${API_BASE}/mcp-oauth/consent/:txnId/approve`, ({ params }) => {
          const result = approveMockConsent(params.txnId as string);
          if (!result) return HttpResponse.json({ error: { code: "TXN_EXPIRED", message: "caducada" } }, { status: 410 });
          return HttpResponse.json(result);
        }),
      );
      const user = userEvent.setup();
      renderPage();

      await user.click(await findEnabledAutorizarButton());

      await waitFor(() => expect(navigateToMock).toHaveBeenCalledTimes(1));
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });

    it("`methods: [\"federated\"]` pinta solo el botón «Confirmar con Google», sin campo de código", async () => {
      setMockFederatedLoginAvailable(true);
      respondReauthRequired(["federated"]);
      const user = userEvent.setup();
      renderPage();

      await user.click(await findEnabledAutorizarButton());
      const dialog = await screen.findByRole("dialog", { name: "Confirmar acceso" });

      expect(within(dialog).getByRole("button", { name: "Confirmar con Google" })).toBeInTheDocument();
      expect(within(dialog).queryByLabelText(/Código de verificación/)).not.toBeInTheDocument();
    });

    it("`methods: [\"totp\", \"federated\"]` pinta ambas opciones", async () => {
      setMockFederatedLoginAvailable(true);
      respondReauthRequired(["totp", "federated"]);
      const user = userEvent.setup();
      renderPage();

      await user.click(await findEnabledAutorizarButton());
      const dialog = await screen.findByRole("dialog", { name: "Confirmar acceso" });

      expect(within(dialog).getByRole("button", { name: "Confirmar con Google" })).toBeInTheDocument();
      expect(within(dialog).getByLabelText(/Código de verificación/)).toBeInTheDocument();
    });

    it("«Confirmar con Google» arranca `POST /auth/federated/start` con el `txn_id` y navega", async () => {
      const navigateToMock = vi.mocked(navigateTo);
      setMockFederatedLoginAvailable(true);
      respondReauthRequired(["federated"]);
      const user = userEvent.setup();
      renderPage();

      await user.click(await findEnabledAutorizarButton());
      const dialog = await screen.findByRole("dialog", { name: "Confirmar acceso" });
      await user.click(within(dialog).getByRole("button", { name: "Confirmar con Google" }));

      await waitFor(() => expect(navigateToMock).toHaveBeenCalledTimes(1));
      const [calledUrl] = navigateToMock.mock.calls[0]!;
      expect(calledUrl).toContain(`txn_id=${MOCK_CONSENT_TXN_ID}`);
    });

    it("con `session.fresh_identification_until` en el futuro, el prompt muestra «Identificado hasta las HH:MM»", async () => {
      const freshUntil = new Date(Date.now() + 3 * 60_000).toISOString();
      setMockFreshIdentificationUntil(freshUntil);
      respondReauthRequired(["totp", "federated"]);
      const user = userEvent.setup();
      renderPage();

      await user.click(await findEnabledAutorizarButton());
      const dialog = await screen.findByRole("dialog", { name: "Confirmar acceso" });

      expect(within(dialog).getByText(/Identificado hasta las \d{2}:\d{2}/)).toBeInTheDocument();
    });

    it("un fallo alcanzable al arrancar el salto federado muestra «Volver a intentarlo» y «Cancelar la solicitud»", async () => {
      const navigateToMock = vi.mocked(navigateTo);
      // `federated_login_available: true` (el dueño SÍ puede usar Google, methods lo confirma):
      // la carrera es que `/auth/federated/start` falla en el instante exacto del clic -- aquí,
      // el interruptor se apaga a mitad de camino (Decisión F: router sin montar ⇒ 404, la única
      // salida alcanzable de esta ruta; el 503 `FEDERATED_UNAVAILABLE` del contrato está tachado).
      setMockFederatedLoginAvailable(true);
      respondReauthRequired(["federated"]);
      server.use(
        http.post(`${API_BASE}/auth/federated/start`, () =>
          HttpResponse.json({ error: { code: "NOT_FOUND", message: "No disponible." } }, { status: 404 }),
        ),
      );
      const user = userEvent.setup();
      renderPage();

      await user.click(await findEnabledAutorizarButton());
      const dialog = await screen.findByRole("dialog", { name: "Confirmar acceso" });
      await user.click(within(dialog).getByRole("button", { name: "Confirmar con Google" }));

      expect(await screen.findByRole("button", { name: "Volver a intentarlo" })).toBeInTheDocument();
      const retryScreenCancel = screen.getByRole("button", { name: "Cancelar la solicitud" });
      expect(navigateToMock).not.toHaveBeenCalled();

      await user.click(retryScreenCancel);

      await waitFor(() => expect(navigateToMock).toHaveBeenCalledTimes(1));
      expect(navigateToMock.mock.calls[0]![0]).toContain("error=access_denied");
    });
  });
});

describe("ConsentimientoPage — `?federated_error=` (contracts/federated-login.md §1, FR-113)", () => {
  beforeEach(() => {
    setMockSessionForTests(true);
    resetMcpOauthFixtures();
  });

  afterEach(() => {
    setMockFederatedLoginAvailable(false);
    setMockFreshIdentificationUntil(null);
  });

  it.each([
    ["identity_mismatch", /esa no es la cuenta con la que entraste/i],
    ["provider_unavailable", /google no responde ahora mismo/i],
    // Claves heredadas de `Object.prototype` y un código inventado: caen al genérico, nunca
    // renderizan un objeto (`Object.hasOwn`, no `in` ni acceso directo por índice).
    ["__proto__", /no se ha podido entrar con google/i],
    ["constructor", /no se ha podido entrar con google/i],
    ["algo_inventado", /no se ha podido entrar con google/i],
  ])("pinta `%s` en un `role=\"alert\"` y limpia la URL, sin bloquear la pantalla", async (code, expectedMessage) => {
    renderPage(`/oauth/autorizar?txn=${MOCK_CONSENT_TXN_ID}&federated_error=${code}`);

    expect(await screen.findByRole("alert")).toHaveTextContent(expectedMessage);
    expect(window.location.search).toBe("");
    expect(await screen.findByText("Claude Code", { exact: false })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Autorizar" })).toBeInTheDocument();
  });

  it("sin `federated_error` en la URL no pinta ningún alert", async () => {
    renderPage();

    await screen.findByText("Claude Code", { exact: false });
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});
