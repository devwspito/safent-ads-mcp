import { describe, expect, it, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { setMockSessionForTests } from "@/mocks/handlers";
import { SenalesPage } from "./SenalesPage";

let lastSearch = "";

function LocationProbe() {
  const location = useLocation();
  lastSearch = location.search;
  return null;
}

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/senales?business_id=biz_ejemplo"]}>
        <Routes>
          <Route
            path="/senales"
            element={
              <>
                <SenalesPage />
                <LocationProbe />
              </>
            }
          />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("SenalesPage filtros ↔ URL", () => {
  beforeEach(() => setMockSessionForTests(true));

  it("sincroniza el filtro de plataforma con la URL", async () => {
    const user = renderInit();
    await screen.findByLabelText("Plataforma");

    await user.selectOptions(screen.getByLabelText("Plataforma"), "meta");

    await waitFor(() => expect(new URLSearchParams(lastSearch).get("platform")).toBe("meta"));
  });

  it("sincroniza el tipo de señal y la fuerza mínima con la URL", async () => {
    const user = renderInit();
    await screen.findByLabelText("Tipo de señal");

    await user.selectOptions(screen.getByLabelText("Tipo de señal"), "SELL");
    await waitFor(() => expect(new URLSearchParams(lastSearch).get("kind")).toBe("SELL"));

    await user.selectOptions(screen.getByLabelText("Fuerza mínima"), "70");
    await waitFor(() => expect(new URLSearchParams(lastSearch).get("min_strength")).toBe("70"));
  });

  function renderInit() {
    renderPage();
    return userEvent.setup();
  }
});
