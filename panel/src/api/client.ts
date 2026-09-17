/**
 * Typed fetch client for ads-api.
 * Session travels in the `ads_session` HttpOnly cookie (rest-api.md §Seguridad transversal);
 * every mutating request carries `X-CSRF-Token` (double submit) read from a non-HttpOnly cookie
 * the API sets for that purpose. Responses are parsed through Zod before use.
 */
import { z } from "zod";
import { getAdsBasePath } from "@/utils/basePath";

// HTTP input is unknown; T is the validated OUTPUT, which can differ after
// a schema transform (e.g. a wire series normalized for the chart).
type ZodSchema<T> = z.ZodType<T, z.ZodTypeDef, unknown>;

// 026 (contracts/cockpit-read-model.md §5): bajo el puente de sesión de
// Safent el documento se sirve en `/ads/...`, así que las llamadas a la API
// también deben llevar ese prefijo -- `getAdsBasePath()` lee lo que
// `composition/app.py` inyectó en `index.html` ("" fuera del modo empotrado).

/** Para respuestas `204 No Content` sin cuerpo (logout, login, unpair…). */
export const voidResponseSchema = z.undefined();
const CSRF_COOKIE_NAME = "ads_csrf";

export class ApiRequestError extends Error {
  constructor(
    message: string,
    public readonly code: string,
    public readonly status: number,
    public readonly details?: Record<string, unknown>,
  ) {
    super(message);
    this.name = "ApiRequestError";
  }
}

export class ApiSchemaError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ApiSchemaError";
  }
}

let unauthorizedListeners: Array<() => void> = [];

/** Subscribes to 401 responses (session expired/absent). Returns an unsubscribe function. */
export function onUnauthorized(listener: () => void): () => void {
  unauthorizedListeners.push(listener);
  return () => {
    unauthorizedListeners = unauthorizedListeners.filter((l) => l !== listener);
  };
}

function readCookie(name: string): string | null {
  const match = document.cookie.match(new RegExp(`(?:^|; )${name}=([^;]*)`));
  const value = match?.[1];
  return value ? decodeURIComponent(value) : null;
}

const MUTATING_METHODS = new Set(["POST", "PUT", "PATCH", "DELETE"]);

interface RequestOptions {
  method?: string;
  body?: unknown;
  query?: Record<string, string | number | boolean | undefined>;
  /** Cabeceras extra, p.ej. `X-Action-Confirmation` (intención exacta) en mutaciones sensibles. */
  headers?: Record<string, string>;
}

function buildUrl(path: string, query?: RequestOptions["query"]): string {
  // Embedded requests always use the authenticated same-origin bridge.
  const prefix = getAdsBasePath();
  const API_BASE = prefix ? `${prefix}/api/v1` : (import.meta.env.VITE_API_BASE ?? "/api/v1");
  const url = new URL(`${API_BASE}${path}`, window.location.origin);
  if (query) {
    for (const [key, value] of Object.entries(query)) {
      if (value !== undefined) url.searchParams.set(key, String(value));
    }
  }
  return url.toString();
}

async function finishResponse<T>(path: string, schema: ZodSchema<T>, response: Response): Promise<T> {
  if (response.status === 204) {
    return schema.parse(undefined);
  }

  const payload: unknown = await response.json().catch(() => null);

  if (response.status === 401) {
    const code = isApiErrorPayload(payload) ? payload.error.code : "UNAUTHORIZED";
    // `REAUTH_REQUIRED` (prueba de presencia fresca de una mutación sensible, TOTP o
    // identificación federada — `fresh_identification.py`) no es una sesión caducada: NO
    // dispara la redirección global a `/login` — el llamador (`useFreshIdentification`) lo
    // muestra inline en el propio `FreshIdentificationPrompt` para que el propietario pueda
    // reintentar sin perder el resto del formulario ni la sesión.
    if (code === "REAUTH_REQUIRED") {
      const message = isApiErrorPayload(payload) ? payload.error.message : "Código de verificación no válido.";
      const details = isApiErrorPayload(payload) ? payload.error.details : undefined;
      throw new ApiRequestError(message, code, 401, details);
    }
    unauthorizedListeners.forEach((listener) => listener());
    throw new ApiRequestError("Sesión no válida.", "UNAUTHORIZED", 401);
  }

  if (!response.ok) {
    const code = isApiErrorPayload(payload) ? payload.error.code : "UNKNOWN_ERROR";
    const message = isApiErrorPayload(payload) ? payload.error.message : "Algo ha fallado en el servidor.";
    const details = isApiErrorPayload(payload) ? payload.error.details : undefined;
    throw new ApiRequestError(message, code, response.status, details);
  }

  const parsed = schema.safeParse(payload);
  if (!parsed.success) {
    throw new ApiSchemaError(`Respuesta de ${path} no coincide con el contrato: ${parsed.error.message}`);
  }
  return parsed.data;
}

