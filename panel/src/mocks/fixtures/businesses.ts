import type { Business } from "@/api/schemas";

export const MOCK_OWNER = {
  owner_id: "owner_demo",
  email: "owner@negocio-ejemplo.es",
};

export const MOCK_BUSINESSES: Business[] = [
  { business_id: "biz_ejemplo", name: "Negocio Ejemplo" },
  { business_id: "biz_norte", name: "Tienda Norte" },
];

// MOCK_BUSINESSES es un literal fijo y no vacío definido arriba en este mismo módulo.
export const DEFAULT_MOCK_BUSINESS: Business = MOCK_BUSINESSES[0]!;

export const MOCK_PASSWORD = "demo1234";
// Spec 002 (mcp_oauth): `X-Reauth-Token` de las mutaciones del panel
// OAuth (consentimiento y revocacion de agentes). El login ya no pide
// TOTP tras la lane/003; esta re-autenticacion por accion si.
export const MOCK_TOTP_CODE = "123456";
