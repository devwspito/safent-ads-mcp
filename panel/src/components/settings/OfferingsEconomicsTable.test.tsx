import { beforeEach, describe, expect, it } from "vitest";
import { http, HttpResponse } from "msw";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { API_BASE } from "@/mocks/handlers/apiBase";
import { resetEconomicsFixtures } from "@/mocks/handlers";
import { server } from "@/mocks/server";
import { OfferingsEconomicsTable } from "./OfferingsEconomicsTable";

const BUSINESS_ID = "biz_ejemplo";

function renderTable() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <OfferingsEconomicsTable businessId={BUSINESS_ID} />
    </QueryClientProvider>,
  );
}

describe("OfferingsEconomicsTable", () => {
  beforeEach(() => resetEconomicsFixtures());

  it("marca como Provisional la oferta sin economía y no la oferta ya rellena", async () => {
    renderTable();

    await screen.findByText("Plan Anual Pro");

    expect(screen.getByTitle(/Provisional: introduce IVA/)).toBeInTheDocument();
    expect(screen.getAllByTitle(/Provisional:/)).toHaveLength(1);
  });

  it("precarga los campos de la oferta que ya tiene economía", async () => {
    renderTable();
    const heading = await screen.findByText("Plan Mensual");

    const row = within(heading.closest("li")!);
    expect(row.getByLabelText("IVA (%)")).toHaveValue(21);
    expect(row.getByLabelText("Coste de entrega (€)")).toHaveValue(30);
    expect(row.getByLabelText("Coste comercial (€/mes)")).toHaveValue(50);
    expect(row.getByLabelText("Devolución (%, opcional)")).toHaveValue(5);
  });

  it("guardar una oferta provisional la quita del estado Provisional", async () => {
    const user = userEvent.setup();
    renderTable();
    await screen.findByText("Plan Anual Pro");

    const saveButton = screen.getByRole("button", { name: "Guardar economía de Plan Anual Pro" });
    const vatInput = within(saveButton.closest("li")!).getByLabelText("IVA (%)");
    const deliveryInput = within(saveButton.closest("li")!).getByLabelText("Coste de entrega (€)");
    const salesInput = within(saveButton.closest("li")!).getByLabelText("Coste comercial (€/mes)");

    await user.type(vatInput, "21");
    await user.type(deliveryInput, "90");
    await user.type(salesInput, "140");
    await user.click(saveButton);

    await waitFor(() => expect(screen.getByText("Guardado.")).toBeInTheDocument());
    expect(screen.queryByTitle(/Provisional:/)).not.toBeInTheDocument();
  });

  it("no envía la petición si faltan campos obligatorios", async () => {
    const user = userEvent.setup();
    renderTable();
    await screen.findByText("Plan Anual Pro");

    await user.click(screen.getByRole("button", { name: "Guardar economía de Plan Anual Pro" }));

    expect(await screen.findByText("IVA, coste de entrega y coste comercial son obligatorios.")).toBeInTheDocument();
  });

  it("un 422 del servidor se muestra sin romper la fila", async () => {
    server.use(
      http.put(`${API_BASE}/offerings/:id/economics`, () =>
        HttpResponse.json({ error: { code: "VALIDATION_ERROR", message: "vat_rate_pct fuera de rango." } }, { status: 422 }),
      ),
    );
    const user = userEvent.setup();
    renderTable();
    await screen.findByText("Plan Anual Pro");

    const saveButton = screen.getByRole("button", { name: "Guardar economía de Plan Anual Pro" });
    const scope = saveButton.closest("li")!;
    await user.type(within(scope).getByLabelText("IVA (%)"), "21");
    await user.type(within(scope).getByLabelText("Coste de entrega (€)"), "90");
    await user.type(within(scope).getByLabelText("Coste comercial (€/mes)"), "140");
    await user.click(saveButton);

    expect(await screen.findByText("vat_rate_pct fuera de rango.")).toBeInTheDocument();
  });
});
