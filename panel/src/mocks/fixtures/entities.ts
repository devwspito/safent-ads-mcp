import type { EntityBadge, EntityChildRow, EntityChildrenResponse, EntityLevel } from "@/api/schemas";
import { makeRng, pickFrom, series } from "./deterministicRandom";

const EUR = "EUR";

const LEVEL_ORDER: EntityLevel[] = ["campaign", "ad_set", "ad", "creative"];

const AD_SET_NAMES = ["Búsqueda genérica", "Remarketing 30 d", "Lookalike clientes", "Interés categoría"];
const AD_NAMES = ["Anuncio responsive · precio", "Anuncio responsive · plazas", "Carrusel testimonios"];
const CREATIVE_NAMES = ["Vídeo testimonio 15s", "Banner calendario", "Imagen plazas limitadas"];

function parseRef(entityRef: string): { platform: string; level: EntityLevel; id: string } {
  const [platform, level, ...rest] = entityRef.split(":");
  const parsedLevel = LEVEL_ORDER.includes(level as EntityLevel) ? (level as EntityLevel) : "campaign";
  return { platform: platform ?? "google", level: parsedLevel, id: rest.join(":") };
}

function childLevel(level: EntityLevel): EntityLevel | null {
  const index = LEVEL_ORDER.indexOf(level);
  if (index < 0 || index >= LEVEL_ORDER.length - 1) return null;
  return LEVEL_ORDER[index + 1] ?? null;
}

function namesForLevel(level: EntityLevel): string[] {
  if (level === "ad_set") return AD_SET_NAMES;
  if (level === "ad") return AD_NAMES;
  return CREATIVE_NAMES;
}

function badgesFor(rng: () => number, level: EntityLevel): EntityBadge[] {
  const badges: EntityBadge[] = [];
  if (level === "ad_set" && rng() > 0.7) badges.push("presupuesto_compartido");
  if (rng() > 0.75) badges.push("en_aprendizaje");
  if (rng() > 0.9) badges.push("obsoleto");
  return badges;
}

const SIGNAL_KINDS = ["BUY", "HOLD", "SELL", "EXIT"] as const;
const CAUSES = [
  "Coste por lead bajo el objetivo en 7 días",
  "Frecuencia sobre 3,5 con caída de CTR en 5 días",
  "Sin conversiones con gasto sostenido 5 días",
  "Mejora sostenida de coste por conversión en 14 días",
];

export function buildEntityChildren(entityRef: string): EntityChildrenResponse {
  const parsed = parseRef(entityRef);
  const level = childLevel(parsed.level) ?? "creative";
  const rng = makeRng(entityRef);
  const names = namesForLevel(level);
  const count = 2 + Math.floor(rng() * 3);

  const items: EntityChildRow[] = Array.from({ length: count }, (_, index) => {
    const childRef = `${parsed.platform}:${level}:${parsed.id}-${level}-${index + 1}`;
    const spendToday = Math.round(rng() * 40 * 100) / 100;
    const hasSignal = rng() > 0.3;
    return {
      entity_ref: childRef,
      level,
      name: `${pickFrom(names, (index % names.length) / names.length)} #${index + 1}`,
      status: rng() > 0.85 ? "PAUSED" : "ACTIVE",
      spend_today: { amount: spendToday, currency: EUR },
      spend_window: { amount: Math.round(spendToday * 6.5 * 100) / 100, currency: EUR },
      conversions_by_kind: {
        lead: Math.floor(rng() * 12),
        whatsapp: Math.floor(rng() * 6),
        call: Math.floor(rng() * 3),
        business_conversion: Math.floor(rng() * 2),
      },
      cost_per_lead: rng() > 0.15 ? { amount: Math.round((12 + rng() * 20) * 100) / 100, currency: EUR } : null,
      cost_per_business_conversion: null,
      signal: hasSignal
        ? {
            kind: pickFrom(SIGNAL_KINDS, rng()),
            strength: Math.floor(rng() * 100),
            cause: pickFrom(CAUSES, rng()),
          }
        : null,
      freshness: { last_ingested_at: new Date(Date.now() - Math.floor(rng() * 40) * 60_000).toISOString(), lag_minutes: Math.floor(rng() * 40), is_stale: false, no_data: false },
      badges: badgesFor(rng, level),
      has_children: level !== "creative",
    };
  });

  return {
    parent_ref: entityRef,
    parent_name: parsed.id.replace(/-/g, " "),
    level,
    items,
  };
}

export function entitySparkline(entityRef: string): number[] {
  return series(entityRef, 14, 10 + (makeRng(entityRef)() * 20), 0.4);
}
