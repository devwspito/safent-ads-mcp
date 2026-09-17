import { beforeEach, describe, expect, it } from "vitest";
import { http, HttpResponse } from "msw";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { BrandKit } from "@/api/schemas/brand";
import { API_BASE } from "@/mocks/handlers/apiBase";
import { resetBrandFixtures, setBrandKitFixture } from "@/mocks/handlers";
import { server } from "@/mocks/server";
import { BrandClaimsEditor } from "./BrandClaimsEditor";

const BUSINESS_ID = "biz_ejemplo";

function makeKit(overrides: Partial<BrandKit> = {}): BrandKit {
  return {
    brand_kit_id: "bk_ejemplo",
    business_id: BUSINESS_ID,
    typography: { primary_family: "Inter", secondary_family: null, licence_note: "Google Fonts", weights: [] },
    palette: [],
    tone_of_voice: { description: "Cercano y claro.", adjectives: [], avoid: [] },
    assets: [],
    claims_allowlist: ["Envío gratis"],
    forbidden_claims: [
      { claim: "garantizado", is_floor: true },
      { claim: "resultados asegurados", is_floor: false },
    ],
    legal_disclaimers: [{ text: "Aviso legal existente", applies_to: null }],
    platform_constraints: [],
    is_complete: false,
    is_confirmed: true,
    updated_at: "2026-09-10T09:00:00Z",
    ...overrides,
  };
}

function renderEditor(kit: BrandKit) {
  setBrandKitFixture(BUSINESS_ID, kit);
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <BrandClaimsEditor businessId={BUSINESS_ID} kit={kit} />
    </QueryClientProvider>,
  );
}

/** Cada `ClaimChipList` repite el texto "Añadir": hay que acotar al input+botón de la
 * lista concreta (el `<div>` que envuelve ambos), nunca buscar el botón en toda la página. */
async function addClaim(user: ReturnType<typeof userEvent.setup>, fieldLabel: string, value: string) {
  const input = screen.getByLabelText(fieldLabel);
  await user.type(input, value);
  const row = input.closest("div") as HTMLElement;
  await user.click(within(row).getByRole("button", { name: "Añadir" }));
}

describe("BrandClaimsEditor", () => {
  beforeEach(() => resetBrandFixtures());

  it("pinta los reclamos permitidos, prohibidos (con el suelo) y los avisos legales existentes", () => {
    renderEditor(makeKit());

    expect(screen.getByText("Envío gratis")).toBeInTheDocument();
    expect(screen.getByText("garantizado")).toBeInTheDocument();
    expect(screen.getByText("resultados asegurados")).toBeInTheDocument();
    expect(screen.getByText("Aviso legal existente")).toBeInTheDocument();
  });

  it("el suelo de seguridad no tiene botón de quitar", () => {
    renderEditor(makeKit());

    const floorChip = screen.getByText("garantizado").closest("li");
    expect(floorChip).not.toBeNull();
    expect(within(floorChip as HTMLElement).queryByRole("button")).not.toBeInTheDocument();

    const ownedChip = screen.getByText("resultados asegurados").closest("li");
    expect(within(ownedChip as HTMLElement).getByRole("button", { name: /Quitar/ })).toBeInTheDocument();
  });

  it("añadir un reclamo permitido lo agrega a la lista, y quitarlo lo elimina", async () => {
    const user = userEvent.setup();
    renderEditor(makeKit());

    await addClaim(user, "Reclamos permitidos", "Atención personalizada");

    expect(await screen.findByText("Atención personalizada")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Quitar Atención personalizada" }));

    expect(screen.queryByText("Atención personalizada")).not.toBeInTheDocument();
  });

  it("un reclamo permitido que coincide con uno prohibido bloquea el guardado con un aviso", async () => {
    const user = userEvent.setup();
    renderEditor(makeKit());

    await addClaim(user, "Reclamos permitidos", "resultados asegurados");

    expect(await screen.findByRole("alert")).toHaveTextContent(/está en reclamos permitidos y prohibidos a la vez/);
    expect(screen.getByRole("button", { name: "Guardar" })).toBeDisabled();
  });

  it("un conflicto devuelto por el servidor se muestra en línea sin romper el formulario", async () => {
    server.use(
      http.put(`${API_BASE}/brand/claims`, () =>
        HttpResponse.json(
          { error: { code: "VALIDATION_ERROR", message: "Un reclamo permitido coincide con uno prohibido." } },
          { status: 422 },
        ),
      ),
    );
    const user = userEvent.setup();
    renderEditor(makeKit());

    await user.click(screen.getByRole("button", { name: "Guardar" }));

    expect(await screen.findByText("Un reclamo permitido coincide con uno prohibido.")).toBeInTheDocument();
  });

  it("guardar con éxito muestra la confirmación", async () => {
    const user = userEvent.setup();
    renderEditor(makeKit());

    await user.click(screen.getByRole("button", { name: "Guardar" }));

    await waitFor(() => expect(screen.getByText("Guardado.")).toBeInTheDocument());
  });
});
