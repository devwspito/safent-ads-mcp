import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { beforeEach, describe, expect, it } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { http, HttpResponse } from "msw";
import { server } from "@/mocks/server";
import { API_BASE } from "@/mocks/handlers/apiBase";
import { setMockSessionForTests } from "@/mocks/handlers";
import { buildCockpit, resetCockpitFixtures } from "@/mocks/fixtures/cockpit";
import { entityChildrenResponseSchema } from "@/api/schemas";
import { CampanasPage } from "./CampanasPage";

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      {/* "todas": estas pruebas cubren pausar/reanudar/eliminar, no el filtro de pestañas —
          en "Activas" (por defecto) la fila pausada saldría de la vista tras la propia acción. */}
      <MemoryRouter initialEntries={["/campanas?business_id=biz_ejemplo&estado=todas"]}>
        <Routes>
          <Route path="/campanas" element={<CampanasPage />} />
          <Route path="/ajustes" element={<div>Ajustes</div>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("CampanasPage", () => {
  beforeEach(() => {
    setMockSessionForTests(true);
    resetCockpitFixtures();
  });

  it("agrupa las campañas por plataforma y por cuenta, con la gramática de fila", async () => {
    renderPage();
    expect(await screen.findByText("Google Ads")).toBeInTheDocument();
    expect(screen.getByText("Meta Ads")).toBeInTheDocument();
    const row = (await screen.findByText("Display Retargeting")).closest("[data-entity-ref]") as HTMLElement;
    expect(within(row).getByText("Google")).toBeInTheDocument();
    expect(within(row).getByText("Negocio Ejemplo")).toBeInTheDocument();
    expect(within(row).getByText("Activa")).toBeInTheDocument();
    expect(within(row).getByText(/tope/)).toBeInTheDocument();
    expect(within(row).getByText(/hoy/)).toBeInTheDocument();
    expect(within(row).getByRole("button", { name: "Pausar" })).toBeEnabled();
  });

  it("pausar es optimista, abre el aviso «Hecho» con deshacer y la fila pasa a Reanudar", async () => {
    const user = userEvent.setup();
    renderPage();
    // La única cuenta de Meta del fixture es de solo lectura — usamos una campaña de Google.
    const row = (await screen.findByText("Búsqueda Marca")).closest("[data-entity-ref]") as HTMLElement;
    const pauseButton = within(row).getByRole("button", { name: "Pausar" });
    await user.click(pauseButton);

    expect(within(row).getByRole("button", { name: "Reanudar" })).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: "Deshacer" })).toBeInTheDocument();
  });

  it("eliminar pide confirmación en una hoja; «Pausar en su lugar» pausa sin borrar", async () => {
    const user = userEvent.setup();
    renderPage();
    const row = (await screen.findByText("Búsqueda Genérica")).closest("[data-entity-ref]") as HTMLElement;
    await user.click(within(row).getByRole("button", { name: /Más opciones/ }));
    await user.click(screen.getByRole("menuitem", { name: "Eliminar" }));

    const dialog = screen.getByRole("dialog", { name: /Eliminar «Búsqueda Genérica»/ });
    expect(within(dialog).getByText(/No se puede deshacer/)).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Pausar en su lugar" }));

    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    await waitFor(() => expect(within(row).getByRole("button", { name: "Reanudar" })).toBeInTheDocument());
  });

  it("eliminar de verdad quita la fila y avisa sin cuenta atrás", async () => {
    const user = userEvent.setup();
    renderPage();
    const row = (await screen.findByText("Búsqueda Competencia")).closest("[data-entity-ref]") as HTMLElement;
    await user.click(within(row).getByRole("button", { name: /Más opciones/ }));
    await user.click(screen.getByRole("menuitem", { name: "Eliminar" }));
    await user.click(screen.getByRole("button", { name: "Eliminar" }));

    await waitFor(() => expect(screen.queryByText("Búsqueda Competencia")).not.toBeInTheDocument());
    expect(await screen.findByText("«Búsqueda Competencia» eliminada.")).toBeInTheDocument();
  });

  it("una cuenta con el freno puesto deshabilita Pausar con el motivo a la vista", async () => {
    server.use(
      http.get(`${API_BASE}/kill-switch`, () =>
        HttpResponse.json({
          items: [{ brake_id: "b1", scope_kind: "platform_account", scope_id: "google:100-000-0002", scope_label: "Cuenta", mode: "ALL", engaged_at: new Date().toISOString(), engaged_by: "owner", reason: null }],
          effective: { engaged: false, mode: null, scope_kind: null, scope_id: null, engaged_at: null, reason: null },
          by_account: [
            { platform_account_id: "google:100-000-0002", engaged: true, mode: "ALL", source_scope_kind: "platform_account" },
            { platform_account_id: "meta:act_100000000000001", engaged: false, mode: null, source_scope_kind: null },
          ],
        }),
      ),
    );
    renderPage();
    const row = (await screen.findByText("Búsqueda Marca")).closest("[data-entity-ref]") as HTMLElement;
    const pauseButton = within(row).getByRole("button", { name: "Pausar" });
    expect(pauseButton).toBeDisabled();
    expect(within(row).getByText("Los cambios de esta cuenta están parados.")).toBeInTheDocument();
  });

  // Hotfix 0.2.20 Bug C: `/portfolio`/`/cockpit` used to carry the internal
  // account UUID while `/platform-accounts` carries the canonical ref, so a
  // row whose account could not be matched simply vanished from the page.
  it("una fila cuyo platform_account_id no coincide con ninguna cuenta se muestra bajo «Cuenta no identificada»", async () => {
    server.use(
      http.get(`${API_BASE}/cockpit`, ({ request }) => {
        const url = new URL(request.url);
        const businessId = url.searchParams.get("business_id") ?? "biz_ejemplo";
        const view = buildCockpit(businessId, "today");
        const [first, ...rest] = view.rows;
        return HttpResponse.json({
          ...view,
          rows: first ? [{ ...first, platform_account_id: "google:no-existe-999" }, ...rest] : view.rows,
        });
      }),
    );
    renderPage();

    // Aparece dos veces a propósito: el título de la sección y el nombre de cuenta de la propia
    // fila (CampaignRow ya enseña "Cuenta no identificada" en su línea de plataforma/cuenta).
    await waitFor(() => expect(screen.getAllByText("Cuenta no identificada").length).toBeGreaterThanOrEqual(2));
    const [heading] = screen.getAllByText("Cuenta no identificada");
    const section = heading!.closest("section") as HTMLElement;
    expect(within(section).getAllByRole("button", { name: /Pausar|Reanudar/ }).length).toBeGreaterThan(0);
  });

  it("buscar filtra por nombre sin pedir nada al servidor", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("Display Retargeting");
    await user.type(screen.getByLabelText("Buscar campaña"), "genérica");
    expect(screen.queryByText("Display Retargeting")).not.toBeInTheDocument();
    expect(await screen.findByText("Búsqueda Genérica")).toBeInTheDocument();
  });
});

