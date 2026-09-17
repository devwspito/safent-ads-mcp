import { beforeEach, describe, expect, it } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { App } from "@/App";
import { resetHardCapsFixtures, resetMcpOauthFixtures, setMockSessionForTests } from "@/mocks/handlers";
import { resetPackagesFixtures } from "@/mocks/fixtures/packages";
import { MOCK_TOTP_CODE } from "@/mocks/fixtures/businesses";

/**
 * Glosario vinculante — spec.md "Reglas" + design.md §10.1: «agente», «ejecución», «señal»,
 * «guardrail», «pacing», «kill switch», «diff», «hash», «tool», «entidad», «lente»,
 * «presupuesto de atención», «cursor» (como paginación visible), «MTD», «ROAS», «ROI» no
 * pueden aparecer en las cuatro pantallas principales, sus hojas ni sus avisos. Recorre
 * Propuestas, Campañas (con una fila y su detalle desplegados, y el menú «⋯» abierto),
 * Resultados y Ajustes — nunca `/ajustes/avanzado`, que design.md §10 exime explícitamente.
 * §13.1 exime también cualquier bloque plegado marcado como zona técnica: el helper de
 * abajo recorta el contenido de los `<details>` cerrados antes de comparar, igual que un
 * lector de pantalla no anuncia lo que sigue oculto.
 */
const FORBIDDEN_WORDS: RegExp[] = [
  /agentes?/i,
  /ejecuci[oó]n(es)?/i,
  /señal(es)?/i,
  /guardrails?/i,
  /\bpacing\b/i,
  /kill[\s-]?switch/i,
  /\bdiffs?\b/i,
  /\bhash(es)?\b/i,
  /\btools?\b/i,
  /\bentidad(es)?\b/i,
  /\blente(s)?\b/i,
  /presupuesto de atenci[oó]n/i,
  /\bcursor(es)?\b/i,
  /\bMTD\b/,
  /\bROAS\b/,
  /\bROI\b/,
];

function renderApp() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <App />
    </QueryClientProvider>,
  );
}

/** El contenido de un `<details>` cerrado no es vista principal (design.md §13.1). */
function visibleText(container: HTMLElement): string {
  const clone = container.cloneNode(true) as HTMLElement;
  clone.querySelectorAll("details:not([open])").forEach((details) => {
    const summary = details.querySelector("summary");
    details.replaceChildren(...(summary ? [summary.cloneNode(true)] : []));
  });
  return clone.textContent ?? "";
}

function assertNoForbiddenWords(text: string) {
  for (const pattern of FORBIDDEN_WORDS) {
    expect(text).not.toMatch(pattern);
  }
}

describe("Vocabulario de la calle — Propuestas (spec.md)", () => {
  it("no muestra ninguna palabra prohibida en la bandeja, la navegación ni el detalle", async () => {
    setMockSessionForTests(true);
    window.history.pushState({}, "", "/propuestas");
    const user = userEvent.setup();
    const { container } = renderApp();

    await screen.findByRole("heading", { name: "Propuestas", level: 1 });
    const detailButtons = await screen.findAllByRole("button", { name: "Detalle" });
    assertNoForbiddenWords(visibleText(container));

    for (const button of detailButtons) {
      await user.click(button);
    }
    assertNoForbiddenWords(visibleText(container));
  });
});

describe("Vocabulario de la calle — Campañas (design.md §10)", () => {
  it("no muestra ninguna palabra prohibida en las filas, el menú «⋯» ni el detalle de campaña", async () => {
    setMockSessionForTests(true);
    window.history.pushState({}, "", "/campanas");
    const user = userEvent.setup();
    const { container } = renderApp();

    await screen.findByRole("heading", { name: "Campañas", level: 1 });
    assertNoForbiddenWords(visibleText(container));

    const [firstDetailToggle] = await screen.findAllByRole("button", { name: /Ver detalle de|Detalle/ });
    await user.click(firstDetailToggle!);
    assertNoForbiddenWords(visibleText(container));

    const [firstMenu] = screen.getAllByRole("button", { name: /Más opciones para/ });
    await user.click(firstMenu!);
    const menu = screen.getByRole("menu");
    assertNoForbiddenWords(visibleText(menu));
  });
});

describe("Vocabulario de la calle — Resultados (design.md §10)", () => {
  it("no muestra ninguna palabra prohibida en la cabecera ni en la tabla por cuenta", async () => {
    setMockSessionForTests(true);
    window.history.pushState({}, "", "/resultados");
    const { container } = renderApp();

    await screen.findByRole("heading", { name: "Resultados", level: 1 });
    await screen.findByText("Gasto");
    assertNoForbiddenWords(visibleText(container));
  });
});

