import { expect, it } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { http, HttpResponse } from "msw";
import { server } from "@/mocks/server";
import { WorkspaceDetail } from "./WorkspacesPage";
import type { Workspace } from "@/api/queries/workspaces";

const business = "11111111-1111-1111-1111-111111111111";
const project = "22222222-2222-2222-2222-222222222222";
const draft = "33333333-3333-3333-3333-333333333333";
const workspace: Workspace = {
  id: project, business_id: business, workspace_key: "launch", revision: 2, contract_version: 1, updated_at: "2026-09-19T00:00:00Z",
  brief: { title: "Opening", objective: "Meet customers", schedule: "17 October", total_budget: { amount: "650", currency: "EUR" }, notes: "Owner decisions", source_slug: null, resources: [] },
  accounts: [], campaigns_has_more: false, activity: [], runtime_jobs: [],
  capabilities: { shared_context: true, prepare_paused_proposal: true, automatic_activation: false, runtime_role: "draft_preparation", budget_is_enforced_cap: false },
  campaigns: [{ draft: { draft_id: draft, draft_key: "campaign", business_id: business, revision: 4, state: "draft", brief: { title: "Meet customers", platform: "meta", daily_budget: { amount: "15", currency: "EUR" }, landing_url: "https://example.com/event", notes: null }, missing_fields: [], proposal_id: null, executable: false, updated_at: "2026-09-19T00:00:00Z" }, proposal: null, execution: null, step: { state: "ready", label: "Preparar revisión de creación en pausa", authorizes_spend: false } }],
};
function mount(data = workspace) {
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })}><MemoryRouter><WorkspaceDetail workspace={data} /></MemoryRouter></QueryClientProvider>);
}

it("saves context with revision and no activation authority", async () => {
  const calls: Record<string, unknown>[] = [];
  server.use(http.post("/api/v1/workspaces", async ({ request }) => { calls.push(await request.json() as Record<string, unknown>); return HttpResponse.json(workspace); }));
  mount();
  const user = userEvent.setup();
  await user.clear(screen.getByLabelText("Fecha y horario confirmados"));
  await user.type(screen.getByLabelText("Fecha y horario confirmados"), "18 October");
  await user.click(screen.getByRole("button", { name: "Guardar contexto compartido" }));
  await waitFor(() => expect(calls).toHaveLength(1));
  expect(calls[0]).toMatchObject({ workspace_key: "launch", expected_revision: 2, changes: { schedule: "18 October", objective: "Meet customers" } });
  expect(screen.getByText(/No acredita un límite aplicado/)).toBeInTheDocument();
});

it("prepares through the shared command without needing videos or invoking approval", async () => {
  const calls: unknown[] = [];
  server.use(http.post(`/api/v1/workspaces/${project}/prepare-campaign`, async ({ request }) => { calls.push(await request.json()); return HttpResponse.json(workspace); }));
  mount(); const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "Campañas" }));
  await user.click(screen.getByRole("button", { name: "Preparar revisión de creación en pausa" }));
  await waitFor(() => expect(calls).toEqual([{ draft_id: draft, expected_revision: 4 }]));
  expect(await screen.findByText(/Propuesta preparada. Revisa el cambio exacto/)).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /^Activar/ })).not.toBeInTheDocument();
});

it("links the exact existing proposal instead of offering another preparation", async () => {
  const proposal = "44444444-4444-4444-4444-444444444444";
  const item = workspace.campaigns[0]!;
  mount({ ...workspace, campaigns: [{ ...item, proposal: { id: proposal, state: "pending", diff_hash: "a".repeat(64), expires_at: "2026-09-22T00:00:00Z" }, step: { state: "approval", label: "Revisar y aprobar creación en pausa", authorizes_spend: false } }] });
  await userEvent.click(screen.getByRole("button", { name: "Campañas" }));
  expect(screen.getByRole("link", { name: "Ver decisión y aprobar" })).toHaveAttribute("href", `/propuestas?business_id=${business}&proposal_id=${proposal}`);
  expect(screen.queryByRole("button", { name: "Preparar revisión de creación en pausa" })).not.toBeInTheDocument();
});

it("shows optimistic-conflict errors without overwriting newer context", async () => {
  server.use(http.post("/api/v1/workspaces", () => HttpResponse.json({ error: { code: "WORKSPACE_CHANGED", message: "Cambió la revisión" } }, { status: 409 })));
  mount();
  await userEvent.click(screen.getByRole("button", { name: "Guardar contexto compartido" }));
  expect(await screen.findByRole("alert")).toHaveTextContent(/Recarga antes/);
});
