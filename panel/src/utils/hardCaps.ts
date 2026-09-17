/**
 * Reglas puras de la sección «Topes» (spec 008, `contracts/hard-caps.openapi.yaml`): importes en
 * unidad menor ↔ unidad mayor, validación del formulario contra el sobre y traducción de los
 * códigos de error. Sin React y sin red: lo que decide se puede probar sin pintar nada.
 */
import { ApiRequestError } from "@/api/client";
import {
  MAX_MINOR_AMOUNT,
  type CapSource,
  type CappedField,
  type HardCapsUpdate,
  type HardCapsView,
  type SpendEnvelope,
} from "@/api/schemas/hardCaps";
import { formatMoney } from "@/utils/money";

/** El motor convierte a unidad menor con un factor fijo (`money_minor_units`), nunca por divisa. */
const MINOR_UNITS_PER_MAJOR = 100;

/** Un importe escrito a mano: dígitos y, como mucho, dos decimales tras coma o punto. */
const MAJOR_AMOUNT_PATTERN = /^(\d{1,15})(?:[.,](\d{1,2}))?$/;

export function formatMinor(minor: number, currency: string): string {
  return formatMoney({ amount: minor / MINOR_UNITS_PER_MAJOR, currency });
}

/** Valor inicial del campo: aritmética entera, para que 4000 nunca se vea como «39,999999». */
export function formatMinorForInput(minor: number): string {
  const whole = Math.trunc(minor / MINOR_UNITS_PER_MAJOR);
  const cents = minor % MINOR_UNITS_PER_MAJOR;
  return cents === 0 ? String(whole) : `${whole},${String(cents).padStart(2, "0")}`;
}

/**
 * Unidad mayor escrita por el dueño → unidad menor entera. `null` cuando no es un importe: el
 * contrato rechaza coma flotante, `true`, `NaN` y todo lo que no sea un entero acotado, así que
 * el panel no envía nada que ya sepa que se va a rechazar.
 */
export function parseMajorToMinor(input: string): number | null {
  const match = MAJOR_AMOUNT_PATTERN.exec(input.trim());
  if (!match) return null;
  const cents = (match[2] ?? "").padEnd(2, "0");
  const minor = Number(match[1]) * MINOR_UNITS_PER_MAJOR + Number(cents);
  if (!Number.isSafeInteger(minor) || minor > MAX_MINOR_AMOUNT) return null;
  return minor;
}

export function describeSource(source: CapSource): string {
  switch (source) {
    case "file":
      return "Tope del fichero config/caps.yaml.";
    case "panel":
      return "Tope fijado desde el panel.";
    case "file_and_panel":
      return "Tope del fichero y del panel: se aplica el menor de los dos.";
    case "none":
      return "Esta cuenta no puede escribir todavía.";
  }
}

export interface CapsFormValues {
  daily: string;
  monthly: string;
  ceiling: string;
}

export type CapsFormField = keyof CapsFormValues;
export type CapsFormErrors = Partial<Record<CapsFormField, string>>;

export interface CapsFormResult {
  errors: CapsFormErrors;
  /** Lo que se enviará, ya en unidad menor y con la divisa del sobre. `null` si algo falla. */
  caps: HardCapsUpdate | null;
}

const NOT_AN_AMOUNT = "Escribe un importe con dos decimales como mucho.";

/**
 * Validación de cortesía, nunca de autoridad: el bróker revalida los tres importes contra el
 * sobre **al aplicar**. Aquí solo se evita un viaje que ya se sabe rechazado, y se dice en qué
 * campo está el problema.
 */
export function validateCapsForm(values: CapsFormValues, envelope: SpendEnvelope): CapsFormResult {
  const daily = parseMajorToMinor(values.daily);
  const monthly = parseMajorToMinor(values.monthly);
  const ceiling = parseMajorToMinor(values.ceiling);
  const errors: CapsFormErrors = {};

  if (daily === null) errors.daily = NOT_AN_AMOUNT;
  else if (daily > envelope.max_daily_cap_minor) {
    errors.daily = `Como mucho ${formatMinor(envelope.max_daily_cap_minor, envelope.currency)} al día.`;
  }
  if (monthly === null) errors.monthly = NOT_AN_AMOUNT;
  else if (monthly > envelope.max_monthly_cap_minor) {
    errors.monthly = `Como mucho ${formatMinor(envelope.max_monthly_cap_minor, envelope.currency)} al mes.`;
  }
  if (ceiling === null) errors.ceiling = NOT_AN_AMOUNT;
  else if (ceiling > envelope.max_ceiling_minor) {
    errors.ceiling = `Como mucho ${formatMinor(envelope.max_ceiling_minor, envelope.currency)} de techo.`;
  }

  if (daily !== null && monthly !== null && !errors.monthly && monthly < daily) {
    errors.monthly = "El tope mensual no puede ser menor que el diario.";
  }
  if (daily !== null && ceiling !== null && !errors.daily && !errors.ceiling && daily > ceiling) {
    errors.daily = "El tope diario no puede pasar del techo.";
  }

  if (Object.keys(errors).length > 0 || daily === null || monthly === null || ceiling === null) {
    return { errors, caps: null };
  }
  return {
    errors,
    caps: {
      daily_cap_minor: daily,
      monthly_cap_minor: monthly,
      ceiling_minor: ceiling,
      // La divisa sale del sobre, nunca de un literal del panel: es confirmación, no conversión.
      currency: envelope.currency,
    },
  };
}

