import type { Platform } from "@/api/schemas";

/** A resume pointer, never an OAuth URL, state, token, or cached result. */
export function reconnectSessionKey(ownerId: string, businessId: string, provider: Platform): string | null {
  return ownerId && businessId
    ? `safent-ads:reconnect:v1:${JSON.stringify([ownerId, businessId, provider])}`
    : null;
}

export function isReconnectSessionId(value: unknown): value is string {
  return typeof value === "string" && /^[A-Za-z0-9_-]{1,200}$/.test(value);
}

export function readReconnectSession(key: string | null): string | null {
  if (!key) return null;
  try {
    const value = localStorage.getItem(key);
    if (isReconnectSessionId(value)) return value;
    if (value !== null) localStorage.removeItem(key);
  } catch { /* A denied storage API must not prevent connecting in this window. */ }
  return null;
}

export function saveReconnectSession(key: string | null, sessionId: string): boolean {
  if (!key || !isReconnectSessionId(sessionId)) return false;
  try {
    localStorage.setItem(key, sessionId);
    return true;
  } catch { return false; }
}
