import { beforeEach, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { http, HttpResponse } from "msw";
import { server } from "@/mocks/server";
import { API_BASE } from "@/mocks/handlers/apiBase";
import { setMockSessionForTests } from "@/mocks/handlers";
import { getProposalDetail } from "@/mocks/fixtures/proposals";
import { proposalDetailSchema, type ProposalDetail } from "@/api/schemas/proposals";
import { CampaignCreationPanel } from "./CampaignCreationPanel";
import { axe } from "jest-axe";

const detail = (platform = "google"): ProposalDetail => proposalDetailSchema.parse({ ...getProposalDetail("prop_006"), action_kind: "create_campaign", platform, creation_plan: null, creation_plan_error: "campaign_creation_plan_required" });
function mount(data = detail()) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const reload = vi.fn();
  const tree = (value: ProposalDetail) => <QueryClientProvider client={queryClient}><MemoryRouter initialEntries={["/?business_id=biz_ejemplo"]}><CampaignCreationPanel detail={value} stale={false} onReload={reload} /></MemoryRouter></QueryClientProvider>;
  return { ...render(tree(data)), reload, tree };
}
beforeEach(() => setMockSessionForTests(true));

it("keeps the creation dialog labeled and keyboard-dismissable", async () => {
  const user=userEvent.setup(); mount(detail());
  const opener=screen.getByRole("button",{name:"Completar plan de creación"});
  await user.click(opener);
  expect((await axe(screen.getByRole("dialog"),{rules:{"color-contrast":{enabled:false}}})).violations).toEqual([]);
  await user.keyboard("{Escape}");
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(opener).toHaveFocus();
});

it("starts without inferred policy/networks and saves exact reviewed plan+hash, never approves", async () => {
  let patch: Record<string, unknown> | undefined; let approves = 0;
  server.use(http.patch(`${API_BASE}/proposals/:id`, async ({request}) => { patch = await request.json() as Record<string, unknown>; return HttpResponse.json({diff_hash:"new-reviewed-hash"}); }), http.post(`${API_BASE}/proposals/:id/approve`, () => { approves++; return HttpResponse.json({}); }));
  const user=userEvent.setup(), data=detail(), view=mount(data);
  await user.click(screen.getByRole("button",{name:"Completar plan de creación"}));
  const dialog=screen.getByRole("dialog");
  expect(within(dialog).getAllByRole("combobox").every(select => (select as HTMLSelectElement).value === "")).toBe(true);
  await user.type(screen.getByLabelText("Nombre de campaña"), "Búsqueda revisada");
  await user.type(screen.getByLabelText("Presupuesto diario (EUR)"), "999999999999.99");
  await user.click(screen.getByRole("button",{name:"Revisar plan"}));
  expect(within(dialog).getByRole("alert")).toBeInTheDocument();
  await user.selectOptions(screen.getByLabelText("Publicidad política en la Unión Europea"),"DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING");
  for(const label of ["Búsqueda de Google","Red de búsqueda","Red de contenido","Socios de búsqueda"]) await user.selectOptions(screen.getByLabelText(label),"no");
  await user.click(screen.getByRole("button",{name:"Revisar plan"}));
  expect(screen.getByText("999999999999.99 EUR")).toBeInTheDocument();
  expect(patch).toBeUndefined();
  // Refresh while reviewing must not change the CAS hash of this draft.
  view.rerender(view.tree({...data,diff:{...data.diff,diff_hash:"background-new-hash"}}));
  await user.click(screen.getByRole("button",{name:"Guardar plan sin aprobar"}));
  await waitFor(()=>expect(patch).toBeDefined());
  expect(Object.keys(patch!).sort()).toEqual(["creation_plan","diff_hash"]);
  expect(patch?.diff_hash).toBe(data.diff.diff_hash);
  expect((patch?.creation_plan as {daily_budget:{amount:string};status:string}).daily_budget.amount).toBe("999999999999.99");
  expect((patch?.creation_plan as {status:string}).status).toBe("PAUSED");
  expect(approves).toBe(0); expect(view.reload).toHaveBeenCalled();
});