async function request<T>(path: string, schema: ZodSchema<T>, options: RequestOptions = {}): Promise<T> {
  const method = options.method ?? "GET";
  const headers: Record<string, string> = { ...options.headers };
  if (options.body !== undefined) headers["Content-Type"] = "application/json";
  if (MUTATING_METHODS.has(method)) {
    const csrfToken = readCookie(CSRF_COOKIE_NAME);
    if (csrfToken) headers["X-CSRF-Token"] = csrfToken;
  }

  const response = await fetch(buildUrl(path, options.query), {
    method,
    headers,
    credentials: "include",
    body: options.body !== undefined ? JSON.stringify(options.body) : undefined,
  });

  return finishResponse(path, schema, response);
}

/**
 * Subida multipart (`POST /brand/assets`): sin `Content-Type` manual — el navegador fija el
 * boundary del `multipart/form-data` — pero con el mismo `X-CSRF-Token` de doble envío que
 * cualquier otra mutación (rest-api.md §Seguridad transversal).
 */
async function requestMultipart<T>(path: string, schema: ZodSchema<T>, formData: FormData, query?: RequestOptions["query"]): Promise<T> {
  const headers: Record<string, string> = {};
  const csrfToken = readCookie(CSRF_COOKIE_NAME);
  if (csrfToken) headers["X-CSRF-Token"] = csrfToken;

  const response = await fetch(buildUrl(path, query), {
    method: "POST",
    headers,
    credentials: "include",
    body: formData,
  });

  return finishResponse(path, schema, response);
}

function isApiErrorPayload(value: unknown): value is { error: { code: string; message: string; details?: Record<string, unknown> } } {
  return typeof value === "object" && value !== null && "error" in value;
}

export const apiClient = {
  get: <T>(path: string, schema: ZodSchema<T>, query?: RequestOptions["query"]) =>
    request(path, schema, { method: "GET", query }),
  post: <T>(
    path: string,
    schema: ZodSchema<T>,
    body?: unknown,
    query?: RequestOptions["query"],
    headers?: RequestOptions["headers"],
  ) => request(path, schema, { method: "POST", body, query, headers }),
  put: <T>(
    path: string,
    schema: ZodSchema<T>,
    body?: unknown,
    query?: RequestOptions["query"],
    headers?: RequestOptions["headers"],
  ) => request(path, schema, { method: "PUT", body, query, headers }),
  patch: <T>(
    path: string,
    schema: ZodSchema<T>,
    body?: unknown,
    query?: RequestOptions["query"],
    headers?: RequestOptions["headers"],
  ) => request(path, schema, { method: "PATCH", body, query, headers }),
  delete: <T>(
    path: string,
    schema: ZodSchema<T>,
    body?: unknown,
    query?: RequestOptions["query"],
    headers?: RequestOptions["headers"],
  ) => request(path, schema, { method: "DELETE", body, query, headers }),
  postForm: <T>(path: string, schema: ZodSchema<T>, formData: FormData, query?: RequestOptions["query"]) =>
    requestMultipart(path, schema, formData, query),
};
