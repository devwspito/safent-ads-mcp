import { HttpResponse } from "msw";

// Demo-only transport behavior. Production owner/CSRF/session/HMAC live on the server.
const pending = new Map<string, { binding: string; expires: number }>();
export async function requireMockConfirmation(request: Request) {
  const binding = `${request.method}\n${request.url}\n${await request.clone().text()}`;
  const token = request.headers.get("X-Action-Confirmation");
  if (!token) {
    const value = crypto.randomUUID();
    const expires = Date.now() + 120_000;
    pending.set(value, { binding, expires });
    return HttpResponse.json({ error: { code: "CONFIRMATION_REQUIRED", message: "Revisa y confirma esta acción.",
      details: { confirmation_token: value, expires_at: new Date(expires).toISOString() } } }, { status: 428 });
  }
  const entry = pending.get(token);
  if (!entry || entry.expires <= Date.now() || entry.binding !== binding) {
    return HttpResponse.json({ error: { code: "CONFIRMATION_INVALID", message: "Confirmación no válida." } }, { status: 409 });
  }
  pending.delete(token);
  return null;
}
