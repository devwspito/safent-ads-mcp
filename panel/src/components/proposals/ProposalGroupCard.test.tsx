import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { http, HttpResponse } from "msw";
import { server } from "@/mocks/server";
import { API_BASE } from "@/mocks/handlers/apiBase";
import { setMockSessionForTests } from "@/mocks/handlers";
import { listPackageFeedItems, resetPackagesFixtures } from "@/mocks/fixtures/packages";
import { googleSearchWithTargetingDetail } from "@/mocks/fixtures/googleSearchWithTargeting";
import { proposalDetailSchema } from "@/api/schemas/proposals";
import type { PlatformAccount } from "@/api/schemas/connections";
import type { ProposalGroup, ProposalItem } from "@/api/schemas/proposals";
import { ProposalGroupCard, type ProposalGroupActions } from "./ProposalGroupCard";

const noop: ProposalGroupActions = {
  onFocus: vi.fn(),
  onToggleExpand: vi.fn(),
  onApprove: vi.fn(),
  onReject: vi.fn(),
  onPostpone: vi.fn(),
  onBatchApprove: vi.fn(),
  onBatchReject: vi.fn(),
  onApprovePackage: vi.fn(),
  onRejectPackage: vi.fn(),
};

let clients: QueryClient[] = [];
function mount(group: ProposalGroup, accounts: PlatformAccount[] = [], expandedProposalId: string | null = null) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  clients.push(client);
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <ProposalGroupCard businessId="biz_ejemplo" group={group} focusedProposalId={null} expandedProposalId={expandedProposalId} writeDisabledReason={null} writeDisabledShortReason={null} accounts={accounts} actions={noop} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function buildProposal(overrides: Partial<ProposalItem> = {}): ProposalItem {
  return {
    action_kind: "update",
    proposal_id: "prop_test_1",
    proposed_by: null,
    entity_ref: "google:campaign:c-test",
    entity_name: "Búsqueda de prueba",
    platform: "google",
    account_label: "google:account:5e1a6c8e-2f3d-4b7a-9c1e-8f2b6a7d4c10:9b3f2a71-6d4c-4e8a-b1f0-7c5e3a9d2f44:1000000001",
    current_daily_budget: null,
    diff: { parametro: "presupuesto_diario", valor_actual: 30, valor_propuesto: 45, diff_hash: "hash-1" },
    classification: "critical",
    urgency: "critical",
    risk_level: "high",
    requires_expansion: true,
    requires_typed_confirmation: false,
    estimated_impact: { amount: 100, currency: "EUR" },
    cause: "Prueba de causa",
    expires_at: new Date(Date.now() + 3_600_000).toISOString(),
    postponed_until: null,
    state: "pending",
    ...overrides,
  };
}

function buildGroup(proposal: ProposalItem): ProposalGroup {
  return {
    group_kind: "cause",
    cause_key: "grupo-de-prueba",
    cause: "Grupo de prueba",
    count: 1,
    total_impact: proposal.estimated_impact,
    batch_eligible: false,
    closes_at: null,
    proposals: [proposal],
  };
}

const RAW_ACCOUNT_REF = "google:account:5e1a6c8e-2f3d-4b7a-9c1e-8f2b6a7d4c10:9b3f2a71-6d4c-4e8a-b1f0-7c5e3a9d2f44:1000000001";
const RAW_REF_ACCOUNT: PlatformAccount = {
  platform_account_id: RAW_ACCOUNT_REF,
  platform: "google",
  external_account_id: "1000000001",
  label: RAW_ACCOUNT_REF,
  status: "ACTIVE",
  currency: "EUR",
  timezone: "Europe/Madrid",
  api_tier: "Básico",
  token: { health: "ok", expires_at: null, checked_at: new Date().toISOString() },
  quota: { window: "24H", used_pct: null, writes_remaining: null },
  unavailable_levers: [],
  last_synced_at: null,
  last_error_code: null,
};

beforeEach(() => {
  setMockSessionForTests(true);
  resetPackagesFixtures();
});
afterEach(() => {
  clients.forEach((client) => client.clear());
  clients = [];
});

describe("ProposalGroupCard — un paquete nunca entra en un lote (api.md §1, tasks.md T052)", () => {
  it("con dos paquetes y batch_eligible=true del servidor, no aparece «Aprobar las N iguales»", () => {
    const packages = listPackageFeedItems();
    expect(packages.length).toBeGreaterThanOrEqual(2);
    const group: ProposalGroup = {
      group_kind: "cause",
      cause_key: "grupo-mixto-de-prueba",
      cause: "Grupo de prueba",
      count: 2,
      total_impact: { amount: 0, currency: "EUR" },
      // Un servidor con un fallo enviaría esto — la defensa vive también en el cliente.
      batch_eligible: true,
      closes_at: null,
      proposals: [packages[0]!, packages[1]!],
    };
    mount(group);
    expect(screen.queryByRole("button", { name: /Aprobar las \d+ iguales/ })).not.toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: /Aprobar y publicar/ })).toHaveLength(2);
  });
});

