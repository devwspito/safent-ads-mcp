import { http, HttpResponse } from "msw";
import { server } from "@/mocks/server";
import { API_BASE } from "@/mocks/handlers/apiBase";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";
import { resetCloudflareFixtures } from "@/mocks/handlers";
import { connectCloudflareToken } from "@/mocks/fixtures/cloudflare";
import { CloudflareConnectionCard } from "./CloudflareConnectionCard";

const SECRET_TOKEN = "sk-cloudflare-super-secret-token-do-not-leak";
const ACCOUNT_ID = "a".repeat(32);

function renderCard() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <CloudflareConnectionCard />
    </QueryClientProvider>,
  );
}

describe("CloudflareConnectionCard", () => {
  beforeEach(() => resetCloudflareFixtures());

  it("empieza desconectada sin vocabulario técnico visible, revelado tras un clic", async () => {
    const user = userEvent.setup();
    renderCard();

    expect(await screen.findByText("No conectado")).toBeInTheDocument();
    expect(screen.queryByText(/token/i)).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Crear token en Cloudflare" })).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Conectar Cloudflare" }));

    expect(screen.getByRole("link", { name: "Crear token en Cloudflare" })).toHaveAttribute(
      "href",
      "https://dash.cloudflare.com/profile/api-tokens",
    );
    expect(screen.getByText(/Zone.Read/)).toBeInTheDocument();
    expect(screen.getByText(/DNS.Edit/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Conectar Cloudflare" })).toBeDisabled();
  });

  it("muestra la ayuda de detección automática y mantiene el identificador de cuenta bajo Opciones", async () => {
    const user = userEvent.setup();
    renderCard();
    await user.click(await screen.findByRole("button", { name: "Conectar Cloudflare" }));

    expect(
      screen.getByText("Pega el token. Si es un token de cuenta, detectamos la cuenta solos."),
    ).toBeInTheDocument();
    expect(screen.getByText("Opciones").closest("details")).not.toHaveAttribute("open");
  });

  it("conecta un token de cuenta sin teclear el identificador de cuenta", async () => {
    const user = userEvent.setup();
    let submittedAccountId: string | undefined;
    server.use(
      http.post(`${API_BASE}/integrations/cloudflare/token`, async ({ request }) => {
        const body = (await request.json()) as { token: string; account_id?: string };
        submittedAccountId = body.account_id;
        return HttpResponse.json(
          connectCloudflareToken({ account_id: "b".repeat(32) }),
          { status: 201 },
        );
      }),
    );
    renderCard();
    await user.click(await screen.findByRole("button", { name: "Conectar Cloudflare" }));

    await user.type(screen.getByLabelText("Token de Cloudflare"), SECRET_TOKEN);
    expect(screen.getByRole("button", { name: "Conectar Cloudflare" })).toBeEnabled();
    await user.click(screen.getByRole("button", { name: "Conectar Cloudflare" }));

    await waitFor(() => expect(submittedAccountId).toBeUndefined());
    expect(await screen.findByText("Conectado a example.com, example.net")).toBeInTheDocument();
  });

  it("teclear un identificador de cuenta actualiza el enlace de creación de token", async () => {
    const user = userEvent.setup();
    renderCard();
    await user.click(await screen.findByRole("button", { name: "Conectar Cloudflare" }));

    await user.type(screen.getByLabelText("Identificador de cuenta (opcional)"), ACCOUNT_ID);

    expect(screen.getByRole("link", { name: "Crear token en Cloudflare" })).toHaveAttribute(
      "href",
      `https://dash.cloudflare.com/${ACCOUNT_ID}/api-tokens`,
    );
  });

  it("conectar guarda el token, muestra las zonas y nunca deja el token en el DOM", async () => {
    const user = userEvent.setup();
    let submittedToken: string | undefined;
    server.use(
      http.post(`${API_BASE}/integrations/cloudflare/token`, async ({ request }) => {
        const body = (await request.json()) as { token: string; account_id?: string };
        submittedToken = body.token;
        return HttpResponse.json(connectCloudflareToken({ account_id: body.account_id }), { status: 201 });
      }),
    );
    renderCard();
    await user.click(await screen.findByRole("button", { name: "Conectar Cloudflare" }));

    await user.type(screen.getByLabelText("Token de Cloudflare"), SECRET_TOKEN);
    await user.click(screen.getByRole("button", { name: "Conectar Cloudflare" }));

    await waitFor(() => expect(submittedToken).toBe(SECRET_TOKEN));
    expect(await screen.findByText("Conectado a example.com, example.net")).toBeInTheDocument();
    expect(document.body.textContent).not.toContain(SECRET_TOKEN);
    expect(screen.queryByLabelText("Token de Cloudflare")).not.toBeInTheDocument();
  });

  it("un token inválido muestra el motivo en español y no queda conectado", async () => {
    const user = userEvent.setup();
    server.use(
      http.post(`${API_BASE}/integrations/cloudflare/token`, () =>
        HttpResponse.json(
          {
            error: {
              code: "CLOUDFLARE_TOKEN_INVALID",
              message: "Cloudflare rechazó el token o no puede leer zonas DNS con él.",
              details: {},
            },
          },
          { status: 422 },
        ),
      ),
    );
    renderCard();
    await user.click(await screen.findByRole("button", { name: "Conectar Cloudflare" }));

    await user.type(screen.getByLabelText("Token de Cloudflare"), "token-invalido");
    await user.click(screen.getByRole("button", { name: "Conectar Cloudflare" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Cloudflare rechazó el token");
    expect(screen.getByText("No conectado")).toBeInTheDocument();
  });

  it("desconectar exige escribir DESCONECTAR y vuelve al estado sin conectar", async () => {
    const user = userEvent.setup();
    connectCloudflareToken({ account_id: ACCOUNT_ID });
    renderCard();
    await screen.findByText("Conectado a example.com, example.net");

    await user.click(screen.getByRole("button", { name: "Desconectar" }));
    const dialog = await screen.findByRole("dialog", { name: "Desconectar Cloudflare" });
    const confirmButton = within(dialog).getByRole("button", { name: "Desconectar" });
    expect(confirmButton).toBeDisabled();

    await user.type(within(dialog).getByLabelText(/Escribe DESCONECTAR/), "DESCONECTAR");
    expect(confirmButton).toBeEnabled();
    await user.click(confirmButton);

    expect(await screen.findByText("No conectado")).toBeInTheDocument();
  });

  it("reemplazar precarga la cuenta guardada sin precargar el token", async () => {
    const user = userEvent.setup();
    connectCloudflareToken({ account_id: ACCOUNT_ID });
    renderCard();
    await screen.findByText("Conectado a example.com, example.net");

    await user.click(screen.getByRole("button", { name: "Reemplazar" }));

    expect(screen.getByLabelText("Identificador de cuenta (opcional)")).toHaveValue(ACCOUNT_ID);
    expect(screen.getByLabelText("Token de Cloudflare")).toHaveValue("");
  });
});
