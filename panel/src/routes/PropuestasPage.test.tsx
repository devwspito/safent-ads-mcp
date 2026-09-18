import { http, HttpResponse } from "msw";
import { API_BASE } from "@/mocks/handlers/apiBase";
import { server } from "@/mocks/server";
import { describe, expect, it, beforeEach } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { setMockSessionForTests } from "@/mocks/handlers";
import { PropuestasPage } from "./PropuestasPage";
import { getProposalDetail, listProposalGroups } from "@/mocks/fixtures/proposals";

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter
        initialEntries={["/propuestas?business_id=biz_ejemplo"]}
      >
        <Routes>
          <Route path="/propuestas" element={<PropuestasPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("PropuestasPage", () => {
  beforeEach(() => setMockSessionForTests(true));

  it("lists editorial launch plans under the page header without a contradictory empty inbox", async () => {
    const baseline = listProposalGroups("urgency");
    server.use(
      http.get(`${API_BASE}/proposals`, () => HttpResponse.json({ ...baseline, pending_count: 0, groups: [] })),
      http.get(`${API_BASE}/launch-plans`, () => HttpResponse.json({ items: [{
        slug: "opening", title: "Opening proposal", summary: "Personal invitation", proposal_id: null,
        landing_url: "/eventos/opening", blockers: ["Not ready to launch"], documents: [{ title: "Strategy", text: "Full private document" }],
        video_slots: [], revision: "a".repeat(64), review: { approved: false, approved_at: null },
      }] })),
    );
    renderPage();
    const card = await screen.findByRole("link", { name: /Opening proposal.*Ver detalle/ });
    expect(screen.getByRole("heading", { name: "Propuestas" }).compareDocumentPosition(card) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(screen.getByText(/1 plan disponible/)).toBeInTheDocument();
    expect(screen.queryByText("Nada que decidir")).not.toBeInTheDocument();
    expect(screen.queryByText("Nada pendiente ahora mismo")).not.toBeInTheDocument();
    expect(screen.queryByText("Full private document")).not.toBeInTheDocument();
  });

  it("bloquea crear sin plan incluso con flags de expansión/lote incorrectos y atajos", async () => {
    let approvals=0;
    const item={...getProposalDetail("prop_006"),action_kind:"create_campaign",entity_name:"Creación pendiente",requires_expansion:false,creation_plan:null,creation_plan_error:"campaign_creation_plan_required"};
    const baseline=listProposalGroups("urgency");
    const inbox={...baseline,groups:[{...baseline.groups[0],batch_eligible:true,count:1,proposals:[item]}]};
    server.use(http.get(`${API_BASE}/proposals`,()=>HttpResponse.json(inbox)),http.get(`${API_BASE}/proposals/:id`,()=>HttpResponse.json(item)),http.post(`${API_BASE}/proposals/:id/approve`,()=>{approvals++;return HttpResponse.json({});}),http.post(`${API_BASE}/proposals/batch/approve`,()=>{approvals++;return HttpResponse.json({});}));
    const user=userEvent.setup(); renderPage();
    await screen.findByText("Crear una campaña nueva");
    expect(screen.getByRole("button",{name:"Revisar y aprobar"})).toBeEnabled();
    expect(screen.queryByRole("button",{name:"Aprobar las 1"})).toBeNull();
    await user.click(screen.getByRole("button",{name:"Revisar y aprobar"}));
    expect(await screen.findByText(/Falta un plan ejecutable/)).toBeInTheDocument();
    expect(screen.getByRole("button",{name:"Aprobar"})).toBeDisabled();
    await user.click(screen.getByText("Crear una campaña nueva")); await user.keyboard("aA");
    expect(approvals).toBe(0);
  });

  it("guardar creación no aprueba y la aprobación posterior usa el nuevo hash revisado", async () => {
    let approval: Record<string,unknown>|undefined;
    let item={...getProposalDetail("prop_006")!,action_kind:"create_campaign",platform:"meta",entity_name:"Creación revisada",requires_expansion:true,creation_plan:null as Record<string,unknown>|null,creation_plan_error:"campaign_creation_plan_required" as string|null};
    const baseline=listProposalGroups("urgency");
    server.use(http.get(`${API_BASE}/proposals`,()=>HttpResponse.json({...baseline,groups:[{...baseline.groups[0],batch_eligible:false,count:1,proposals:[item]}]})),http.get(`${API_BASE}/proposals/:id`,()=>HttpResponse.json(item)),http.patch(`${API_BASE}/proposals/:id`,async({request})=>{
      const body=await request.json() as {diff_hash:string;creation_plan:Record<string,unknown>};
      expect(body.diff_hash).toBe(item.diff.diff_hash);
      item={...item,creation_plan:body.creation_plan,creation_plan_error:null,diff:{...item.diff,diff_hash:"new-creation-hash"}};
      return HttpResponse.json({diff_hash:item.diff.diff_hash});
    }),http.post(`${API_BASE}/proposals/:id/approve`,async({request})=>{approval=await request.json() as Record<string,unknown>;return HttpResponse.json({authorization_id:"created-auth",execution_id:"created-exec",execution_scheduled_at:new Date(Date.now()+30000).toISOString(),undo_deadline:null,grace_seconds:30});}));
    const user=userEvent.setup(); renderPage();
    await screen.findByText("Crear una campaña nueva"); await user.click(screen.getByRole("button",{name:"Detalle"}));
    await user.click(await screen.findByRole("button",{name:"Completar plan de creación"}));
    await user.type(screen.getByLabelText("Nombre de campaña"),"Campaña pausada"); await user.type(screen.getByLabelText("Presupuesto diario (EUR)"),"15.35");
    await user.selectOptions(screen.getByLabelText("Objetivo de Meta"),"OUTCOME_TRAFFIC");await user.selectOptions(screen.getByLabelText("Categorías especiales"),"none");
    await user.click(screen.getByRole("button",{name:"Revisar plan"}));await user.click(screen.getByRole("button",{name:"Guardar plan sin aprobar"}));
    await waitFor(()=>expect(screen.queryByRole("dialog")).toBeNull());
    expect(approval).toBeUndefined();
    await waitFor(()=>expect(screen.getByRole("button",{name:"Aprobar"})).toBeEnabled());
    await user.click(screen.getByRole("button",{name:"Aprobar"}));
    await waitFor(()=>expect(approval?.diff_hash).toBe("new-creation-hash"));
  });

  it("acepta la respuesta real con undo_deadline null y usa la fecha programada", async () => {
    server.use(http.post(`${API_BASE}/proposals/:id/approve`, () => HttpResponse.json({
      authorization_id: "auth-real", execution_id: "exec-real",
      execution_scheduled_at: new Date(Date.now() + 30000).toISOString(),
      undo_deadline: null, grace_seconds: 30,
    })));
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("Recomendado");
    const approve = screen.getAllByRole("button", { name: "Aprobar" }).find((button) => !button.hasAttribute("disabled"))!;
    await user.click(approve);
    expect(await screen.findByRole("button", { name: "Deshacer" })).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("muestra errores de aprobación sin rechazos de promesa sin capturar", async () => {
    server.use(http.post(`${API_BASE}/proposals/:id/approve`, () =>
      HttpResponse.json({ error: { code: "DIFF_CHANGED", message: "Cambio actualizado" } }, { status: 409 })));
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("Recomendado");
    await user.click(screen.getAllByRole("button", { name: "Aprobar" }).find((button) => !button.hasAttribute("disabled"))!);
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Deshacer" })).not.toBeInTheDocument();
  });

  it("agrupa por causa dentro de su sección de urgencia", async () => {
    renderPage();
    expect(await screen.findByText("Recomendado")).toBeInTheDocument();
    expect(screen.getAllByText(/Limitada por presupuesto con coste por lead bajo objetivo/).length).toBeGreaterThan(0);
  });

  it("en riesgo alto, el botón principal abre el detalle en vez de deshabilitar Aprobar sin explicación", async () => {
    const user = userEvent.setup();
    renderPage();
    const causeRow = await screen.findByText(/Sin conversiones en 21 días con gasto sostenido/);
    const card = causeRow.closest("div")?.parentElement as HTMLElement;
    const reviewButton = within(card).getByRole("button", { name: "Revisar y aprobar" });
    expect(reviewButton).toBeEnabled();

    await user.click(reviewButton);
    await waitFor(() => expect(within(card).getByRole("button", { name: "Aprobar" })).toBeEnabled());
  });

  it("aprobar una propuesta de bajo riesgo abre la barra de deshacer", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("Recomendado");

    const rows = screen.getAllByRole("button", { name: "Aprobar" }).filter((btn) => !btn.hasAttribute("disabled"));
    expect(rows.length).toBeGreaterThan(0);
    await user.click(rows[0]!);

    expect(await screen.findByRole("button", { name: "Deshacer" })).toBeInTheDocument();
  });

  it("el menú «⋯» es un botón real con «Más tarde» y «Ver detalle», Esc cierra y devuelve el foco", async () => {
    let postponedUntil: string | undefined;
    server.use(
      http.post(`${API_BASE}/proposals/:id/postpone`, async ({ request }) => {
        postponedUntil = ((await request.json()) as { until: string }).until;
        return HttpResponse.json({});
      }),
    );
    const user = userEvent.setup();
    renderPage();
    const entityLabel = await screen.findByText("Meta Lookalike Clientes");
    const row = entityLabel.closest("[data-proposal-id]") as HTMLElement;

    const menuButton = within(row).getByRole("button", { name: /Más opciones para Meta Lookalike Clientes/ });
    expect(menuButton).toHaveAttribute("aria-haspopup", "menu");
    await user.click(menuButton);
    const menu = screen.getByRole("menu", { name: /Más opciones para Meta Lookalike Clientes/ });
    expect(within(menu).getByRole("menuitem", { name: "Más tarde" })).toBeInTheDocument();
    expect(within(menu).getByRole("menuitem", { name: "Ver detalle" })).toBeInTheDocument();

    await user.keyboard("{Escape}");
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
    expect(menuButton).toHaveFocus();

    await user.click(menuButton);
    await user.click(within(screen.getByRole("menu")).getByRole("menuitem", { name: "Más tarde" }));

    await waitFor(() => expect(postponedUntil).toBeDefined());
    expect(new Date(postponedUntil!).getTime()).toBeGreaterThan(Date.now());

    // Confirmación inline con la fecha real, y un sitio permanente para encontrarla — nada
    // desaparece en silencio al posponer (encargo (7)).
    expect(await screen.findByText(/Pospuesta: vuelve el/)).toBeInTheDocument();
    const postponedToggle = screen.getByRole("button", { name: /Pospuestas \(1\)/ });
    await user.click(postponedToggle);
    expect(screen.getByText(/Meta Lookalike Clientes · vuelve el/)).toBeInTheDocument();
  });

  it("aprobar con un diff_hash caducado por edición ajena muestra el error real, nunca un éxito falso", async () => {
    const user = userEvent.setup();
    renderPage();
    const entityLabel = await screen.findByText("Búsqueda Genérica");
    const row = entityLabel.closest("[data-proposal-id]") as HTMLElement;

    server.use(
      http.post(`${API_BASE}/proposals/:id/approve`, () =>
        HttpResponse.json({ error: { code: "DIFF_CHANGED", message: "La propuesta cambió mientras decidías. Revisa el valor vigente." } }, { status: 409 })),
    );
    await user.click(within(row).getByRole("button", { name: "Aprobar" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/cambió mientras tanto/);
    expect(screen.queryByRole("button", { name: "Deshacer" })).not.toBeInTheDocument();
  });
});

describe("PropuestasPage — creación de campaña real, payload companion 0.2.20", () => {
  beforeEach(() => setMockSessionForTests(true));

  it("Google: nombre del plan, dinero real y cuenta legible, nunca la referencia cruda", async () => {
    renderPage();
    const headline = await screen.findByText("Crear la campaña «Acme | Primera consulta gratis | Centro Norte»");
    const row = headline.closest("[data-proposal-id]") as HTMLElement;

    expect(within(row).queryByText(/google:account:/)).not.toBeInTheDocument();
    expect(row).toHaveTextContent("Google · Cuenta 1000000001");
    expect(within(row).getByText(/\+10,00 €/)).toBeInTheDocument();
    expect(within(row).getByText(/300 €/)).toBeInTheDocument();
    expect(within(row).getByRole("button", { name: "Revisar y aprobar" })).toBeEnabled();
  });

  it("Meta: misma cabecera consistente, sin «Aprobar» deshabilitado sin explicación", async () => {
    renderPage();
    const headline = await screen.findByText("Crear la campaña «Acme | Primera consulta gratis | Meta Leads»");
    const row = headline.closest("[data-proposal-id]") as HTMLElement;

    expect(row).toHaveTextContent("Meta · Cuenta act_100000000000002");
    expect(within(row).getByRole("button", { name: "Revisar y aprobar" })).toBeEnabled();
    expect(within(row).queryByRole("button", { name: "Aprobar" })).not.toBeInTheDocument();
  });

  it("el detalle esconde el ruido sin información y enseña «Qué se crea» en su lugar", async () => {
    const user = userEvent.setup();
    renderPage();
    const headline = await screen.findByText("Crear la campaña «Acme | Primera consulta gratis | Centro Norte»");
    const row = headline.closest("[data-proposal-id]") as HTMLElement;
    await user.click(within(row).getByRole("button", { name: "Revisar y aprobar" }));

    const summaryLabel = await screen.findByText("Qué se crea");
    const summary = summaryLabel.parentElement as HTMLElement;
    expect(screen.queryByText("Datos (No disponible)")).not.toBeInTheDocument();
    expect(screen.queryByText("Regla")).not.toBeInTheDocument();
    expect(screen.queryByText("Sin histórico")).not.toBeInTheDocument();
    expect(screen.queryByText("Impacto estimado")).not.toBeInTheDocument();
    expect(screen.queryByText(/no hay evaluación/)).not.toBeInTheDocument();
    expect(within(summary).getByText("Acme | Primera consulta gratis | Centro Norte")).toBeInTheDocument();
    expect(within(summary).getByText("En pausa hasta que la actives")).toBeInTheDocument();
    expect(within(summary).getByText("https://example.com/")).toBeInTheDocument();
    expect(screen.getByText(/Este paso crea sólo la campaña y su presupuesto/)).toBeInTheDocument();
  });
});

describe("PropuestasPage — los cuatro estados de pantalla (design.md §13.2)", () => {
  beforeEach(() => setMockSessionForTests(true));

  it("cargando: esqueleto visible, nada más", async () => {
    server.use(http.get(`${API_BASE}/proposals`, () => new Promise(() => {})));
    renderPage();
    expect(await screen.findByRole("status", { name: "Cargando datos…" })).toBeInTheDocument();
    expect(screen.queryByText("Recomendado")).not.toBeInTheDocument();
  });

  it("vacío: nada pendiente muestra el título y la acción que resuelve, sin tabla", async () => {
    const baseline = listProposalGroups("urgency");
    server.use(http.get(`${API_BASE}/proposals`, () => HttpResponse.json({ ...baseline, pending_count: 0, groups: [] })));
    renderPage();
    expect(await screen.findByText("Nada que decidir")).toBeInTheDocument();
    expect(screen.getByText(/Safent vigila tus campañas/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Ver resultados" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Aprobar" })).not.toBeInTheDocument();
  });

  it("error de lectura: bloque local con Reintentar, la cabecera sigue operable", async () => {
    server.use(http.get(`${API_BASE}/proposals`, () => HttpResponse.json({ error: { code: "FAILED", message: "fallo" } }, { status: 500 })));
    renderPage();
    expect(await screen.findByRole("button", { name: "Reintentar" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Propuestas", level: 1 })).toBeInTheDocument();
  });

  it("vacío por el filtro de plataforma ≠ vacío de verdad: dice cuántas hay en la otra y deja «Ver todas»", async () => {
    const baseline = listProposalGroups("urgency");
    const onlyGoogle = { ...baseline, groups: baseline.groups.filter((g) => g.proposals.every((p) => p.platform === "google")) };
    const googleCount = onlyGoogle.groups.flatMap((g) => g.proposals).length;
    server.use(http.get(`${API_BASE}/proposals`, () => HttpResponse.json(onlyGoogle)));
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const user = userEvent.setup();
    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={["/propuestas?business_id=biz_ejemplo&platform=meta"]}>
          <Routes>
            <Route path="/propuestas" element={<PropuestasPage />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );
    expect(await screen.findByText("Nada en Meta")).toBeInTheDocument();
    expect(screen.getByText(`Hay ${googleCount} propuestas en Google.`)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Ver todas" }));
    expect(await screen.findByText("Recomendado")).toBeInTheDocument();
  });

  it("datos: al menos una fila real, en el formato de fila de design.md §2.2", async () => {
    renderPage();
    expect(await screen.findByText("Recomendado")).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "Aprobar" }).length).toBeGreaterThan(0);
  });
});
