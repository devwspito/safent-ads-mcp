import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";
import { server } from "@/mocks/server";
import { API_BASE } from "@/mocks/handlers/apiBase";
import { LaunchPlans } from "./LaunchPlans";

const plan = { slug: "opening", title: "Opening plan", summary: "Meet our neighbours", proposal_id: null, landing_url: "/eventos/opening", blockers: ["Confirm event time"], documents: [{ title: "Strategy", text: "The full strategy" }, { title: "Scripts", text: "The scripts" }], video_slots: [{ id: "v01", title: "Invitation", format: "9:16", copy: "Join us", uploaded: false }], revision: "a".repeat(64), review: { approved: false, approved_at: null } };

function renderPlan() {
  server.use(http.get(`${API_BASE}/launch-plans`, () => HttpResponse.json({ items: [plan] })));
  return render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><LaunchPlans businessId="business-a" /></QueryClientProvider>);
}

describe("Launch plans", () => {
  it("shows real documents, blockers and the landing link", async () => {
    const user = userEvent.setup();
    renderPlan();
    expect(await screen.findByText("Opening plan")).toBeInTheDocument();
    expect(screen.getByText("Confirm event time")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Ver landing/ })).toHaveAttribute("href", "/eventos/opening");
    await user.click(screen.getByRole("button", { name: "Scripts" }));
    expect(screen.getByText("The scripts")).toBeInTheDocument();
    expect(screen.getByLabelText("Añadir vídeo MP4")).toBeInTheDocument();
  });
  it("approves the exact editorial revision without a platform write", async () => {
    const user = userEvent.setup();
    let approved = false;
    server.use(http.post(`${API_BASE}/launch-plans/opening/review`, async ({ request }) => {
      expect(await request.json()).toEqual({ revision: plan.revision });
      approved = true;
      return HttpResponse.json({ approved: true, approved_at: "2026-09-18T00:00:00Z" });
    }));
    renderPlan();
    await user.click(await screen.findByRole("button", { name: "Aprobar plan · sin activar campañas" }));
    expect(await screen.findByText(/Plan revisado y aprobado/)).toBeInTheDocument();
    expect(approved).toBe(true);
  });
});
