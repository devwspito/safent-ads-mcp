import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";
import { useOnboardingStatus } from "@/api/queries/onboarding";
import { resetOnboardingFixtures, setOnboardingStep } from "@/mocks/handlers";
import { OnboardingProgress } from "./OnboardingProgress";

function Harness() {
  const query = useOnboardingStatus();
  return <OnboardingProgress status={query.data} isLoading={query.isLoading} />;
}

function renderProgress() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <Harness />
    </QueryClientProvider>,
  );
}

describe("OnboardingProgress", () => {
  beforeEach(() => resetOnboardingFixtures());

  it("un paso de cuenta bloqueado muestra el motivo en lenguaje del dueño, sin el código crudo", async () => {
    renderProgress();

    expect(await screen.findByText("Cuenta de Google Ads conectada")).toBeInTheDocument();
    expect(screen.getByText("Configura antes el cliente OAuth de Google Ads.")).toBeInTheDocument();
    expect(screen.queryByText("google_app_not_configured")).not.toBeInTheDocument();
  });

  it("con una cuenta conectada, el resumen dice que Anuncios está listo", async () => {
    setOnboardingStep("google_app", "done");
    setOnboardingStep("google_account", "done");

    renderProgress();

    expect(await screen.findByText("Anuncios está listo")).toBeInTheDocument();
    expect(screen.getAllByText("Listo")).toHaveLength(2);
  });

  it("recargar (nueva consulta) deja el progreso exactamente donde estaba -- resumible", async () => {
    setOnboardingStep("meta_app", "done");
    const { unmount } = renderProgress();
    await screen.findByText("Progreso de la configuración");
    unmount();

    renderProgress();

    const metaAppRow = (await screen.findByText("App de Meta Ads")).closest("li");
    expect(metaAppRow).not.toBeNull();
    expect(metaAppRow?.textContent).toContain("Listo");
  });
});
