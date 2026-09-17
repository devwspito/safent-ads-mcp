import { beforeEach, describe, expect, it } from "vitest";
import { http, HttpResponse } from "msw";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MOCK_TOTP_CODE } from "@/mocks/fixtures/businesses";
import { resolveMockHardCaps } from "@/mocks/fixtures/hardCaps";
import {
  resetHardCapsFixtures,
  resetMcpOauthFixtures,
  setMockEnvelopeDeclared,
  setMockPanelStateAvailable,
  setMockSessionForTests,
} from "@/mocks/handlers";
import { API_BASE } from "@/mocks/handlers/apiBase";
import { server } from "@/mocks/server";
import { TopesSection } from "./TopesSection";

const GOOGLE = "Google Ads — Negocio Ejemplo";
const META = "Meta Ads — Negocio Ejemplo";
const GOOGLE_ACCOUNT = "100-000-0002";

function renderSection() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <TopesSection businessId="biz_ejemplo" />
    </QueryClientProvider>,
  );
}

async function cardFor(name: string): Promise<HTMLElement> {
  const heading = await screen.findByRole("heading", { name });
  return heading.closest("article") as HTMLElement;
}

/**
 * El importe en vigor de un campo, leído por su etiqueta y no por su posición. `selector: "dt"`
 * porque la lista y el formulario comparten vocabulario a propósito: «Al día» nombra tanto el
 * importe en vigor como el campo donde se escribe el nuevo.
 */
function amount(card: HTMLElement, label: string): string {
  return within(card).getByText(label, { selector: "dt" }).nextElementSibling?.textContent ?? "";
}

