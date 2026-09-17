import { beforeEach, describe, expect, it } from "vitest";
import { http, HttpResponse } from "msw";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { API_BASE } from "@/mocks/handlers/apiBase";
import { resetEconomicsFixtures } from "@/mocks/handlers";
import { requireMockConfirmation } from "@/mocks/handlers/actionConfirmation";
import { server } from "@/mocks/server";
import { ConversionsCard } from "./ConversionsCard";

const BUSINESS_ID = "biz_ejemplo";

function renderCard() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <ConversionsCard businessId={BUSINESS_ID} />
    </QueryClientProvider>,
  );
}

function csvFile(content: string, name = "conversiones.csv"): File {
  return new File([content], name, { type: "text/csv" });
}

describe("ConversionsCard — importar CSV", () => {
  beforeEach(() => resetEconomicsFixtures());

  it("un CSV válido muestra el resumen de importadas/duplicadas", async () => {
    const user = userEvent.setup();
    renderCard();

    const input = screen.getByLabelText("Elegir archivo") as HTMLInputElement;
    await user.upload(input, csvFile("occurred_at,kind,email\n2026-09-01,lead,a@x.com\n2026-09-02,lead,\n"));

    await waitFor(() => expect(screen.getByText(/1 importadas, 0 duplicadas, 1 rechazadas\./)).toBeInTheDocument());
    expect(screen.getByText(/Línea 3: falta email, phone o external_ref/)).toBeInTheDocument();
  });

  it("un CSV sin las columnas obligatorias muestra el error del servidor", async () => {
    const user = userEvent.setup();
    renderCard();

    const input = screen.getByLabelText("Elegir archivo") as HTMLInputElement;
    await user.upload(input, csvFile("email\na@x.com\n"));

    expect(await screen.findByText(/faltan columnas obligatorias/)).toBeInTheDocument();
  });

  it("un fallo de red al importar muestra el error genérico sin romper el formulario", async () => {
    server.use(http.post(`${API_BASE}/conversions/import`, () => HttpResponse.error()));
    const user = userEvent.setup();
    renderCard();

    const input = screen.getByLabelText("Elegir archivo") as HTMLInputElement;
    await user.upload(input, csvFile("occurred_at,kind,email\n2026-09-01,lead,a@x.com\n"));

    expect(await screen.findByText("No se pudo importar el CSV.")).toBeInTheDocument();
  });
});

describe("ConversionsCard — token de webhook", () => {
  beforeEach(() => resetEconomicsFixtures());

  it("muestra la URL fija del webhook", () => {
    renderCard();
    expect(screen.getByText(`${window.location.origin}/api/v1/conversions/webhook`)).toBeInTheDocument();
  });

  it("pulsar Generar token abre el confirmación preparada sin ejecutar", async () => {
    let requestCount = 0;
    server.use(
      http.post(`${API_BASE}/conversions/webhook-token`, async ({ request }) => {
        requestCount += 1;
        return (await requireMockConfirmation(request)) ?? HttpResponse.json({ token: "whk_never" });
      }),
    );
    const user = userEvent.setup();
    renderCard();

    await user.click(screen.getByRole("button", { name: "Generar token" }));

    expect(await screen.findByRole("dialog", { name: /Confirmar generación del token/ })).toBeInTheDocument();
    await waitFor(() => expect(requestCount).toBe(1));
  });

  it("tras confirmar, genera el token y permite copiarlo una vez", async () => {
    server.use(
      http.post(`${API_BASE}/conversions/webhook-token`, async ({ request }) =>
        (await requireMockConfirmation(request)) ?? HttpResponse.json({ token: "whk_test123" }),
      ),
    );
    const user = userEvent.setup();
    Object.defineProperty(navigator, "clipboard", {
      value: { writeText: async () => undefined },
      configurable: true,
    });
    renderCard();

    await user.click(screen.getByRole("button", { name: "Generar token" }));
    const dialog = await screen.findByRole("dialog", { name: /Confirmar generación del token/ });
    expect(within(dialog).queryByLabelText(/Código de verificación/)).not.toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Generar token" }));

    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(await screen.findByText("whk_test123")).toBeInTheDocument();
    expect(screen.getByText("Guarda este token ahora: no se volverá a mostrar.")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Copiar" }));
    expect(await screen.findByRole("button", { name: "Copiado" })).toBeInTheDocument();
  });

  it("un fallo tras confirmar no repite la acción", async () => {
    server.use(http.post(`${API_BASE}/conversions/webhook-token`, async ({request}) => {
      const challenge = await requireMockConfirmation(request);
      return challenge ?? HttpResponse.json({error:{code:"FAILED",message:"Error saneado"}},{status:503});
    }));
    const user = userEvent.setup();
    renderCard();

    await user.click(screen.getByRole("button", { name: "Generar token" }));
    const dialog = await screen.findByRole("dialog", { name: /Confirmar generación del token/ });
    expect(within(dialog).queryByLabelText(/Código de verificación/)).not.toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Generar token" }));

    expect(await within(dialog).findByRole("alert")).toHaveTextContent(/Comprueba el estado/i);
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(screen.queryByText(/^whk_/)).not.toBeInTheDocument();
  });

  it("bloqueado por intentos (429), pide esperar en vez de dejar reintentar en bucle", async () => {
    server.use(
      http.post(`${API_BASE}/conversions/webhook-token`, () =>
        HttpResponse.json({ error: { code: "ACCOUNT_LOCKED", message: "Demasiados intentos recientes." } }, { status: 429 }),
      ),
    );
    const user = userEvent.setup();
    renderCard();

    await user.click(screen.getByRole("button", { name: "Generar token" }));
    const dialog = await screen.findByRole("dialog", { name: /Confirmar generación del token/ });
    expect(within(dialog).queryByLabelText(/Código de verificación/)).not.toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Generar token" }));

    expect(await within(dialog).findByRole("alert")).toHaveTextContent(/No se pudo preparar/i);
  });
});
