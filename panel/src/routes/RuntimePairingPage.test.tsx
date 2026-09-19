import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { http, HttpResponse } from "msw";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { server } from "@/mocks/server";
import { API_BASE } from "@/mocks/handlers/apiBase";
import { setMockSessionForTests } from "@/mocks/handlers/auth";
import { RuntimePairingPage } from "./RuntimePairingPage";

const request = { challenge: "a".repeat(64), public_key: "b".repeat(400), runtime: "codex", label: "Mi Mac" };
function show(valid = true) {
  window.history.replaceState({}, "", valid ? "/runtime/vincular?" + new URLSearchParams(request) : "/runtime/vincular");
  return render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><MemoryRouter><RuntimePairingPage /></MemoryRouter></QueryClientProvider>);
}

describe("RuntimePairingPage", () => {
  beforeEach(() => {
    setMockSessionForTests(true);
    vi.stubGlobal("crypto", { subtle: { digest: async () => new Uint8Array(32).fill(17).buffer } });
  });
  afterEach(() => { vi.unstubAllGlobals(); window.history.replaceState({}, "", "/"); });
  it("never authorizes on page load and requires matching-code consent", async () => {
    let calls = 0;
    server.use(http.post(`${API_BASE}/runtime/pair`, async ({ request: sent }) => {
      calls++; expect(await sent.json()).toEqual(request);
      return HttpResponse.json({ approved: true, connection_id: "00000000-0000-4000-8000-000000000001" });
    }));
    show();
    const button = await screen.findByRole("button", { name: "Autorizar este equipo" });
    expect(button).toBeDisabled(); expect(calls).toBe(0);
    expect(await screen.findByText("111111111111")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("checkbox"));
    await userEvent.click(button);
    expect(await screen.findByRole("status")).toHaveTextContent("Autorizado");
    expect(calls).toBe(1);
  });
  it("rejects incomplete installation links", () => {
    show(false);
    expect(screen.getByRole("alert")).toHaveTextContent("Enlace de instalación inválido");
    expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
  });
  it("keeps pairing in place while login opens another tab", async () => {
    setMockSessionForTests(false); show();
    await waitFor(() => expect(screen.getByRole("link", { name: /Iniciar sesión/ })).toHaveAttribute("target", "_blank"));
    expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
  });
});
