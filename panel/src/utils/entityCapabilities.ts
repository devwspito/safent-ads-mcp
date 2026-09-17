export interface EntityCapabilities {
  canPause: boolean;
  canResume: boolean;
  canDelete: boolean;
}

/**
 * Espejo puro de `entity_capabilities.py` (`panel/application`, backend): no hay todavía una
 * ruta `GET /entities/{ref}` que sirva `can_pause/can_resume/can_delete` (spec 002-panel-simple,
 * commit 06800d8 solo añadió los `POST` de escritura) — el panel deriva la misma regla desde el
 * mismo par de datos (`status`, `is_controllable`) que ya trae cada fila de `/cockpit`.
 */
export function entityCapabilities(status: string, isControllable: boolean): EntityCapabilities {
  return {
    canPause: isControllable && status === "ACTIVE",
    canResume: isControllable && status === "PAUSED",
    canDelete: isControllable && status !== "REMOVED",
  };
}
