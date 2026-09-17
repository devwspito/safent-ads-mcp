import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { DegradedBanner } from "./DegradedBanner";
import { EmptyState } from "./EmptyState";
import { ErrorState } from "./ErrorState";
import { KillSwitchBanner } from "./KillSwitchBanner";
import { ReloadingIndicator } from "./ReloadingIndicator";
import { Skeleton } from "./Skeleton";
import { StaleBanner } from "./StaleBanner";

describe("Estados comunes", () => {
  it("EmptyState muestra título, cuerpo y acción", () => {
    render(<EmptyState title="Sin cuentas" body="Conecta una cuenta" action={<button>Ir</button>} />);
    expect(screen.getByText("Sin cuentas")).toBeInTheDocument();
    expect(screen.getByText("Conecta una cuenta")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Ir" })).toBeInTheDocument();
  });

  it("ErrorState invoca onRetry al pulsar Reintentar", async () => {
    const onRetry = vi.fn();
    const user = userEvent.setup();
    render(<ErrorState message="Fallo de red" onRetry={onRetry} />);

    await user.click(screen.getByRole("button", { name: "Reintentar" }));
    expect(onRetry).toHaveBeenCalledOnce();
  });

  it("StaleBanner declara el motivo y no permite descartarse", () => {
    render(<StaleBanner lagMinutes={125} />);
    expect(screen.getByRole("status")).toHaveTextContent(/hace 2 h/);
    expect(screen.getByText(/escritura deshabilitada/i)).toBeInTheDocument();
  });

  it("DegradedBanner marca la cuenta caída sin ocultar las demás", () => {
    render(<DegradedBanner accountLabel="Meta Ads — Negocio Ejemplo" reason="en solo lectura desde hace 1 h" />);
    expect(screen.getByText(/Meta Ads — Negocio Ejemplo/)).toBeInTheDocument();
    expect(screen.getByText(/las demás cuentas siguen operativas/i)).toBeInTheDocument();
  });

  it("KillSwitchBanner es una franja persistente con reanudar", async () => {
    const onRequestRelease = vi.fn();
    const user = userEvent.setup();
    render(<KillSwitchBanner engagedAt="2026-09-09T10:00:00.000Z" reason="Gasto anómalo" onRequestRelease={onRequestRelease} />);

    expect(screen.getByRole("alert")).toHaveTextContent(/Los cambios están parados desde/);
    await user.click(screen.getByRole("button", { name: "Reanudar los cambios" }));
    expect(onRequestRelease).toHaveBeenCalledOnce();
  });

  it("ReloadingIndicator anuncia el refresco sin bloquear el contenido", () => {
    render(<ReloadingIndicator />);
    expect(screen.getByRole("status")).toHaveTextContent("Actualizando…");
  });

  it("Skeleton se renderiza oculto a lectores de pantalla", () => {
    const { container } = render(<Skeleton height="40px" />);
    expect(container.querySelector('[aria-hidden="true"]')).toBeInTheDocument();
  });
});
