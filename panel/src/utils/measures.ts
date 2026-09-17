/** Etiquetas honestas para un `Measure` no disponible — cockpit-read-model.md §2, FR-009/SC-006. */
import type { MeasureStatus } from "@/api/schemas/cockpit";

const MEASURE_STATUS_LABEL: Record<Exclude<MeasureStatus, "available">, string> = {
  no_data: "Sin datos",
  insufficient_volume: "Insuficiente",
  immature_window: "Ventana inmadura",
  learning: "En aprendizaje",
  not_controllable: "No controlable",
  no_customer_source: "Sin fuente de clientes",
  stale: "Obsoleto",
};

export function measureStatusLabel(status: Exclude<MeasureStatus, "available">): string {
  return MEASURE_STATUS_LABEL[status];
}
