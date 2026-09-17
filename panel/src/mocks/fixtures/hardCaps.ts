/**
 * Topes duros por cuenta (spec 008, `contracts/hard-caps.openapi.yaml`): el sobre del fichero,
 * el tope del fichero, el tope del panel y la resolución `min(fichero, panel)` que el bróker
 * devuelve ya recortada.
 *
 * Este doble modela lo que hace el BRÓKER, no lo que le gustaría al panel: el sobre acota solo
 * lo que fija el panel (nunca las entradas del fichero), el estado del panel se ignora entero
 * cuando no se puede leer o cuando el sobre desaparece, y una cuenta sin tope de ninguna de las
 * dos procedencias deniega el 100 % de las escrituras.
 */
import type {
  CapAmounts,
  CappedField,
  EffectiveCaps,
  HardCapsUpdate,
  HardCapsView,
  SpendEnvelope,
} from "@/api/schemas/hardCaps";

interface FileCaps {
  daily_cap_minor: number;
  monthly_cap_minor: number;
  floor_minor: number;
  ceiling_minor: number;
}

interface AccountCapsRecord {
  file: FileCaps | null;
  panel: HardCapsUpdate | null;
}

/** El mismo sobre del `quickstart.md` §C, con `max_accounts` a 3. */
const ENVELOPE_LIMITS = {
  currency: "EUR",
  max_daily_cap_minor: 5000,
  max_monthly_cap_minor: 100_000,
  max_ceiling_minor: 20_000,
  min_floor_minor: 500,
  max_accounts: 3,
  max_cap_changes_per_day: 10,
} as const;

/** Divisa que devuelve `ads-api` cuando no hay sobre declarado: la del motor, no una del panel. */
const ENGINE_DEFAULT_CURRENCY = "EUR";

function defaultAccounts(): Record<string, AccountCapsRecord> {
  return {
    // Fichero y panel: el panel pidió 50,00 € al día y el fichero solo deja 40,00 € — recortado.
    "100-000-0002": {
      file: { daily_cap_minor: 4000, monthly_cap_minor: 80_000, floor_minor: 200, ceiling_minor: 15_000 },
      panel: { daily_cap_minor: 5000, monthly_cap_minor: 20_000, ceiling_minor: 9000, currency: "EUR" },
    },
    // Sin tope de ninguna procedencia: deniega el 100 % de las escrituras.
    "act_100000000000001": { file: null, panel: null },
    // Solo fichero.
    "1000000001": {
      file: { daily_cap_minor: 3000, monthly_cap_minor: 60_000, floor_minor: 100, ceiling_minor: 12_000 },
      panel: null,
    },
    // Solo panel: el suelo sale del sobre, porque no hay entrada de fichero que lo declare.
    "act_100000000000002": {
      file: null,
      panel: { daily_cap_minor: 2000, monthly_cap_minor: 40_000, ceiling_minor: 8000, currency: "EUR" },
    },
  };
}

let accounts = defaultAccounts();
let envelopeDeclared = true;
let panelStateAvailable = true;
let capChangesToday = 0;

export function resetHardCapsFixtures() {
  accounts = defaultAccounts();
  envelopeDeclared = true;
  panelStateAvailable = true;
  capChangesToday = 0;
}

/** Despliegue sin `panel_managed:` en `config/caps.yaml`: el panel no puede fijar nada. */
export function setMockEnvelopeDeclared(declared: boolean) {
  envelopeDeclared = declared;
}

/** El bróker no pudo leer su estado: se resuelve solo con el fichero, nunca con el del panel. */
export function setMockPanelStateAvailable(available: boolean) {
  panelStateAvailable = available;
}

function accountsUsed(): number {
  return Object.values(accounts).filter((record) => record.panel !== null).length;
}

function currentEnvelope(): SpendEnvelope | null {
  if (!envelopeDeclared) return null;
  return {
    ...ENVELOPE_LIMITS,
    accounts_used: accountsUsed(),
    cap_changes_today: capChangesToday,
  };
}

function clampToEnvelope(panel: HardCapsUpdate, envelope: SpendEnvelope) {
  return {
    daily_cap_minor: Math.min(panel.daily_cap_minor, envelope.max_daily_cap_minor),
    monthly_cap_minor: Math.min(panel.monthly_cap_minor, envelope.max_monthly_cap_minor),
    ceiling_minor: Math.min(panel.ceiling_minor, envelope.max_ceiling_minor),
  };
}

/** Los tres importes de UNA fuente, tal como los declara: sin recortar y sin divisa. */
function capAmounts(source: FileCaps | HardCapsUpdate | null): CapAmounts | null {
  if (source === null) return null;
  return {
    daily_cap_minor: source.daily_cap_minor,
    monthly_cap_minor: source.monthly_cap_minor,
    ceiling_minor: source.ceiling_minor,
  };
}

