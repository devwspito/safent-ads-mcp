import { useCallback, useMemo } from "react";
import { useSearchParams } from "react-router-dom";

const STORAGE_KEY = "safent_business_id";

/**
 * Negocio seleccionado — sincronizado a la URL de la vista activa y recordado en
 * localStorage para que la navegación entre vistas no lo pierda (panel-interaction-spec.md §1).
 */
export function useBusinessFilter(businesses: Array<{ business_id: string; name: string }> | undefined) {
  const [searchParams, setSearchParams] = useSearchParams();

  const businessId = useMemo(() => {
    const fromUrl = searchParams.get("business_id");
    if (fromUrl && businesses?.some((b) => b.business_id === fromUrl)) return fromUrl;
    let fromStorage: string | null = null;
    try { fromStorage = localStorage.getItem(STORAGE_KEY); } catch { /* Storage is optional. */ }
    if (fromStorage && businesses?.some((b) => b.business_id === fromStorage)) return fromStorage;
    return businesses?.[0]?.business_id ?? "";
  }, [searchParams, businesses]);

  const setBusinessId = useCallback(
    (id: string) => {
      if (!businesses?.some((business) => business.business_id === id)) return;
      try { localStorage.setItem(STORAGE_KEY, id); } catch { /* URL remains authoritative UI state. */ }
      setSearchParams(
        (prev) => {
          const next = new URLSearchParams(prev);
          next.set("business_id", id);
          return next;
        },
        { replace: true },
      );
    },
    [setSearchParams, businesses],
  );

  return { businessId, setBusinessId };
}
