import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { CommandPalette } from "./CommandPalette";

function renderPalette() {
  return render(
    <MemoryRouter>
      <CommandPalette onClose={vi.fn()} />
    </MemoryRouter>,
  );
}

/** Historial es alcanzable sin sesión ni datos de negocio — la paleta solo salta rutas. */
describe("CommandPalette — Historial alcanzable", () => {
  it("lista Historial entre los destinos, sin necesitar sesión", () => {
    renderPalette();
    expect(screen.getByRole("option", { name: /Historial/ })).toBeInTheDocument();
  });

  it("filtra a Historial al escribir su nombre", async () => {
    const user = userEvent.setup();
    renderPalette();
    await user.type(screen.getByRole("combobox", { name: "Buscar una vista" }), "histo");
    const options = screen.getAllByRole("option");
    expect(options).toHaveLength(1);
    expect(options[0]).toHaveTextContent("Historial");
  });
});
