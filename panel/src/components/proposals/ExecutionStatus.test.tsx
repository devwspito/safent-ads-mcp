import { useState } from "react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useSearchParams } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { http, HttpResponse } from "msw";
import { API_BASE } from "@/mocks/handlers/apiBase";
import { server } from "@/mocks/server";
import { setMockSessionForTests } from "@/mocks/handlers";
import { getProposalDetail } from "@/mocks/fixtures/proposals";
import { ExecutionStatus } from "./ExecutionStatus";
import { ProposalDetailPanel } from "./ProposalDetailPanel";
import { PropuestasPage } from "@/routes/PropuestasPage";
import { UnconfirmedExecutions } from "./UnconfirmedExecutions";

const execution = {
  execution_id: "exec-unknown", proposal_id: "prop_001", entity_name: "Búsqueda Marca",
  outcome: "UNKNOWN", error_code: "remote_outcome_unknown", applied_value: null, previous_value: 100,
  estimated_impact: { amount: 20, currency: "EUR" }, started_at: "2026-09-11T10:00:00Z",
  undo_deadline: null, finished_at: null, undone_at: null, compensating_proposal_id: null,
};

let clients: QueryClient[] = [];
function mount(children = <ExecutionStatus executionId="exec-unknown" proposalId="prop_001" />) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  clients.push(client);
  return render(<QueryClientProvider client={client}><MemoryRouter initialEntries={["/?business_id=biz_ejemplo"]}
>{children}</MemoryRouter></QueryClientProvider>);
}

beforeEach(() => setMockSessionForTests(true));
afterEach(() => { clients.forEach((client) => client.clear()); clients = []; });

