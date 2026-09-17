import { useEffect, useRef, type ReactNode } from "react";
import styles from "./Modal.module.css";

interface ModalProps {
  label: string;
  onClose: () => void;
  children: ReactNode;
  width?: string;
  busy?: boolean;
  /**
   * Ancla explícita para devolver el foco al cerrar (WCAG 2.4.3). Por defecto, el foco vuelve a
   * quien tuviera el foco justo antes de montar ESTE diálogo — correcto para un diálogo suelto,
   * pero no cuando uno sustituye a otro en una cadena (T062: el segundo diálogo monta con el
   * primero ya desmontado, así que "quien lo abrió" en ese instante ya no es el botón original de
   * la fila, sino lo que el primer diálogo dejó al restaurar el foco). Pásalo cuando dos o más
   * diálogos encadenados deban devolver el foco al MISMO control de origen.
   */
  returnFocusTo?: { current: HTMLElement | null };
}

const FOCUSABLE_SELECTOR =
  'a[href], button:not([disabled]), textarea:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])';

function focusable(dialog: HTMLElement) {
  return Array.from(dialog.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR)).filter(element =>
    !element.matches(':disabled, [type="hidden"]') && !element.closest('[hidden], [inert], [aria-hidden="true"]'),
  );
}

/**
 * Cáscara común de diálogo modal: overlay + `role="dialog"` + cierre al pulsar fuera.
 * Gestión de foco (panel-interaction-spec.md §8, WCAG 2.4.3/2.1.2): al abrir, el foco entra
 * en el diálogo (sin robar el de un campo con `autoFocus` propio); `Tab`/`Shift+Tab` quedan
 * atrapados dentro mientras está abierto; al cerrarse, el foco vuelve a quien lo abrió.
 */
export function Modal({ label, onClose, children, width, busy = false, returnFocusTo }: ModalProps) {
  // Capture during render, before a descendant's autoFocus runs at commit.
  const openerRef = useRef(document.activeElement instanceof HTMLElement ? document.activeElement : null);
  const dialogRef = useRef<HTMLDivElement>(null);
  const closeRef = useRef(onClose);
  const busyRef = useRef(busy);
  const returnFocusRef = useRef(returnFocusTo);
  closeRef.current = onClose;
  busyRef.current = busy;
  returnFocusRef.current = returnFocusTo;

  useEffect(() => {
    const previouslyFocused = returnFocusRef.current?.current ?? openerRef.current;
    const dialog = dialogRef.current;

    if (dialog && !dialog.contains(document.activeElement)) {
      const firstFocusable = focusable(dialog)[0];
      (firstFocusable ?? dialog).focus();
    }

    function trapTab(event: KeyboardEvent) {
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopImmediatePropagation();
        if (!busyRef.current) closeRef.current();
        return;
      }
      if (event.key !== "Tab" || !dialog) return;
      const items = focusable(dialog);
      if (items.length === 0) {
        event.preventDefault();
        dialog.focus();
        return;
      }
      const first = items[0]!;
      const last = items[items.length - 1]!;
      if (!dialog.contains(document.activeElement)) {
        event.preventDefault();
        (event.shiftKey ? last : first).focus();
      } else if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    }

    document.addEventListener("keydown", trapTab, true);
    return () => {
      document.removeEventListener("keydown", trapTab, true);
      if (previouslyFocused?.isConnected) previouslyFocused.focus();
    };
  }, []);

  return (
    <div className={styles.overlay} onClick={() => { if (!busy) onClose(); }}>
      <div
        ref={dialogRef}
        className={styles.dialog}
        style={width ? { width } : undefined}
        role="dialog"
        aria-modal="true"
        aria-busy={busy}
        aria-label={label}
        tabIndex={-1}
        onClick={(event) => event.stopPropagation()}
      >
        {children}
      </div>
    </div>
  );
}