describe("Ajustes → Topes", () => {
  beforeEach(() => {
    setMockSessionForTests(true);
    resetHardCapsFixtures();
    resetMcpOauthFixtures();
  });

  it("sin sobre declarado no hay formulario, y el tope del fichero sigue a la vista", async () => {
    setMockEnvelopeDeclared(false);
    renderSection();

    const card = await cardFor(GOOGLE);
    await waitFor(() => expect(amount(card, "Al día")).toMatch(/^40,00\s€$/));
    expect(
      within(card).getByText(
        "El sobre de gasto no está declarado: decláralo como panel_managed en config/caps.yaml para fijar topes desde aquí.",
      ),
    ).toBeInTheDocument();
    expect(within(card).queryByRole("button", { name: "Guardar topes" })).toBeNull();
    expect(within(card).getByText("Tope del fichero config/caps.yaml.")).toBeInTheDocument();
  });

  it("una cuenta sin tope de ninguna procedencia lo dice y ofrece fijar el primero", async () => {
    renderSection();

    const card = await cardFor(META);
    await waitFor(() =>
      expect(within(card).getByText("Esta cuenta no puede escribir todavía.")).toBeInTheDocument(),
    );
    expect(within(card).queryByText("Al día", { selector: "dt" })).toBeNull();
    expect(within(card).getByLabelText("Al día")).toHaveValue("");
    expect(within(card).getByRole("button", { name: "Guardar topes" })).toBeEnabled();
  });

  it("un campo recortado se lee entero: lo guardado y lo que está en vigor", async () => {
    renderSection();

    const card = await cardFor(GOOGLE);
    await waitFor(() =>
      expect(within(card).getByText("Guardado 50,00 €, en vigor 40,00 €.")).toBeInTheDocument(),
    );
    expect(within(card).getByText("Tope del fichero y del panel: se aplica el menor de los dos.")).toBeInTheDocument();
    expect(within(card).getByText("Queda 1 cuenta de 3 con tope del panel.")).toBeInTheDocument();
    expect(within(card).getByText("Quedan 10 cambios de tope para hoy, de 10.")).toBeInTheDocument();
    expect(amount(card, "Suelo")).toMatch(/^2,00\s€$/);
  });

  it("sin `from_panel` no se inventa el importe guardado: solo se dice que está recortado", async () => {
    // El campo es opcional en el contrato: un bróker que no lo mande no puede hacer que la
    // pantalla se invente un importe que nadie escribió.
    const withoutSaved = { ...resolveMockHardCaps(GOOGLE_ACCOUNT), from_panel: undefined };
    server.use(
      http.get(`${API_BASE}/accounts/${GOOGLE_ACCOUNT}/hard-caps`, () => HttpResponse.json(withoutSaved)),
    );
    renderSection();

    const card = await cardFor(GOOGLE);
    await waitFor(() => expect(within(card).getByText("Recortado: en vigor 40,00 €.")).toBeInTheDocument());
  });

  it("el aviso de estado del panel no disponible dice que se aplica solo el fichero", async () => {
    setMockPanelStateAvailable(false);
    renderSection();

    const card = await cardFor(GOOGLE);
    await waitFor(() =>
      expect(
        within(card).getByText("Estado del panel no disponible: se aplica solo el fichero."),
      ).toBeInTheDocument(),
    );
    expect(amount(card, "Al día")).toMatch(/^40,00\s€$/);
  });

  it("bajar un tope solo pide confirmar la acción, nunca identificarse", async () => {
    const user = userEvent.setup();
    renderSection();

    const card = await cardFor(GOOGLE);
    const daily = await within(card).findByLabelText("Al día");
    await waitFor(() => expect(daily).toHaveValue("40"));
    await user.clear(daily);
    await user.type(daily, "30");
    await user.click(within(card).getByRole("button", { name: "Guardar topes" }));

    const dialog = await screen.findByRole("dialog", { name: "Revisa y confirma esta acción" });
    expect(screen.queryByRole("dialog", { name: "Confirma que eres tú" })).toBeNull();
    expect(within(dialog).getByText("Al día: 30,00 €")).toBeInTheDocument();

    await user.click(within(dialog).getByRole("button", { name: "Guardar topes" }));
    await waitFor(() => expect(within(card).getByText("Guardado.")).toBeInTheDocument());
    expect(amount(card, "Al día")).toMatch(/^30,00\s€$/);
  });

  it("subir pide identificarse y después confirmar, con un solo código", async () => {
    const user = userEvent.setup();
    renderSection();

    const card = await cardFor(GOOGLE);
    const daily = await within(card).findByLabelText("Al día");
    await waitFor(() => expect(daily).toHaveValue("40"));
    await user.clear(daily);
    await user.type(daily, "45");
    await user.click(within(card).getByRole("button", { name: "Guardar topes" }));

    const presence = await screen.findByRole("dialog", { name: "Confirma que eres tú" });
    await user.type(within(presence).getByLabelText(/Código de verificación/), MOCK_TOTP_CODE);
    await user.click(within(presence).getByRole("button", { name: "Continuar" }));

    const confirmDialog = await screen.findByRole("dialog", { name: "Revisa y confirma esta acción" });
    expect(within(confirmDialog).getByText("Al día: 45,00 €")).toBeInTheDocument();
    await user.click(within(confirmDialog).getByRole("button", { name: "Guardar topes" }));

    await waitFor(() => expect(within(card).getByText("Guardado.")).toBeInTheDocument());
    // El fichero sigue mandando, y la pantalla lo dice entero en vez de dejar al dueño
    // preguntándose por qué ve 40,00 € donde escribió 45,00 €.
    expect(within(card).getByText("Guardado 45,00 €, en vigor 40,00 €.")).toBeInTheDocument();
  });

  it("quitar el tope del panel con entrada de fichero debajo pide identificarse, y devuelve el tope del fichero", async () => {
    const user = userEvent.setup();
    renderSection();

    const card = await cardFor(GOOGLE);
    await waitFor(async () => expect(await within(card).findByLabelText("Al día")).toHaveValue("40"));
    await user.click(within(card).getByRole("button", { name: "Quitar el tope del panel" }));

    const presence = await screen.findByRole("dialog", { name: "Confirma que eres tú" });
    await user.type(within(presence).getByLabelText(/Código de verificación/), MOCK_TOTP_CODE);
    await user.click(within(presence).getByRole("button", { name: "Continuar" }));

    const confirmDialog = await screen.findByRole("dialog", { name: "Revisa y confirma esta acción" });
    expect(within(confirmDialog).getByText("Se quita el tope fijado desde el panel.")).toBeInTheDocument();
    await user.click(within(confirmDialog).getByRole("button", { name: "Quitar el tope" }));

    await waitFor(() =>
      expect(within(card).getByText("Tope del fichero config/caps.yaml.")).toBeInTheDocument(),
    );
    expect(within(card).queryByRole("button", { name: "Quitar el tope del panel" })).toBeNull();
    expect(amount(card, "Al día")).toMatch(/^40,00\s€$/);
  });

  it("un importe fuera del sobre se dice en su campo y no llega a salir", async () => {
    const user = userEvent.setup();
    renderSection();

    const card = await cardFor(GOOGLE);
    const daily = await within(card).findByLabelText("Al día");
    await waitFor(() => expect(daily).toHaveValue("40"));
    await user.clear(daily);
    await user.type(daily, "60");
    await user.click(within(card).getByRole("button", { name: "Guardar topes" }));

    const error = await within(card).findByRole("alert");
    expect(error).toHaveTextContent(/^Como mucho 50,00\s€ al día\.$/);
    expect(daily).toHaveAttribute("aria-invalid", "true");
    expect(daily).toHaveFocus();
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("al cerrar la confirmación, el foco vuelve al botón que la abrió", async () => {
    const user = userEvent.setup();
    renderSection();

    const card = await cardFor(GOOGLE);
    const daily = await within(card).findByLabelText("Al día");
    await waitFor(() => expect(daily).toHaveValue("40"));
    await user.clear(daily);
    await user.type(daily, "30");
    const save = within(card).getByRole("button", { name: "Guardar topes" });
    await user.click(save);

    const dialog = await screen.findByRole("dialog", { name: "Revisa y confirma esta acción" });
    await user.click(within(dialog).getByRole("button", { name: "Cancelar" }));

    await waitFor(() => expect(save).toHaveFocus());
  });
});
