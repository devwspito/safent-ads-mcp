import { describe, expect, it, beforeEach } from "vitest";
import { http, HttpResponse } from "msw";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { API_BASE } from "@/mocks/handlers/apiBase";
import { resetBrandFixtures, setMockSessionForTests } from "@/mocks/handlers";
import { server } from "@/mocks/server";
import { AjustesNegocioPage } from "./AjustesNegocioPage";

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/ajustes/negocio?business_id=biz_ejemplo"]}>
        <AjustesNegocioPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("AjustesNegocioPage", () => {
  beforeEach(() => {
    setMockSessionForTests(true);
    resetBrandFixtures();
  });

  it("vuelve a Ajustes y muestra Marca, Ofertas y economía, Calendario de eventos y Conversiones, en ese orden", async () => {
    renderPage();

    expect(screen.getByRole("link", { name: "‹ Ajustes" })).toHaveAttribute("href", "/ajustes");
    expect(await screen.findByRole("heading", { name: "Tu negocio", level: 1 })).toBeInTheDocument();

    const marca = await screen.findByRole("heading", { name: "Marca" });
    const economia = screen.getByRole("heading", { name: "Ofertas y economía" });
    const calendario = screen.getByRole("heading", { name: "Calendario de eventos" });
    const conversiones = screen.getByRole("heading", { name: "Conversiones" });

    const order = [marca, economia, calendario, conversiones];
    for (let i = 0; i < order.length - 1; i += 1) {
      expect(order[i]!.compareDocumentPosition(order[i + 1]!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    }
  });
});

describe("AjustesNegocioPage — Calendario de eventos", () => {
  beforeEach(() => setMockSessionForTests(true));

  it("añadir un evento válido lo persiste vía MSW y lo refleja en la lista", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText(/Secundaria — Madrid/);

    await user.type(screen.getByLabelText("Nombre"), "Lanzamiento otoño");
    await user.selectOptions(screen.getByLabelText("Tipo"), "launch");
    fireEvent.change(screen.getByLabelText("Ventana — desde"), { target: { value: "2026-09-01" } });
    fireEvent.change(screen.getByLabelText("Ventana — hasta"), { target: { value: "2026-09-30" } });

    await user.click(screen.getByRole("button", { name: "Añadir evento" }));

    await waitFor(() => expect(screen.getByText("Evento añadido.")).toBeInTheDocument());
    expect(await screen.findByText(/Lanzamiento otoño/)).toBeInTheDocument();
    expect(screen.getByLabelText("Nombre")).toHaveValue("");
  });

  it("una ventana invertida bloquea el envío con el error del cliente, sin llamar al servidor", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText(/Secundaria — Madrid/);

    await user.type(screen.getByLabelText("Nombre"), "Evento con ventana invertida");
    fireEvent.change(screen.getByLabelText("Ventana — desde"), { target: { value: "2026-09-30" } });
    fireEvent.change(screen.getByLabelText("Ventana — hasta"), { target: { value: "2026-09-01" } });

    expect(await screen.findByText("La ventana debe abrir antes de cerrar.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Añadir evento" })).toBeDisabled();
  });

  it("un servidor que devuelve 422 muestra el mensaje sin romper el formulario", async () => {
    server.use(
      http.post(`${API_BASE}/calendar-events`, () =>
        HttpResponse.json({ error: { code: "VALIDATION_ERROR", message: "El tipo de evento no es válido." } }, { status: 422 }),
      ),
    );

    const user = userEvent.setup();
    renderPage();
    await screen.findByText(/Secundaria — Madrid/);

    await user.type(screen.getByLabelText("Nombre"), "Evento rechazado por el servidor");
    fireEvent.change(screen.getByLabelText("Ventana — desde"), { target: { value: "2026-09-01" } });
    fireEvent.change(screen.getByLabelText("Ventana — hasta"), { target: { value: "2026-09-30" } });
    await user.click(screen.getByRole("button", { name: "Añadir evento" }));

    expect(await screen.findByText("El tipo de evento no es válido.")).toBeInTheDocument();
  });

  it("eliminar un evento pide la palabra ELIMINAR antes de habilitar la confirmación", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText(/Secundaria — Madrid/);

    await user.click(screen.getByRole("button", { name: "Eliminar evento: Secundaria — Madrid" }));
    const dialog = await screen.findByRole("dialog", { name: 'Eliminar "Secundaria — Madrid"' });
    const confirmButton = within(dialog).getByRole("button", { name: "Eliminar" });
    expect(confirmButton).toBeDisabled();

    await user.type(within(dialog).getByLabelText("Escribe ELIMINAR para confirmar"), "ELIMINAR");
    expect(confirmButton).toBeEnabled();

    await user.click(confirmButton);
    await waitFor(() => expect(screen.queryByText(/Secundaria — Madrid/)).not.toBeInTheDocument());
  });

  it("un servidor que rechaza el borrado muestra el motivo por clase de error, sin perder el resto de la lista", async () => {
    server.use(
      http.delete(`${API_BASE}/calendar-events/:id`, () =>
        HttpResponse.json({ error: { code: "CONFLICT", message: "conflicto" } }, { status: 409 }),
      ),
    );

    const user = userEvent.setup();
    renderPage();
    await screen.findByText(/Primaria — Valencia/);

    await user.click(screen.getByRole("button", { name: "Eliminar evento: Primaria — Valencia" }));
    const dialog = await screen.findByRole("dialog", { name: 'Eliminar "Primaria — Valencia"' });
    await user.type(within(dialog).getByLabelText("Escribe ELIMINAR para confirmar"), "ELIMINAR");
    await user.click(within(dialog).getByRole("button", { name: "Eliminar" }));

    expect(await screen.findByText(/cambió mientras tanto/)).toBeInTheDocument();
    expect(screen.getByText(/Primaria — Valencia/)).toBeInTheDocument();
  });
});

