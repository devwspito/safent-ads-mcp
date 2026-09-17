import { describe, expect, it } from "vitest";
import { accountDisplayName, platformAccountDisplayName, platformLabel, platformShortLabel } from "./platform";

describe("platformLabel/platformShortLabel", () => {
  it("traduce cada plataforma a su etiqueta completa y corta", () => {
    expect(platformLabel("google")).toBe("Google Ads");
    expect(platformLabel("meta")).toBe("Meta Ads");
    expect(platformShortLabel("google")).toBe("Google");
    expect(platformShortLabel("meta")).toBe("Meta");
  });
});

describe("accountDisplayName", () => {
  it("quita el prefijo de plataforma cuando el nombre de cuenta lo repite", () => {
    expect(accountDisplayName("google", "Google Ads — Negocio Ejemplo")).toBe("Negocio Ejemplo");
    expect(accountDisplayName("meta", "Meta Ads — Negocio Ejemplo")).toBe("Negocio Ejemplo");
  });

  it("deja el nombre tal cual si ya viene limpio", () => {
    expect(accountDisplayName("google", "Cuenta Principal")).toBe("Cuenta Principal");
  });

  it("nulo o vacío se propaga como null, nunca una cadena vacía visible", () => {
    expect(accountDisplayName("google", null)).toBeNull();
    expect(accountDisplayName("google", undefined)).toBeNull();
  });
});

describe("platformAccountDisplayName", () => {
  it("cuando label es la misma referencia cruda que platform_account_id, cae a «Cuenta <external_account_id>» (bug real de /platform-accounts)", () => {
    const rawRef = "google:account:5e1a6c8e-2f3d-4b7a-9c1e-8f2b6a7d4c10:9b3f2a71-6d4c-4e8a-b1f0-7c5e3a9d2f44:1000000001";
    expect(platformAccountDisplayName({ platform: "google", label: rawRef, platform_account_id: rawRef, external_account_id: "1000000001" })).toBe("Cuenta 1000000001");
  });

  it("con una cuenta de Meta, cae al identificador act_… tal cual, nunca a la referencia cruda", () => {
    expect(platformAccountDisplayName({ platform: "meta", label: "meta:act_100000000000002", platform_account_id: "meta:act_100000000000002", external_account_id: "act_100000000000002" })).toBe("Cuenta act_100000000000002");
  });

  it("un label ya elegible se deja tal cual", () => {
    expect(platformAccountDisplayName({ platform: "google", label: "Google Ads — Negocio Ejemplo", platform_account_id: "google:100-000-0002", external_account_id: "100-000-0002" })).toBe("Negocio Ejemplo");
  });
});
