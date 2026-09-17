import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { http, HttpResponse } from "msw";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import type { Platform } from "@/api/schemas";
import { reconnectSessionKey } from "@/utils/reconnectSession";
import { server } from "@/mocks/server";
import { API_BASE } from "@/mocks/handlers/apiBase";
import { setMockSessionForTests } from "@/mocks/handlers";
import { ConnectProviderCard } from "./ConnectProviderCard";

const AUTHORIZE = "https://accounts.google.com/o/oauth2/v2/auth?state=synthetic-only-state";
const START = `${API_BASE}/platform-accounts/:provider/reconnect/start`;
function success(url = AUTHORIZE) {
  return HttpResponse.json({ session_id: "synthetic-session", authorize_url: url, expires_at: new Date(Date.now() + 600_000).toISOString() }, { status: 201 });
}
function renderCard({ ownerId = "owner-one", businessId = "business-one", provider = "google", required = false }: {
  ownerId?: string; businessId?: string; provider?: Platform; required?: boolean;
} = {}) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  function view(currentBusiness: string, configured = true, currentOwner = ownerId, currentProvider = provider) {
    return <QueryClientProvider client={queryClient}><ConnectProviderCard ownerId={currentOwner} businessId={currentBusiness} provider={currentProvider} configured={configured} googleCustomerIdRequired={required} /></QueryClientProvider>;
  }
  const rendered = render(view(businessId));
  return {
    ...rendered, queryClient,
    changeBusiness: () => rendered.rerender(view("business-two")),
    changeOwner: () => rendered.rerender(view(businessId, true, "owner-two")),
    changeProvider: () => rendered.rerender(view(businessId, true, ownerId, "meta")),
    restoreScope: () => rendered.rerender(view(businessId)),
    configuration: (configured: boolean) => rendered.rerender(view(businessId, configured)),
  };
}
beforeEach(() => {
  setMockSessionForTests(true);
  vi.spyOn(window, "open").mockReturnValue(null);
  server.use(http.get(`${API_BASE}/platform-accounts/:provider/reconnect/status`, () => HttpResponse.json({ state: "waiting", error_code: null, message: null })));
});
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });

it.each([403, 503])("shows a safe start error for HTTP %s, never an unhandled rejection or false connection", async status => {
  let calls = 0;
  server.use(http.post(START, () => { calls++; return HttpResponse.json({ error: { code: "FAILED", message: "SYNTHETIC_PRIVATE_DETAIL" } }, { status }); }));
  const user = userEvent.setup();
  renderCard();
  await user.click(screen.getByRole("button", { name: "Conectar" }));
  expect(await screen.findByRole("alert")).toHaveTextContent(/No se pudo/);
  expect(document.body.textContent).not.toContain("SYNTHETIC_PRIVATE_DETAIL");
  expect(screen.queryByText(/Conexión completada/)).not.toBeInTheDocument();
  expect(screen.queryByRole("link", { name: "Continuar en Google" })).not.toBeInTheDocument();
  expect(window.open).not.toHaveBeenCalled();
  expect(calls).toBe(1);
  expect(screen.getByRole("button", { name: "Conectar" })).toBeEnabled();
});

it("deduplicates rapid clicks while pending and exposes a safe continuation when popup opening returns null", async () => {
  let release!: () => void;
  const hold = new Promise<void>(resolve => { release = resolve; });
  let calls = 0;
  server.use(http.post(START, async () => { calls++; await hold; return success(); }));
  renderCard();
  const connect = screen.getByRole("button", { name: "Conectar" });
  await act(async () => { connect.click(); connect.click(); });
  await waitFor(() => expect(calls).toBe(1));
  expect(connect).toBeDisabled();
  expect(screen.getByText("Preparando la conexión…")).toBeVisible();
  release();
  const link = await screen.findByRole("link", { name: "Continuar en Google" });
  expect(link).toHaveAttribute("href", AUTHORIZE);
  expect(link).toHaveAttribute("target", "_blank");
  expect(link).toHaveAttribute("rel", "noopener noreferrer");
  expect(window.open).toHaveBeenCalledTimes(1);
  expect(screen.queryByText(/Conexión completada/)).not.toBeInTheDocument();
  expect(connect).toBeDisabled();
  expect(calls).toBe(1);
});

