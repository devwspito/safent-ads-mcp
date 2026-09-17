import { Modal } from "@/components/common/Modal";
import { MAIN_ROUTES } from "@/routes/routeConfig";
import styles from "./CommandPalette.module.css";

interface ShortcutsHelpProps {
  onClose: () => void;
}

const GLOBAL_SHORTCUTS: Array<[string, string]> = [
  ["Ctrl/Cmd K", "Buscar y saltar a una vista"],
  ["?", "Esta ayuda"],
  ["Esc", "Cerrar la capa superior"],
  ["Shift F", "Abrir el freno de emergencia"],
];

/** `?`: ayuda de atajos — panel-interaction-spec.md §1. */
export function ShortcutsHelp({ onClose }: ShortcutsHelpProps) {
  return (
    <Modal label="Atajos de teclado" onClose={onClose}>
      <ul className={styles.list} role="list" style={{ maxHeight: "none" }}>
        {MAIN_ROUTES.map((route) => (
          <li key={route.path} className={styles.item}>
            <span>{route.label}</span>
            <span>{route.shortcut}</span>
          </li>
        ))}
        {GLOBAL_SHORTCUTS.map(([key, label]) => (
          <li key={key} className={styles.item}>
            <span>{label}</span>
            <span>{key}</span>
          </li>
        ))}
      </ul>
    </Modal>
  );
}
