import { http, HttpResponse } from "msw";
import { server } from "@/mocks/server";
import { API_BASE } from "@/mocks/handlers/apiBase";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";
import { useDeletePlatformAppCredentials, usePlatformApps } from "@/api/queries/platformApps";
import { requireMockConfirmation } from "@/mocks/handlers/actionConfirmation";
import { resetPlatformAppsFixtures } from "@/mocks/handlers";
import { DeveloperCredentialsCard } from "./DeveloperCredentialsCard";

const SECRET_CLIENT_SECRET = "super-secret-client-secret";

/** Mismo cableado que `ConexionesPage`: la tarjeta recibe el estado ya resuelto por `usePlatformApps`. */
function GoogleCardHarness() {
  const platformAppsQuery = usePlatformApps();
  const deletePlatformApp = useDeletePlatformAppCredentials();
  const status = platformAppsQuery.data?.items.find((item) => item.platform === "google");

  return (
    <DeveloperCredentialsCard
      platform="google"
      status={status}
      isLoading={platformAppsQuery.isLoading}
      onDelete={(platform) => deletePlatformApp.mutate(platform)}
      isDeleting={deletePlatformApp.isPending}
    />
  );
}

function renderCard() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <GoogleCardHarness />
    </QueryClientProvider>,
  );
}