it("accepts a confirmed unchanged plan without a false hash conflict or approval", async () => {
  const plan = {schema_version:1,platform:"meta",name:"Tráfico",status:"PAUSED",daily_budget:{amount:"10.00",currency:"EUR"},native:{objective:"OUTCOME_TRAFFIC",buying_type:"AUCTION",bid_strategy:"LOWEST_COST_WITHOUT_CAP",special_ad_categories:[],special_ad_category_country:[]}};
  const data={...detail("meta"),creation_plan:plan,creation_plan_error:null};
  let patch:Record<string,unknown>|undefined; let approves=0;
  server.use(http.patch(`${API_BASE}/proposals/:id`,async({request})=>{patch=await request.json() as Record<string,unknown>;return HttpResponse.json({diff_hash:data.diff.diff_hash});}),http.post(`${API_BASE}/proposals/:id/approve`,()=>{approves++;return HttpResponse.json({});}));
  const user=userEvent.setup(),view=mount(data);
  await user.click(screen.getByRole("button",{name:"Editar plan de creación"}));
  await user.click(screen.getByRole("button",{name:"Revisar plan"}));
  await user.click(screen.getByRole("button",{name:"Guardar plan sin aprobar"}));
  await waitFor(()=>expect(screen.queryByRole("dialog")).toBeNull());
  expect(patch).toEqual({diff_hash:data.diff.diff_hash,creation_plan:plan});
  expect(approves).toBe(0);expect(view.reload).toHaveBeenCalled();
});

it("requires explicit Meta objective/categories/countries; a third decimal is not rounded", async () => {
  const user=userEvent.setup(); mount(detail("meta"));
  await user.click(screen.getByRole("button",{name:"Completar plan de creación"}));
  await user.type(screen.getByLabelText("Nombre de campaña"),"Viviendas");
  await user.type(screen.getByLabelText("Presupuesto diario (EUR)"),"20.001");
  await user.selectOptions(screen.getByLabelText("Objetivo de Meta"),"OUTCOME_LEADS");
  await user.selectOptions(screen.getByLabelText("Categorías especiales"),"selected");
  await user.click(screen.getByLabelText("Vivienda"));
  await user.click(screen.getByRole("button",{name:"Revisar plan"}));
  expect(screen.queryByRole("button",{name:"Guardar plan sin aprobar"})).toBeNull();
  await user.clear(screen.getByLabelText("Presupuesto diario (EUR)")); await user.type(screen.getByLabelText("Presupuesto diario (EUR)"),"20,01");
  await user.click(screen.getByRole("button",{name:"Revisar plan"}));
  expect(screen.queryByRole("button",{name:"Guardar plan sin aprobar"})).toBeNull();
  await user.type(screen.getByLabelText("Países de categorías especiales (ISO2)"),"ES");
  await user.click(screen.getByRole("button",{name:"Revisar plan"}));
  expect(screen.getByText("20.01 EUR")).toBeInTheDocument();
  expect(screen.getByText("Vivienda")).toBeInTheDocument();
  expect(screen.getByText("ES")).toBeInTheDocument();
});

it("preserves a reviewed draft on409 and never resubmits it over a new hash", async () => {
  let writes=0;
  server.use(http.patch(`${API_BASE}/proposals/:id`,()=>{writes++;return HttpResponse.json({error:{code:"DIFF_CHANGED",message:"Updated"}},{status:409});}));
  const user=userEvent.setup(); mount(detail("meta"));
  await user.click(screen.getByRole("button",{name:"Completar plan de creación"}));
  await user.type(screen.getByLabelText("Nombre de campaña"),"Tráfico"); await user.type(screen.getByLabelText("Presupuesto diario (EUR)"),"10");
  await user.selectOptions(screen.getByLabelText("Objetivo de Meta"),"OUTCOME_TRAFFIC"); await user.selectOptions(screen.getByLabelText("Categorías especiales"),"none");
  await user.click(screen.getByRole("button",{name:"Revisar plan"})); await user.click(screen.getByRole("button",{name:"Guardar plan sin aprobar"}));
  await waitFor(()=>expect(screen.getByRole("button",{name:"Guardar plan sin aprobar"})).toBeDisabled());
  expect(within(screen.getByRole("dialog")).getByRole("alert")).toHaveTextContent("Conservamos este borrador");
  expect(writes).toBe(1);
  await user.keyboard("a"); expect(writes).toBe(1);
});
