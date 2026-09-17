/**
 * Etiquetas y niveles de fuerza de las señales — panel-interaction-spec.md §5,
 * panel-visual-spec.md §4 (Ficha de señal).
 */
import type { CreativeSignalKind, SignalKind } from "@/api/schemas";

export type StrengthTier = "weak" | "medium" | "strong";

/** 0–39 débil (informativa) · 40–69 media (propuesta) · 70–100 fuerte (prioritaria/defensiva). */
export function strengthTier(strength: number): StrengthTier {
  if (strength >= 70) return "strong";
  if (strength >= 40) return "medium";
  return "weak";
}

const kindLabels: Record<string, string> = {
  BUY: "SUBIR",
  HOLD: "MANTENER",
  SELL: "BAJAR",
  EXIT: "SALIR",
  FATIGUE: "FATIGA",
  WINNER: "GANADORA",
  LOSER: "PERDEDORA",
  ANOMALY: "ANOMALÍA",
};

export function signalKindLabel(kind: SignalKind | CreativeSignalKind | "ANOMALY"): string {
  return kindLabels[kind] ?? kind;
}

const kindTokenVar: Record<string, string> = {
  BUY: "sig-subir",
  HOLD: "sig-mantener",
  SELL: "sig-bajar",
  EXIT: "sig-salir",
  FATIGUE: "sig-fatiga",
  WINNER: "sig-ganadora",
  LOSER: "sig-perdedora",
  ANOMALY: "sig-mantener",
};

/** Prefijo de las variables `--sig-*` (fill/line/text) que pinta esta señal. */
export function signalTokenPrefix(kind: SignalKind | CreativeSignalKind | "ANOMALY"): string {
  return kindTokenVar[kind] ?? "sig-mantener";
}

const actionLabels: Record<string, string> = {
  autonomo: "Autónomo",
  propuesta: "Propuesta",
  nada: "Nada",
};

export function actionTakenLabel(action: string): string {
  return actionLabels[action] ?? action;
}

const outcomeLabels: Record<string, string> = {
  confirmada: "Confirmada",
  no_confirmada: "No confirmada",
  en_curso: "En curso",
};

export function outcomeLabel(status: string, daysRemaining: number | null): string {
  if (status === "en_curso" && daysRemaining != null) {
    return `En curso (faltan ${daysRemaining} d)`;
  }
  return outcomeLabels[status] ?? status;
}

const badgeLabels: Record<string, string> = {
  no_controlable: "No controlable",
  en_aprendizaje: "En aprendizaje",
  presupuesto_compartido: "Presupuesto compartido",
  obsoleto: "Obsoleto",
  insuficiente: "Insuficiente",
};

export function entityBadgeLabel(badge: string): string {
  return badgeLabels[badge] ?? badge;
}

const statusLabels: Record<string, string> = {
  ACTIVE: "Activa",
  PAUSED: "Pausada",
  REMOVED: "Eliminada",
  DRIFTED: "Desincronizada",
  LEARNING: "En aprendizaje",
};

export function entityStatusLabel(status: string): string {
  return statusLabels[status] ?? status;
}
