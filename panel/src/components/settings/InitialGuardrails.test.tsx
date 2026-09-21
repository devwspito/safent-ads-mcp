import { expect, it } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { http, HttpResponse } from "msw";
import { server } from "@/mocks/server";
import { InitialGuardrailForm } from "./InitialGuardrails";

it("requires explicit limits and confirmation; saves against the selected business without approving", async () => {
  const calls: { url: string; body: Record<string, unknown> }[] = [];
  server.use(http.put("/api/v1/guardrails/:ref", async ({ request }) => {
    const body = await request.json() as Record<string, unknown>;
    calls.push({ url: request.url, body });
    return HttpResponse.json({ ...body, guardrail_id: "meta:account:test", scope: "platform_account", scope_label: "Meta", currency: "EUR" });
  }));
  render(<QueryClientProvider client={new QueryClient()}><InitialGuardrailForm businessId="business-test" account={{ account_ref: "meta:account:test", platform: "meta", platform_account_id: "act_test", currency: "EUR" }} /></QueryClientProvider>);
  expect(screen.getByLabelText("Tope diario de cuenta (EUR)")).toHaveValue(null);
  const user = userEvent.setup();
  const values = ["15", "650", "0", "15", "20", "1"];
  const inputs = screen.getAllByRole("spinbutton");
  for (let i = 0; i < inputs.length; i++) await user.type(inputs[i]!, values[i]!);
  await user.click(screen.getByRole("button", { name: "Revisar límites de esta cuenta" }));
  expect(calls).toHaveLength(0);
  expect(screen.getByRole("button", { name: "Guardar límites" })).toBeDisabled();
  await user.type(screen.getByLabelText(/Escribe CONFIGURAR/), "CONFIGURAR");
  await user.click(screen.getByRole("button", { name: "Guardar límites" }));
  await waitFor(() => expect(calls).toHaveLength(1));
  expect(new URL(calls[0]!.url).searchParams.get("business_id")).toBe("business-test");
  expect(calls[0]!.body).toMatchObject({ daily_cap: 15, monthly_cap: 650, budget_floor: 0, budget_ceiling: 15 });
  expect(await screen.findByRole("heading", { name: /Configurar límites/ })).toBeInTheDocument();
});