describe("ProposalGroupCard — nombre de cuenta (hotfix companion 0.2.20)", () => {
  it("cuando account_label es la referencia cruda de la cuenta, cruza con /platform-accounts y muestra «Cuenta <external_account_id>», nunca la referencia", () => {
    mount(buildGroup(buildProposal()), [RAW_REF_ACCOUNT]);
    const where = screen.getByText("Búsqueda de prueba").parentElement as HTMLElement;
    expect(where).toHaveTextContent("Google · Cuenta 1000000001");
    expect(where).not.toHaveTextContent(RAW_ACCOUNT_REF);
  });

  it("sin ninguna cuenta que cruzar, cae al comportamiento anterior en vez de romper la fila", () => {
    mount(buildGroup(buildProposal({ account_label: "Google Ads — Negocio Ejemplo" })), []);
    const where = screen.getByText("Búsqueda de prueba").parentElement as HTMLElement;
    expect(where).toHaveTextContent("Google · Negocio Ejemplo");
  });
});

describe("ProposalGroupCard — enlace al paquete (contracts/api.md §1)", () => {
  it("con package_id, la fila muestra la ficha «Paquete» enlazada al detalle", () => {
    mount(buildGroup(buildProposal({ package_id: "pkg_meta_001" })));
    const link = screen.getByRole("link", { name: "Paquete" });
    expect(link).toHaveAttribute("href", "/propuestas/paquete/pkg_meta_001");
  });

  it("sin package_id, no aparece ninguna ficha de paquete", () => {
    mount(buildGroup(buildProposal()));
    expect(screen.queryByRole("link", { name: "Paquete" })).not.toBeInTheDocument();
  });

  it("el detalle expandido de una propuesta con paquete también enlaza al paquete", async () => {
    // La fila no declara `package_id` (sin ficha propia); el detalle real de `prop_005` sí trae
    // `package_id: "pkg_meta_001"` (fixture `mocks/fixtures/proposals.ts`) al expandirse.
    mount(buildGroup(buildProposal({ proposal_id: "prop_005" })), [], "prop_005");
    const link = await screen.findByRole("link", { name: "Paquete" });
    expect(link).toHaveAttribute("href", "/propuestas/paquete/pkg_meta_001");
  });
});

describe("ProposalGroupCard — Aprobar consistente cuando exige revisar el detalle (design.md §2.3)", () => {
  it("una propuesta que exige expansión abre el detalle en vez de deshabilitar Aprobar sin más", async () => {
    const user = userEvent.setup();
    mount(buildGroup(buildProposal()));
    const reviewButton = screen.getByRole("button", { name: "Revisar y aprobar" });
    expect(reviewButton).toBeEnabled();
    expect(screen.queryByRole("button", { name: "Aprobar" })).not.toBeInTheDocument();

    await user.click(reviewButton);
    expect(noop.onToggleExpand).toHaveBeenCalledWith("prop_test_1");
    expect(noop.onApprove).not.toHaveBeenCalled();
  });

  it("sin exigir expansión, el botón principal aprueba directamente como siempre", () => {
    mount(buildGroup(buildProposal({ requires_expansion: false, proposal_id: "prop_test_2" })));
    expect(screen.getByRole("button", { name: "Aprobar" })).toBeEnabled();
    expect(screen.queryByRole("button", { name: "Revisar y aprobar" })).not.toBeInTheDocument();
  });
});

describe("ProposalGroupCard — creación Google real SEARCH + geographic_targeting (bug: .strict() bloqueaba «Revisar y aprobar» en Google mientras Meta funcionaba)", () => {
  it("expandida, enseña el presupuesto real (nunca «Sin coste extra») y no queda bloqueada por un plan que no valida", async () => {
    const rawDetail = googleSearchWithTargetingDetail();
    const detail = proposalDetailSchema.parse(rawDetail);
    server.use(http.get(`${API_BASE}/proposals/:id`, () => HttpResponse.json(rawDetail)));
    const proposal = buildProposal({
      proposal_id: detail.proposal_id,
      action_kind: "create_campaign",
      entity_name: detail.entity_name,
      diff: detail.diff,
      estimated_impact: detail.estimated_impact,
      requires_expansion: true,
      state: "pending",
    });
    const view = mount(buildGroup(proposal), [], proposal.proposal_id);
    const row = view.container.querySelector(`[data-proposal-id="${proposal.proposal_id}"]`) as HTMLElement;

    expect(row.textContent).toContain("+5,00");
    expect(row.textContent).toContain("€/día");
    expect(row.textContent).not.toContain("Sin coste extra");

    await waitFor(() => expect(screen.getByRole("button", { name: "Aprobar" })).toBeEnabled());
  });
});
