/** `contracts/rest-api.md` §Conexiones, Telegram y ajustes. */
interface UnavailableLever {
  code: string;
  label: string;
  reason: string;
}

interface AccountRecord {
  platform_account_id: string;
  platform: "google" | "meta";
  external_account_id: string;
  label: string;
  status: "ACTIVE" | "THROTTLED" | "SUSPENDED" | "READ_ONLY";
  currency: string;
  timezone: string;
  token_health: "ok" | "expiring_soon" | "expired" | "revoked";
  token_expires_in_days: number | null;
  api_tier: string;
  quota_used_pct: number | null;
  writes_remaining: number | null;
  unavailable_levers: UnavailableLever[];
  last_synced_minutes_ago: number | null;
  last_error_code: string | null;
}

// `platform_account_id` sigue el `AccountRef` real (`<platform>:<external_account_id>`,
// connections_router.py: "la misma cadena que devuelve GET /platform-accounts") — `revoke`
// depende de esa forma para resolver la cuenta.
const accounts: AccountRecord[] = [
  {
    platform_account_id: "google:100-000-0002",
    platform: "google",
    external_account_id: "100-000-0002",
    label: "Google Ads — Negocio Ejemplo",
    status: "ACTIVE",
    currency: "EUR",
    timezone: "Europe/Madrid",
    token_health: "ok",
    token_expires_in_days: 42,
    api_tier: "Básico",
    quota_used_pct: 34,
    writes_remaining: 480,
    unavailable_levers: [
      { code: "pmax_segmentation", label: "Segmentación en Performance Max", reason: "Advantage+/PMax sólo expone lectura y presupuesto" },
    ],
    last_synced_minutes_ago: 12,
    last_error_code: null,
  },
  {
    platform_account_id: "meta:act_100000000000001",
    platform: "meta",
    external_account_id: "act_100000000000001",
    label: "Meta Ads — Negocio Ejemplo",
    status: "READ_ONLY",
    currency: "EUR",
    timezone: "Europe/Madrid",
    token_health: "expiring_soon",
    token_expires_in_days: 6,
    api_tier: "Limitado",
    quota_used_pct: 78,
    writes_remaining: 12,
    unavailable_levers: [
      { code: "campaign_creation", label: "Creación de campañas", reason: "Token de sólo lectura" },
      { code: "advantage_segmentation", label: "Segmentación en Advantage+", reason: "Advantage+/PMax sólo expone lectura y presupuesto" },
    ],
    last_synced_minutes_ago: 54,
    last_error_code: "TOKEN_EXPIRING_SOON",
  },
  {
    // `platform_accounts.label` todavía no existe en la API real (list_platform_accounts.py):
    // `label` llega igual a `platform_account_id`, la referencia cruda — mismo bug que
    // demuestran las dos propuestas de creación de campaña de más abajo.
    platform_account_id: "google:account:5e1a6c8e-2f3d-4b7a-9c1e-8f2b6a7d4c10:9b3f2a71-6d4c-4e8a-b1f0-7c5e3a9d2f44:1000000001",
    platform: "google",
    external_account_id: "1000000001",
    label: "google:account:5e1a6c8e-2f3d-4b7a-9c1e-8f2b6a7d4c10:9b3f2a71-6d4c-4e8a-b1f0-7c5e3a9d2f44:1000000001",
    status: "ACTIVE",
    currency: "EUR",
    timezone: "Europe/Madrid",
    token_health: "ok",
    token_expires_in_days: 60,
    api_tier: "Básico",
    quota_used_pct: 8,
    writes_remaining: 500,
    unavailable_levers: [],
    last_synced_minutes_ago: 4,
    last_error_code: null,
  },
  {
    platform_account_id: "meta:act_100000000000002",
    platform: "meta",
    external_account_id: "act_100000000000002",
    label: "meta:act_100000000000002",
    status: "ACTIVE",
    currency: "EUR",
    timezone: "Europe/Madrid",
    token_health: "ok",
    token_expires_in_days: 60,
    api_tier: "Completo",
    quota_used_pct: 3,
    writes_remaining: 500,
    unavailable_levers: [],
    last_synced_minutes_ago: 4,
    last_error_code: null,
  },
];

export function listPlatformAccounts() {
  return {
    items: accounts.map((a) => ({
      platform_account_id: a.platform_account_id,
      platform: a.platform,
      external_account_id: a.external_account_id,
      label: a.label,
      status: a.status,
      currency: a.currency,
      timezone: a.timezone,
      api_tier: a.api_tier,
      token: {
        health: a.token_health,
        expires_at: a.token_expires_in_days === null ? null : new Date(Date.now() + a.token_expires_in_days * 86_400_000).toISOString(),
        checked_at: new Date(Date.now() - 3 * 60_000).toISOString(),
      },
      quota: {
        window: "24H",
        used_pct: a.quota_used_pct,
        writes_remaining: a.writes_remaining,
      },
      unavailable_levers: a.unavailable_levers,
      last_synced_at: a.last_synced_minutes_ago === null ? null : new Date(Date.now() - a.last_synced_minutes_ago * 60_000).toISOString(),
      last_error_code: a.last_error_code,
    })),
  };
}