describe("AjustesNegocioPage — Marca", () => {
  beforeEach(() => {
    setMockSessionForTests(true);
    resetBrandFixtures();
  });

  it("sin kit ni borrador todavía, muestra el estado incompleto y el motivo", async () => {
    renderPage();
    expect(await screen.findByText("Incompleto")).toBeInTheDocument();
    expect(screen.getByText(/Todavía no hay ninguna identidad de marca/)).toBeInTheDocument();
  });

  it("rastrear el sitio web es el camino feliz: deja candidatos en el borrador", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByRole("heading", { name: "Marca" });

    await user.type(screen.getByLabelText("Sitio web del negocio (opcional)"), "https://negocio-ejemplo.es");
    await user.click(screen.getByRole("button", { name: "Rastrear" }));

    expect(await screen.findByText("Rastreo completado. Revisa los candidatos más abajo.")).toBeInTheDocument();
    expect(screen.getByText("borrador · sin confirmar")).toBeInTheDocument();
    expect(screen.getByText("Inter")).toBeInTheDocument();
  });

  it("un sitio inalcanzable explica el motivo del servidor y ofrece el camino manual", async () => {
    server.use(
      http.post(`${API_BASE}/brand/discover`, () =>
        HttpResponse.json(
          { error: { code: "DISCOVERY_UNREACHABLE", message: "No se pudo rastrear el sitio indicado." } },
          { status: 422 },
        ),
      ),
    );

    const user = userEvent.setup();
    renderPage();
    await screen.findByRole("heading", { name: "Marca" });

    await user.type(screen.getByLabelText("Sitio web del negocio (opcional)"), "https://sitio-caido.example");
    await user.click(screen.getByRole("button", { name: "Rastrear" }));

    expect(await screen.findByText("No se pudo rastrear el sitio indicado.")).toBeInTheDocument();
    const manualLink = screen.getByRole("link", { name: "Subir logotipos y tipografía a mano" });
    expect(manualLink).toHaveAttribute("href", "#brand-manual-upload");
    expect(screen.getByRole("heading", { name: "Subir a mano" })).toBeInTheDocument();
  });

  it("revisar el borrador y confirmar deja el kit de marca confirmado", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByRole("heading", { name: "Marca" });

    await user.type(screen.getByLabelText("Sitio web del negocio (opcional)"), "https://negocio-ejemplo.es");
    await user.click(screen.getByRole("button", { name: "Rastrear" }));
    await screen.findByText("borrador · sin confirmar");

    await user.click(screen.getByRole("checkbox", { name: /Logotipo \(vector\)/ }));
    await user.click(screen.getByRole("radio", { name: /Inter/ }));
    await user.selectOptions(screen.getAllByLabelText("Usar como")[0]!, "primary");

    await user.type(screen.getByLabelText("Nota de licencia de la tipografía"), "Google Fonts, licencia SIL Open Font");
    await user.type(screen.getByLabelText("Tono de voz"), "Cercano y claro, sin tecnicismos.");

    await user.click(screen.getByRole("button", { name: "Confirmar" }));

    expect(await screen.findByText("Marca confirmada.")).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByText("Incompleto")).not.toBeInTheDocument());
  });

  it("subir un archivo que supera el tope muestra el error del servidor", async () => {
    server.use(
      http.post(`${API_BASE}/brand/assets`, () =>
        HttpResponse.json(
          { error: { code: "VALIDATION_ERROR", message: "El fichero supera el tope de 10485760 bytes." } },
          { status: 422 },
        ),
      ),
    );

    const user = userEvent.setup();
    renderPage();
    const uploadSection = await screen.findByRole("heading", { name: "Subir a mano" });
    const file = new File(["contenido"], "logo.png", { type: "image/png" });

    await user.upload(within(uploadSection.closest("section")!).getByLabelText("Elegir archivo"), file);

    expect(await screen.findByText("El fichero supera el tope de 10485760 bytes.")).toBeInTheDocument();
  });
});

describe("AjustesNegocioPage — Conversiones", () => {
  beforeEach(() => setMockSessionForTests(true));

  it("muestra el CSV y la URL fija del webhook, que ya no viven en Ajustes", async () => {
    renderPage();
    expect(await screen.findByRole("heading", { name: "Conversiones" })).toBeInTheDocument();
    expect(screen.getByText(/Sube un CSV del CRM/)).toBeInTheDocument();
    expect(screen.getByText(`${window.location.origin}/api/v1/conversions/webhook`)).toBeInTheDocument();
  });
});