function clampedFields(panel: HardCapsUpdate, effective: EffectiveCaps): CappedField[] {
  const fields: CappedField[] = ["daily_cap_minor", "monthly_cap_minor", "ceiling_minor"];
  return fields.filter((field) => panel[field] > effective[field]);
}

function view(platformAccountId: string, record: AccountCapsRecord): HardCapsView {
  const envelope = currentEnvelope();
  // Sobre retirado o estado ilegible: se ignora el documento ENTERO del panel, nunca por
  // entradas, y se cae al fichero. Ignorarlo es siempre igual o más restrictivo que aplicarlo.
  const panel = envelope !== null && panelStateAvailable ? record.panel : null;
  const currency = envelope?.currency ?? ENGINE_DEFAULT_CURRENCY;
  const base = {
    platform_account_id: platformAccountId,
    currency,
    panel_state_available: panelStateAvailable,
    envelope,
    // Lo GUARDADO a cada lado, que no siempre es lo que se aplica. El bróker los manda siempre;
    // este doble también, porque si no la pantalla de «guardado X, en vigor Y» solo existiría en
    // los tests que la fuerzan a mano.
    from_file: capAmounts(record.file),
    from_panel: capAmounts(panel),
  };

  if (panel === null) {
    const file = record.file;
    return {
      ...base,
      source: file === null ? "none" : "file",
      writable: file !== null,
      effective: file,
      clamped_by: [],
    };
  }

  const clamped = clampToEnvelope(panel, envelope as SpendEnvelope);
  const file = record.file;
  const effective: EffectiveCaps =
    file === null
      ? { ...clamped, floor_minor: (envelope as SpendEnvelope).min_floor_minor }
      : {
          daily_cap_minor: Math.min(clamped.daily_cap_minor, file.daily_cap_minor),
          monthly_cap_minor: Math.min(clamped.monthly_cap_minor, file.monthly_cap_minor),
          ceiling_minor: Math.min(clamped.ceiling_minor, file.ceiling_minor),
          floor_minor: file.floor_minor,
        };
  return {
    ...base,
    source: file === null ? "panel" : "file_and_panel",
    writable: true,
    effective,
    clamped_by: clampedFields(panel, effective),
  };
}

export function resolveMockHardCaps(platformAccountId: string): HardCapsView | null {
  const record = accounts[platformAccountId];
  return record ? view(platformAccountId, record) : null;
}

export interface MockCapsDenial {
  status: number;
  code: string;
}

/** Los cuatro rechazos del sobre, en el mismo orden que el bróker los comprueba. */
export function setMockHardCaps(platformAccountId: string, caps: HardCapsUpdate): HardCapsView | MockCapsDenial {
  const record = accounts[platformAccountId];
  if (!record) return { status: 404, code: "NOT_FOUND" };
  const envelope = currentEnvelope();
  if (envelope === null) return { status: 409, code: "ENVELOPE_NOT_DECLARED" };
  if (caps.currency !== envelope.currency) return { status: 400, code: "INVALID_CAPS" };
  if (
    caps.daily_cap_minor > envelope.max_daily_cap_minor ||
    caps.monthly_cap_minor > envelope.max_monthly_cap_minor ||
    caps.ceiling_minor > envelope.max_ceiling_minor
  ) {
    return { status: 409, code: "ENVELOPE_EXCEEDED" };
  }
  if (record.panel === null && envelope.accounts_used >= envelope.max_accounts) {
    return { status: 409, code: "ENVELOPE_ACCOUNTS_EXHAUSTED" };
  }
  if (envelope.cap_changes_today >= envelope.max_cap_changes_per_day) {
    return { status: 409, code: "ENVELOPE_CHANGES_EXHAUSTED" };
  }
  record.panel = { ...caps };
  capChangesToday += 1;
  return view(platformAccountId, record);
}

export function deleteMockHardCaps(platformAccountId: string): HardCapsView | MockCapsDenial {
  const record = accounts[platformAccountId];
  if (!record) return { status: 404, code: "NOT_FOUND" };
  const envelope = currentEnvelope();
  if (envelope === null) return { status: 409, code: "ENVELOPE_NOT_DECLARED" };
  if (envelope.cap_changes_today >= envelope.max_cap_changes_per_day) {
    return { status: 409, code: "ENVELOPE_CHANGES_EXHAUSTED" };
  }
  record.panel = null;
  capChangesToday += 1;
  return view(platformAccountId, record);
}

export function isMockCapsDenial(result: HardCapsView | MockCapsDenial): result is MockCapsDenial {
  return "code" in result;
}
