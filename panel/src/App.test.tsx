import { describe, expect, it, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { App } from "@/App";
import { setMockSessionForTests } from "@/mocks/handlers";
import { getResponse, http, HttpResponse } from "msw";
import { server } from "@/mocks/server";
import { API_BASE } from "@/mocks/handlers/apiBase";

function renderApp() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <App />
    </QueryClientProvider>,
  );
}

describe("App routing", () => {
  afterEach(() => document.querySelectorAll('meta[name="safent-ads-base-path"]').forEach(el => el.remove()));
  beforeEach(() => {
    setMockSessionForTests(false);
    window.history.pushState({}, "", "/");
  });

  it("redirects an unauthenticated visitor to /login", async () => {
    renderApp();
    await waitFor(() => expect(screen.getByRole("heading", { name: /safent ads/i })).toBeInTheDocument());
    expect(screen.getByLabelText(/correo/i)).toBeInTheDocument();
  });

  it("lands on /propuestas once authenticated and shows the four destinations in the fixed order", async () => {
    setMockSessionForTests(true);
    renderApp();

    const nav = await screen.findByRole("navigation", { name: "Navegación principal" });
    const hrefs = within(nav)
      .getAllByRole("link")
      .map((el) => el.getAttribute("href")?.split("?")[0]);
    expect(hrefs).toEqual(["/propuestas", "/campanas", "/resultados", "/ajustes"]);
    expect(within(nav).getByRole("link", { name: /Propuestas/ })).toBeInTheDocument();

    await waitFor(() => expect(screen.getByRole("heading", { name: "Propuestas", level: 1 })).toBeInTheDocument());
    expect(window.location.pathname).toBe("/propuestas");

    // 7 propuestas normales + 2 de creación de campaña real (companion 0.2.20) + 4 paquetes de
    // campaña de ejemplo (003-paquete-de-campana §T052; el cuarto es el de Máximo Rendimiento
    // sin anuncios de 005-google-campaign-types §T037).
    const propuestasLink = within(nav).getByRole("link", { name: /Propuestas/ });
    await waitFor(() => expect(within(propuestasLink).getByText("13")).toBeInTheDocument());
  });

  it("keeps embedded router and API requests under /ads using CSP-safe metadata", async () => {
    const meta = document.createElement("meta");
    meta.name = "safent-ads-base-path";
    meta.content = "/ads";
    document.head.append(meta);
    window.history.pushState({}, "", "/ads/");
    const requests: string[] = [];
    server.use(http.all("*/ads/api/v1/*", async ({ request }) => {
      requests.push(new URL(request.url).pathname);
      return await getResponse([...server.listHandlers()], new Request(request.url.replace("/ads/api/v1/", "/api/v1/")))
        ?? HttpResponse.json({}, { status: 404 });
    }));
    setMockSessionForTests(true);
    renderApp();

    await waitFor(() => expect(screen.getByRole("heading", { name: "Propuestas", level: 1 })).toBeInTheDocument());
    expect(window.location.pathname).toBe("/ads/propuestas");
    expect(requests).toContain("/ads/api/v1/auth/me");
    expect(screen.queryByLabelText(/contraseña/i)).not.toBeInTheDocument();
    const nav = screen.getByRole("navigation", { name: "Navegación principal" });
    expect(within(nav).getByRole("link", { name: /Ajustes/ })).toHaveAttribute("href", "/ads/ajustes");
  });

  it("jumps view with the `2` shortcut and opens the command palette with Ctrl+K", async () => {
    setMockSessionForTests(true);
    const user = userEvent.setup();
    renderApp();

    await waitFor(() => expect(screen.getByRole("heading", { name: "Propuestas", level: 1 })).toBeInTheDocument());

    await user.keyboard("2");
    await waitFor(() => expect(screen.getByRole("heading", { name: "Campañas", level: 1 })).toBeInTheDocument());

    await user.keyboard("{Control>}k{/Control}");
    expect(await screen.findByRole("dialog", { name: /buscar y saltar a una vista/i })).toBeInTheDocument();

    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  });

  it.each([
    ["/propuestas", "Propuestas"], ["/campanas", "Campañas"], ["/resultados", "Resultados"], ["/ajustes", "Ajustes"],
  ])("keeps the shared navigation and a named main view on %s", async (path, heading) => {
    setMockSessionForTests(true);
    window.history.pushState({}, "", path);
    renderApp();
    const main = await screen.findByRole("main");
    expect(await within(main).findByRole("heading", { name: heading, level: 1 })).toBeInTheDocument();
    expect(screen.getByRole("navigation", { name: "Navegación principal" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Parar cambios|Cambios parados/ })).toBeInTheDocument();
  });

  it.each([
    ["/cartera", "/resultados"], ["/cockpit", "/resultados"],
    ["/conexiones", "/ajustes"], ["/reglas", "/ajustes"],
    ["/senales", "/propuestas"], ["/creatividades", "/campanas"],
    ["/registro", "/propuestas/historial"],
  ])("redirects the retired route %s to %s", async (from, to) => {
    setMockSessionForTests(true);
    window.history.pushState({}, "", from);
    renderApp();
    await waitFor(() => expect(window.location.pathname).toBe(to));
  });

  it("a session network error offers retry instead of pretending the owner logged out", async () => {
    server.use(http.get(`${API_BASE}/auth/me`, () => HttpResponse.json({}, { status: 503 })));
    renderApp();
    expect(await screen.findByRole("alert")).toHaveTextContent(/servidor/);
    expect(screen.getByRole("button", { name: "Reintentar" })).toBeInTheDocument();
    expect(screen.queryByLabelText(/Contraseña/)).not.toBeInTheDocument();
  });

  it("search accepts unaccented names and remains accessible with zero results", async () => {
    setMockSessionForTests(true);
    const user = userEvent.setup();
    renderApp();
    await screen.findByRole("heading", { name: "Propuestas", level: 1 });
    await user.keyboard("{Control>}k{/Control}");
    const input = screen.getByRole("combobox", { name: "Buscar una vista" });
    await user.type(input, "resultados");
    expect(screen.getByRole("option", { name: /Resultados/ })).toBeInTheDocument();
    await user.clear(input);
    await user.type(input, "zzzz");
    expect(screen.getByRole("listbox")).toBeEmptyDOMElement();
    expect(screen.getByRole("status")).toHaveTextContent("Sin resultados");
    await user.keyboard("{ArrowDown}{ArrowUp}{Enter}");
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    await user.clear(input);
    await user.type(input, "ajustes{Enter}");
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(await screen.findByRole("heading", { name: "Ajustes", level: 1 })).toBeInTheDocument();
  });
});
