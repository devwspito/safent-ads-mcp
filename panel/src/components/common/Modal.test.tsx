import { useEffect, useState } from "react";
import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Modal } from "./Modal";

/** Simula la composición real (`AppShell`): `Esc` global cierra la capa superior. */
function Harness({ autofocus = false }: { autofocus?: boolean }) {
  const [open, setOpen] = useState(false);

  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") setOpen(false);
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);

  return (
    <div>
      <button type="button" onClick={() => setOpen(true)}>
        Abrir
      </button>
      {open ? (
        <Modal label="Diálogo de prueba" onClose={() => setOpen(false)}>
          {autofocus ? <input aria-label="Destino" autoFocus /> : null}
          <button type="button">Primero</button>
          <button type="button">Segundo</button>
        </Modal>
      ) : null}
    </div>
  );
}

describe("Modal — gestión de foco (panel-interaction-spec.md §8, WCAG 2.4.3)", () => {
  it('ignores hidden and fieldset-disabled controls when placing and trapping focus', async () => {
    const user = userEvent.setup();
    render(<Modal label="Aprobación" onClose={() => {}}><div hidden><button>Oculto</button></div><fieldset disabled><button>Ocupado</button></fieldset><button>Disponible</button></Modal>);
    expect(screen.getByRole('button', { name: 'Disponible' })).toHaveFocus();
    await user.tab();
    expect(screen.getByRole('button', { name: 'Disponible' })).toHaveFocus();
  });

  it('does not close a busy approval with Escape or backdrop', async () => {
    const close = vi.fn();
    const user = userEvent.setup();
    render(<Modal label="Enviando aprobación" busy onClose={close}><button disabled>Enviando</button></Modal>);
    await user.keyboard('{Escape}');
    expect(close).not.toHaveBeenCalled();
    await user.click(screen.getByRole('dialog').parentElement!);
    expect(close).not.toHaveBeenCalled();
    expect(screen.getByRole('dialog')).toHaveAttribute('aria-busy', 'true');
  });
  it("un campo autoFocus no pierde el disparador al cerrar", async () => {
    const user = userEvent.setup();
    render(<Harness autofocus />);
    const trigger = screen.getByRole("button", { name: "Abrir" });
    await user.click(trigger);
    expect(screen.getByLabelText("Destino")).toHaveFocus();
    await user.keyboard("{Escape}");
    expect(trigger).toHaveFocus();
  });
  it("al abrir, el foco entra en el diálogo; al cerrar con Escape, vuelve al disparador", async () => {
    const user = userEvent.setup();
    render(<Harness />);

    const trigger = screen.getByRole("button", { name: "Abrir" });
    await user.click(trigger);

    const dialog = await screen.findByRole("dialog", { name: "Diálogo de prueba" });
    expect(dialog.contains(document.activeElement)).toBe(true);

    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(document.activeElement).toBe(trigger);
  });

  it("Tab en el último elemento enfocable vuelve al primero (el foco no se escapa del diálogo)", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    await user.click(screen.getByRole("button", { name: "Abrir" }));
    await screen.findByRole("dialog");

    const first = screen.getByRole("button", { name: "Primero" });
    const second = screen.getByRole("button", { name: "Segundo" });
    second.focus();

    await user.tab();
    expect(document.activeElement).toBe(first);
  });

  it("Shift+Tab en el primer elemento va al último", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    await user.click(screen.getByRole("button", { name: "Abrir" }));
    await screen.findByRole("dialog");

    const first = screen.getByRole("button", { name: "Primero" });
    const second = screen.getByRole("button", { name: "Segundo" });
    first.focus();

    await user.tab({ shift: true });
    expect(document.activeElement).toBe(second);
  });
});
