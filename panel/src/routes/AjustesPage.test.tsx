import { describe, expect, it, beforeEach } from "vitest";
import { http, HttpResponse } from "msw";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { API_BASE } from "@/mocks/handlers/apiBase";
import { setMockSessionForTests } from "@/mocks/handlers";
import { server } from "@/mocks/server";
import { AjustesPage } from "./AjustesPage";

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/ajustes?business_id=biz_ejemplo"]}>
        <AjustesPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

/** El contenido de un `<details>` cerrado no es vista principal (design.md §13.1): igual que un
 * lector de pantalla no anuncia lo que sigue oculto, aquí tampoco cuenta como "visible en Ajustes". */
function visibleText(container: HTMLElement): string {
  const clone = container.cloneNode(true) as HTMLElement;
  clone.querySelectorAll("details:not([open])").forEach((details) => {
    const summary = details.querySelector("summary");
    details.replaceChildren(...(summary ? [summary.cloneNode(true)] : []));
  });
  return clone.textContent ?? "";
}

describe("AjustesPage", () => {
  beforeEach(() => setMockSessionForTests(true));

  it("carga el horario activo", async () => {
    renderPage();
    expect(await screen.findByLabelText("Horario activo — desde")).toHaveValue("08:00");
  });

  it("guarda los cambios de tema", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByLabelText("Tema");

    await user.selectOptions(screen.getByLabelText("Tema"), "dark");
    await user.click(screen.getByRole("button", { name: "Guardar preferencias y avisos" }));

    await waitFor(() => expect(screen.getByText("Guardado.")).toBeInTheDocument());
  });

  it("fusiona Tus cuentas, Límites de gasto, Avisos y Preferencias en una sola pantalla, con Tu negocio y Opciones avanzadas al pie", async () => {
    const { container } = renderPage();
    expect(await screen.findByRole("heading", { name: "Tus cuentas" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Conectar tus cuentas" })).toBeInTheDocument();
    expect((await screen.findAllByLabelText("Tope al día"))[0]).toBeInTheDocument();
    expect(await screen.findByRole("heading", { name: "Límites de gasto" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Avisos" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Preferencias" })).toBeInTheDocument();

    const negocioLink = screen.getByRole("link", { name: "Tu negocio →" });
    expect(negocioLink).toHaveAttribute("href", "/ajustes/negocio");
    const advancedLink = screen.getByRole("link", { name: "Opciones avanzadas →" });
    expect(advancedLink).toHaveAttribute("href", "/ajustes/avanzado");

    const text = visibleText(container);
    expect(text).not.toMatch(/webhook/i);
    expect(text).not.toMatch(/\btoken\b/i);
    expect(text).not.toMatch(/\bcsv\b/i);
  });

  it("Límites de gasto: bajar un tope es instantáneo, subirlo pide escribir SUBIR", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findAllByLabelText("Tope al día");

    const dailyInputs = screen.getAllByLabelText("Tope al día");
    await user.clear(dailyInputs[0]!);
    await user.type(dailyInputs[0]!, "10");
    await user.click(screen.getAllByRole("button", { name: "Guardar" })[0]!);
    await waitFor(() => expect(screen.getAllByText("Guardado.")[0]).toBeInTheDocument());
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();

    await user.clear(dailyInputs[0]!);
    await user.type(dailyInputs[0]!, "999");
    await user.click(screen.getAllByRole("button", { name: "Guardar" })[0]!);

    const dialog = await screen.findByRole("dialog", { name: "Subir un límite de gasto" });
    await user.type(within(dialog).getByLabelText(/Escribe SUBIR/), "SUBIR");
    await user.click(within(dialog).getByRole("button", { name: "Subir" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  });
});

describe("AjustesPage — Avisos (Telegram)", () => {
  beforeEach(() => setMockSessionForTests(true));

  it("emparejar exige confirmación explícita y solo entonces muestra el código de Telegram", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("Sin emparejar todavía.");

    await user.click(screen.getByRole("button", { name: "Emparejar" }));
    const dialog = await screen.findByRole("dialog", { name: /Confirmar emparejamiento de Telegram/ });
    expect(within(dialog).queryByLabelText(/Código de verificación/)).not.toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Emparejar" }));

    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(await screen.findByText(/Escribe este código al bot/)).toBeInTheDocument();
  });
});

describe("AjustesPage — los cuatro estados de pantalla (design.md §13.2)", () => {
  beforeEach(() => setMockSessionForTests(true));

  it("cargando por sección: Tus cuentas espera su propia consulta, sin bloquear el resto", async () => {
    server.use(http.get(`${API_BASE}/platform-accounts`, () => new Promise(() => {})));
    renderPage();
    // El resto de secciones no depende de /platform-accounts y sigue cargando la suya.
    expect(await screen.findByRole("heading", { name: "Límites de gasto" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Tus cuentas" })).toBeInTheDocument();
  });

  it("vacío (Tus cuentas): sin cuentas conectadas todavía, con los botones de conectar ya visibles", async () => {
    server.use(http.get(`${API_BASE}/platform-accounts`, () => HttpResponse.json({ items: [] })));
    renderPage();
    expect(await screen.findByText("Sin cuentas conectadas")).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "Conectar" }).length).toBeGreaterThan(0);
  });

  it("error de lectura por sección: Límites de gasto informa y ofrece Reintentar sin romper el resto", async () => {
    server.use(http.get(`${API_BASE}/guardrails`, () => HttpResponse.json({ error: { code: "FAILED", message: "fallo" } }, { status: 500 })));
    renderPage();
    expect(await screen.findByRole("button", { name: "Reintentar" })).toBeInTheDocument();
    expect(await screen.findByRole("heading", { name: "Avisos" })).toBeInTheDocument();
  });

  it("datos: las cuatro secciones con contenido real", async () => {
    renderPage();
    expect(await screen.findByRole("heading", { name: "Conectar tus cuentas" })).toBeInTheDocument();
    expect((await screen.findAllByLabelText("Tope al día"))[0]).toBeInTheDocument();
    expect(await screen.findByText("Sin emparejar todavía.")).toBeInTheDocument();
    expect(screen.getByLabelText("Tema")).toBeInTheDocument();
  });
});
