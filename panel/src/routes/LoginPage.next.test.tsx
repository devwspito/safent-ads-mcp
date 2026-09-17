import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { http, HttpResponse, delay } from "msw";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { API_BASE } from "@/mocks/handlers/apiBase";
import { MOCK_PASSWORD } from "@/mocks/fixtures/businesses";
import { setMockFederatedLoginAvailable, setMockSessionForTests } from "@/mocks/handlers";
import { server } from "@/mocks/server";
import { navigateTo } from "@/utils/navigation";
import { LoginPage } from "./LoginPage";

vi.mock("@/utils/navigation", () => ({ navigateTo: vi.fn(() => true) }));

function Stub({ label }: { label: string }) {
  return <h1>{label}</h1>;
}

function renderLoginAt(path: string) {
  window.history.pushState({}, "", path);
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/login" element={<LoginPage />} />
          <Route path="/propuestas" element={<Stub label="Propuestas" />} />
          <Route path="/oauth/autorizar" element={<Stub label="Consentimiento" />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

async function logIn() {
  const user = userEvent.setup();
  await user.type(screen.getByLabelText("Correo"), "owner@negocio-ejemplo.es");
  await user.type(screen.getByLabelText("Contraseña"), MOCK_PASSWORD);
  await user.click(screen.getByRole("button", { name: "Continuar" }));
}

describe("LoginPage — `?next=`", () => {
  beforeEach(() => setMockSessionForTests(false));

  it("vuelve a `next` tras entrar cuando es una ruta relativa del panel", async () => {
    renderLoginAt("/login?next=%2Foauth%2Fautorizar%3Ftxn%3Dabc");
    await logIn();

    expect(await screen.findByRole("heading", { name: "Consentimiento" })).toBeInTheDocument();
  });

  it("por defecto va a /propuestas sin `next`", async () => {
    renderLoginAt("/login");
    await logIn();

    expect(await screen.findByRole("heading", { name: "Propuestas" })).toBeInTheDocument();
  });

  it("rechaza un `next` absoluto de otro origen", async () => {
    renderLoginAt(`/login?next=${encodeURIComponent("https://malo.example")}`);
    await logIn();

    expect(await screen.findByRole("heading", { name: "Propuestas" })).toBeInTheDocument();
  });

  it("rechaza un `next` protocolo-relativo (`//host`)", async () => {
    renderLoginAt(`/login?next=${encodeURIComponent("//malo.example")}`);
    await logIn();

    expect(await screen.findByRole("heading", { name: "Propuestas" })).toBeInTheDocument();
  });

  it("rechaza un `next` que no empieza por `/` y también redirige ya con sesión activa", async () => {
    setMockSessionForTests(true);
    renderLoginAt(`/login?next=${encodeURIComponent("propuestas")}`);

    expect(await screen.findByRole("heading", { name: "Propuestas" })).toBeInTheDocument();
  });
});

describe("LoginPage — «Entrar con Google» (contracts/federated-login.md §4)", () => {
  beforeEach(() => {
    setMockSessionForTests(false);
    setMockFederatedLoginAvailable(false);
  });

  afterEach(() => setMockFederatedLoginAvailable(false));

  it("pinta «Entrar con Google» cuando `GET /auth/federated/status` responde 200", async () => {
    setMockFederatedLoginAvailable(true);
    renderLoginAt("/login");

    expect(await screen.findByRole("button", { name: "Entrar con Google" })).toBeInTheDocument();
  });

  it("no pinta «Entrar con Google» cuando `GET /auth/federated/status` responde 404", async () => {
    server.use(
      http.get(`${API_BASE}/auth/federated/status`, async () => {
        await delay(20);
        return HttpResponse.json({ error: { code: "NOT_FOUND", message: "No disponible." } }, { status: 404 });
      }),
    );
    renderLoginAt("/login");
    await screen.findByLabelText("Correo");
    await new Promise((resolve) => setTimeout(resolve, 40));

    expect(screen.queryByRole("button", { name: "Entrar con Google" })).not.toBeInTheDocument();
  });

  it("mientras `GET /auth/federated/status` está cargando, el botón no se pinta", async () => {
    server.use(
      http.get(`${API_BASE}/auth/federated/status`, async () => {
        await delay(50);
        return HttpResponse.json({ available: true });
      }),
    );
    renderLoginAt("/login");
    await screen.findByLabelText("Correo");

    expect(screen.queryByRole("button", { name: "Entrar con Google" })).not.toBeInTheDocument();
    expect(await screen.findByRole("button", { name: "Entrar con Google" })).toBeInTheDocument();
  });

  it.each([
    ["denied", /no hemos podido entrar con esa cuenta/i],
    ["expired", /la solicitud ha caducado/i],
    ["provider_unavailable", /google no responde ahora mismo/i],
    ["owner_already_bound", /esta instalación ya tiene dueño\. pide acceso a quien la administra\./i],
    ["identity_mismatch", /esa no es la cuenta con la que entraste/i],
    // Claves heredadas de `Object.prototype` y un código inventado: los tres caen al genérico,
    // nunca a un objeto (`Object.hasOwn`, no `in` ni acceso directo por índice).
    ["__proto__", /no se ha podido entrar con google/i],
    ["constructor", /no se ha podido entrar con google/i],
    ["algo_inventado", /no se ha podido entrar con google/i],
  ])("`?federated_error=%s` se pinta en el alert y se limpia de la URL", async (code, expectedMessage) => {
    renderLoginAt(`/login?federated_error=${code}`);

    expect(await screen.findByRole("alert")).toHaveTextContent(expectedMessage);
    expect(window.location.search).toBe("");
  });

  it("el formulario de correo y contraseña sigue usable con `federated_error` en la URL", async () => {
    renderLoginAt("/login?federated_error=denied");
    await screen.findByRole("alert");

    const emailField = screen.getByLabelText("Correo");
    const passwordField = screen.getByLabelText("Contraseña");
    expect(emailField).toBeEnabled();
    expect(passwordField).toBeEnabled();
    expect(screen.getByRole("button", { name: "Continuar" })).toBeEnabled();
  });

  it("conserva el `?next=` (el `txn`) al entrar con Google", async () => {
    setMockFederatedLoginAvailable(true);
    renderLoginAt("/login?next=%2Foauth%2Fautorizar%3Ftxn%3Dabc");
    const user = userEvent.setup();

    const googleButton = await screen.findByRole("button", { name: "Entrar con Google" });
    await user.click(googleButton);

    await waitFor(() => expect(navigateTo).toHaveBeenCalledTimes(1));
    const [calledUrl] = vi.mocked(navigateTo).mock.calls[0]!;
    expect(calledUrl).toContain("txn_id=abc");
  });
});
