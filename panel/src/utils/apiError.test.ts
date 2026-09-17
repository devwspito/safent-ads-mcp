import { describe, expect, it } from "vitest";
import { ApiRequestError } from "@/api/client";
import { describeApiError } from "./apiError";

describe("describeApiError", () => {
  it("403 pide permiso, no un código", () => {
    expect(describeApiError(new ApiRequestError("forbidden", "FORBIDDEN", 403))).toBe("No tienes permiso para ver esto.");
  });

  it("404 dice que no existe, no un código", () => {
    expect(describeApiError(new ApiRequestError("not found", "NOT_FOUND", 404))).toMatch(/no exista/);
  });

  it("409 explica el conflicto de edición concurrente", () => {
    expect(describeApiError(new ApiRequestError("conflict", "CONFLICT", 409))).toMatch(/cambió mientras tanto/);
  });

  it("429 pide esperar, no reintentar en bucle", () => {
    expect(describeApiError(new ApiRequestError("too many", "RATE_LIMITED", 429))).toMatch(/espera/i);
  });

  it("5xx no culpa al usuario", () => {
    expect(describeApiError(new ApiRequestError("boom", "INTERNAL", 503))).toMatch(/no es nada que hayas hecho/i);
  });

  it("422 conserva el mensaje del servidor (ya es específico del campo)", () => {
    expect(describeApiError(new ApiRequestError("El tipo de evento no es válido.", "VALIDATION_ERROR", 422))).toBe(
      "El tipo de evento no es válido.",
    );
  });

  it("un fallo de red (sin respuesta) se distingue de un fallo del servidor", () => {
    expect(describeApiError(new TypeError("Failed to fetch"))).toMatch(/sin conexión/i);
  });

  it("cualquier otra cosa cae en el mensaje genérico de reintento", () => {
    expect(describeApiError("algo raro")).toBe("Inténtalo de nuevo en unos segundos.");
  });
});
