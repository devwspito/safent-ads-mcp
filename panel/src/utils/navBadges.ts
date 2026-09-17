import type { BadgesResponse } from "@/api/schemas/badges";

export interface NavBadge {
  count?: number;
  dot?: "red" | "amber";
}

/**
 * Insignias de los cuatro destinos — panel-interaction-spec.md §1.1:
 * Propuestas cuenta pendientes y avisa en rojo si alguna caduca hoy (aproximado con
 * `proposals.critical`, la misma señal que ya distinguía la fila urgente); Campañas avisa
 * en ámbar si una cuenta está parada o desconectada; Ajustes avisa en rojo si hace falta
 * reconectar. `GET /badges` no distingue "pausada" de "necesita reconexión" todavía
 * (nivel único `warn`/`error`): se reparte por severidad hasta que el contrato lo separe.
 */
export function deriveNavBadges(badges: BadgesResponse | undefined): Record<string, NavBadge> {
  if (!badges) return {};
  return {
    "/propuestas": {
      count: badges.proposals.pending > 0 ? badges.proposals.pending : undefined,
      dot: badges.proposals.critical > 0 ? "red" : undefined,
    },
    "/campanas": badges.connections.level === "warn" ? { dot: "amber" } : {},
    "/ajustes": badges.connections.level === "error" ? { dot: "red" } : {},
  };
}
