import { describe, expect, it } from "vitest";
import { ApiRequestError } from "@/api/client";
import type { HardCapsView, SpendEnvelope } from "@/api/schemas/hardCaps";
import {
  describeAccountsLeft,
  describeChangesLeftToday,
  describeClampedField,
  describeHardCapsError,
  formatMinorForInput,
  parseMajorToMinor,
  raisesTheEffectiveCap,
  validateCapsForm,
  withdrawalRaisesTheEffectiveCap,
} from "./hardCaps";

const envelope: SpendEnvelope = {
  max_daily_cap_minor: 5000,
  max_monthly_cap_minor: 100_000,
  max_ceiling_minor: 20_000,
  min_floor_minor: 500,
  max_accounts: 3,
  accounts_used: 2,
  max_cap_changes_per_day: 10,
  cap_changes_today: 9,
  currency: "EUR",
};

function viewWith(overrides: Partial<HardCapsView>): HardCapsView {
  return {
    platform_account_id: "100-000-0002",
    source: "file_and_panel",
    writable: true,
    currency: "EUR",
    effective: { daily_cap_minor: 4000, monthly_cap_minor: 20_000, floor_minor: 200, ceiling_minor: 9000 },
    clamped_by: [],
    panel_state_available: true,
    envelope,
    ...overrides,
  };
}

describe("importes en unidad menor", () => {
  it("convierte la unidad mayor escrita a mano, con coma o con punto", () => {
    expect(parseMajorToMinor("40")).toBe(4000);
    expect(parseMajorToMinor("12,5")).toBe(1250);
    expect(parseMajorToMinor("12.50")).toBe(1250);
    expect(parseMajorToMinor(" 0 ")).toBe(0);
  });

  it("rechaza todo lo que el contrato rechazaría, en vez de mandarlo y esperar el 400", () => {
    expect(parseMajorToMinor("")).toBeNull();
    expect(parseMajorToMinor("-1")).toBeNull();
    expect(parseMajorToMinor("1,234")).toBeNull();
    expect(parseMajorToMinor("1e3")).toBeNull();
    expect(parseMajorToMinor("cuarenta")).toBeNull();
    expect(parseMajorToMinor("99999999999999")).toBeNull();
  });

  it("devuelve el campo a la unidad mayor sin coma flotante", () => {
    expect(formatMinorForInput(4000)).toBe("40");
    expect(formatMinorForInput(1205)).toBe("12,05");
  });
});

describe("validación del formulario contra el sobre", () => {
  it("acepta tres importes dentro del sobre y pone la divisa del sobre, nunca un literal", () => {
    const { caps, errors } = validateCapsForm({ daily: "40", monthly: "200", ceiling: "90" }, envelope);
    expect(errors).toEqual({});
    expect(caps).toEqual({
      daily_cap_minor: 4000,
      monthly_cap_minor: 20_000,
      ceiling_minor: 9000,
      currency: "EUR",
    });
  });

  it("nombra el campo que pasa del sobre y no envía nada", () => {
    const { caps, errors } = validateCapsForm({ daily: "60", monthly: "200", ceiling: "90" }, envelope);
    expect(errors.daily).toMatch(/Como mucho 50,00\s€ al día\./);
    expect(caps).toBeNull();
  });

  it("rechaza un mensual menor que el diario y un diario por encima del techo", () => {
    expect(validateCapsForm({ daily: "40", monthly: "10", ceiling: "90" }, envelope).errors.monthly).toBe(
      "El tope mensual no puede ser menor que el diario.",
    );
    expect(validateCapsForm({ daily: "40", monthly: "200", ceiling: "30" }, envelope).errors.daily).toBe(
      "El tope diario no puede pasar del techo.",
    );
  });
});

describe("qué cuenta como subir (definición normativa del servidor)", () => {
  it("fijar el primer tope de una cuenta sin tope sube siempre", () => {
    const view = viewWith({ source: "none", writable: false, effective: null });
    expect(
      raisesTheEffectiveCap(view, {
        daily_cap_minor: 1,
        monthly_cap_minor: 1,
        ceiling_minor: 1,
        currency: "EUR",
      }),
    ).toBe(true);
  });

  it("bajar los tres importes no es subir; pasarse en uno solo, sí", () => {
    const view = viewWith({});
    const lower = { daily_cap_minor: 3000, monthly_cap_minor: 20_000, ceiling_minor: 9000, currency: "EUR" };
    expect(raisesTheEffectiveCap(view, lower)).toBe(false);
    expect(raisesTheEffectiveCap(view, { ...lower, ceiling_minor: 9001 })).toBe(true);
  });

  it("retirar el tope del panel sube solo cuando hay entrada de fichero debajo", () => {
    expect(withdrawalRaisesTheEffectiveCap(viewWith({ source: "file_and_panel" }))).toBe(true);
    expect(withdrawalRaisesTheEffectiveCap(viewWith({ source: "panel" }))).toBe(false);
  });
});

describe("lo que se dice en pantalla", () => {
  it("dice qué quedó recortado sin inventarse el importe guardado", () => {
    const view = viewWith({ clamped_by: ["daily_cap_minor"] });
    expect(describeClampedField(view, "daily_cap_minor")).toMatch(/^Recortado: en vigor 40,00\s€\.$/);
    expect(describeClampedField(view, "monthly_cap_minor")).toBeNull();
  });

  it("con el importe guardado del servidor, lo dice entero", () => {
    const view = viewWith({
      clamped_by: ["daily_cap_minor"],
      from_panel: { daily_cap_minor: 5000, monthly_cap_minor: 20_000, ceiling_minor: 9000 },
    });
    expect(describeClampedField(view, "daily_cap_minor")).toMatch(/^Guardado 50,00\s€, en vigor 40,00\s€\.$/);
  });

  it("cuenta lo que queda del sobre en singular, plural y agotado", () => {
    expect(describeAccountsLeft(envelope)).toBe("Queda 1 cuenta de 3 con tope del panel.");
    expect(describeAccountsLeft({ ...envelope, accounts_used: 0 })).toBe("Quedan 3 cuentas de 3 con tope del panel.");
    expect(describeAccountsLeft({ ...envelope, accounts_used: 3 })).toBe(
      "Sin sitio: ya hay 3 cuentas con tope del panel.",
    );
    expect(describeChangesLeftToday(envelope)).toBe("Queda 1 cambio de tope para hoy, de 10.");
    expect(describeChangesLeftToday({ ...envelope, cap_changes_today: 10 })).toBe(
      "No queda ningún cambio de tope para hoy.",
    );
  });

  it("traduce cada código de esta API, y deja hablar al servidor cuando nombra el campo", () => {
    expect(describeHardCapsError(new ApiRequestError("x", "ENVELOPE_EXCEEDED", 409))).toBe(
      "Ese importe supera el sobre declarado en config/caps.yaml.",
    );
    expect(describeHardCapsError(new ApiRequestError("x", "BROKER_UNAVAILABLE", 503))).toBe(
      "El servicio de topes no responde. No se ha cambiado nada.",
    );
    expect(
      describeHardCapsError(new ApiRequestError("Campo floor_minor: el panel no fija este campo.", "INVALID_CAPS", 400)),
    ).toBe("Campo floor_minor: el panel no fija este campo.");
  });
});
