import { useEffect, useId, useRef, useState } from "react";
import styles from "./RowMenu.module.css";

export interface RowMenuItem {
  key: string;
  label: string;
  onSelect: () => void;
  disabled?: boolean;
  danger?: boolean;
}

interface RowMenuProps {
  /** Nombre completo del disparador para lectores de pantalla, p. ej. "Más opciones para Verano - Leads". */
  label: string;
  items: RowMenuItem[];
  disabled?: boolean;
}

function menuItemElements(menu: HTMLElement | null): HTMLButtonElement[] {
  return Array.from(menu?.querySelectorAll<HTMLButtonElement>('[role="menuitem"]:not(:disabled)') ?? []);
}

/**
 * Botón "⋯" real — design.md §2.2/§12.2 y el encargo de Propuestas (d): nada de `<select>`
 * disfrazado. Popover anclado al propio disparador, `Esc` cierra y devuelve el foco al botón
 * que lo abrió, clic fuera cierra, flechas navegan entre elementos.
 */
export function RowMenu({ label, items, disabled }: RowMenuProps) {
  const [open, setOpen] = useState(false);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const menuId = useId();

  useEffect(() => {
    if (!open) return;
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        event.preventDefault();
        setOpen(false);
        triggerRef.current?.focus();
        return;
      }
      if (event.key !== "ArrowDown" && event.key !== "ArrowUp" && event.key !== "Home" && event.key !== "End") return;
      event.preventDefault();
      const elements = menuItemElements(menuRef.current);
      if (elements.length === 0) return;
      const currentIndex = elements.indexOf(document.activeElement as HTMLButtonElement);
      const nextIndex =
        event.key === "Home" ? 0
        : event.key === "End" ? elements.length - 1
        : event.key === "ArrowDown" ? (currentIndex + 1) % elements.length
        : (currentIndex - 1 + elements.length) % elements.length;
      elements[nextIndex]?.focus();
    }
    function onPointerDown(event: PointerEvent) {
      const target = event.target as Node;
      if (menuRef.current?.contains(target) || triggerRef.current?.contains(target)) return;
      setOpen(false);
    }
    document.addEventListener("keydown", onKeyDown);
    document.addEventListener("pointerdown", onPointerDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      document.removeEventListener("pointerdown", onPointerDown);
    };
  }, [open]);

  useEffect(() => {
    if (open) menuItemElements(menuRef.current)[0]?.focus();
  }, [open]);

  return (
    <div className={styles.wrap}>
      <button
        type="button"
        ref={triggerRef}
        className={styles.trigger}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={open ? menuId : undefined}
        aria-label={label}
        disabled={disabled}
        onClick={() => setOpen((value) => !value)}
      >
        ⋯
      </button>
      {open ? (
        <div id={menuId} ref={menuRef} role="menu" aria-label={label} className={styles.menu}>
          {items.map((item) => (
            <button
              key={item.key}
              type="button"
              role="menuitem"
              className={`${styles.item} ${item.danger ? styles.itemDanger : ""}`}
              disabled={item.disabled}
              onClick={() => {
                setOpen(false);
                triggerRef.current?.focus();
                item.onSelect();
              }}
            >
              {item.label}
            </button>
          ))}
        </div>
      ) : null}
    </div>
  );
}
