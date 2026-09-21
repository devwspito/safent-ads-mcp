/**
 * Fuente única de los cuatro destinos del panel — panel-interaction-spec.md §1 (decisión 1):
 * "Cuatro destinos: Propuestas · Campañas · Resultados · Ajustes. Nada más en la navegación."
 * Orden fijo; atajos `1`-`4` mapean por índice.
 */
export interface RouteDescriptor {
  path: string;
  label: string;
  shortcut: string;
}

export const MAIN_ROUTES: RouteDescriptor[] = [
  { path: "/trabajo", label: "Trabajo", shortcut: "1" },
  { path: "/campanas", label: "Campañas", shortcut: "2" },
  { path: "/resultados", label: "Resultados", shortcut: "3" },
  { path: "/ajustes", label: "Ajustes", shortcut: "4" },
];

/** Alcanzables (paleta de comandos) pero fuera de los cuatro destinos fijos — sin atajo numérico. */
export const SECONDARY_ROUTES: RouteDescriptor[] = [{ path: "/propuestas", label: "Decisiones pendientes", shortcut: "" }, { path: "/propuestas/historial", label: "Historial", shortcut: "" }];

export const ALL_ROUTES: RouteDescriptor[] = [...MAIN_ROUTES, ...SECONDARY_ROUTES];