it("retains the deliberate continuation link if the opening API throws", async () => {
  vi.mocked(window.open).mockImplementation(() => { throw new Error("SYNTHETIC_OPENER_DETAIL"); });
  server.use(http.post(START, () => success()));
  const user = userEvent.setup();
  renderCard();
  await user.click(screen.getByRole("button", { name: "Conectar" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Usa el enlace para continuar");
  expect(screen.getByRole("link", { name: "Continuar en Google" })).toHaveAttribute("href", AUTHORIZE);
  expect(document.body.textContent).not.toContain("SYNTHETIC_OPENER_DETAIL");
});

it("explains Composio Customer ID in human language without calling it a developer credential", async () => {
  server.use(http.post(START, () => success("https://connect.composio.dev/link/lk_test_only")));
  renderCard();
  await userEvent.setup().click(screen.getByRole("button", { name: "Conectar" }));
  expect(await screen.findByText(/Customer ID.*número de tu cuenta/)).toHaveTextContent("10 dígitos");
  expect(screen.getByText(/Customer ID.*número de tu cuenta/)).toHaveTextContent("No es una clave");
  expect(screen.queryByText(/Conexión completada/)).not.toBeInTheDocument();
});

it("does not invent a Customer ID prerequisite for direct Google OAuth", async () => {
  server.use(http.post(START, () => success()));
  renderCard();
  await userEvent.setup().click(screen.getByRole("button", { name: "Conectar" }));
  await screen.findByRole("link", { name: "Continuar en Google" });
  expect(screen.queryByText(/Customer ID/)).not.toBeInTheDocument();
});

it("keeps the pending flow across the configuration refetch on return from the external browser", async () => {
  server.use(http.post(START, () => success()));
  const user = userEvent.setup();
  const card = renderCard();
  await user.click(screen.getByRole("button", { name: "Conectar" }));
  await screen.findByRole("link", { name: "Continuar en Google" });
  card.configuration(false);
  expect(screen.queryByRole("link", { name: "Continuar en Google" })).not.toBeInTheDocument();
  card.configuration(true);
  expect(screen.getByRole("link", { name: "Continuar en Google" })).toHaveAttribute("href", AUTHORIZE);
  expect(window.open).toHaveBeenCalledTimes(1);
});

it.each(["javascript:alert(1)", "data:text/html,test", "http://accounts.google.com/authorize", "https://user:secret@example.test/authorize", "/relative/authorize"])(
  "never renders or opens an unsafe authorization URL: %s", async url => {
    server.use(http.post(START, () => success(url)));
    const user = userEvent.setup();
    renderCard();
    await user.click(screen.getByRole("button", { name: "Conectar" }));
    expect(await screen.findByRole("alert")).toBeVisible();
    expect(screen.queryByRole("link", { name: "Continuar en Google" })).not.toBeInTheDocument();
    expect(window.open).not.toHaveBeenCalled();
    expect(screen.queryByText("Esperando a la plataforma…")).not.toBeInTheDocument();
  },
);

it("discards a pending old-business authorization rather than opening it in the new context", async () => {
  let release!: () => void;
  const hold = new Promise<void>(resolve => { release = resolve; });
  let calls = 0;
  server.use(http.post(START, async () => { calls++; await hold; return success(); }));
  const user = userEvent.setup();
  const card = renderCard();
  await user.click(screen.getByRole("button", { name: "Conectar" }));
  await waitFor(() => expect(calls).toBe(1));
  card.changeBusiness();
  release();
  await waitFor(() => expect(screen.getByRole("button", { name: "Conectar" })).toBeEnabled());
  expect(window.open).not.toHaveBeenCalled();
  expect(screen.queryByRole("link", { name: "Continuar en Google" })).not.toBeInTheDocument();
});

it.each(["OAUTH_SESSION_EXPIRED", "BROKER_UNAVAILABLE"])(
  "unblocks a fresh attempt after polling returns %s", async errorCode => {
    let polls = 0;
    let starts = 0;
    server.use(
      http.post(START, () => { starts++; return success(); }),
      http.get(`${API_BASE}/platform-accounts/:provider/reconnect/status`, () => {
        polls++;
        return HttpResponse.json(polls === 1 ? { state: "waiting", error_code: null, message: null } : {
          state: "error", error_code: errorCode, message: "Vuelve a intentarlo.",
        });
      }),
    );
    const user = userEvent.setup();
    renderCard();
    const button = screen.getByRole("button", { name: "Conectar" });
    await user.click(button);
    await screen.findByText("Vuelve a intentarlo.");
    expect(button).toBeEnabled();
    expect(screen.queryByText(/Conexión completada/)).not.toBeInTheDocument();
    await user.click(button);
    await waitFor(() => expect(starts).toBe(2));
  },
);

const STATUS = `${API_BASE}/platform-accounts/:provider/reconnect/status`;
const SESSION_KEY = reconnectSessionKey("owner-one", "business-one", "google")!;

it.each(["google", "meta"] as const)("shows no accessible accounts as an error for %s and starts a fresh attempt", async provider => {
  const key = reconnectSessionKey("owner-one", "business-one", provider)!;
  localStorage.setItem(key, "previous-session");
  const message = "No se encontró ninguna cuenta publicitaria accesible. Vuelve a conectar y autoriza al menos una cuenta publicitaria a la que tengas acceso.";
  let starts = 0;
  server.use(
    http.get(STATUS, ({ request }) => HttpResponse.json(
      new URL(request.url).searchParams.get("session_id") === "previous-session"
        ? { state: "error", error_code: "OAUTH_NO_ACCESSIBLE_ACCOUNTS", message }
        : { state: "waiting", error_code: null, message: null },
    )),
    http.post(START, () => { starts++; return success("https://connect.composio.dev/link/new-attempt"); }),
  );
  const card = renderCard({ provider });
  const invalidate = vi.spyOn(card.queryClient, "invalidateQueries");
  expect(await screen.findByRole("alert")).toHaveTextContent(message);
  expect(screen.queryByText(/Conexión completada/)).not.toBeInTheDocument();
  expect(screen.getByRole("alert").textContent).not.toMatch(/client.?id|app.?id|desarrollador/i);
  expect(invalidate).not.toHaveBeenCalled();
  expect(window.open).not.toHaveBeenCalled();
  const connect = screen.getByRole("button", { name: "Conectar" });
  expect(connect).toBeEnabled();
  await userEvent.setup().click(connect);
  await screen.findByRole("link", { name: `Continuar en ${provider === "google" ? "Google" : "Meta"}` });
  expect(starts).toBe(1);
  expect(localStorage.getItem(key)).toBe("synthetic-session");
  expect(screen.queryByText(/Conexión completada/)).not.toBeInTheDocument();
});

it.each(["ok", "error"])("resumes after reload with only the session ID stored and fetches the %s result", async state => {
  let starts = 0;
  const polls: string[] = [];
  server.use(http.post(START, () => { starts++; return success(); }));
  const card = renderCard({ required: true });
  const user = userEvent.setup();
  await user.type(screen.getByLabelText("Número de cuenta de Google Ads"), "123-456-7890");
  await user.click(screen.getByRole("button", { name: "Conectar" }));
  await screen.findByRole("link", { name: "Continuar en Google" });
  expect(localStorage.getItem(SESSION_KEY)).toBe("synthetic-session");
  expect(localStorage.length).toBe(1);
  expect(localStorage.getItem(SESSION_KEY)).not.toMatch(/state|https|123|token|ok|error/);
  card.unmount();
  server.use(http.get(STATUS, ({ request }) => {
    polls.push(new URL(request.url).searchParams.get("session_id")!);
    return HttpResponse.json({ state, error_code: state === "error" ? "OAUTH_PROVIDER_DENIED" : null, message: state === "error" ? "La plataforma rechazó la conexión." : null });
  }));
  const resumed = renderCard({ required: true });
  const invalidate = vi.spyOn(resumed.queryClient, "invalidateQueries");
  expect(await screen.findByText(state === "ok" ? "Conexión completada." : "La plataforma rechazó la conexión.")).toBeVisible();
  expect(polls).toEqual(["synthetic-session"]);
  expect(screen.getByLabelText("Número de cuenta de Google Ads")).toHaveValue("");
  expect(screen.queryByRole("link", { name: "Continuar en Google" })).not.toBeInTheDocument();
  expect(starts).toBe(1);
  expect(window.open).toHaveBeenCalledTimes(1);
  expect(localStorage.getItem(SESSION_KEY)).toBe("synthetic-session");
  if (state === "ok") await waitFor(() => expect(invalidate).toHaveBeenCalledWith({ queryKey: ["platform-accounts", "business-one"] }));
  else expect(invalidate).not.toHaveBeenCalled();
});

it.each(["owner", "business", "provider"] as const)("isolates a restored attempt by %s, including its query cache", async scope => {
  localStorage.setItem(SESSION_KEY, "synthetic-session");
  const requests: string[] = [];
  server.use(http.get(STATUS, ({ request }) => {
    requests.push(request.url);
    return HttpResponse.json({ state: "error", error_code: "OAUTH_PROVIDER_DENIED", message: "Resultado del intento anterior." });
  }));
  const card = renderCard();
  await screen.findByText("Resultado del intento anterior.");
  if (scope === "owner") card.changeOwner();
  else if (scope === "business") card.changeBusiness();
  else card.changeProvider();
  expect(screen.queryByText("Resultado del intento anterior.")).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Conectar" })).toBeEnabled();
  expect(requests).toHaveLength(1);
  const reconnectQueries = card.queryClient.getQueryCache().findAll({ queryKey: ["reconnect-status"] });
  expect(reconnectQueries.filter(query => query.queryKey.includes("synthetic-session")).map(query => query.queryKey)).toEqual([
    ["reconnect-status", "owner-one", "business-one", "google", "synthetic-session"],
  ]);
  card.restoreScope();
  expect(await screen.findByText("Resultado del intento anterior.")).toBeVisible();
  expect(localStorage.getItem(SESSION_KEY)).toBe("synthetic-session");
  expect(window.open).not.toHaveBeenCalled();
});

it.each(["http", "network", "schema"])("shows a safe %s polling failure and retries the same attempt", async failure => {
  localStorage.setItem(SESSION_KEY, "synthetic-session");
  server.use(http.get(STATUS, () => failure === "network" ? HttpResponse.error() : failure === "schema"
    ? HttpResponse.json({ state: "PRIVATE_INVALID_STATE" })
    : HttpResponse.json({ error: { code: "FAILED", message: "PRIVATE_BACKEND_DETAIL" } }, { status: 503 })));
  renderCard();
  expect(await screen.findByRole("alert")).toHaveTextContent("No se pudo comprobar el resultado");
  expect(document.body.textContent).not.toMatch(/PRIVATE_|Conexión completada|Esperando a la plataforma/);
  expect(screen.getByRole("button", { name: "Conectar" })).toBeDisabled();
  let resumedSession: string | null = null;
  server.use(http.get(STATUS, ({ request }) => {
    resumedSession = new URL(request.url).searchParams.get("session_id");
    return HttpResponse.json({ state: "ok", error_code: null, message: null });
  }));
  await userEvent.setup().click(screen.getByRole("button", { name: "Reintentar comprobación" }));
  expect(await screen.findByText("Conexión completada.")).toBeVisible();
  expect(resumedSession).toBe("synthetic-session");
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  expect(window.open).not.toHaveBeenCalled();
});

it("checks the saved attempt on return to Safent and deduplicates focus and visibility events", async () => {
  localStorage.setItem(SESSION_KEY, "synthetic-session");
  server.use(http.get(STATUS, () => HttpResponse.json({}, { status: 503 })));
  renderCard();
  await screen.findByRole("alert");
  let release!: () => void;
  const hold = new Promise<void>(resolve => { release = resolve; });
  let polls = 0;
  server.use(http.get(STATUS, async () => {
    polls++;
    await hold;
    return HttpResponse.json({ state: "ok", error_code: null, message: null });
  }));
  fireEvent.focus(window);
  await waitFor(() => expect(polls).toBe(1));
  fireEvent(document, new Event("visibilitychange"));
  fireEvent.focus(window);
  release();
  await screen.findByText("Conexión completada.");
  expect(polls).toBe(1);
  expect(window.open).not.toHaveBeenCalled();
});

it.each([404, 410])("makes an unavailable HTTP %s session explicit and allows a new attempt", async status => {
  localStorage.setItem(SESSION_KEY, "synthetic-session");
  server.use(http.get(STATUS, () => HttpResponse.json({}, { status })));
  renderCard();
  expect(await screen.findByRole("alert")).toHaveTextContent("Este intento ya no está disponible");
  expect(screen.getByRole("button", { name: "Conectar" })).toBeEnabled();
});

it.each(["https://example.test/?token=private", '{"session_id":"id","token":"private"}', "bad session"])("ignores and removes an invalid saved value: %s", value => {
  localStorage.setItem(SESSION_KEY, value);
  renderCard();
  expect(localStorage.getItem(SESSION_KEY)).toBeNull();
  expect(screen.getByRole("button", { name: "Conectar" })).toBeEnabled();
  expect(screen.queryByText("Esperando a la plataforma…")).not.toBeInTheDocument();
});

it("keeps connecting usable and warns when storage is unavailable", async () => {
  vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("denied"); });
  server.use(http.post(START, () => success()));
  renderCard();
  await userEvent.setup().click(screen.getByRole("button", { name: "Conectar" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Esta ventana no puede recordar el intento");
  expect(screen.getByRole("link", { name: "Continuar en Google" })).toBeVisible();
});

it.each(["", "123", "123456789a", "１２３４５６７８９０", "123 456 7890"])("validates the managed Google Ads number before starting: %s", value => {
  const start = vi.fn(() => success());
  server.use(http.post(START, start));
  renderCard({ required: true });
  const field = screen.getByLabelText("Número de cuenta de Google Ads");
  expect(field).toBeRequired();
  fireEvent.change(field, { target: { value } });
  fireEvent.click(screen.getByRole("button", { name: "Conectar" }));
  expect(screen.getByRole("alert")).toHaveTextContent("10 dígitos");
  expect(start).not.toHaveBeenCalled();
  expect(window.open).not.toHaveBeenCalled();
});

it.each(["1234567890", "123-456-7890"])("sends only the normalized Google number for %s", async value => {
  let body: unknown;
  server.use(http.post(START, async ({ request }) => { body = await request.json(); return success(); }));
  renderCard({ required: true });
  const user = userEvent.setup();
  await user.type(screen.getByLabelText("Número de cuenta de Google Ads"), value);
  await user.click(screen.getByRole("button", { name: "Conectar" }));
  await screen.findByRole("link", { name: "Continuar en Google" });
  expect(body).toEqual({ google_customer_id: "1234567890" });
  expect(localStorage.getItem(SESSION_KEY)).toBe("synthetic-session");
});

it.each(["google", "meta"] as const)("keeps the request body optional for direct %s OAuth", async provider => {
  let body: string | undefined;
  server.use(http.post(START, async ({ request }) => { body = await request.text(); return success(); }));
  renderCard({ provider });
  if (provider === "google") expect(screen.getByLabelText(/Número de cuenta/)).not.toBeRequired();
  else expect(screen.queryByLabelText(/Número de cuenta/)).not.toBeInTheDocument();
  await userEvent.setup().click(screen.getByRole("button", { name: "Conectar" }));
  await screen.findByRole("link", { name: `Continuar en ${provider === "google" ? "Google" : "Meta"}` });
  expect(body).toBe("");
});

it("explains required Google selection returned by the backend without exposing its payload", async () => {
  server.use(http.post(START, () => HttpResponse.json({ error: { code: "GOOGLE_ACCOUNT_SELECTION_REQUIRED", message: "PRIVATE_DETAIL" } }, { status: 422 })));
  renderCard();
  await userEvent.setup().click(screen.getByRole("button", { name: "Conectar" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Introduce el número de cuenta de Google Ads");
  expect(document.body.textContent).not.toContain("PRIVATE_DETAIL");
});

it("uses the fixed native opener for both connect and Continue, accepting its null success", async () => {
  const invoke = vi.fn().mockResolvedValue(null);
  vi.stubGlobal("__TAURI__", { core: { invoke } });
  const url = "https://connect.composio.dev/link/lk_test_only";
  server.use(http.post(START, () => success(url)));
  renderCard();
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "Conectar" }));
  const link = await screen.findByRole("link", { name: "Continuar en Google" });
  await waitFor(() => expect(invoke).toHaveBeenCalledWith("open_ads_oauth", { url }));
  expect(window.open).not.toHaveBeenCalled();
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  await user.click(link);
  expect(invoke).toHaveBeenCalledTimes(2);
  expect(invoke).toHaveBeenLastCalledWith("open_ads_oauth", { url });
  expect(window.open).not.toHaveBeenCalled();
});

it("keeps native opener rejection visible and retryable without losing the session", async () => {
  const invoke = vi.fn().mockRejectedValue(new Error("PRIVATE_NATIVE_DETAIL"));
  vi.stubGlobal("__TAURI__", { core: { invoke } });
  server.use(http.post(START, () => success()));
  renderCard();
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "Conectar" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Usa el enlace para continuar");
  await user.click(screen.getByRole("link", { name: "Continuar en Google" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Vuelve a pulsar el enlace");
  expect(invoke).toHaveBeenCalledTimes(2);
  expect(localStorage.getItem(SESSION_KEY)).toBe("synthetic-session");
  expect(document.body.textContent).not.toContain("PRIVATE_NATIVE_DETAIL");
  expect(window.open).not.toHaveBeenCalled();
  invoke.mockResolvedValue(null);
  await user.click(screen.getByRole("link", { name: "Continuar en Google" }));
  await waitFor(() => expect(screen.queryByRole("alert")).not.toBeInTheDocument());
  expect(invoke).toHaveBeenCalledTimes(3);
});
