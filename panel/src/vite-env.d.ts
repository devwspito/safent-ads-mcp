/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_API_MOCK?: "0" | "1";
  readonly VITE_API_BASE?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}

/**
 * Inyectados por Safent cuando sirve el panel empotrado bajo `/ads/*`
 * (plan.md §5, T003): base de las rutas y si corre dentro del puente mismo-origen.
 */
interface Window {
  __ADS_BASE_PATH__?: string;
  __ADS_EMBEDDED__?: boolean;
}
