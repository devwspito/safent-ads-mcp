/** Tiempos relativos en español — panel-visual-spec.md §2, panel-interaction-spec.md §7. */
export function formatRelativeTime(isoString: string, now: Date = new Date()): string {
  const date = new Date(isoString);
  const diffMs = date.getTime() - now.getTime();
  const absMs = Math.abs(diffMs);
  const isPast = diffMs < 0;

  if (absMs < 60_000) return isPast ? "hace un momento" : "en un momento";
  if (absMs < 3_600_000) {
    const mins = Math.round(absMs / 60_000);
    return isPast ? `hace ${mins} min` : `en ${mins} min`;
  }
  if (absMs < 86_400_000) {
    const hours = Math.round(absMs / 3_600_000);
    return isPast ? `hace ${hours} h` : `en ${hours} h`;
  }
  const days = Math.round(absMs / 86_400_000);
  return isPast ? `hace ${days} d` : `en ${days} d`;
}

/** `Datos de hace 2 h` / `Datos de hace 12 min` — motivo corto y consistente (StaleBanner, filas deshabilitadas).
 * Defensive against the legacy "never ingested" sentinel (hotfix 0.2.20, Bug B):
 * that is `no_data`, never a real multi-thousand-hour age. */
export function staleDataLabel(lagMinutes: number): string {
  if (lagMinutes >= 1_000_000) return "Sin estadísticas todavía";
  const hours = Math.floor(lagMinutes / 60);
  return `Datos de ${hours >= 1 ? `hace ${hours} h` : `hace ${lagMinutes} min`}`;
}

const exactDateFormatter = new Intl.DateTimeFormat("es-ES", {
  day: "2-digit",
  month: "2-digit",
  year: "numeric",
  hour: "2-digit",
  minute: "2-digit",
});

export function formatExactDate(isoString: string): string {
  return exactDateFormatter.format(new Date(isoString));
}

export function formatWindowLabel(days: number): string {
  return `${days} d`;
}

const timeOnlyFormatter = new Intl.DateTimeFormat("es-ES", { hour: "2-digit", minute: "2-digit" });

/** `10:09` — para «Identificado hasta las HH:MM» (contracts/federated-login.md §2). */
export function formatTimeHHMM(isoString: string): string {
  return timeOnlyFormatter.format(new Date(isoString));
}

const dateOnlyFormatter = new Intl.DateTimeFormat("es-ES", { day: "2-digit", month: "2-digit", year: "numeric" });

/** Para fechas sin componente de hora (p. ej. cierre de un evento de calendario). */
export function formatDateOnly(isoDate: string): string {
  return dateOnlyFormatter.format(new Date(`${isoDate}T00:00:00`));
}

const weekdayDayFormatter = new Intl.DateTimeFormat("es-ES", { weekday: "short", day: "numeric" });

/** `jue 18` — para «Vuelve el jue 18» (posponer con fecha, panel-interaction-spec.md). */
export function weekdayDayLabel(isoString: string): string {
  return weekdayDayFormatter.format(new Date(isoString));
}
