import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";
import { server } from "@/mocks/server";
import { API_BASE } from "@/mocks/handlers/apiBase";
import { storeApiStatus } from "@/mocks/handlers/storeApi";
import { StoreApiConnectionCard } from "./StoreApiConnectionCard";

function renderCard() {
  return render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><StoreApiConnectionCard businessId="business-a" /></QueryClientProvider>);
}

describe("Store API connection", () => {
  it("is independent of campaigns and never preloads credentials", async () => {
    const user = userEvent.setup();
    renderCard();
    expect(await screen.findByText("No conectado")).toBeInTheDocument();
    expect(screen.getByText(/independiente de las campañas/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Conectar catálogo" }));
    expect(screen.getByLabelText("Token de acceso de la tienda")).toHaveValue("");
    expect(screen.getByLabelText("Token de acceso de la tienda")).toHaveAttribute("type", "password");
  });
  it("sends the credential only in the body and clears it after saving", async () => {
    const user = userEvent.setup();
    let received = false;
    server.use(http.put(`${API_BASE}/integrations/store-api`, async ({ request }) => {
      expect(new URL(request.url).searchParams.get("business_id")).toBe("business-a");
      expect(request.url).not.toContain("private-test");
      expect(await request.json()).toEqual({ token: "private-test" });
      received = true;
      return HttpResponse.json({ ...storeApiStatus, configured: true });
    }));
    renderCard();
    await user.click(await screen.findByRole("button", { name: "Conectar catálogo" }));
    await user.type(screen.getByLabelText("Token de acceso de la tienda"), "private-test");
    await user.click(screen.getByRole("button", { name: "Comprobar y guardar" }));
    await waitFor(() => expect(received).toBe(true));
    expect(await screen.findByText(/Conexión comprobada y guardada/)).toBeInTheDocument();
    expect(screen.queryByLabelText("Token de acceso de la tienda")).not.toBeInTheDocument();
    expect(document.body.innerHTML).not.toContain("private-test");
  });
  it("clears a rejected credential and keeps the form available", async () => {
    const user = userEvent.setup();
    server.use(http.put(`${API_BASE}/integrations/store-api`, () => HttpResponse.json({ error: { code: "STORE_API_CONNECTION_FAILED", message: "Comprueba token e IP autorizada." } }, { status: 422 })));
    renderCard();
    await user.click(await screen.findByRole("button", { name: "Conectar catálogo" }));
    await user.type(screen.getByLabelText("Token de acceso de la tienda"), "private-test");
    await user.click(screen.getByRole("button", { name: "Comprobar y guardar" }));
    expect(await screen.findByText("Comprueba token e IP autorizada.")).toBeInTheDocument();
    expect(screen.getByLabelText("Token de acceso de la tienda")).toHaveValue("");
  });
});
