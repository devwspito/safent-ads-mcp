import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it } from "vitest";
import { server } from "@/mocks/server";
import { API_BASE } from "@/mocks/handlers/apiBase";
import type { LaunchPlan } from "@/api/queries/launchPlans";
import { LaunchPreparation } from "./LaunchPreparation";

const plan: LaunchPlan = { slug: "opening", title: "Opening", summary: "Welcome", proposal_id: null, landing_url: "/eventos/opening", blockers: [], documents: [], video_slots: [], revision: "a".repeat(64), review: { approved: true, approved_at: "2026-09-19T00:00:00Z" } };
const job = { id: "job-1", slug: "opening", revision: plan.revision, state: "queued" as const, attempts: 0, message: "Esperando un runtime conectado.", created_at: "2026-09-19T00:00:00Z", updated_at: "2026-09-19T00:00:00Z", lease_until: null, result: null, authorizes_spend: false as const };
function show(value: LaunchPlan) { return render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><LaunchPreparation plan={value} businessId="business-a" /></QueryClientProvider>); }

describe("LaunchPreparation", () => {
  beforeEach(() => server.use(http.get(`${API_BASE}/runtime/connections`, () => HttpResponse.json({ items: [] }))));
  it("does not claim an approved old plan is already being prepared; explicit start only", async () => {
    let started = false;
    server.use(http.post(`${API_BASE}/launch-plans/opening/prepare`, async ({ request }) => { expect(await request.json()).toEqual({ revision: plan.revision }); started = true; return HttpResponse.json(plan.review); }));
    show(plan);
    expect(started).toBe(false);
    expect(screen.getByText("Preparación pendiente de iniciar")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /Iniciar preparación/ }));
    expect(started).toBe(true);
  });
  it("shows a queued job, disconnected bridge, and separate spend boundary", async () => {
    show({ ...plan, preparation: job });
    expect(screen.getByText("Esperando runtime")).toBeInTheDocument();
    expect(await screen.findByText(/Sin conector activo/)).toBeInTheDocument();
    expect(screen.getByText(/No publica anuncios, activa gasto/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Iniciar preparación/ })).not.toBeInTheDocument();
  });
  it("shows the actual reason and offers a retry for blocked results", async () => {
    let retried = false;
    const blocked = { ...job, state: "blocked" as const, result: { summary: "Missing data", blockers: ["Falta presupuesto confirmado"], draft_id: null, draft_revision: null, published: false as const, activation_blockers: [] } };
    server.use(http.post(`${API_BASE}/runtime/jobs/job-1/retry`, () => { retried = true; return HttpResponse.json(job); }));
    show({ ...plan, preparation: blocked });
    expect(screen.getByText("Falta presupuesto confirmado")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Reintentar preparación" }));
    expect(retried).toBe(true);
  });
  it("never creates a bridge credential just by opening the detail", async () => {
    let minted = false;
    server.use(http.post(`${API_BASE}/runtime/connections`, () => { minted = true; return HttpResponse.json({ id: "one", token: "test-credential", expires_in_days: 30 }); }));
    show(plan);
    await userEvent.click(screen.getByText(/Conector con Codex/));
    expect(minted).toBe(false);
    await userEvent.click(screen.getByRole("button", { name: /Crear acceso de preparación/ }));
    expect(minted).toBe(true);
    expect(await screen.findByLabelText("Clave del conector")).toHaveAttribute("type", "password");
  });
});
