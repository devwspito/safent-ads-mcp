import { useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { http, HttpResponse } from "msw";
import { API_BASE } from "@/mocks/handlers/apiBase";
import { server } from "@/mocks/server";
import { setMockSessionForTests } from "@/mocks/handlers";
import { approvePackage, getPackageDetail, listPackageFeedItems, resetPackagesFixtures } from "@/mocks/fixtures/packages";
import type { PackageFeedItem } from "@/api/schemas/proposals";
import { PackageProposalRow, type PackageRowActions } from "./PackageProposalRow";

function feedItem(packageId = "pkg_meta_001"): PackageFeedItem {
  return listPackageFeedItems().find((item) => item.package_id === packageId)!;
}

let clients: QueryClient[] = [];
function Harness({ item, writeDisabledReason = null, actions }: { item: PackageFeedItem; writeDisabledReason?: string | null; actions: Partial<PackageRowActions> }) {
  const [expanded, setExpanded] = useState(false);
  const [focused, setFocused] = useState(false);
  return (
    <PackageProposalRow
      businessId="biz_ejemplo"
      item={item}
      isFocused={focused}
      isExpanded={expanded}
      writeDisabledReason={writeDisabledReason}
      writeDisabledShortReason={writeDisabledReason}
      actions={{
        onFocus: () => setFocused(true),
        onToggleExpand: () => setExpanded((value) => !value),
        onApprove: vi.fn(),
        onReject: vi.fn(),
        ...actions,
      }}
    />
  );
}

function mount(ui: React.ReactElement) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  clients.push(client);
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  setMockSessionForTests(true);
  resetPackagesFixtures();
});
afterEach(() => {
  clients.forEach((client) => client.clear());
  clients = [];
});

