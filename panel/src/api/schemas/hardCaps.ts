/**
 * Contrato OpenAPI de hard-caps v1.1.0 —
 * `GET/PUT/DELETE /api/v1/accounts/{platform_account_id}/hard-caps`.
 *
 * El id de la ruta es el de la PLATAFORMA (`external_account_id`: `customer_id` de Google,
 * `act_…` de Meta), nunca el identificador interno del despliegue.
 *
 * `effective` ya viene recortado a `min(fichero, panel)` y al sobre vigente: es lo que se
 * aplicará, no lo que se guardó. `envelope: null` significa que el despliegue no declara
 * `panel_managed` en `config/caps.yaml` y el panel no puede fijar nada.
 */
import { z } from "zod";

/** `MinorAmount`: entero, unidad menor de `envelope.currency`, con cota superior — el contrato
 * la fija porque JSON admite enteros ilimitados y la aritmética del motor no. */
export const MAX_MINOR_AMOUNT = 1_000_000_000_000;

const minorAmountSchema = z.number().int().min(0).max(MAX_MINOR_AMOUNT);

const currencySchema = z.string().regex(/^[A-Z]{3}$/);

export const capSourceSchema = z.enum(["file", "panel", "file_and_panel", "none"]);
export type CapSource = z.infer<typeof capSourceSchema>;

/** Los tres campos que el panel fija — y los únicos que pueden quedar recortados. */
export const cappedFieldSchema = z.enum(["daily_cap_minor", "monthly_cap_minor", "ceiling_minor"]);
export type CappedField = z.infer<typeof cappedFieldSchema>;

export const effectiveCapsSchema = z.object({
  daily_cap_minor: minorAmountSchema,
  monthly_cap_minor: minorAmountSchema,
  /** Del fichero, o de `envelope.min_floor_minor` en una cuenta solo-panel: el panel no lo fija. */
  floor_minor: minorAmountSchema,
  ceiling_minor: minorAmountSchema,
});
export type EffectiveCaps = z.infer<typeof effectiveCapsSchema>;

export const spendEnvelopeSchema = z.object({
  max_daily_cap_minor: minorAmountSchema,
  max_monthly_cap_minor: minorAmountSchema,
  max_ceiling_minor: minorAmountSchema,
  min_floor_minor: minorAmountSchema,
  max_accounts: z.number().int().min(1),
  accounts_used: z.number().int().min(0),
  max_cap_changes_per_day: z.number().int().min(1),
  cap_changes_today: z.number().int().min(0),
  currency: currencySchema,
});
export type SpendEnvelope = z.infer<typeof spendEnvelopeSchema>;

/**
 * Cuerpo exacto del PUT: tres importes y la divisa de confirmación, nada más. `floor_minor`,
 * `max_step_pct`, `max_changes_per_day` y `autonomy_enabled` no se fijan desde el panel y el
 * servidor los rechaza ruidosamente con 400 `INVALID_CAPS`.
 */
export const hardCapsUpdateSchema = z.object({
  daily_cap_minor: minorAmountSchema,
  monthly_cap_minor: minorAmountSchema,
  ceiling_minor: minorAmountSchema,
  currency: currencySchema,
});
export type HardCapsUpdate = z.infer<typeof hardCapsUpdateSchema>;

/**
 * Los tres importes tal como los declara UNA fuente (`from_file`, `from_panel`): sin recortar y
 * sin resolver. Sin `floor_minor` —el panel no lo fija— y sin `currency`: la divisa de la vista es
 * una sola y ya viaja en `currency`; repetirla por fuente insinuaría que pueden diferir.
 */
export const capAmountsSchema = z.object({
  daily_cap_minor: minorAmountSchema,
  monthly_cap_minor: minorAmountSchema,
  ceiling_minor: minorAmountSchema,
});
export type CapAmounts = z.infer<typeof capAmountsSchema>;

export const hardCapsViewSchema = z.object({
  /**
   * El id de la PLATAFORMA ya canonicalizado por el bróker (`external_account_id`: `customer_id`
   * de Google, `act_…` de Meta) — el mismo que va en la ruta. No es el `platform_account_id`
   * interno del despliegue con el que `TopesSection` indexa la lista de cuentas conectadas:
   * coinciden de nombre y no de valor.
   */
  platform_account_id: z.string(),
  source: capSourceSchema,
  /** `false` con `source = none`: esa cuenta deniega el 100 % de las escrituras. */
  writable: z.boolean(),
  currency: currencySchema,
  effective: effectiveCapsSchema.nullable(),
  clamped_by: z.array(cappedFieldSchema),
  /** `false` cuando el bróker no pudo leer su estado: se resuelve solo con el fichero. */
  panel_state_available: z.boolean(),
  envelope: spendEnvelopeSchema.nullable(),
  /** Lo que declara `config/caps.yaml` para esta cuenta, si declara algo. */
  from_file: capAmountsSchema.nullable().optional(),
  /**
   * Lo que el dueño guardó desde el panel, que no siempre es lo que se aplica. Con él, un campo
   * recortado se lee «guardado X, en vigor Y»; sin él solo se dice que está recortado — nunca se
   * inventa X.
   */
  from_panel: capAmountsSchema.nullable().optional(),
});
export type HardCapsView = z.infer<typeof hardCapsViewSchema>;

/**
 * Cuerpo del PUT con las claves SIEMPRE en el mismo orden: la prueba de confirmación de un solo
 * uso está ligada al cuerpo exacto, así que el reenvío confirmado tiene que ser byte a byte el
 * mismo que produjo el 428.
 */
export function hardCapsUpdateBody(caps: HardCapsUpdate): HardCapsUpdate {
  return {
    daily_cap_minor: caps.daily_cap_minor,
    monthly_cap_minor: caps.monthly_cap_minor,
    ceiling_minor: caps.ceiling_minor,
    currency: caps.currency,
  };
}