describe("campaign drill-down uses the real REST contract", () => {
  const wire = JSON.parse(readFileSync(resolve(process.cwd(), "../tests/contracts/entity-children.json"), "utf8"));

  beforeEach(() => {
    setMockSessionForTests(true);
    resetCockpitFixtures();
  });

  it("accepts backend metadata and renders a leaf without fabricating zero spend", async () => {
    expect(entityChildrenResponseSchema.parse(wire)).toEqual(wire);
    server.use(http.get("*/api/v1/entities/:entityRef/children", () => HttpResponse.json(wire)));
    const user = userEvent.setup();
    renderPage();
    const row = (await screen.findByText("Búsqueda Marca")).closest("[data-entity-ref]") as HTMLElement;
    await user.click(within(row).getByRole("button", { name: "Ver detalle de Búsqueda Marca" }));
    expect(await screen.findByText("Anuncio 1")).toBeInTheDocument();
    expect(screen.queryByText("No se pudo cargar")).not.toBeInTheDocument();
  });

  it("renders a verified empty leaf as empty, not a load error", async () => {
    server.use(http.get("*/api/v1/entities/:entityRef/children", () => HttpResponse.json({ ...wire, items: [] })));
    const user = userEvent.setup();
    renderPage();
    const row = (await screen.findByText("Búsqueda Marca")).closest("[data-entity-ref]") as HTMLElement;
    await user.click(within(row).getByRole("button", { name: "Ver detalle de Búsqueda Marca" }));
    expect(await screen.findByText("Sin grupos de anuncios todavía.")).toBeInTheDocument();
    expect(screen.queryByText("No se pudo cargar")).not.toBeInTheDocument();
  });
});

