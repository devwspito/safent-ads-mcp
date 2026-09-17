import { entityBadgeLabel } from "@/utils/signals";
import styles from "./StatusMarker.module.css";

interface StatusMarkerProps {
  status: "insuficiente" | "en_aprendizaje" | "no_controlable" | "obsoleto" | "presupuesto_compartido";
  label?: string;
}

/**
 * Marcadores de estado no accionable — panel-visual-spec.md §4: "dicen no hay juicio,
 * no alerta". Registro visual distinto a propósito de las fichas de señal.
 */
export function StatusMarker({ status, label }: StatusMarkerProps) {
  return <span className={`${styles.marker} ${styles[status]}`}>{label ?? entityBadgeLabel(status)}</span>;
}
