import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { getResponse, http, HttpResponse } from "msw";
import { server } from "@/mocks/server";
import { App } from "@/App";
import { BusinessOnboarding } from "./BusinessOnboarding";

const business = { business_id: "10000000-0000-4000-8000-000000000001", name: "Mi marca", slug: "mi-marca" };
const session = { owner_id: "owner", email: "owner@example.test", businesses: [] as typeof business[] };
let current = session;
let reads = 0;

function renderApp(prefix = "") {
  if (prefix) {
    const meta = document.createElement("meta");
    meta.name = "safent-ads-base-path";
    meta.content = prefix;
    document.head.append(meta);
    // The embedded document uses the same API contract under its authenticated prefix.
    server.use(http.get("*/ads/api/v1/*", async ({ request }) =>
      await getResponse([...server.listHandlers()], new Request(request.url.replace("/ads/api/v1/", "/api/v1/")))
      ?? HttpResponse.json({}, { status: 404 })));
  }
  window.history.replaceState({}, "", `${prefix}/propuestas?business_id=foreign`);
  return render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><App /></QueryClientProvider>);
}

async function fill() {
  const user = userEvent.setup();
  await user.type(await screen.findByLabelText("Nombre del negocio"), "Mi marca");
  await user.clear(screen.getByLabelText("Zona horaria"));
  await user.type(screen.getByLabelText("Zona horaria"), "Europe/Madrid");
  return user;
}