describe("PackageProposalRow — gramática de fila (tasks.md T052)", () => {
  it("propuesto, sin desplegar: dinero real, Aprobar y publicar deshabilitado hasta abrir el detalle", async () => {
    const { container } = mount(<Harness item={feedItem()} actions={{}} />);
    expect(screen.getByText(/Publicar la campaña «Reserva de citas»/)).toBeInTheDocument();
    expect(screen.getByText(/Meta · Cuenta Principal/)).toBeInTheDocument();
    expect(container.querySelector('[class*="money"]')?.textContent).toMatch(/20,00\s€\/día/);
    const approve = screen.getByRole("button", { name: /Aprobar y publicar/ });
    expect(approve).toBeDisabled();
    expect(screen.getByText("Abre el detalle para revisarlo antes de aprobar.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Rechazar" })).toBeEnabled();
  });

  it("sin desplegar ya enseña miniatura, plataforma y recuentos (qué se va a aprobar)", async () => {
    mount(<Harness item={feedItem()} actions={{}} />);
    expect(await screen.findByAltText("Cita fuera de horario")).toBeInTheDocument();
    expect(screen.getByText("Meta")).toBeInTheDocument();
    expect(await screen.findByText(/anuncios? · \d+ públicos?/)).toBeInTheDocument();
  });

  it("al abrir el detalle con la huella vigente, Aprobar y publicar se habilita y manda la huella vista", async () => {
    const onApprove = vi.fn();
    const user = userEvent.setup();
    mount(<Harness item={feedItem()} actions={{ onApprove }} />);
    await user.click(screen.getByRole("button", { name: "Detalle" }));
    await screen.findByText("Qué se va a publicar");
    const approve = await screen.findByRole("button", { name: /Aprobar y publicar/ });
    await waitFor(() => expect(approve).toBeEnabled());
    await user.click(approve);
    expect(onApprove).toHaveBeenCalledWith(expect.objectContaining({ package_id: "pkg_meta_001" }), expect.any(String));
  });

  it("huella cambiada tras abrir el detalle: motivo exacto y Aprobar y publicar sigue deshabilitado", async () => {
    const item = feedItem();
    server.use(http.get(`${API_BASE}/packages/:id`, () => HttpResponse.json({ ...getPackageDetail("pkg_meta_001"), package_hash: "otra-huella-distinta" })));
    const user = userEvent.setup();
    mount(<Harness item={item} actions={{}} />);
    await user.click(screen.getByRole("button", { name: "Detalle" }));
    await screen.findByText("Qué se va a publicar");
    expect(await screen.findByText("El paquete cambió; revísalo de nuevo.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Aprobar y publicar/ })).toBeDisabled();
  });

  it("el freno o los datos viejos deshabilitan Aprobar y Rechazar con el motivo compartido", () => {
    mount(<Harness item={feedItem()} writeDisabledReason="Cambios parados." actions={{}} />);
    expect(screen.getByRole("button", { name: /Aprobar y publicar/ })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Rechazar" })).toBeDisabled();
    expect(screen.getAllByText("Cambios parados.").length).toBeGreaterThan(0);
  });

  it("Rechazar no exige tener el detalle abierto", async () => {
    const onReject = vi.fn();
    const user = userEvent.setup();
    mount(<Harness item={feedItem()} actions={{ onReject }} />);
    await user.click(screen.getByRole("button", { name: "Rechazar" }));
    expect(onReject).toHaveBeenCalledWith(expect.objectContaining({ package_id: "pkg_meta_001" }));
  });

  it("una vez aprobado (state ≠ proposed), la fila enseña el progreso en vez de dinero y botones", async () => {
    const approved = { ...feedItem(), state: "publishing" as const };
    server.use(
      http.get(`${API_BASE}/packages/:id`, () =>
        HttpResponse.json({
          ...getPackageDetail("pkg_meta_001"),
          state: "publishing",
          publication: { state: "running", done_count: 2, total_count: 8, progress_sentence: "Creado 2 de 8. Nada está entregando todavía.", halted: null, campaign_entity_ref: null, activated_at: null, undo_deadline: null },
        }),
      ),
    );
    mount(<Harness item={approved} actions={{}} />);
    expect(screen.queryByRole("button", { name: /Aprobar y publicar/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Rechazar" })).not.toBeInTheDocument();
    expect(await screen.findByText("Creado 2 de 8. Nada está entregando todavía.")).toBeInTheDocument();
  });
});

describe("PackageProposalRow — cambiar la imagen invalida la aprobación (tasks.md T054)", () => {
  it("sustituir una imagen cambia la huella y Aprobar y publicar vuelve a exigir revisión", async () => {
    const user = userEvent.setup();
    mount(<Harness item={feedItem()} actions={{}} />);

    await user.click(screen.getByRole("button", { name: "Detalle" }));
    await screen.findByText("Qué se va a publicar");
    await waitFor(() => expect(screen.getByRole("button", { name: /Aprobar y publicar/ })).toBeEnabled());

    const card = (await screen.findAllByAltText("Cita fuera de horario")).map((img) => img.closest("figure")).find((figure): figure is HTMLElement => figure !== null)!;
    await user.click(within(card).getByRole("button", { name: "Cambiar imagen" }));
    const useButtons = await within(card).findAllByRole("button", { name: "Usar esta imagen" });
    await user.click(useButtons[0]!);
    await waitFor(() => expect(within(card).getByAltText("Cita fuera de horario · imagen alternativa")).toBeInTheDocument());

    await waitFor(() => expect(screen.getByRole("button", { name: /Aprobar y publicar/ })).toBeDisabled());
    expect(screen.getByText("El paquete cambió; revísalo de nuevo.")).toBeInTheDocument();
  });

  it("un paquete que ya no está `proposed` no admite cambiar imagen ni regenerar (PACKAGE_NOT_EDITABLE)", async () => {
    const packageHash = getPackageDetail("pkg_meta_001")!.package_hash;
    // Aprobarlo de verdad recalcula `actions.can_replace_image/can_regenerate` en el servidor
    // (getPackageDetail), no sólo el campo `state` de la respuesta.
    approvePackage("pkg_meta_001", packageHash, "biz_ejemplo");
    const user = userEvent.setup();
    mount(<Harness item={feedItem()} actions={{}} />);
    await user.click(screen.getByRole("button", { name: "Detalle" }));
    const card = (await screen.findAllByAltText("Cita fuera de horario")).map((img) => img.closest("figure")).find((figure): figure is HTMLElement => figure !== null)!;
    expect(within(card).getByRole("button", { name: "Cambiar imagen" })).toBeDisabled();
    expect(within(card).getByRole("button", { name: "Regenerar" })).toBeDisabled();
  });
});
