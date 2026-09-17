import { beforeEach, describe, expect, it } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { http, HttpResponse } from "msw";
import { server } from "@/mocks/server";
import { API_BASE } from "@/mocks/handlers/apiBase";
import { setMockSessionForTests } from "@/mocks/handlers";
import { getPackageDetail, resetPackagesFixtures } from "@/mocks/fixtures/packages";
import { PackagePreviewPage } from "./PackagePreviewPage";

function renderPage(packageId = "pkg_meta_001") {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[`/propuestas/paquete/${packageId}?business_id=biz_ejemplo`]}>
        <Routes>
          <Route path="/propuestas/paquete/:packageId" element={<PackagePreviewPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("PackagePreviewPage — datos (contracts/api.md §2, §7)", () => {
  beforeEach(() => {
    setMockSessionForTests(true);
    resetPackagesFixtures();
  });

  it("Meta: dos conjuntos, cuatro anuncios con imagen, orden fijo del detalle", async () => {
    renderPage("pkg_meta_001");
    expect(await screen.findByText("Reserva de citas")).toBeInTheDocument();
    expect(screen.getAllByRole("figure")).toHaveLength(4);

    const headings = screen.getAllByRole("heading", { level: 3 }).map((node) => node.textContent);
    expect(headings).toEqual(["Qué se va a publicar", "A quién", "Dinero", "Por qué", "Qué pasará al aprobar"]);

    const [firstImage] = screen.getAllByRole("img");
    expect(firstImage).toHaveAttribute("alt", "Cita fuera de horario");
    expect(screen.getByText("20,00 €")).toBeInTheDocument();
    expect(screen.getByText("Te queda 1.240 € de tope este mes.")).toBeInTheDocument();
    expect(screen.getByText(/Se crearán 1 campaña, 2 conjuntos y 4 anuncios en Meta/)).toBeInTheDocument();
    expect(screen.getByText("«Hazme una campaña de reserva de citas veterinarias.»")).toBeInTheDocument();

    // §7 punto 6: plegado por defecto, rótulo exacto, cerrado no cuenta como visible.
    const details = screen.getByText("Ajustes que exige la plataforma").closest("details");
    expect(details).not.toHaveAttribute("open");
  });

  it("Google: un grupo, tres anuncios de sólo texto, palabras clave truncadas a 8 + «y N más»", async () => {
    renderPage("pkg_google_001");
    expect(await screen.findByText("Búsqueda Reservas")).toBeInTheDocument();
    // §7: cada tarjeta es un `figure` con `figcaption` aunque no lleve imagen (AdPreview.image = null).
    expect(screen.getAllByRole("figure")).toHaveLength(3);
    expect(screen.queryAllByRole("img")).toHaveLength(0);
    expect(screen.getAllByText("Anuncio de solo texto")).toHaveLength(3);
    expect(screen.getByText(/y 4 más$/)).toBeInTheDocument();
    expect(screen.getByText("Se compran 12 palabras clave.")).toBeInTheDocument();
    expect(screen.getByText("Sin datos de competencia.")).toBeInTheDocument();
  });
});

describe("PackagePreviewPage — Máximo Rendimiento sin anuncios (tasks.md T037)", () => {
  beforeEach(() => {
    setMockSessionForTests(true);
    resetPackagesFixtures();
  });

  it("muestra_el_grupo_de_recursos_sin_decir_asset_group", async () => {
    renderPage("pkg_google_pmax_001");
    expect(await screen.findByText("Máximo rendimiento reservas")).toBeInTheDocument();

    // El grupo de recursos ES el anuncio (`ads: []`): ni sección vacía ni tarjeta rota.
    expect(screen.getAllByRole("figure")).toHaveLength(1);
    expect(screen.getAllByRole("img")).toHaveLength(3);
    expect(screen.getByText("Reserva ya")).toBeInTheDocument();
    expect(screen.getByText("https://negocio-ejemplo.es/reservar")).toBeInTheDocument();
    expect(screen.getAllByText(/Grupo de recursos/).length).toBeGreaterThan(0);

    const bodyText = document.body.textContent?.toLowerCase() ?? "";
    expect(bodyText).not.toContain("asset group");
    expect(bodyText).not.toContain("pmax");
    expect(bodyText).not.toContain("opt-out");
  });

  it("no_pliega_el_canal_la_puja_ni_la_meta", async () => {
    renderPage("pkg_google_pmax_001");
    await screen.findByText("Máximo rendimiento reservas");

    // Casilla 23 (threat-model.md §8): canal, puja con objetivo y «Optimiza para» van siempre
    // desplegados, nunca detrás del `<details>` de §7 punto 6 (que sigue igual para Meta/Búsqueda).
    expect(screen.getByText("Máximo rendimiento")).toBeInTheDocument();
    expect(screen.getByText(/Maximizar conversiones, objetivo/)).toBeInTheDocument();
    expect(screen.getByText(/Optimiza para: Conseguir reservas/)).toBeInTheDocument();

    const details = screen.getByText("Ajustes que exige la plataforma").closest("details");
    expect(details).not.toHaveAttribute("open");
    expect(within(details as HTMLElement).queryByText("Máximo rendimiento")).not.toBeInTheDocument();
  });

  it("el mismo flujo de aprobar/rechazar sigue disponible (Aprobar y publicar vive en la fila de Propuestas, T052)", async () => {
    renderPage("pkg_google_pmax_001");
    const detail = getPackageDetail("pkg_google_pmax_001")!;
    expect(await screen.findByText("Máximo rendimiento reservas")).toBeInTheDocument();
    expect(detail.approvable).toBe(true);
    expect(detail.on_approve.activates).toBe(true);
  });

  it("«Qué pasará al aprobar» habla de grupos de recursos e imágenes, nunca de «0 anuncios»", async () => {
    renderPage("pkg_google_pmax_001");
    await screen.findByText("Máximo rendimiento reservas");

    const sentence = screen.getByText(/Se crearán 1 campaña y 1 grupo de recursos con 3 imágenes en/);
    expect(sentence).toBeInTheDocument();
    expect(sentence.textContent).not.toContain("anuncios");
  });
});

describe("PackagePreviewPage — los cuatro estados de pantalla (design.md §2.5, §13.2)", () => {
  beforeEach(() => {
    setMockSessionForTests(true);
    resetPackagesFixtures();
  });

  it("cargando: esqueleto visible, nada más", async () => {
    server.use(http.get(`${API_BASE}/packages/:id`, () => new Promise(() => {})));
    renderPage();
    expect(await screen.findByRole("status", { name: "Cargando datos…" })).toBeInTheDocument();
    expect(screen.queryByText("Reserva de citas")).not.toBeInTheDocument();
  });

  it("vacío: design.md §7 dice que no aplica — un paquete sin conjuntos no revienta el panel", async () => {
    const base = getPackageDetail("pkg_meta_001")!;
    server.use(
      http.get(`${API_BASE}/packages/:id`, () => HttpResponse.json({ ...base, campaign: { ...base.campaign, ad_sets: [] } })),
    );
    renderPage();
    expect(await screen.findByText("Reserva de citas")).toBeInTheDocument();
    expect(screen.queryByRole("figure")).not.toBeInTheDocument();
    expect(screen.getByText("Dinero")).toBeInTheDocument();
  });

  it("error de lectura: bloque local con Reintentar", async () => {
    server.use(http.get(`${API_BASE}/packages/:id`, () => HttpResponse.json({ error: { code: "NOT_FOUND", message: "El paquete ya no existe." } }, { status: 404 })));
    renderPage();
    expect(await screen.findByRole("button", { name: "Reintentar" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Paquete (comprobación)", level: 1 })).toBeInTheDocument();
  });
});

describe("PackagePreviewPage — «Cambiar imagen» y «Regenerar» (contracts/api.md §6)", () => {
  beforeEach(() => {
    setMockSessionForTests(true);
    resetPackagesFixtures();
  });

  it("«Cambiar imagen» abre candidatos y aplicar uno cambia el `alt` del anuncio", async () => {
    const user = userEvent.setup();
    renderPage("pkg_meta_001");
    const card = (await screen.findByAltText("Cita fuera de horario")).closest("figure") as HTMLElement;

    await user.click(within(card).getByRole("button", { name: "Cambiar imagen" }));
    const useButtons = await within(card).findAllByRole("button", { name: "Usar esta imagen" });
    await user.click(useButtons[0]!);

    await waitFor(() => expect(within(card).getByAltText("Cita fuera de horario · imagen alternativa")).toBeInTheDocument());
  });

  it("«Regenerar» encadena regenerate → creative-jobs → PATCH y aplica la imagen nueva", async () => {
    const user = userEvent.setup();
    renderPage("pkg_meta_001");
    const card = (await screen.findByAltText("Primera visita")).closest("figure") as HTMLElement;

    await user.click(within(card).getByRole("button", { name: "Regenerar" }));

    await waitFor(() => expect(within(card).getByAltText("Primera visita · regenerada")).toBeInTheDocument());
    expect(within(card).getByRole("button", { name: "Regenerar" })).not.toBeDisabled();
  });

  it("un anuncio de sólo texto sin `can_replace_image`/`can_regenerate` no muestra esas acciones activas", async () => {
    renderPage("pkg_google_001");
    await screen.findByText("Búsqueda Reservas");
    const buttons = screen.getAllByRole("button", { name: "Cambiar imagen" });
    expect(buttons.every((button) => (button as HTMLButtonElement).disabled)).toBe(true);
  });
});
