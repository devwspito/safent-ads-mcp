import { describe, expect, it, beforeEach } from "vitest";
import { http, HttpResponse } from "msw";
import { API_BASE } from "@/mocks/handlers/apiBase";
import { server } from "@/mocks/server";
import { listCreatives } from "@/mocks/fixtures/creatives";
import { buildPortfolio } from "@/mocks/fixtures/portfolio";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { setMockSessionForTests } from "@/mocks/handlers";
import { CreatividadesPage } from "./CreatividadesPage";

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/creatividades?business_id=biz_ejemplo"]}>
        <CreatividadesPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("CreatividadesPage", () => {
  beforeEach(() => setMockSessionForTests(true));

  it("muestra el carril Por aprobar con vista previa en formato real", async () => {
    renderPage();
    expect(await screen.findByText(/Por aprobar/)).toBeInTheDocument();
    const preview = await screen.findByAltText(/Vista previa de Nueva pieza: precio 2026/);
    expect(preview).toHaveAttribute("src", expect.stringContaining("1080"));
  });

  it("filtra la galería por señal", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText(/Por aprobar/);

    await user.click(screen.getByRole("button", { name: "Ganadora" }));
    expect(await screen.findByText("Testimonio Secundaria 15s")).toBeInTheDocument();
    expect(screen.queryByText("Calendario Primaria 20s")).not.toBeInTheDocument();
  });

  it("prepara una propuesta sólo con destino y texto explícitos, sin publicar", async () => {
    let sent: Record<string, unknown> | undefined;
    server.use(http.get(`${API_BASE}/creatives`, ({ request }) => {
      const data = listCreatives({ pendingApproval: new URL(request.url).searchParams.get("pending_approval") === "true" });
      return HttpResponse.json({ items: data.items.map((item) => ({ ...item, policy_verdict: "PASS" })) });
    }));
    server.use(http.post(`${API_BASE}/creatives/:assetId/propose-publication`, async ({ request }) => {
      sent = await request.json() as Record<string, unknown>;
      return HttpResponse.json({ proposal_id: "proposal_ui", diff_hash: "hash_ui", expires_at: new Date().toISOString() });
    }));
    const user = userEvent.setup();
    renderPage();
    await screen.findByText(/Por aprobar/);

    const [publishButton] = screen.getAllByRole("button", { name: "Preparar publicación" });
    await user.click(publishButton!);

    const dialog = await screen.findByRole("dialog", { name: "Preparar publicación" });
    const confirmButton = within(dialog).getByRole("button", { name: "Crear propuesta" });
    expect(confirmButton).toBeDisabled();

    await user.type(within(dialog).getByLabelText(/Escribe PUBLICAR/), "PUBLICAR");
    expect(confirmButton).toBeDisabled();
    const campaign = within(dialog).getByLabelText("Campaña");
    await waitFor(() => expect(within(campaign).getAllByRole("option").length).toBeGreaterThan(1));
    await user.selectOptions(campaign, within(campaign).getAllByRole("option")[1]!);
    const group = within(dialog).getByLabelText("Grupo de anuncios");
    await waitFor(() => expect(within(group).getAllByRole("option").length).toBeGreaterThan(1));
    await user.selectOptions(group, within(group).getAllByRole("option")[1]!);
    await user.type(within(dialog).getByLabelText("Título"), "Un título revisado");
    await user.type(within(dialog).getByLabelText("Texto del anuncio"), "Un mensaje aprobado por el propietario");
    await user.type(within(dialog).getByLabelText("Llamada a la acción"), "Más información");
    await user.click(confirmButton);

    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(sent?.ad_set_ref).toMatch(/^(google|meta):ad_set:/);
    expect(sent?.ad_copy).toEqual({ headline: "Un título revisado", primary_text: "Un mensaje aprobado por el propietario", cta: "Más información" });
    expect(await screen.findByText(/el anuncio todavía no se ha publicado/)).toBeInTheDocument();
  });

  it.each(["named", "duplicate", "unavailable", "scoped"])("dos conexiones mantienen destinos distinguibles con metadatos %s", async (metadata) => {
    const accountRef = (suffix: string) => metadata === "scoped"
      ? `google:account:aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa:${suffix === "norte" ? "11111111-1111-4111-8111-111111111111" : "22222222-2222-4222-8222-222222222222"}:123`
      : `google:acc-${suffix}`;
    server.use(http.get(`${API_BASE}/creatives`, ({ request }) => {
      const data = listCreatives({ pendingApproval: new URL(request.url).searchParams.get("pending_approval") === "true" });
      return HttpResponse.json({ items: data.items.map((item) => ({ ...item, policy_verdict: "PASS" })) });
    }));
    server.use(http.get(`${API_BASE}/portfolio`, () => {
      const base = buildPortfolio("biz_ejemplo");
      const template = base.rows[0]!;
      return HttpResponse.json({
        ...base,
        rows: [
          { ...template, entity_ref: "google:campaign:c-norte", name: "Búsqueda Marca", platform: "google", platform_account_id: accountRef("norte") },
          { ...template, entity_ref: "google:campaign:c-sur", name: "Búsqueda Marca", platform: "google", platform_account_id: accountRef("sur") },
        ],
      });
    }));
    server.use(http.get(`${API_BASE}/platform-accounts`, () => metadata === "unavailable" ? HttpResponse.json({}, { status: 503 }) : HttpResponse.json({
      items: ["norte", "sur"].map((suffix) => ({
        platform_account_id: accountRef(suffix),
        platform: "google",
        external_account_id: metadata === "scoped" ? "123" : `acc-${suffix}`,
        label: metadata === "duplicate" || metadata === "scoped" ? "Mismo alias" : `Google Ads — Cliente ${suffix === "norte" ? "Norte" : "Sur"}`,
        status: "ACTIVE",
        currency: "EUR",
        timezone: "Europe/Madrid",
        api_tier: "Básico",
        token: { health: "ok", expires_at: null, checked_at: new Date().toISOString() },
        quota: { window: "24H", used_pct: null, writes_remaining: null },
        unavailable_levers: [],
        last_synced_at: null,
        last_error_code: null,
      })),
    })));

    const user = userEvent.setup();
    renderPage();
    await screen.findByText(/Por aprobar/);

    const [publishButton] = screen.getAllByRole("button", { name: "Preparar publicación" });
    await user.click(publishButton!);

    const dialog = await screen.findByRole("dialog", { name: "Preparar publicación" });
    const campaign = within(dialog).getByLabelText("Campaña");
    await waitFor(() => expect(within(campaign).getAllByRole("option").length).toBeGreaterThan(2));

    await waitFor(() => {
      const optionLabels = within(campaign).getAllByRole("option").map((option) => option.textContent);
      for (const suffix of ["norte", "sur"]) {
        const label = metadata === "named" ? `Google Ads — Cliente ${suffix === "norte" ? "Norte" : "Sur"}`
          : `${metadata === "duplicate" ? "Mismo alias" : "Google Ads"} · google:acc-${suffix}`;
        expect(optionLabels).toContain(metadata === "scoped"
          ? `123 / ${suffix === "norte" ? "11111111" : "22222222"} · Búsqueda Marca · Mismo alias`
          : metadata === "duplicate"
          ? `google:acc-${suffix} · Búsqueda Marca · Mismo alias`
          : `Búsqueda Marca · ${label}`);
      }
      expect(new Set(optionLabels).size).toBe(optionLabels.length);
    });
  });

  it("no ofrece publicar piezas con revisión pendiente o fallida", async () => {
    renderPage();
    await screen.findByText(/Por aprobar/);
    screen.getAllByRole("button", { name: "Preparar publicación" }).forEach((button) => expect(button).toBeDisabled());
  });

  it("mantiene el rechazo abierto y muestra un error si el servidor no confirma", async () => {
    server.use(http.post(`${API_BASE}/creatives/:assetId/reject`, () => HttpResponse.json({}, { status: 503 })));
    const user = userEvent.setup();
    renderPage();
    await screen.findByText(/Por aprobar/);
    await user.click(screen.getAllByRole("button", { name: "Rechazar" })[0]!);
    const dialog = await screen.findByRole("dialog", { name: "Rechazar pieza" });
    await user.type(within(dialog).getByLabelText("Motivo"), "El texto necesita revisión");
    await user.click(within(dialog).getByRole("button", { name: "Rechazar" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent(/servidor/i);
    expect(dialog).toBeInTheDocument();
  });

  it("un error en el carril de revisión no parece una bandeja vacía", async () => {
    server.use(http.get(`${API_BASE}/creatives`, () => HttpResponse.json({}, { status: 503 })));
    renderPage();
    expect((await screen.findAllByRole("alert")).length).toBeGreaterThan(0);
    expect(screen.queryByRole("button", { name: "Preparar publicación" })).not.toBeInTheDocument();
  });
});
