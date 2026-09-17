/** Palabras llanas de estado — design.md §10.2, vinculante en las cuatro pantallas principales. */
const STATUS_LABEL: Record<string, string> = {
  ACTIVE: "Activa",
  PAUSED: "Pausada",
  REMOVED: "Eliminada",
  LEARNING: "Aprendiendo",
  DRIFTED: "Necesita revisión",
};

export function plainEntityStatusLabel(status: string): string {
  return STATUS_LABEL[status] ?? status;
}
