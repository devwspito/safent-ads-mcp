import { describe, expect, it, beforeEach } from "vitest";
import { http, HttpResponse } from "msw";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { API_BASE } from "@/mocks/handlers/apiBase";
import { resetRulesFixtures, setMockSessionForTests } from "@/mocks/handlers";
import { requireMockConfirmation } from "@/mocks/handlers/actionConfirmation";
import { server } from "@/mocks/server";
import { ReglasPage } from "./ReglasPage";

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/reglas?business_id=biz_ejemplo"]}>
        <ReglasPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("ReglasPage", () => {
  beforeEach(() => {
    setMockSessionForTests(true);
    resetRulesFixtures();
  });

  it("muestra la puerta de autonomía deshabilitada con el motivo por cuenta", async () => {
    renderPage();
    expect(await screen.findByText(/Autonomía deshabilitada:/)).toBeInTheDocument();
    expect(screen.getByText(/Google Ads — Negocio Ejemplo: Pregunta 2/)).toBeInTheDocument();
  });

  it("lista el catálogo con disparos y acierto, y deshabilita Automática por la puerta", async () => {
    renderPage();
    expect(await screen.findByText("Bajar presupuesto por CPL sobre umbral")).toBeInTheDocument();
    expect(screen.getByText("6 disparos/30 d")).toBeInTheDocument();

    const automaticaButtons = screen.getAllByRole("button", { name: "Automática" });
    // Ya está en AUTO (M03), así que sigue habilitado; el resto deberían estar bloqueados por la puerta.
    const disabledCount = automaticaButtons.filter((btn) => btn.hasAttribute("disabled")).length;
    expect(disabledCount).toBeGreaterThan(0);
  });

  it("activar el freno exige confirmación y lo refleja en la franja", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("El freno está desactivado.");

    await user.click(screen.getByRole("button", { name: "Parar cambios" }));
    const dialog = await screen.findByRole("dialog", { name: /Parar los cambios/ });
    await user.click(within(dialog).getByRole("button", { name: "Parar cambios" }));

    await waitFor(() => expect(screen.getByText(/El freno está activo/)).toBeInTheDocument());
  });
});

describe("ReglasPage — confirmaciones de la puerta de autonomía", () => {
  beforeEach(() => {
    setMockSessionForTests(true);
    resetRulesFixtures();
  });

  function findQ3Row() {
    const input = screen.getByLabelText(/Pregunta 3: tope mensual duro/);
    const row = input.closest("div");
    if (!row) throw new Error("no se encontró la fila de la pregunta 3");
    return { input, row };
  }

  it("pulsar Confirmar abre el confirmación preparada sin ejecutar", async () => {
    let requestCount = 0;
    server.use(
      http.post(`${API_BASE}/rules/autonomy-gate/confirmations`, async ({ request }) => {
        requestCount += 1;
        return (await requireMockConfirmation(request)) ?? HttpResponse.json({});
      }),
    );
    const user = userEvent.setup();
    renderPage();
    await screen.findByText(/Autonomía deshabilitada:/);

    const { input, row } = findQ3Row();
    await user.type(input, "5000");
    await user.click(within(row).getByRole("button", { name: "Confirmar" }));

    expect(await screen.findByRole("dialog", { name: /Confirmar respuesta de la puerta de autonomía/ })).toBeInTheDocument();
    await waitFor(() => expect(requestCount).toBe(1));
  });

  it("tras confirmar, confirma la pregunta y la retira de las pendientes", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText(/Autonomía deshabilitada:/);
    expect(screen.getByText(/Google Ads — Negocio Ejemplo: Pregunta 3/)).toBeInTheDocument();

    const { input, row } = findQ3Row();
    await user.type(input, "5000");
    await user.click(within(row).getByRole("button", { name: "Confirmar" }));

    const dialog = await screen.findByRole("dialog", { name: /Confirmar respuesta de la puerta de autonomía/ });
    expect(within(dialog).queryByLabelText(/Código de verificación/)).not.toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Confirmar" }));

    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    await waitFor(() => expect(screen.queryByLabelText(/Pregunta 3: tope mensual duro/)).not.toBeInTheDocument());
  });

  it("un fallo tras confirmar no repite la acción", async () => {
    server.use(http.post(`${API_BASE}/rules/autonomy-gate/confirmations`, async ({request}) => {
      const challenge = await requireMockConfirmation(request);
      return challenge ?? HttpResponse.json({error:{code:"FAILED",message:"Error saneado"}},{status:503});
    }));
    const user = userEvent.setup();
    renderPage();
    await screen.findByText(/Autonomía deshabilitada:/);

    const { input, row } = findQ3Row();
    await user.type(input, "5000");
    await user.click(within(row).getByRole("button", { name: "Confirmar" }));

    const dialog = await screen.findByRole("dialog", { name: /Confirmar respuesta de la puerta de autonomía/ });
    expect(within(dialog).queryByLabelText(/Código de verificación/)).not.toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Confirmar" }));

    expect(await within(dialog).findByRole("alert")).toHaveTextContent(/Comprueba el estado/i);
    expect(screen.getByLabelText(/Pregunta 3: tope mensual duro/)).toBeInTheDocument();
  });

  it("bloqueado por intentos (429) pide esperar dentro del prompt", async () => {
    server.use(
      http.post(`${API_BASE}/rules/autonomy-gate/confirmations`, () =>
        HttpResponse.json({ error: { code: "ACCOUNT_LOCKED", message: "Demasiados intentos recientes." } }, { status: 429 }),
      ),
    );
    const user = userEvent.setup();
    renderPage();
    await screen.findByText(/Autonomía deshabilitada:/);

    const { input, row } = findQ3Row();
    await user.type(input, "5000");
    await user.click(within(row).getByRole("button", { name: "Confirmar" }));

    const dialog = await screen.findByRole("dialog", { name: /Confirmar respuesta de la puerta de autonomía/ });
    expect(within(dialog).queryByLabelText(/Código de verificación/)).not.toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Confirmar" }));

    expect(await within(dialog).findByRole("alert")).toHaveTextContent(/No se pudo preparar/i);
  });
});
