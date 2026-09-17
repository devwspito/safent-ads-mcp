import { describe, expect, it, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { setMockSessionForTests } from "@/mocks/handlers";
import { RegistroPage } from "./RegistroPage";

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/registro?business_id=biz_ejemplo"]}>
        <RegistroPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("RegistroPage", () => {
  beforeEach(() => setMockSessionForTests(true));

  it("muestra la bitácora en orden inverso con la insignia de cadena verificada", async () => {
    renderPage();
    expect(await screen.findByText(/Cadena verificada/)).toBeInTheDocument();
    expect(screen.getByText("#10")).toBeInTheDocument();
  });

  it("expande una fila y muestra antes/después", async () => {
    const user = userEvent.setup();
    renderPage();
    const row = await screen.findByText(/Presupuesto diario 172 € → 120 €/);
    await user.click(row);
    expect(await screen.findByText("Antes")).toBeInTheDocument();
    expect(screen.getByText("Después")).toBeInTheDocument();
  });
});
