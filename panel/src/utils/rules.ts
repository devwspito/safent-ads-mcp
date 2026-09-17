import type { AutonomyGate, AutonomyLevel } from "@/api/schemas/rules";

export type RuleSwitchState = "apagada" | "avisar" | "automatica";

export function autonomyLevelForSwitch(state: RuleSwitchState): AutonomyLevel {
  return state === "automatica" ? "AUTO" : "NOTIFY";
}

/**
 * `ready` es por cuenta (rest-api.md §Reglas): agrega para el aviso preventivo del catálogo.
 * El 409 `AUTONOMY_GATE_OPEN` real al guardar sigue siendo la fuente de verdad (C-18-like:
 * nunca desde caché).
 */
export function autonomyGateSummary(gate: AutonomyGate | undefined): { ready: boolean; blockedReason: string | null } {
  if (!gate) return { ready: false, blockedReason: null };
  if (gate.ready) return { ready: true, blockedReason: null };
  const blockedLabels = gate.accounts
    .filter((account) => !account.ready)
    .flatMap((account) => account.missing.map((question) => `${account.label}: ${question.label}`));
  return { ready: false, blockedReason: blockedLabels.length > 0 ? blockedLabels.join(" · ") : "Confirmaciones pendientes por cuenta" };
}