describe("first business onboarding", () => {
  beforeEach(() => {
    current = { ...session, businesses: [] };
    reads = 0;
    localStorage.clear();
    server.use(http.get("*/api/v1/auth/me", () => { reads++; return HttpResponse.json(current); }));
  });
  afterEach(() => document.querySelectorAll('meta[name="safent-ads-base-path"]').forEach(el => el.remove()));

  // Text inputs strip CR/LF before onChange; all other C0 characters and DEL remain invalid.
  it.each([...Array.from({ length: 32 }, (_, code) => code).filter(code => code !== 10 && code !== 13), 127])("rejects control character %i before posting", async code => {
    const post = vi.fn(() => HttpResponse.json(business, { status: 201 }));
    server.use(http.post("*/api/v1/onboarding/business", post));
    const onReady = vi.fn();
    render(<BusinessOnboarding ownerId="owner" onReady={onReady} />);
    fireEvent.change(screen.getByLabelText("Nombre del negocio"), { target: { value: `Mi${String.fromCharCode(code)}marca` } });
    await userEvent.click(screen.getByRole("button", { name: "Crear negocio y continuar" }));
    expect(screen.getByRole("alert")).toHaveTextContent("Indica un nombre y una moneda de referencia");
    expect(post).not.toHaveBeenCalled();
    expect(onReady).not.toHaveBeenCalled();
  });

  it.each(["Mi marca", "Mi~marca", `Mi${String.fromCharCode(128)}marca`, "Mi marca 🌱"])("preserves accepted name %s", async name => {
    let posted: unknown;
    server.use(http.post("*/api/v1/onboarding/business", async ({ request }) => {
      posted = await request.json();
      current = { ...session, businesses: [business] };
      return HttpResponse.json(business, { status: 201 });
    }));
    const onReady = vi.fn();
    render(<BusinessOnboarding ownerId="owner" onReady={onReady} />);
    fireEvent.change(screen.getByLabelText("Nombre del negocio"), { target: { value: name } });
    await userEvent.click(screen.getByRole("button", { name: "Crear negocio y continuar" }));
    await waitFor(() => expect(onReady).toHaveBeenCalledOnce());
    expect(posted).toMatchObject({ name });
  });

  it.each(["", "/ads"])("gates empty-business routes and posts under prefix %s, then verifies /me before Connections", async prefix => {
    const requests: string[] = [];
    document.cookie = "ads_csrf=onboarding-qa-token; path=/";
    const capture = ({ request }: { request: Request }) => requests.push(new URL(request.url).pathname);
    server.events.on("request:start", capture);
    let posted: unknown;
    server.use(http.post(`*${prefix}/api/v1/onboarding/business`, async ({ request }) => {
      expect(request.headers.get("X-CSRF-Token")).toBe("onboarding-qa-token");
      posted = await request.json();
      current = { ...session, businesses: [business] };
      return HttpResponse.json(business, { status: 201 });
    }));
    const view = renderApp(prefix);
    expect(await screen.findByRole("heading", { name: "Tu negocio, primero" })).toBeInTheDocument();
    expect(screen.getByLabelText("Nombre del negocio")).toHaveFocus();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(requests).toEqual([`${prefix}/api/v1/auth/me`]);
    const user = await fill();
    await user.click(screen.getByRole("button", { name: "Crear negocio y continuar" }));
    await waitFor(() => expect(window.location.pathname).toBe(`${prefix}/ajustes`));
    expect(new URLSearchParams(window.location.search).get("business_id")).toBe(business.business_id);
    expect(posted).toEqual({ name: "Mi marca", timezone: "Europe/Madrid", reference_currency: "EUR" });
    expect(reads).toBe(2);
    view.unmount();
    server.events.removeListener("request:start", capture);
  });

  it.each([409, 503])("resolves %s via /me without repeating POST", async status => {
    let posts = 0;
    server.use(http.post("*/api/v1/onboarding/business", () => {
      posts++;
      current = { ...session, businesses: [business] };
      return HttpResponse.json({ error: { code: "BUSINESS_ALREADY_CONFIGURED", message: "Conflict" } }, { status });
    }));
    renderApp();
    const user = await fill();
    await user.click(screen.getByRole("button", { name: "Crear negocio y continuar" }));
    await waitFor(() => expect(window.location.pathname).toBe("/ajustes"));
    expect(posts).toBe(1);
    expect(reads).toBe(2);
  });

  it("uncertain write and failed refresh keep the draft and offer only a read retry", async () => {
    let posts = 0;
    server.use(http.post("*/api/v1/onboarding/business", () => {
      posts++;
      return HttpResponse.error();
    }));
    renderApp();
    const user = await fill();
    server.use(http.get("*/api/v1/auth/me", () => HttpResponse.json({}, { status: 503 })));
    await user.click(screen.getByRole("button", { name: "Crear negocio y continuar" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("No pudimos comprobar");
    expect(screen.getByRole("alert")).toHaveFocus();
    expect(screen.getByLabelText("Nombre del negocio")).toHaveValue("Mi marca");
    expect(screen.queryByRole("button", { name: "Crear negocio y continuar" })).not.toBeInTheDocument();
    server.use(http.get("*/api/v1/auth/me", () => HttpResponse.json({ ...session, businesses: [business] })));
    await user.tab();
    expect(screen.getByRole("button", { name: "Comprobar estado" })).toHaveFocus();
    await user.keyboard("{Enter}");
    await waitFor(() => expect(window.location.pathname).toBe("/ajustes"));
    expect(posts).toBe(1);
  });

  it("403 is a real error, not an empty-state success", async () => {
    server.use(http.post("*/api/v1/onboarding/business", () => HttpResponse.json({ error: { code: "OWNER_CONFIGURATION_AMBIGUOUS", message: "Forbidden" } }, { status: 403 })));
    renderApp();
    const user = await fill();
    await user.click(screen.getByRole("button", { name: "Crear negocio y continuar" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("configuración de propietarios");
    expect(window.location.pathname).toBe("/propuestas");
    expect(reads).toBe(1);
  });

  it("a conflict without a visible business stays blocked until an explicit status check", async () => {
    let posts = 0;
    server.use(http.post("*/api/v1/onboarding/business", () => {
      posts++;
      return HttpResponse.json({ error: { code: "BUSINESS_ALREADY_CONFIGURED", message: "Conflict" } }, { status: 409 });
    }));
    renderApp();
    const user = await fill();
    await user.click(screen.getByRole("button", { name: "Crear negocio y continuar" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Todavía no podemos confirmar");
    expect(screen.getByLabelText("Nombre del negocio")).toBeDisabled();
    expect(posts).toBe(1);
    current = { ...session, businesses: [business] };
    await user.click(screen.getByRole("button", { name: "Comprobar estado" }));
    await waitFor(() => expect(window.location.pathname).toBe("/ajustes"));
    expect(posts).toBe(1);
  });

  it("a lost write confirmed absent permits only a new explicit submission", async () => {
    let posts = 0;
    server.use(http.post("*/api/v1/onboarding/business", () => {
      posts++;
      return HttpResponse.error();
    }));
    renderApp();
    const user = await fill();
    await user.click(screen.getByRole("button", { name: "Crear negocio y continuar" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("aún no hay un negocio");
    expect(screen.getByRole("button", { name: "Crear negocio y continuar" })).toBeEnabled();
    expect(posts).toBe(1);
    expect(reads).toBe(2);
  });

  it("a different owner returned after creation never becomes this form's business", async () => {
    const onReady = vi.fn();
    server.use(http.post("*/api/v1/onboarding/business", () => {
      current = { ...session, owner_id: "different-owner", businesses: [business] };
      return HttpResponse.json(business, { status: 201 });
    }));
    render(<BusinessOnboarding ownerId="owner" onReady={onReady} />);
    const user = await fill();
    await user.click(screen.getByRole("button", { name: "Crear negocio y continuar" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("La sesión ha cambiado");
    expect(onReady).not.toHaveBeenCalled();
    expect(screen.queryByRole("button", { name: "Crear negocio y continuar" })).not.toBeInTheDocument();
  });

  it("does not create an onboarding state from a failed initial /me", async () => {
    server.use(http.get("*/api/v1/auth/me", () => HttpResponse.json({}, { status: 503 })));
    renderApp();
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(screen.queryByLabelText("Nombre del negocio")).not.toBeInTheDocument();
  });

  it("ignores a late POST after unmount and cannot double-submit", async () => {
    let finish!: () => void;
    let responded!: () => void;
    const responseDone = new Promise<void>(resolve => { responded = resolve; });
    const responseListener = () => responded();
    server.events.on("response:mocked", responseListener);
    let posts = 0;
    server.use(http.post("*/api/v1/onboarding/business", async () => {
      posts++;
      await new Promise<void>(resolve => { finish = resolve; });
      return HttpResponse.json(business, { status: 201 });
    }));
    const onReady = vi.fn();
    const view = render(<BusinessOnboarding ownerId="owner" onReady={onReady} />);
    const user = await fill();
    await user.dblClick(screen.getByRole("button", { name: "Crear negocio y continuar" }));
    await waitFor(() => expect(posts).toBe(1));
    view.unmount();
    await act(async () => { finish(); await responseDone; });
    expect(onReady).not.toHaveBeenCalled();
    expect(reads).toBe(0);
    server.events.removeListener("response:mocked", responseListener);
  });
});
