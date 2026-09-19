import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { http, HttpResponse } from "msw";
import { describe, expect, it, beforeEach } from "vitest";
import { server } from "@/mocks/server";
import { setMockSessionForTests } from "@/mocks/handlers";
import { API_BASE } from "@/mocks/handlers/apiBase";
import { LaunchProposalPage } from "@/routes/LaunchProposalPage";
import { LaunchPlans } from "./LaunchPlans";

const plan = { slug: "opening", title: "Opening plan", summary: "Meet our pets", proposal_id: null, landing_url: "/eventos/opening", blockers: ["Confirm event time"], documents: [{ title: "Strategy", text: "The full strategy" }, { title: "Scripts", text: "The scripts" }], video_slots: [{ id: "v01", title: "Invitation", format: "9:16", copy: "Join us", uploaded: false }], revision: "a".repeat(64), review: { approved: false, approved_at: null } };

function renderPlan(path = "/propuestas?business_id=biz_ejemplo") {
  server.use(http.get(`${API_BASE}/launch-plans`, ({ request }) => {
    expect(new URL(request.url).searchParams.get("business_id")).toBe("biz_ejemplo");
    return HttpResponse.json({ items: [plan] });
  }));
  return render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <MemoryRouter initialEntries={[path]}><Routes>
      <Route path="/propuestas" element={<LaunchPlans businessId="biz_ejemplo" />} />
      <Route path="/propuestas/lanzamiento/:slug" element={<LaunchProposalPage />} />
    </Routes></MemoryRouter>
  </QueryClientProvider>);
}

describe("Launch plans", () => {
  beforeEach(() => { setMockSessionForTests(true); server.use(http.get(`${API_BASE}/runtime/connections`, () => HttpResponse.json({ items: [] }))); });

  it("lists only a summary; opens documents and video uploads on a dedicated detail route", async () => {
    const user = userEvent.setup();
    renderPlan();
    const card = await screen.findByRole("link", { name: /Opening plan.*Ver detalle/ });
    expect(card).toHaveAttribute("href", "/propuestas/lanzamiento/opening?business_id=biz_ejemplo");
    expect(screen.queryByText("The full strategy")).not.toBeInTheDocument();
    expect(screen.queryByText("Confirm event time")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Añadir vídeo MP4")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Aprobar plan/ })).not.toBeInTheDocument();
    await user.click(card);
    expect(await screen.findByRole("heading", { name: "Detalle de la propuesta" })).toBeInTheDocument();
    expect(await screen.findByText("Confirm event time")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Ver landing/ })).toHaveAttribute("href", "/eventos/opening");
    await user.click(screen.getByRole("button", { name: "Scripts" }));
    expect(screen.getByText("The scripts")).toBeInTheDocument();
    expect(screen.getByLabelText("Añadir vídeo MP4")).toBeInTheDocument();
    await user.click(screen.getByRole("link", { name: /Volver a propuestas/ }));
    expect(await screen.findByText("Ver detalle")).toBeInTheDocument();
    expect(screen.queryByText("The scripts")).not.toBeInTheDocument();
  });

  it("loads a bookmarked detail directly and approves only the exact editorial revision", async () => {
    const user = userEvent.setup();
    let approved = false;
    server.use(http.post(`${API_BASE}/launch-plans/opening/review`, async ({ request }) => {
      expect(new URL(request.url).searchParams.get("business_id")).toBe("biz_ejemplo");
      expect(await request.json()).toEqual({ revision: plan.revision });
      approved = true;
      return HttpResponse.json({ approved: true, approved_at: "2026-09-18T00:00:00Z" });
    }));
    renderPlan("/propuestas/lanzamiento/opening?business_id=biz_ejemplo");
    await user.click(await screen.findByRole("button", { name: "Aprobar plan · sin activar campañas" }));
    expect(await screen.findByText(/Plan aprobado y preparación encolada/)).toBeInTheDocument();
    expect(approved).toBe(true);
  });

  it("does not substitute another proposal for an unknown slug", async () => {
    renderPlan("/propuestas/lanzamiento/missing?business_id=biz_ejemplo");
    expect(await screen.findByText(/Esta propuesta no está disponible/)).toBeInTheDocument();
    expect(screen.queryByText("Opening plan")).not.toBeInTheDocument();
  });

  it("lets the user retry a failed detail without showing stale approval controls", async () => {
    const user = userEvent.setup();
    renderPlan("/propuestas/lanzamiento/opening?business_id=biz_ejemplo");
    server.use(http.get(`${API_BASE}/launch-plans`, () => new HttpResponse(null, { status: 503 })));
    expect(await screen.findByRole("alert")).toHaveTextContent("No se pudo cargar la propuesta");
    expect(screen.queryByRole("button", { name: /Aprobar plan/ })).not.toBeInTheDocument();
    server.use(http.get(`${API_BASE}/launch-plans`, () => HttpResponse.json({ items: [plan] })));
    await user.click(screen.getByRole("button", { name: "Reintentar" }));
    expect(await screen.findByText("The full strategy")).toBeInTheDocument();
  });
});
