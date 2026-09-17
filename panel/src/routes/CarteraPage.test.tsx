import { describe, expect, it, beforeEach } from "vitest";
import { http, HttpResponse } from "msw";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { API_BASE } from "@/mocks/handlers/apiBase";
import { setMockSessionForTests } from "@/mocks/handlers";
import { server } from "@/mocks/server";
import { CarteraPage } from "./CarteraPage";

function renderPage(initialEntries: string[] = ["/resultados?business_id=biz_ejemplo"]) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={initialEntries}>
        <CarteraPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("CarteraPage", () => {
  beforeEach(() => setMockSessionForTests(true));

  it("muestra las cuatro cifras de cabecera y las cuentas agrupadas por plataforma", async () => {
    renderPage();

    expect(await screen.findByText("Gasto")).toBeInTheDocument();
    expect(screen.getByText("Leads")).toBeInTheDocument();
    expect(screen.getByText("Coste por lead")).toBeInTheDocument();
    expect(screen.getByText("Ritmo del mes")).toBeInTheDocument();

    expect(await screen.findByText("Google Ads")).toBeInTheDocument();
    expect(screen.getByText("Meta Ads")).toBeInTheDocument();
    expect(screen.getAllByText("Negocio Ejemplo")).toHaveLength(2);
  });

  it("cambia el periodo y refleja la selección en los controles", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("Gasto");

    const button14d = screen.getByRole("button", { name: "14 días" });
    await user.click(button14d);

    await waitFor(() => expect(button14d).toHaveAttribute("aria-pressed", "true"));
  });

  it("a 30 días no inventa una diferencia: muestra la etiqueta de sin comparación", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("Gasto");
    await user.click(screen.getByRole("button", { name: "30 días" }));

    expect(await screen.findByText("Sin comparación disponible a 30 días")).toBeInTheDocument();
  });
});

describe("CarteraPage — los cuatro estados de pantalla (design.md §13.2)", () => {
  beforeEach(() => setMockSessionForTests(true));

  it("cargando: esqueleto visible, nada más", async () => {
    server.use(http.get(`${API_BASE}/portfolio`, () => new Promise(() => {})));
    renderPage();
    expect(await screen.findByRole("status", { name: "Cargando datos…" })).toBeInTheDocument();
    expect(screen.queryByText("Gasto")).not.toBeInTheDocument();
  });

  it("vacío sin cuentas conectadas: título, frase y la acción que resuelve", async () => {
    server.use(http.get(`${API_BASE}/platform-accounts`, () => HttpResponse.json({ items: [] })));
    renderPage();
    expect(await screen.findByText("Todavía no hay resultados que mostrar")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Conectar una cuenta" })).toBeInTheDocument();
  });

  it("error de lectura: bloque local con Reintentar, la cabecera sigue operable", async () => {
    server.use(http.get(`${API_BASE}/portfolio`, () => HttpResponse.json({ error: { code: "FAILED", message: "fallo" } }, { status: 500 })));
    renderPage();
    expect(await screen.findByRole("button", { name: "Reintentar" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Resultados", level: 1 })).toBeInTheDocument();
  });

  it("datos: las cuatro cifras y al menos una cuenta real", async () => {
    renderPage();
    expect(await screen.findByText("Gasto")).toBeInTheDocument();
    expect(screen.getByText("Google Ads")).toBeInTheDocument();
  });
});