describe("DeveloperCredentialsCard", () => {
  beforeEach(() => resetPlatformAppsFixtures());

  it("antes de guardar explica el retorno según el tipo visible, no el estado null del servidor", async () => {
    server.use(http.get(`${API_BASE}/platform-apps`, () => HttpResponse.json({ items: [{
      platform: "google", configured: false, client_type: null, client_id_masked: null,
      login_customer_id_masked: null, updated_at: null,
      redirect_uri: "http://127.0.0.1:48371/ads/api/v1/platform-accounts/google/reconnect/callback",
    }] })));
    const user = userEvent.setup();
    renderCard();
    await screen.findByText("Retorno local de escritorio (puerto dinámico; no se registra manualmente)");
    expect(screen.getByLabelText("Tipo de cliente OAuth")).toHaveValue("desktop");
    expect(screen.queryByText(/para registrar en el cliente web/)).not.toBeInTheDocument();
    await user.selectOptions(screen.getByLabelText("Tipo de cliente OAuth"), "web");
    expect(screen.getByText("URI de retorno para registrar en el cliente web de Google")).toBeVisible();
    expect(screen.queryByText(/puerto dinámico; no se registra/)).not.toBeInTheDocument();
  });

  it("reemplazar usa el tipo del formulario y cancelar recupera la guía del cliente guardado", async () => {
    const user = userEvent.setup();
    renderCard();
    await screen.findByText("URI de retorno para registrar en el cliente web de Google");
    await user.click(screen.getByRole("button", { name: "Reemplazar" }));
    expect(screen.getByText(/puerto dinámico; no se registra manualmente/)).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Cancelar" }));
    expect(screen.getByText("URI de retorno para registrar en el cliente web de Google")).toBeVisible();
  });

  it("escritorio permite guardar sin secreto y descarta un secreto web anterior", async () => {
    const user = userEvent.setup();
    let submitted: Record<string, unknown> | undefined;
    server.use(http.put(`${API_BASE}/platform-apps/google`, async ({request}) => {
      const body = await request.clone().json() as Record<string, unknown>;
      const challenge = await requireMockConfirmation(request);
      if (challenge) return challenge;
      submitted = body;
      return HttpResponse.json({platform:"google",configured:true,client_type:"desktop",client_id_masked:"****.com",login_customer_id_masked:null,redirect_uri:"http://127.0.0.1:48371/ads/callback",updated_at:null});
    }));
    renderCard();
    await user.click(await screen.findByRole("button", {name:"Reemplazar"}));
    expect(screen.getByLabelText("Tipo de cliente OAuth")).toHaveValue("desktop");
    expect(screen.getByText(/no necesitas un secreto de cliente/)).toBeInTheDocument();
    await user.selectOptions(screen.getByLabelText("Tipo de cliente OAuth"), "web");
    await user.type(screen.getByLabelText("Client ID"), "desktop.apps.googleusercontent.com");
    await user.type(screen.getByLabelText("Client secret"), SECRET_CLIENT_SECRET);
    await user.selectOptions(screen.getByLabelText("Tipo de cliente OAuth"), "desktop");
    expect(screen.queryByLabelText("Client secret")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", {name:"Guardar"}));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText(/Escritorio · PKCE sin secreto/)).toBeInTheDocument();
    expect(dialog.textContent).not.toContain(SECRET_CLIENT_SECRET);
    await user.click(within(dialog).getByRole("button", {name:"Guardar"}));
    await waitFor(() => expect(submitted).toBeDefined());
    expect(submitted?.client_type).toBe("desktop");
    expect(submitted).not.toHaveProperty("client_secret");
  });

  it("muestra el estado configurado con los ids enmascarados y la redirect URI", async () => {
    renderCard();

    expect(await screen.findByText(/Configurada/)).toBeInTheDocument();
    expect(screen.getByText("****.com")).toBeInTheDocument();
    expect(screen.getByText("****7890")).toBeInTheDocument();
    expect(screen.getByText(/reconnect\/callback/)).toBeInTheDocument();
  });

  it("reemplazar exige confirmación explícita y nunca deja el secreto en el DOM tras guardar", async () => {
    const user = userEvent.setup();
    renderCard();
    await screen.findByText(/Configurada/);

    await user.click(screen.getByRole("button", { name: "Reemplazar" }));
    expect(screen.queryByLabelText(/Token de desarrollador/i)).not.toBeInTheDocument();
    await user.selectOptions(screen.getByLabelText("Tipo de cliente OAuth"), "web");
    await user.type(screen.getByLabelText("Client ID"), "nuevo123.apps.googleusercontent.com");
    await user.type(screen.getByLabelText("Client secret"), SECRET_CLIENT_SECRET);
    await user.click(screen.getByRole("button", { name: "Guardar" }));

    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).queryByLabelText(/Código de verificación/)).not.toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Guardar" }));

    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(screen.getByText("****.com")).toBeInTheDocument();
    expect(document.body.textContent).not.toContain(SECRET_CLIENT_SECRET);
  });

  it("un fallo tras confirmar no repite la acción", async () => {
    server.use(http.put(`${API_BASE}/platform-apps/google`, async ({request}) => {
      const challenge = await requireMockConfirmation(request);
      return challenge ?? HttpResponse.json({error:{code:"FAILED",message:"Error saneado"}},{status:503});
    }));
    const user = userEvent.setup();
    renderCard();
    await screen.findByText(/Configurada/);

    await user.click(screen.getByRole("button", { name: "Reemplazar" }));
    await user.selectOptions(screen.getByLabelText("Tipo de cliente OAuth"), "web");
    await user.type(screen.getByLabelText("Client ID"), "nuevo123.apps.googleusercontent.com");
    await user.type(screen.getByLabelText("Client secret"), SECRET_CLIENT_SECRET);
    await user.click(screen.getByRole("button", { name: "Guardar" }));

    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).queryByLabelText(/Código de verificación/)).not.toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Guardar" }));

    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });

  it("eliminar exige escribir ELIMINAR y deja la plataforma sin configurar", async () => {
    const user = userEvent.setup();
    renderCard();
    await screen.findByText(/Configurada/);

    await user.click(screen.getByRole("button", { name: "Eliminar" }));
    const dialog = await screen.findByRole("dialog", { name: /Eliminar credenciales/ });
    const confirmButton = within(dialog).getByRole("button", { name: "Eliminar" });
    expect(confirmButton).toBeDisabled();

    await user.type(within(dialog).getByLabelText(/Escribe ELIMINAR/), "ELIMINAR");
    expect(confirmButton).toBeEnabled();
    await user.click(confirmButton);

    expect(await screen.findByText("Sin configurar")).toBeInTheDocument();
  });
});
