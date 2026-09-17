import type { RowAction } from "@/api/schemas/cockpit";

const BLOCKED_REASON_LABEL: Record<NonNullable<RowAction["blocked_reason"]>, string> = {
  brake_engaged: "Freno activo",
  stale_data: "Datos obsoletos",
  guardrail: "Fuera de guardarraíl",
  not_controllable: "No controlable",
  immature_window: "Ventana inmadura",
};

export function blockedReasonLabel(reason: RowAction["blocked_reason"]): string {
  if (!reason) return "Bloqueado";
  return BLOCKED_REASON_LABEL[reason];
}

const ACTION_KIND_LABEL: Record<RowAction["kind"], string> = {
  approve_increase: "Aprobar subida",
  apply_decrease: "Bajar",
  apply_exit: "Salir",
  review_proposal: "Ver propuesta",
  none: "Sin acción",
};

export function actionKindLabel(kind: RowAction["kind"]): string {
  return ACTION_KIND_LABEL[kind];
}

/** Palabra de confirmación tecleada por tipo de acción — mismo patrón que `BrakeDialog` (REACTIVAR). */
export function typedConfirmationWord(kind: RowAction["kind"]): string {
  if (kind === "apply_exit") return "SALIR";
  if (kind === "apply_decrease") return "BAJAR";
  return "CONFIRMAR";
}
