import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { RowMenu } from "./RowMenu";

describe("RowMenu", () => {
  it("es un botón real con aria-haspopup=menu, abre un role=menu con sus elementos", async () => {
    const user = userEvent.setup();
    const onSelect = vi.fn();
    render(<RowMenu label="Más opciones para Verano" items={[{ key: "a", label: "Más tarde", onSelect }]} />);

    const trigger = screen.getByRole("button", { name: "Más opciones para Verano" });
    expect(trigger).toHaveAttribute("aria-haspopup", "menu");
    expect(trigger).toHaveAttribute("aria-expanded", "false");

    await user.click(trigger);
    expect(trigger).toHaveAttribute("aria-expanded", "true");
    const item = screen.getByRole("menuitem", { name: "Más tarde" });
    await user.click(item);
    expect(onSelect).toHaveBeenCalledOnce();
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
  });

  it("Esc cierra el menú y devuelve el foco al botón que lo abrió", async () => {
    const user = userEvent.setup();
    render(<RowMenu label="Más opciones" items={[{ key: "a", label: "Ver detalle", onSelect: vi.fn() }]} />);
    const trigger = screen.getByRole("button", { name: "Más opciones" });
    await user.click(trigger);
    expect(screen.getByRole("menu")).toBeInTheDocument();

    await user.keyboard("{Escape}");
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
  });

  it("un clic fuera cierra el menú", async () => {
    const user = userEvent.setup();
    render(
      <div>
        <RowMenu label="Más opciones" items={[{ key: "a", label: "Ver detalle", onSelect: vi.fn() }]} />
        <button type="button">Fuera</button>
      </div>,
    );
    await user.click(screen.getByRole("button", { name: "Más opciones" }));
    expect(screen.getByRole("menu")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Fuera" }));
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
  });

  it("deshabilitado no abre", async () => {
    const user = userEvent.setup();
    render(<RowMenu label="Más opciones" disabled items={[{ key: "a", label: "Ver detalle", onSelect: vi.fn() }]} />);
    await user.click(screen.getByRole("button", { name: "Más opciones" }));
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
  });
});
