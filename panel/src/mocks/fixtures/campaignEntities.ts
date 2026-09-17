/** Entidades compartidas entre fixtures (Señales, Propuestas, Creatividades, Registro) para que el mismo nombre aparezca en toda la app. */
export interface CampaignEntitySeed {
  ref: string;
  name: string;
  platform: "google" | "meta";
}

export const CAMPAIGN_ENTITIES: CampaignEntitySeed[] = [
  { ref: "google:campaign:c-brand-search", name: "Búsqueda Marca", platform: "google" },
  { ref: "google:campaign:c-display-retargeting", name: "Display Retargeting", platform: "google" },
  { ref: "meta:campaign:c-meta-prospeccion", name: "Meta Prospección", platform: "meta" },
  { ref: "meta:campaign:c-meta-lookalike", name: "Meta Lookalike Clientes", platform: "meta" },
  { ref: "google:campaign:c-busqueda-generica", name: "Búsqueda Genérica", platform: "google" },
  { ref: "meta:campaign:c-meta-advantage-catalogo", name: "Meta Advantage+ Catálogo", platform: "meta" },
  { ref: "google:campaign:c-busqueda-competencia", name: "Búsqueda Competencia", platform: "google" },
];
