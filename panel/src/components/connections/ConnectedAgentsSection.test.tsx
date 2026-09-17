import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { setMockSessionForTests } from "@/mocks/handlers";
import { useRevokeGrantFlow } from "@/hooks/useRevokeGrantFlow";
import { ConnectedAgentsSection } from "./ConnectedAgentsSection";

vi.mock("@/hooks/useRevokeGrantFlow", () => ({ useRevokeGrantFlow: vi.fn() }));

function renderSection() {
  setMockSessionForTests(true);
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <ConnectedAgentsSection />
    </QueryClientProvider>,
  );
}

/**
 * `useRevokeGrantFlow` (T062, code review de PR #20): un 5xx o una identificación fresca perdida
 * en la llamada final de confirmación puede dejar `confirmError` puesto con el diálogo ya cerrado
 * — sin más código que un `errorMessage` leído solo por `ActionConfirmationDialog`, ese error
 * queda invisible ("el clic no hace nada"). Este caso simula ese estado directamente sobre el
 * hook (en vez de esperar a que un timing exacto lo produzca de verdad) para comprobar que la
 * sección lo enseña de todos modos, fuera de cualquier diálogo.
 */
describe("ConnectedAgentsSection — error del tercer POST visible fuera del diálogo", () => {
  it("muestra `confirmError` en la alerta general aunque el segundo diálogo ya esté cerrado", async () => {
    vi.mocked(useRevokeGrantFlow).mockReturnValue({
      presence: {
        isPromptOpen: false,
        methods: [],
        isSubmitting: false,
        errorMessage: null,
        isStartingGoogle: false,
        federatedStartError: null,
        start: vi.fn(),
        confirm: vi.fn(),
        confirmWithGoogle: vi.fn(),
        retryFederatedStart: vi.fn(),
        cancel: vi.fn(),
      },
      isConfirmationOpen: false,
      confirmSubmitting: false,
      confirmError: "El servidor ha tenido un problema. No es nada que hayas hecho.",
      confirmExpiresAt: null,
      confirm: vi.fn(),
      cancelConfirmation: vi.fn(),
    });

    renderSection();

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "El servidor ha tenido un problema. No es nada que hayas hecho.",
    );
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });
});
