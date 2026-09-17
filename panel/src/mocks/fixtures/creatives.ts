import { placeholderCreativeUri } from "@/utils/placeholderImage";

const FORMAT_SIZE: Record<string, [number, number]> = {
  "1080x1920": [1080, 1920],
  "1080x1080": [1080, 1080],
  "1200x628": [1200, 628],
  "300x250": [300, 250],
  "728x90": [728, 90],
};

type ReviewState = "pending" | "approved" | "rejected";

interface CreativeSeed {
  asset_id: string;
  media_kind: "image" | "video" | "banner" | "audio";
  format: keyof typeof FORMAT_SIZE;
  signal: "FATIGUE" | "WINNER" | "LOSER" | "LEARNING";
  policy_verdict: "PASS" | "FAIL" | "PENDING";
  policy_findings: string[];
  spend: number;
  hook_rate_pct: number | null;
  hold_rate_pct: number | null;
  frequency: number | null;
  days_in_rotation: number;
  ads_running_on: string[];
  review_state: ReviewState;
  label: string;
}

const SEEDS: CreativeSeed[] = [
  {
    asset_id: "cr_001",
    media_kind: "video",
    format: "1080x1920",
    signal: "WINNER",
    policy_verdict: "PASS",
    policy_findings: [],
    spend: 412,
    hook_rate_pct: 38.2,
    hold_rate_pct: 21.4,
    frequency: 1.8,
    days_in_rotation: 12,
    ads_running_on: ["google:ad:ad-precio-1", "meta:ad:ad-testimonios-2"],
    review_state: "approved",
    label: "Testimonio Secundaria 15s",
  },
  {
    asset_id: "cr_002",
    media_kind: "video",
    format: "1080x1920",
    signal: "FATIGUE",
    policy_verdict: "PASS",
    policy_findings: [],
    spend: 289,
    hook_rate_pct: 19.1,
    hold_rate_pct: 8.4,
    frequency: 5.2,
    days_in_rotation: 24,
    ads_running_on: ["google:ad:ad-plazas-2"],
    review_state: "approved",
    label: "Calendario Primaria 20s",
  },
  {
    asset_id: "cr_003",
    media_kind: "image",
    format: "1080x1080",
    signal: "LOSER",
    policy_verdict: "PASS",
    policy_findings: [],
    spend: 96,
    hook_rate_pct: 6.8,
    hold_rate_pct: null,
    frequency: 3.1,
    days_in_rotation: 9,
    ads_running_on: ["google:ad:ad-precio-1"],
    review_state: "approved",
    label: "Plazas limitadas EOI",
  },
  {
    asset_id: "cr_004",
    media_kind: "banner",
    format: "1200x628",
    signal: "WINNER",
    policy_verdict: "PASS",
    policy_findings: [],
    spend: 158,
    hook_rate_pct: null,
    hold_rate_pct: null,
    frequency: 2.4,
    days_in_rotation: 6,
    ads_running_on: ["meta:ad:ad-testimonios-2"],
    review_state: "approved",
    label: "Banner FP Informática",
  },
  {
    asset_id: "cr_005",
    media_kind: "video",
    format: "1080x1920",
    signal: "LEARNING",
    policy_verdict: "PENDING",
    policy_findings: [],
    spend: 34,
    hook_rate_pct: null,
    hold_rate_pct: null,
    frequency: 0.6,
    days_in_rotation: 2,
    ads_running_on: [],
    review_state: "pending",
    label: "Nueva pieza: precio 2026 (generada)",
  },
  {
    asset_id: "cr_006",
    media_kind: "image",
    format: "1080x1080",
    signal: "LEARNING",
    policy_verdict: "FAIL",
    policy_findings: ["Texto superpuesto ocupa más del 20 % del área (norma Meta)."],
    spend: 0,
    hook_rate_pct: null,
    hold_rate_pct: null,
    frequency: null,
    days_in_rotation: 0,
    ads_running_on: [],
    review_state: "pending",
    label: "Nueva pieza: plazas 2026 (generada)",
  },
];

export interface CreativesFixtureFilters {
  signal?: string;
  pendingApproval?: boolean;
}

export function listCreatives(filters: CreativesFixtureFilters) {
  const items = SEEDS.filter((seed) => !filters.signal || seed.signal === filters.signal)
    .filter((seed) => filters.pendingApproval === undefined || (seed.review_state === "pending") === filters.pendingApproval)
    .map((seed) => {
      const [width, height] = FORMAT_SIZE[seed.format] ?? [1080, 1080];
      return {
        asset_id: seed.asset_id,
        business_id: "biz_ejemplo",
        label: seed.label,
        media_kind: seed.media_kind,
        format: seed.format,
        preview_url: placeholderCreativeUri(width, height, seed.label),
        signal: seed.signal,
        policy_verdict: seed.policy_verdict,
        policy_findings: seed.policy_findings,
        review_state: seed.review_state,
        spend: { amount: seed.spend, currency: "EUR" },
        hook_rate_pct: seed.hook_rate_pct,
        hold_rate_pct: seed.hold_rate_pct,
        frequency: seed.frequency,
        days_in_rotation: seed.days_in_rotation,
        ads_running_on: seed.ads_running_on,
        signal_id: `sig_${seed.asset_id}`,
        brief_id: `brief_${seed.asset_id}`,
      };
    });
  return { items };
}

function findSeed(assetId: string): CreativeSeed | undefined {
  return SEEDS.find((seed) => seed.asset_id === assetId);
}

/** `reject` es estado NUESTRO (`review_state`): nunca toca plataforma (rest-api.md §Creatividades). */
export function rejectCreative(assetId: string): boolean {
  const seed = findSeed(assetId);
  if (!seed) return false;
  seed.review_state = "rejected";
  return true;
}

/** Publicar crea una propuesta y, si se aprueba, deja la pieza en `approved` (nunca escribe directo, FR-33). */
export function approveCreative(assetId: string): boolean {
  const seed = findSeed(assetId);
  if (!seed) return false;
  seed.review_state = "approved";
  return true;
}

/** Regenerar deja la pieza otra vez en el carril "Por aprobar", pendiente de revisión de política. */
export function regenerateCreative(assetId: string): boolean {
  const seed = findSeed(assetId);
  if (!seed) return false;
  seed.review_state = "pending";
  seed.policy_verdict = "PENDING";
  return true;
}