describe("execution outcome in the real proposal detail", () => {
  it("shows unresolved executions after reload even when the pending inbox is empty", async () => {
    let queriedBusiness = "";
    server.use(
      http.get(`${API_BASE}/executions`, ({ request }) => {
        const url = new URL(request.url);
        queriedBusiness = url.searchParams.get("business_id") ?? "";
        expect(url.searchParams.get("outcome")).toBe("UNKNOWN");
        return HttpResponse.json({ items: [execution] });
      }),
      http.get(`${API_BASE}/proposals`, () => HttpResponse.json({ lens: "urgency", pending_count: 0, deferred_count: 0, total_impact: null, next_expiring_at: null, attention_budget: null, next_cursor: null, groups: [] })),
    );
    mount(<PropuestasPage />);
    expect(await screen.findByRole("region", { name: "Cambios sin confirmar" })).toBeInTheDocument();
    expect(await screen.findByText("Nada que decidir")).toBeInTheDocument();
    expect(queriedBusiness).toBe("biz_ejemplo");
    expect(screen.queryByRole("button", { name: /aprobar|deshacer/i })).not.toBeInTheDocument();
  });

  it("does not report an empty unresolved queue when the list cannot be read", async () => {
    server.use(http.get(`${API_BASE}/executions`, () => new HttpResponse(null, { status: 503 })));
    mount(<UnconfirmedExecutions businessId="biz_ejemplo" />);
    expect(await screen.findByText(/No podemos confirmar si hay cambios pendientes/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Actualizar estado" })).toBeInTheDocument();
  });

  it("makes UNKNOWN reachable from the proposal and exposes no write or undo", async () => {
    server.use(
      http.get(`${API_BASE}/proposals/prop_001`, () => HttpResponse.json({ ...getProposalDetail("prop_001"), state: "executing", execution_id: execution.execution_id })),
      http.get(`${API_BASE}/executions/exec-unknown`, () => HttpResponse.json(execution)),
    );
    mount(<ProposalDetailPanel proposalId="prop_001" />);
    expect(await screen.findByText("Resultado pendiente de confirmar")).toBeInTheDocument();
    expect(screen.getByText(/mantiene la reserva de seguridad/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /deshacer|aprobar|reintentar/i })).not.toBeInTheDocument();
  });

  it("refreshes only with GET and shows reconciliation when a receipt is confirmed", async () => {
    const user = userEvent.setup();
    let reads = 0;
    server.use(http.get(`${API_BASE}/executions/exec-unknown`, () => HttpResponse.json(++reads === 1 ? execution : {
      ...execution, outcome: "SUCCEEDED", error_code: null, finished_at: "2026-09-11T10:01:00Z", applied_value: 120,
    })));
    mount();
    await screen.findByText("Resultado pendiente de confirmar");
    await user.tab();
    expect(screen.getByRole("button", { name: "Actualizar estado" })).toHaveFocus();
    await user.keyboard("{Enter}");
    expect(await screen.findByText("Cambio confirmado")).toBeInTheDocument();
    expect(reads).toBe(2);
    expect(screen.queryByText(/mantiene la reserva/)).not.toBeInTheDocument();
  });

  it("keeps uncertainty visible when a status refresh fails", async () => {
    const user = userEvent.setup();
    let reads = 0;
    server.use(http.get(`${API_BASE}/executions/exec-unknown`, () => ++reads === 1 ? HttpResponse.json(execution) : new HttpResponse(null, { status: 503 })));
    mount();
    await screen.findByText("Resultado pendiente de confirmar");
    await user.click(screen.getByRole("button", { name: "Actualizar estado" }));
    expect(await screen.findByText(/último conocido/)).toBeInTheDocument();
    expect(screen.getByText("Resultado pendiente de confirmar")).toBeInTheDocument();
    expect(screen.queryByText("Cambio fallido")).not.toBeInTheDocument();
  });

  it("does not invent a result for unavailable or mismatched responses", async () => {
    server.use(http.get(`${API_BASE}/executions/exec-unknown`, () => HttpResponse.json({ ...execution, proposal_id: "another-proposal", outcome: "SUCCEEDED" })));
    mount();
    expect(await screen.findByText("Estado de ejecución no disponible")).toBeInTheDocument();
    expect(screen.queryByText("Cambio confirmado")).not.toBeInTheDocument();
  });

  it("never presents a cached outcome from the previous business", async () => {
    const user = userEvent.setup();
    let reads = 0;
    // Both targets must be authorized by /me; changing a foreign URL is not a business switch.
    server.use(http.get(`${API_BASE}/auth/me`, () => HttpResponse.json({
      owner_id: "owner", email: "owner@example.test", businesses: [
        { business_id: "biz_ejemplo", name: "Original" },
        { business_id: "other-business", name: "Otro negocio autorizado" },
      ],
    })));
    server.use(http.get(`${API_BASE}/executions/exec-unknown`, () => ++reads === 1 ? HttpResponse.json(execution) : new HttpResponse(null, { status: 403 })));
    function Switcher() {
      const [, setParams] = useSearchParams();
      return <><button onClick={() => setParams({ business_id: "other-business" })}>Cambiar negocio</button><ExecutionStatus executionId="exec-unknown" proposalId="prop_001" /></>;
    }
    mount(<Switcher />);
    await screen.findByText("Resultado pendiente de confirmar");
    await user.click(screen.getByRole("button", { name: "Cambiar negocio" }));
    expect(await screen.findByText("Estado de ejecución no disponible")).toBeInTheDocument();
    expect(screen.queryByText("Resultado pendiente de confirmar")).not.toBeInTheDocument();
  });

  it("ignores a late outcome after the visible execution changes", async () => {
    const user = userEvent.setup();
    let release!: () => void;
    const held = new Promise<void>((resolve) => { release = resolve; });
    let firstRead = false;
    server.use(http.get(`${API_BASE}/executions/:id`, async ({ params }) => {
      if (params.id === "exec-unknown") { firstRead = true; await held; return HttpResponse.json(execution); }
      return HttpResponse.json({ ...execution, execution_id: "exec-other", proposal_id: "other", outcome: "SUCCEEDED" });
    }));
    function Switcher() {
      const [other, setOther] = useState(false);
      return <><button onClick={() => setOther(true)}>Otra propuesta</button><ExecutionStatus executionId={other ? "exec-other" : "exec-unknown"} proposalId={other ? "other" : "prop_001"} /></>;
    }
    mount(<Switcher />);
    await waitFor(() => expect(firstRead).toBe(true));
    await user.click(screen.getByRole("button", { name: "Otra propuesta" }));
    await screen.findByText("Cambio confirmado");
    release();
    await waitFor(() => expect(clients[0]?.getQueryData(["execution", "biz_ejemplo", "prop_001", "exec-unknown"])).toBeDefined());
    expect(screen.queryByText("Resultado pendiente de confirmar")).not.toBeInTheDocument();
  });
});
