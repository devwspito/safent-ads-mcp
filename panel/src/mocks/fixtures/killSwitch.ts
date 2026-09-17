/**
 * Frenos acumulables por ámbito (rest-api.md §Ejecución, deshacer y freno): uno activo por
 * ámbito, nunca "el freno" en singular. `effective` es el más restrictivo de
 * global ∪ business ∪ platform_account, con ALL sobre AUTONOMOUS.
 */
type ScopeKind = "global" | "business" | "platform_account";
type Mode = "AUTONOMOUS" | "ALL";

interface BrakeItem {
  brake_id: string;
  scope_kind: ScopeKind;
  scope_id: string | null;
  scope_label: string;
  mode: Mode;
  engaged_at: string;
  engaged_by: string;
  reason: string | null;
}

const PLATFORM_ACCOUNT_IDS = ["google:100-000-0002", "meta:act_100000000000001"];
const BUSINESS_LABEL = "Negocio Ejemplo";

let nextId = 1;
const items: BrakeItem[] = [];

function scopeLabelFor(scopeKind: ScopeKind, scopeId: string | null): string {
  if (scopeKind === "global") return "Todo el panel";
  if (scopeKind === "business") return BUSINESS_LABEL;
  return `Cuenta ${scopeId}`;
}

interface EffectiveResolution {
  engaged: boolean;
  mode: Mode | null;
  scope_kind: ScopeKind | null;
  scope_id: string | null;
  engaged_at: string | null;
  reason: string | null;
}

function resolveEffective(applicable: BrakeItem[]): EffectiveResolution {
  const chosen = applicable.find((item) => item.mode === "ALL") ?? applicable.find((item) => item.mode === "AUTONOMOUS");
  if (!chosen) return { engaged: false, mode: null, scope_kind: null, scope_id: null, engaged_at: null, reason: null };
  return {
    engaged: true,
    mode: chosen.mode,
    scope_kind: chosen.scope_kind,
    scope_id: chosen.scope_id,
    engaged_at: chosen.engaged_at,
    reason: chosen.reason,
  };
}

export function getKillSwitchState(businessId: string) {
  const relevant = items.filter(
    (item) => item.scope_kind === "global" || item.scope_kind === "platform_account" || (item.scope_kind === "business" && item.scope_id === businessId),
  );
  const effective = resolveEffective(relevant.filter((item) => item.scope_kind !== "platform_account"));
  const byAccount = PLATFORM_ACCOUNT_IDS.map((platformAccountId) => {
    const applicable = relevant.filter((item) => item.scope_kind !== "platform_account" || item.scope_id === platformAccountId);
    const resolution = resolveEffective(applicable);
    return {
      platform_account_id: platformAccountId,
      engaged: resolution.engaged,
      mode: resolution.mode,
      source_scope_kind: resolution.scope_kind,
    };
  });

  return { items: relevant, effective, by_account: byAccount };
}

export interface EngageInput {
  scope_kind: ScopeKind;
  scope_id: string | null;
  mode: Mode;
  reason: string;
}

export function engageBrake(input: EngageInput): void {
  items.push({
    brake_id: `brake_${nextId++}`,
    scope_kind: input.scope_kind,
    scope_id: input.scope_id,
    scope_label: scopeLabelFor(input.scope_kind, input.scope_id),
    mode: input.mode,
    engaged_at: new Date().toISOString(),
    engaged_by: "owner_demo",
    reason: input.reason || null,
  });
}

export interface DisengageInput {
  scope_kind: ScopeKind;
  scope_id: string | null;
  mode: Mode;
  typed_confirmation?: string;
}

export type DisengageResult = { ok: true } | { ok: false; code: "TYPED_CONFIRMATION_REQUIRED"; phrase: string };

export function disengageBrake(input: DisengageInput): DisengageResult {
  if (input.typed_confirmation?.trim().toUpperCase() !== "REACTIVAR") {
    return { ok: false, code: "TYPED_CONFIRMATION_REQUIRED", phrase: "REACTIVAR" };
  }
  const index = items.findIndex(
    (item) => item.scope_kind === input.scope_kind && item.scope_id === input.scope_id && item.mode === input.mode,
  );
  if (index !== -1) items.splice(index, 1);
  return { ok: true };
}