/**
 * Definición normativa de «subir» (`data-model.md` §`AccountHardCap`), la misma que aplica el
 * servidor: se compara contra el efectivo actual (`fichero ∧ panel`), y `source=none → panel`
 * sube siempre, porque pasa de denegar el 100 % de las escrituras a poder escribir. Aquí solo
 * anuncia lo que va a pasar; quien lo exige es `ads-api`.
 */
export function raisesTheEffectiveCap(view: HardCapsView, caps: HardCapsUpdate): boolean {
  const effective = view.effective;
  if (effective === null) return true;
  return (
    caps.daily_cap_minor > effective.daily_cap_minor ||
    caps.monthly_cap_minor > effective.monthly_cap_minor ||
    caps.ceiling_minor > effective.ceiling_minor
  );
}

/** Retirar el tope del panel sube cuando hay entrada de fichero: el efectivo pasa de
 * `min(fichero, panel)` a `fichero` (revisión T027, C3). */
export function withdrawalRaisesTheEffectiveCap(view: HardCapsView): boolean {
  return view.source === "file_and_panel";
}

/**
 * Un campo recortado se dice entero: lo que está en vigor es lo que aplicará el bróker, y si el
 * servidor manda `from_panel`, también lo que se guardó. Sin `from_panel` no se inventa el
 * importe guardado — un tope que nadie escribió confunde más que decir solo que está recortado.
 */
export function describeClampedField(view: HardCapsView, field: CappedField): string | null {
  const effective = view.effective;
  if (effective === null || !view.clamped_by.includes(field)) return null;
  const inForce = formatMinor(effective[field], view.currency);
  const saved = view.from_panel?.[field];
  return saved === undefined
    ? `Recortado: en vigor ${inForce}.`
    : `Guardado ${formatMinor(saved, view.currency)}, en vigor ${inForce}.`;
}

export function describeAccountsLeft(envelope: SpendEnvelope): string {
  const left = accountsLeft(envelope);
  if (left === 0) return `Sin sitio: ya hay ${envelope.max_accounts} cuentas con tope del panel.`;
  if (left === 1) return `Queda 1 cuenta de ${envelope.max_accounts} con tope del panel.`;
  return `Quedan ${left} cuentas de ${envelope.max_accounts} con tope del panel.`;
}

export function describeChangesLeftToday(envelope: SpendEnvelope): string {
  const left = changesLeftToday(envelope);
  if (left === 0) return "No queda ningún cambio de tope para hoy.";
  if (left === 1) return `Queda 1 cambio de tope para hoy, de ${envelope.max_cap_changes_per_day}.`;
  return `Quedan ${left} cambios de tope para hoy, de ${envelope.max_cap_changes_per_day}.`;
}

function accountsLeft(envelope: SpendEnvelope): number {
  return Math.max(0, envelope.max_accounts - envelope.accounts_used);
}

function changesLeftToday(envelope: SpendEnvelope): number {
  return Math.max(0, envelope.max_cap_changes_per_day - envelope.cap_changes_today);
}

const ERROR_MESSAGES: Record<string, string> = {
  ENVELOPE_NOT_DECLARED: "El sobre de gasto no está declarado en config/caps.yaml.",
  ENVELOPE_EXCEEDED: "Ese importe supera el sobre declarado en config/caps.yaml.",
  ENVELOPE_ACCOUNTS_EXHAUSTED: "Ya hay tantas cuentas con tope del panel como permite el sobre.",
  ENVELOPE_CHANGES_EXHAUSTED: "Se acabaron los cambios de tope de hoy.",
  CONFIRMATION_USED: "Esa confirmación ya se usó. Vuelve a intentarlo.",
  BROKER_UNAVAILABLE: "El servicio de topes no responde. No se ha cambiado nada.",
  CAPS_STATE_UNWRITABLE: "No se ha podido guardar el tope. No se ha cambiado nada.",
  CSRF_INVALID: "Recarga la página y vuelve a intentarlo.",
  NOT_FOUND: "Esta cuenta ya no está disponible.",
};

/**
 * Un mensaje por código (`hard-caps.openapi.yaml` §responses). `INVALID_CAPS` conserva el texto
 * del servidor porque nombra el campo exacto que explica el rechazo.
 */
export function describeHardCapsError(error: unknown): string {
  if (!(error instanceof ApiRequestError)) return "Inténtalo de nuevo en unos segundos.";
  if (error.code === "INVALID_CAPS" && error.message) return error.message;
  return ERROR_MESSAGES[error.code] ?? "No se ha podido completar. Inténtalo de nuevo.";
}
