import { describe, expect, it, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { axe, toHaveNoViolations } from "jest-axe";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { App } from "@/App";
import { setMockSessionForTests } from "@/mocks/handlers";
import { ALL_ROUTES } from "@/routes/routeConfig";

expect.extend(toHaveNoViolations);

/**
 * Auditoría de accesibilidad — smoke, no auditoría completa (T122, panel-spec §8).
 * jsdom no pinta layout real: el contraste de color ya se mide de forma determinista
 * en `styles/tokens.contrast.test.ts` sobre los valores exactos de `tokens.css` — aquí
 * se desactiva esa única regla de axe para no duplicar (ni falsear) esa comprobación.
 */
const AXE_OPTIONS = { rules: { "color-contrast": { enabled: false } } };

function renderApp() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <App />
    </QueryClientProvider>,
  );
}

const ROUTE_HEADINGS: Record<string, string> = {
  "/propuestas": "Propuestas",
  "/campanas": "Campañas",
  "/resultados": "Resultados",
  "/ajustes": "Ajustes",
};

describe("Accesibilidad — cada destino del panel (T122)", () => {
  beforeEach(() => setMockSessionForTests(true));

  it.each(ALL_ROUTES)("$label ($path) no tiene violaciones de axe", async (route) => {
    window.history.pushState({}, "", route.path);
    const { container } = renderApp();

    await screen.findByRole("heading", { name: ROUTE_HEADINGS[route.path], level: 1 });

    const results = await axe(container, AXE_OPTIONS);
    expect(results).toHaveNoViolations();
  });
});

describe("Accesibilidad — diálogos globales (T122)", () => {
  beforeEach(() => {
    setMockSessionForTests(true);
    window.history.pushState({}, "", "/propuestas");
  });

  it("paleta de comandos (Ctrl/Cmd+K)", async () => {
    const user = userEvent.setup();
    renderApp();
    await screen.findByRole("heading", { name: "Propuestas", level: 1 });

    await user.keyboard("{Control>}k{/Control}");
    const dialog = await screen.findByRole("dialog", { name: /buscar y saltar a una vista/i });

    expect(await axe(dialog, AXE_OPTIONS)).toHaveNoViolations();
  });

  it("ayuda de atajos (?)", async () => {
    const user = userEvent.setup();
    renderApp();
    await screen.findByRole("heading", { name: "Propuestas", level: 1 });

    await user.keyboard("?");
    const dialog = await screen.findByRole("dialog", { name: /atajos de teclado/i });

    expect(await axe(dialog, AXE_OPTIONS)).toHaveNoViolations();
  });

  it("parar los cambios (Shift+F)", async () => {
    const user = userEvent.setup();
    renderApp();
    await screen.findByRole("heading", { name: "Propuestas", level: 1 });

    await user.keyboard("{Shift>}F{/Shift}");
    const dialog = await screen.findByRole("dialog", { name: /parar los cambios/i });

    expect(await axe(dialog, AXE_OPTIONS)).toHaveNoViolations();
  });
});
