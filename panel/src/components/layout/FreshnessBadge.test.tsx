import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { FreshnessBadge } from "./FreshnessBadge";

/** Hotfix 0.2.20 Bug B: an account with no ingested statistics yet is not
 * "stale data" — it must read "Sin estadísticas todavía", never a
 * fictitious age of tens of thousands of hours. */
describe("FreshnessBadge", () => {
  it("shows a pending-data message for no_data, not a sentinel age", () => {
    render(<FreshnessBadge lagMinutes={0} noData />);
    expect(screen.getByText("Sin estadísticas todavía")).toBeInTheDocument();
  });

  it("still shows the legacy sentinel defensively when no_data is absent", () => {
    render(<FreshnessBadge lagMinutes={1_000_000} />);
    expect(screen.getByText("Estadísticas pendientes")).toBeInTheDocument();
  });

  it("shows a real age for genuinely stale data", () => {
    render(<FreshnessBadge lagMinutes={125} />);
    expect(screen.getByText(/hace 2 h/)).toBeInTheDocument();
  });
});
