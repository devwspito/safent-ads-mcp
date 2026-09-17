import { http, HttpResponse } from "msw";
import { MAX_MINOR_AMOUNT, type HardCapsUpdate } from "@/api/schemas/hardCaps";
import { raisesTheEffectiveCap, withdrawalRaisesTheEffectiveCap } from "@/utils/hardCaps";
import { requireMockConfirmation } from "./actionConfirmation";
import { hasMockFreshIdentification } from "./freshIdentification";
import {
  deleteMockHardCaps,
  isMockCapsDenial,
  resolveMockHardCaps,
  setMockHardCaps,
  type MockCapsDenial,
} from "../fixtures/hardCaps";
import { isMockSessionActive } from "./auth";
import { API_BASE } from "./apiBase";

/** Mensajes del router real (`accounts/presentation/hard_caps_router.py`): cada uno nombra el
 * fichero o la variable que explica el rechazo, nunca importes de otra cuenta. */
const DENIAL_MESSAGES: Record<string, string> = {
  NOT_FOUND: "Cuenta no encontrada.",
  ENVELOPE_NOT_DECLARED:
    "Este despliegue no declara panel_managed en config/caps.yaml: los topes solo se fijan en ese fichero.",
  ENVELOPE_EXCEEDED: "El importe supera el sobre declarado en config/caps.yaml.",
  ENVELOPE_ACCOUNTS_EXHAUSTED:
    "Ya hay tantas cuentas con tope del panel como permite panel_managed.max_accounts en config/caps.yaml.",
  ENVELOPE_CHANGES_EXHAUSTED:
    "Se agotaron los cambios de tope de hoy (panel_managed.max_cap_changes_per_day en config/caps.yaml).",
  INVALID_CAPS: "Los topes pedidos no son válidos.",
};

function apiError(status: number, code: string, message?: string) {
  return HttpResponse.json(
    { error: { code, message: message ?? DENIAL_MESSAGES[code] ?? "Petición rechazada." } },
    { status },
  );
}

function denial(result: MockCapsDenial) {
  return apiError(result.status, result.code);
}

function unauthorized() {
  return apiError(401, "UNAUTHORIZED", "Sesión no válida.");
}

/** El 401 de frescura llega SIEMPRE antes que el 428 de confirmación, como en el servidor. */
function reauthRequired() {
  return HttpResponse.json(
    {
      error: {
        code: "REAUTH_REQUIRED",
        message: "Vuelve a identificarte para subir el tope.",
        details: { methods: ["totp"] },
      },
    },
    { status: 401 },
  );
}

const AMOUNT_FIELDS = ["daily_cap_minor", "monthly_cap_minor", "ceiling_minor"] as const;

function isMinorAmount(value: unknown): value is number {
  return typeof value === "number" && Number.isInteger(value) && value >= 0 && value <= MAX_MINOR_AMOUNT;
}

/**
 * `extra="forbid"` más `strict=True` del servidor: `floor_minor`, `max_step_pct`,
 * `max_changes_per_day` y `autonomy_enabled` se rechazan RUIDOSAMENTE, nunca se ignoran, y
 * `3.0`, `"3"` o `true` no pasan por un entero.
 */
function parseCapsBody(body: unknown): { caps: HardCapsUpdate } | { message: string } {
  if (typeof body !== "object" || body === null || Array.isArray(body)) {
    return { message: "El cuerpo debe ser un objeto JSON." };
  }
  const payload = body as Record<string, unknown>;
  const extra = Object.keys(payload).find(
    (key) => !AMOUNT_FIELDS.includes(key as (typeof AMOUNT_FIELDS)[number]) && key !== "currency",
  );
  if (extra) return { message: `Campo ${extra}: el panel no fija este campo.` };
  for (const field of AMOUNT_FIELDS) {
    if (!isMinorAmount(payload[field])) {
      return { message: `Campo ${field}: debe ser un entero en unidad menor.` };
    }
  }
  const currency = payload.currency;
  if (typeof currency !== "string" || !/^[A-Z]{3}$/.test(currency)) {
    return { message: "Campo currency: debe ser un código ISO-4217." };
  }
  const caps: HardCapsUpdate = {
    daily_cap_minor: payload.daily_cap_minor as number,
    monthly_cap_minor: payload.monthly_cap_minor as number,
    ceiling_minor: payload.ceiling_minor as number,
    currency,
  };
  if (caps.monthly_cap_minor < caps.daily_cap_minor) {
    return { message: "Campo monthly_cap_minor: no puede ser menor que daily_cap_minor." };
  }
  if (caps.daily_cap_minor > caps.ceiling_minor) {
    return { message: "Campo daily_cap_minor: no puede pasar de ceiling_minor." };
  }
  return { caps };
}

export const hardCapsHandlers = [
  http.get(`${API_BASE}/accounts/:accountId/hard-caps`, ({ params }) => {
    if (!isMockSessionActive()) return unauthorized();
    const view = resolveMockHardCaps(decodeURIComponent(params.accountId as string));
    return view ? HttpResponse.json(view) : apiError(404, "NOT_FOUND");
  }),

  http.put(`${API_BASE}/accounts/:accountId/hard-caps`, async ({ request, params }) => {
    if (!isMockSessionActive()) return unauthorized();
    const accountId = decodeURIComponent(params.accountId as string);
    const parsed = parseCapsBody(await request.clone().json());
    if ("message" in parsed) return apiError(400, "INVALID_CAPS", parsed.message);
    const current = resolveMockHardCaps(accountId);
    if (!current) return apiError(404, "NOT_FOUND");
    if (raisesTheEffectiveCap(current, parsed.caps) && !hasMockFreshIdentification(request, `caps:${accountId}`)) {
      return reauthRequired();
    }
    const confirmation = await requireMockConfirmation(request);
    if (confirmation) return confirmation;
    const result = setMockHardCaps(accountId, parsed.caps);
    return isMockCapsDenial(result) ? denial(result) : HttpResponse.json(result);
  }),

  http.delete(`${API_BASE}/accounts/:accountId/hard-caps`, async ({ request, params }) => {
    if (!isMockSessionActive()) return unauthorized();
    const accountId = decodeURIComponent(params.accountId as string);
    const current = resolveMockHardCaps(accountId);
    if (!current) return apiError(404, "NOT_FOUND");
    // Retirar el tope del panel es una SUBIDA cuando hay entrada de fichero: el efectivo pasa
    // de `min(fichero, panel)` a `fichero`.
    if (withdrawalRaisesTheEffectiveCap(current) && !hasMockFreshIdentification(request, `caps:${accountId}`)) {
      return reauthRequired();
    }
    const confirmation = await requireMockConfirmation(request);
    if (confirmation) return confirmation;
    const result = deleteMockHardCaps(accountId);
    return isMockCapsDenial(result) ? denial(result) : HttpResponse.json(result);
  }),
];