// "Conectar" es por PLATAFORMA, no por cuenta (rest-api.md §Conexiones): el propietario
// autoriza y el bróker descubre cuantas cuentas devuelva la plataforma de una vez.
const reconnectSessions = new Map<string, { provider: "google" | "meta"; startedAt: number }>();

export function startReconnect(provider: "google" | "meta") {
  const sessionId = `rc_${provider}_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`;
  reconnectSessions.set(sessionId, { provider, startedAt: Date.now() });
  return { session_id: sessionId, authorize_url: "https://ads.example/oauth/authorize", expires_at: new Date(Date.now() + 10 * 60_000).toISOString() };
}

export function reconnectStatus(sessionId: string) {
  const session = reconnectSessions.get(sessionId);
  if (!session) return { state: "error" as const, error_code: "RECONNECT_TIMEOUT", message: "Sesión de reconexión no encontrada." };
  const elapsed = Date.now() - session.startedAt;
  if (elapsed < 500) return { state: "waiting" as const, error_code: null, message: null };
  for (const account of accounts) {
    if (account.platform !== session.provider) continue;
    account.status = "ACTIVE";
    account.token_health = "ok";
    account.token_expires_in_days = 60;
    account.last_error_code = null;
  }
  return { state: "ok" as const, error_code: null, message: null };
}

export function revokePlatformAccount(platformAccountId: string): { ok: true } | { ok: false; code: "NOT_FOUND" | "CREDENTIAL_ALREADY_REVOKED" } {
  const account = accounts.find((a) => a.platform_account_id === platformAccountId);
  if (!account) return { ok: false, code: "NOT_FOUND" };
  if (account.token_health === "revoked") return { ok: false, code: "CREDENTIAL_ALREADY_REVOKED" };
  account.token_health = "revoked";
  account.status = "SUSPENDED";
  return { ok: true };
}

let metaSystemUserAccountSeq = 0;

/** El bróker valida el token contra la plataforma antes de que exista ninguna cuenta;
 * el propio token nunca vuelve en la respuesta ni se guarda en este módulo. */
export function registerMetaSystemUserToken() {
  metaSystemUserAccountSeq += 1;
  const externalAccountId = `act_su_${900000 + metaSystemUserAccountSeq}`;
  const record: AccountRecord = {
    platform_account_id: `meta:${externalAccountId}`,
    platform: "meta",
    external_account_id: externalAccountId,
    label: `Meta Ads — System User ${metaSystemUserAccountSeq}`,
    status: "ACTIVE",
    currency: "EUR",
    timezone: "Europe/Madrid",
    token_health: "ok",
    token_expires_in_days: null,
    api_tier: "Completo",
    quota_used_pct: 0,
    writes_remaining: null,
    unavailable_levers: [],
    last_synced_minutes_ago: null,
    last_error_code: null,
  };
  accounts.push(record);
  return {
    accounts: [
      {
        platform: record.platform,
        external_account_id: record.external_account_id,
        label: record.label,
        currency: record.currency,
        timezone: record.timezone,
        api_tier: record.api_tier,
      },
    ],
  };
}

let telegramStatus: "unpaired" | "pending" | "paired" = "unpaired";
let telegramCode: string | null = null;
let telegramCodeIssuedAt = 0;

export function getTelegramPairing() {
  if (telegramStatus === "pending" && Date.now() - telegramCodeIssuedAt > 800) {
    telegramStatus = "paired";
  }
  return {
    status: telegramStatus,
    chat_id_masked: telegramStatus === "paired" ? "***6789" : null,
    paired_at: telegramStatus === "paired" ? new Date().toISOString() : null,
    allowlist_configured: true,
    pairing_code: telegramStatus === "pending" ? telegramCode : null,
    code_expires_at: telegramStatus === "pending" ? new Date(telegramCodeIssuedAt + 10 * 60_000).toISOString() : null,
  };
}

export function startTelegramPairing() {
  telegramStatus = "pending";
  telegramCode = Math.random().toString(36).slice(2, 8).toUpperCase();
  telegramCodeIssuedAt = Date.now();
  return { pairing_code: telegramCode, code_expires_at: new Date(telegramCodeIssuedAt + 10 * 60_000).toISOString() };
}

export function unpairTelegram() {
  telegramStatus = "unpaired";
  telegramCode = null;
  telegramCodeIssuedAt = 0;
}

/** Reinicia el emparejamiento de Telegram a "sin emparejar" — usar entre tests que emparejan. */
export function resetConnectionsFixtures() {
  unpairTelegram();
}
