import { describe, expect, it } from "vitest";
import { http, HttpResponse } from "msw";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { API_BASE } from "@/mocks/handlers/apiBase";
import { server } from "@/mocks/server";
import { OfferingsEconomicsTable } from "./OfferingsEconomicsTable";

const business = "11111111-1111-1111-1111-111111111111";
const offering = "22222222-2222-2222-2222-222222222222";

function setup(fail = false) {
  const calls: unknown[] = [];
  let items: unknown[] = [];
  server.use(
    http.get(`${API_BASE}/offerings`, () => HttpResponse.json({ items })),
    http.post(`${API_BASE}/offerings`, async ({ request }) => {
      expect(new URL(request.url).searchParams.get("business_id")).toBe(business);
      const body = await request.json() as Record<string, string | null>;
      calls.push(body);
      if (fail) return HttpResponse.json({ error: { code: "OFFERING_CODE_CONFLICT", message: "Ese código ya identifica otra oferta." } }, { status: 409 });
      items = [{ offering_id: offering, code: body.code, title: body.title, is_active: true,
        list_price: body.price_amount === null ? null : { amount: Number(body.price_amount), currency: body.price_currency }, economics: null }];
      return HttpResponse.json({ offering_id: offering, ...body, created: true });
    }),
  );
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={queryClient}><OfferingsEconomicsTable businessId={business} /></QueryClientProvider>);
  return calls;
}

async function nameOffering() {
  const user = userEvent.setup();
  await user.type(await screen.findByLabelText("Código de oferta"), "acme");
  await user.type(screen.getByLabelText("Nombre de oferta"), "Plan Acme");
  return user;
}

describe("explicit catalog onboarding", () => {
  it("never posts automatically; optional price remains absent and refreshes the real list", async () => {
    const calls = setup(); const user = await nameOffering();
    expect(calls).toHaveLength(0);
    await user.click(screen.getByRole("button", { name: "Crear oferta" }));
    expect(await screen.findByText("Plan Acme")).toBeInTheDocument();
    expect(await screen.findByRole("status")).toHaveTextContent("Oferta guardada");
    expect(calls).toEqual([{ code: "acme", title: "Plan Acme", price_amount: null, price_currency: null }]);
  });
  it("requires paired explicit price/currency and preserves the exact decimal string", async () => {
    const calls = setup(); const user = await nameOffering();
    await user.type(screen.getByLabelText("Precio (opcional)"), "19.95");
    await user.click(screen.getByRole("button", { name: "Crear oferta" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("precio y moneda juntos");
    expect(calls).toHaveLength(0);
    await user.type(screen.getByLabelText("Moneda del precio"), "EUR");
    await user.click(screen.getByRole("button", { name: "Crear oferta" }));
    await waitFor(() => expect(calls).toHaveLength(1));
    expect(calls[0]).toEqual({ code: "acme", title: "Plan Acme", price_amount: "19.95", price_currency: "EUR" });
  });
  it("shows a conflict without retrying or declaring success", async () => {
    const calls = setup(true); const user = await nameOffering();
    await user.click(screen.getByRole("button", { name: "Crear oferta" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Ese código ya identifica otra oferta.");
    expect(calls).toHaveLength(1);
    expect(screen.queryByText(/Oferta guardada/)).not.toBeInTheDocument();
    expect(screen.getByLabelText("Código de oferta")).toHaveValue("acme");
  });
});
