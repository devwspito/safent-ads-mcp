import { expect, it } from "vitest";
import { http, HttpResponse } from "msw";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { API_BASE } from "@/mocks/handlers/apiBase";
import { server } from "@/mocks/server";
import { CampaignDrafts } from "./CampaignDrafts";

const business = "11111111-1111-1111-1111-111111111111";
const draftId = "22222222-2222-2222-2222-222222222222";
const initial = { draft_id: draftId, draft_key: "idea", business_id: business, revision: 1, state: "draft", brief: { title: "Owner idea", platform: "google", daily_budget: null, landing_url: null, notes: null }, missing_fields: ["daily_budget", "landing_url", "creation_plan"], proposal_id: null, executable: false, updated_at: "2026-09-14T00:00:00Z" };
function setup(empty = false, fail = false) {
  const calls: unknown[] = [];
  let items = empty ? [] : [initial];
  server.use(
    http.get(`${API_BASE}/campaign-drafts`, () => HttpResponse.json({ items, has_more: false })),
    http.post(`${API_BASE}/campaign-drafts`, async ({ request }) => {
      expect(new URL(request.url).searchParams.get("business_id")).toBe(business);
      const body = await request.json() as { changes: Record<string, unknown> };
      calls.push(body);
      if (fail) return HttpResponse.json({ error: { code: "CAMPAIGN_DRAFT_CHANGED", message: "Cambió la versión" } }, { status: 409 });
      const result = { ...initial, revision: empty ? 1 : 2, brief: { ...initial.brief, ...body.changes } };
      items = [result as typeof initial];
      return HttpResponse.json(result);
    }),
  );
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><CampaignDrafts businessId={business} /></QueryClientProvider>);
  return calls;
}
it("shows durable incomplete drafts without an approval button or invented budget", async () => {
  const calls = setup();
  const card = await screen.findByRole("article", { name: "Borrador Owner idea" });
  expect(card).toHaveTextContent("Presupuesto: pendiente");
  expect(card).toHaveTextContent("Destino: pendiente");
  expect(within(card).getByRole("button", { name: "Preparar propuesta para revisión" })).toBeDisabled();
  expect(within(card).queryByRole("button", { name: /^Aprobar/ })).not.toBeInTheDocument();
  expect(calls).toEqual([]);
});
it("creates only after the owner submits, leaving budget and destination absent", async () => {
  const calls = setup(true); const user = userEvent.setup();
  await user.type(screen.getByLabelText("Nombre del borrador"), "Tomorrow idea");
  expect(calls).toEqual([]);
  await user.click(screen.getByRole("button", { name: "Guardar borrador" }));
  expect(await screen.findByRole("article", { name: "Borrador Tomorrow idea" })).toHaveTextContent("No puede ejecutarse");
  expect(calls).toEqual([expect.objectContaining({ changes: { title: "Tomorrow idea" } })]);
});
it("updates explicit budget and destination with revision, without promoting", async () => {
  const calls = setup(); const user = userEvent.setup();
  await user.click(await screen.findByText("Editar presupuesto, destino y notas"));
  await user.type(screen.getByLabelText("Presupuesto diario"), "30.50");
  await user.selectOptions(screen.getByLabelText("Moneda"), "EUR");
  await user.type(screen.getByLabelText("URL de reserva o destino"), "https://example.com/reserve");
  await user.click(screen.getByRole("button", { name: "Guardar cambios del borrador" }));
  await waitFor(() => expect(calls).toHaveLength(1));
  expect(calls[0]).toEqual({ draft_key: "idea", expected_revision: 1, changes: { daily_budget: { amount: "30.50", currency: "EUR" }, landing_url: "https://example.com/reserve", notes: null } });
});
it("keeps a version conflict visible and never auto-retries", async () => {
  const calls = setup(false, true); const user = userEvent.setup();
  await user.click(await screen.findByText("Editar presupuesto, destino y notas"));
  await user.type(screen.getByLabelText("Notas del borrador"), "Owner edit");
  await user.click(screen.getByRole("button", { name: "Guardar cambios del borrador" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("No se ha aprobado ni ejecutado");
  expect(calls).toHaveLength(1);
  expect(screen.getByLabelText("Notas del borrador")).toHaveValue("Owner edit");
});