describe("CampanasPage — los cuatro estados de pantalla (design.md §13.2)", () => {
  beforeEach(() => {
    setMockSessionForTests(true);
    resetCockpitFixtures();
  });

  it("cargando: esqueleto visible, nada más", async () => {
    server.use(http.get(`${API_BASE}/cockpit`, () => new Promise(() => {})));
    renderPage();
    expect(await screen.findByRole("status", { name: "Cargando datos…" })).toBeInTheDocument();
    expect(screen.queryByText("Google Ads")).not.toBeInTheDocument();
  });

  it("vacío sin cuentas conectadas: título, frase y la acción que resuelve", async () => {
    server.use(http.get(`${API_BASE}/platform-accounts`, () => HttpResponse.json({ items: [] })));
    renderPage();
    expect(await screen.findByText("Todavía no hay campañas que mostrar")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Conectar una cuenta" })).toBeInTheDocument();
  });

  it("error de lectura: bloque local con Reintentar, la cabecera sigue operable", async () => {
    server.use(http.get(`${API_BASE}/cockpit`, () => HttpResponse.json({ error: { code: "FAILED", message: "fallo" } }, { status: 500 })));
    renderPage();
    expect(await screen.findByRole("button", { name: "Reintentar" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Campañas", level: 1 })).toBeInTheDocument();
  });

  it("datos: al menos una fila real agrupada por plataforma y cuenta", async () => {
    renderPage();
    expect(await screen.findByText("Google Ads")).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "Pausar" }).length).toBeGreaterThan(0);
  });
});

function renderAt(path: string) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/campanas" element={<CampanasPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("CampanasPage — pestañas Activas/Pausadas/Todas", () => {
  beforeEach(() => {
    setMockSessionForTests(true);
    resetCockpitFixtures();
  });

  it("por defecto aterriza en Activas con la cuenta correcta y sincroniza ?estado= al cambiar", async () => {
    const user = userEvent.setup();
    renderAt("/campanas?business_id=biz_ejemplo");
    await screen.findByText("Display Retargeting");
    const activasTab = screen.getByRole("tab", { name: /Activas/ });
    await waitFor(() => expect(activasTab).toHaveAttribute("aria-selected", "true"));
    expect(within(activasTab).getByText("6")).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /Pausadas/ })).toHaveTextContent("1");
    expect(screen.getByRole("tab", { name: /Todas/ })).toHaveTextContent("8");
    expect(screen.getByText("Display Retargeting")).toBeInTheDocument();
    expect(screen.queryByText("Retargeting frío")).not.toBeInTheDocument();

    await user.click(screen.getByRole("tab", { name: /Pausadas/ }));
    expect(await screen.findByText("Retargeting frío")).toBeInTheDocument();
    expect(screen.queryByText("Display Retargeting")).not.toBeInTheDocument();
  });

  it("respeta ?estado=todas al aterrizar y enseña las ocho campañas", async () => {
    renderAt("/campanas?business_id=biz_ejemplo&estado=todas");
    expect(await screen.findByText("Retargeting frío")).toBeInTheDocument();
    expect(screen.getByText("Display Retargeting")).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /Todas/ })).toHaveAttribute("aria-selected", "true");
  });

  it("pestaña vacía: título contextual y «Ver todas» cambia a Todas", async () => {
    server.use(
      http.get(`${API_BASE}/cockpit`, () => {
        const cockpit = buildCockpit("biz_ejemplo");
        return HttpResponse.json({ ...cockpit, rows: cockpit.rows.map((row) => ({ ...row, status: row.status === "PAUSED" ? "ACTIVE" : row.status })) });
      }),
    );
    const user = userEvent.setup();
    renderAt("/campanas?business_id=biz_ejemplo&estado=pausadas");
    expect(await screen.findByText("Ninguna campaña pausada")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Ver todas" }));
    expect(await screen.findByText("Retargeting frío")).toBeInTheDocument();
  });
});
