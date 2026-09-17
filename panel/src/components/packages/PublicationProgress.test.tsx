import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { http, HttpResponse } from "msw";
import { API_BASE } from "@/mocks/handlers/apiBase";
import { server } from "@/mocks/server";
import { setMockSessionForTests } from "@/mocks/handlers";
import { getPackageDetail, resetPackagesFixtures } from "@/mocks/fixtures/packages";
import type { PackagePreview, PublicationStatus } from "@/api/schemas/packages";
import { PublicationProgress } from "./PublicationProgress";

let clients: QueryClient[] = [];
function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  clients.push(client);
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <PublicationProgress businessId="biz_ejemplo" packageId="pkg_meta_001" />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function detailWith(publication: PublicationStatus, state: PackagePreview["state"] = "publishing"): PackagePreview {
  const base = getPackageDetail("pkg_meta_001")!;
  return { ...base, state, publication };
}

beforeEach(() => {
  setMockSessionForTests(true);
  resetPackagesFixtures();
});
afterEach(() => {
  clients.forEach((client) => client.clear());
  clients = [];
});

describe("PublicationProgress — los cinco desenlaces (tasks.md T053)", () => {
  it("pendiente: cuenta atrás y Deshacer cancela sin haber creado nada", async () => {
    const undoDeadline = new Date(Date.now() + 45_000).toISOString();
    let undoBody: unknown;
    server.use(
      http.get(`${API_BASE}/packages/:id`, () =>
        HttpResponse.json(
          detailWith(
            { state: "pending", done_count: 0, total_count: 8, progress_sentence: "Todavía no se ha creado nada.", halted: null, campaign_entity_ref: null, activated_at: null, undo_deadline: undoDeadline },
            "approved",
          ),
        ),
      ),
      http.post(`${API_BASE}/packages/:id/undo`, async ({ request }) => {
        undoBody = await request.json();
        return HttpResponse.json({ undo_kind: "cancelled_publication", sentence: "Publicación cancelada. No se ha creado nada." });
      }),
    );
    const user = userEvent.setup();
    mount();
    expect(await screen.findByText(/Puedes cancelarlo entero/)).toBeInTheDocument();
    const region = screen.getByRole("status", { name: "Progreso de la publicación" });
    expect(region).toHaveAttribute("aria-live", "polite");
    await user.click(screen.getByRole("button", { name: /Deshacer/ }));
    await waitFor(() => expect(undoBody).toMatchObject({ reason: expect.any(String) }));
  });

  it("en marcha: cuenta pasos hechos, sin botones de decisión", async () => {
    server.use(
      http.get(`${API_BASE}/packages/:id`, () =>
        HttpResponse.json(detailWith({ state: "running", done_count: 3, total_count: 8, progress_sentence: "Creado 3 de 8. Nada está entregando todavía.", halted: null, campaign_entity_ref: null, activated_at: null, undo_deadline: null })),
      ),
    );
    mount();
    expect(await screen.findByText("Creado 3 de 8. Nada está entregando todavía.")).toBeInTheDocument();
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });

  it("publicada: aviso verde con Deshacer (pausa) y Ver en Campañas", async () => {
    const undoDeadline = new Date(Date.now() + 2 * 3_600_000).toISOString();
    server.use(
      http.get(`${API_BASE}/packages/:id`, () =>
        HttpResponse.json(
          detailWith(
            { state: "completed", done_count: 8, total_count: 8, progress_sentence: "Campaña publicada y activa.", halted: null, campaign_entity_ref: "meta:campaign:pkg_meta_001", activated_at: new Date().toISOString(), undo_deadline: undoDeadline },
            "published",
          ),
        ),
      ),
    );
    mount();
    expect(await screen.findByText("Campaña publicada y activa.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Deshacer/ })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Ver en Campañas" })).toHaveAttribute("href", "/campanas");
  });

  it("parcial: aviso ámbar con Continuar, que reanuda la publicación", async () => {
    let resumeCalled = false;
    server.use(
      http.get(`${API_BASE}/packages/:id`, () =>
        HttpResponse.json(
          detailWith(
            {
              state: "halted",
              done_count: 2,
              total_count: 5,
              progress_sentence: "Creado 2 de 5.",
              halted: { reason_plain: "No se pudo crear uno de los anuncios: la plataforma rechazó la imagen.", next_step_plain: "Puedes continuar.", can_resume: true },
              campaign_entity_ref: "meta:campaign:pkg_meta_001",
              activated_at: null,
              undo_deadline: null,
            },
            "partially_published",
          ),
        ),
      ),
      http.post(`${API_BASE}/packages/:id/resume`, () => {
        resumeCalled = true;
        return HttpResponse.json({ publication_id: "pub_0001", approval_expires_at: new Date().toISOString() }, { status: 202 });
      }),
    );
    const user = userEvent.setup();
    mount();
    expect(await screen.findByText(/No se pudo crear uno de los anuncios/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Continuar" }));
    await waitFor(() => expect(resumeCalled).toBe(true));
  });

  it("fallo total: aviso rojo con «Ver por qué» desplegable, sin Continuar", async () => {
    server.use(
      http.get(`${API_BASE}/packages/:id`, () =>
        HttpResponse.json(
          detailWith(
            {
              state: "halted",
              done_count: 0,
              total_count: 8,
              progress_sentence: "Creado 0 de 8.",
              halted: { reason_plain: "Meta no pudo crear la campaña: la cuenta rechazó la petición.", next_step_plain: "Vuelve a proponerla.", can_resume: false },
              campaign_entity_ref: null,
              activated_at: null,
              undo_deadline: null,
            },
            "failed",
          ),
        ),
      ),
    );
    const user = userEvent.setup();
    mount();
    expect(await screen.findByText("No se ha podido crear la campaña. No se ha creado nada ni se ha gastado nada.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Continuar" })).not.toBeInTheDocument();
    expect(screen.queryByText(/rechazó la petición/)).not.toBeVisible();
    await user.click(screen.getByText("Ver por qué"));
    expect(screen.getByText(/rechazó la petición/)).toBeVisible();
  });
});
