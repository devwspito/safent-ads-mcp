/**
 * Filtros del cockpit de Propuestas — sincronizados a la URL, con la lente persistida en
 * localStorage. Portado de `oposads-agent/panel/src/hooks/useCockpitFilters.ts`
 * (panel-interaction-spec.md §8).
 */
import { useCallback, useMemo } from "react";
import { useSearchParams } from "react-router-dom";

export type CockpitLens = "urgency" | "calendar_event";

export interface CockpitFilters {
  platform?: string;
  state?: string;
}

const LENS_KEY = "cockpit_lens";

function readLensFromStorage(): CockpitLens {
  const raw = localStorage.getItem(LENS_KEY);
  return raw === "calendar_event" ? "calendar_event" : "urgency";
}

export function useCockpitFilters() {
  const [searchParams, setSearchParams] = useSearchParams();

  const lens = useMemo<CockpitLens>(() => {
    const raw = searchParams.get("lens");
    if (raw === "calendar_event" || raw === "urgency") return raw;
    return readLensFromStorage();
  }, [searchParams]);

  const filters = useMemo<CockpitFilters>(
    () => ({
      platform: searchParams.get("platform") ?? undefined,
      state: searchParams.get("state") ?? undefined,
    }),
    [searchParams],
  );

  const setLens = useCallback(
    (next: CockpitLens) => {
      localStorage.setItem(LENS_KEY, next);
      setSearchParams(
        (prev) => {
          const params = new URLSearchParams(prev);
          params.set("lens", next);
          return params;
        },
        { replace: true },
      );
    },
    [setSearchParams],
  );

  const setFilter = useCallback(
    (key: keyof CockpitFilters, value: string | undefined) => {
      setSearchParams(
        (prev) => {
          const params = new URLSearchParams(prev);
          if (value == null || value === "") params.delete(key);
          else params.set(key, value);
          return params;
        },
        { replace: true },
      );
    },
    [setSearchParams],
  );

  return { lens, filters, setLens, setFilter };
}