describe("Vocabulario de la calle — Ajustes (design.md §10)", () => {
  it("no muestra ninguna palabra prohibida fuera de los detalles técnicos plegados", async () => {
    setMockSessionForTests(true);
    resetHardCapsFixtures();
    window.history.pushState({}, "", "/ajustes");
    const { container } = renderApp();

    await screen.findByRole("heading", { name: "Ajustes", level: 1 });
    await screen.findByRole("heading", { name: "Límites de gasto" });
    // «Topes» (spec 008 T033) entra en el barrido con sus tarjetas ya cargadas: el sobre, la
    // procedencia y lo recortado se pintan después de la respuesta, no en el esqueleto.
    await screen.findByRole("heading", { name: "Topes" });
    await screen.findAllByRole("button", { name: "Guardar topes" });
    await screen.findByText("Esta cuenta no puede escribir todavía.");
    assertNoForbiddenWords(visibleText(container));
  });

  it("tampoco con los dos diálogos de «Topes» abiertos (T033, quitar el tope del panel)", async () => {
    setMockSessionForTests(true);
    resetHardCapsFixtures();
    resetMcpOauthFixtures();
    window.history.pushState({}, "", "/ajustes");
    const user = userEvent.setup();
    renderApp();

    await screen.findByRole("heading", { name: "Topes" });
    const [withdraw] = await screen.findAllByRole("button", { name: "Quitar el tope del panel" });
    await user.click(withdraw!);

    const presenceDialog = await screen.findByRole("dialog", { name: "Confirma que eres tú" });
    assertNoForbiddenWords(visibleText(presenceDialog));

    await user.type(within(presenceDialog).getByLabelText(/Código de verificación/), MOCK_TOTP_CODE);
    await user.click(within(presenceDialog).getByRole("button", { name: "Continuar" }));

    const confirmDialog = await screen.findByRole("dialog", { name: "Revisa y confirma esta acción" });
    assertNoForbiddenWords(visibleText(confirmDialog));
  });

  it("tampoco con los dos diálogos de «Quitar acceso» abiertos (T062, «Aplicaciones con acceso»)", async () => {
    setMockSessionForTests(true);
    resetMcpOauthFixtures();
    window.history.pushState({}, "", "/ajustes");
    const user = userEvent.setup();
    renderApp();

    await screen.findByRole("heading", { name: "Ajustes", level: 1 });
    const [revokeButton] = await screen.findAllByRole("button", { name: "Quitar acceso" });
    await waitFor(() => expect(revokeButton).toBeEnabled());
    await user.click(revokeButton!);

    const presenceDialog = await screen.findByRole("dialog", { name: "Confirmar quitar acceso" });
    assertNoForbiddenWords(visibleText(presenceDialog));

    await user.type(within(presenceDialog).getByLabelText(/Código de verificación/), MOCK_TOTP_CODE);
    await user.click(within(presenceDialog).getByRole("button", { name: "Continuar" }));

    const confirmDialog = await screen.findByRole("dialog", { name: "Revisa y confirma esta acción" });
    assertNoForbiddenWords(visibleText(confirmDialog));
  });
});

describe("Vocabulario de la calle — Paquete de campaña (003-paquete-de-campana §T051)", () => {
  beforeEach(() => resetPackagesFixtures());

  it("no muestra ninguna palabra prohibida en el detalle del paquete de Meta, con «Cambiar imagen» abierto", async () => {
    setMockSessionForTests(true);
    window.history.pushState({}, "", "/propuestas/paquete/pkg_meta_001");
    const user = userEvent.setup();
    const { container } = renderApp();

    await screen.findByText("Reserva de citas");
    assertNoForbiddenWords(visibleText(container));

    const [firstChangeImage] = screen.getAllByRole("button", { name: "Cambiar imagen" });
    await user.click(firstChangeImage!);
    await screen.findAllByRole("button", { name: "Usar esta imagen" });
    assertNoForbiddenWords(visibleText(container));
  });

  it("no muestra ninguna palabra prohibida en el paquete de Google (palabras clave y sin datos de competencia)", async () => {
    setMockSessionForTests(true);
    window.history.pushState({}, "", "/propuestas/paquete/pkg_google_001");
    const { container } = renderApp();

    await screen.findByText("Búsqueda Reservas");
    assertNoForbiddenWords(visibleText(container));
  });
});

describe("Vocabulario de la calle — Progreso de publicación de un paquete (tasks.md T053)", () => {
  beforeEach(() => resetPackagesFixtures());

  it("no muestra ninguna palabra prohibida en la tira de progreso tras «Aprobar y publicar»", async () => {
    setMockSessionForTests(true);
    window.history.pushState({}, "", "/propuestas");
    const user = userEvent.setup();
    const { container } = renderApp();

    const row = (await screen.findByText(/Publicar la campaña «Recordatorio de citas»/)).closest('[data-package-id]') as HTMLElement;
    await user.click(within(row).getByRole("button", { name: "Detalle" }));
    await screen.findByText("Qué se va a publicar");
    const approve = within(row).getByRole("button", { name: /Aprobar y publicar/ });
    await waitFor(() => expect(approve).toBeEnabled());
    await user.click(approve);

    await screen.findByRole("status", { name: "Progreso de la publicación" });
    assertNoForbiddenWords(visibleText(container));
  });
});

describe("Vocabulario de la calle — Tu negocio (design.md §6.1.3, §10)", () => {
  it("no muestra ninguna palabra prohibida en Marca, Ofertas y economía, Calendario o Conversiones", async () => {
    setMockSessionForTests(true);
    window.history.pushState({}, "", "/ajustes/negocio");
    const { container } = renderApp();

    await screen.findByRole("heading", { name: "Tu negocio", level: 1 });
    await screen.findByRole("heading", { name: "Conversiones" });
    assertNoForbiddenWords(visibleText(container));
  });
});
