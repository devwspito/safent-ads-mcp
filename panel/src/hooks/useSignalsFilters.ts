import { useCallback, useMemo } from "react";
import { useSearchParams } from "react-router-dom";

export interface SignalsFilterState {
  platform: string | undefined;
  kind: string | undefined;
  minStrength: number | undefined;
  since: string | undefined;
}

const SINCE_OPTIONS = ["hoy", "7d", "14d", "30d"] as const;
export type SinceOption = (typeof SINCE_OPTIONS)[number];

function sinceToIso(option: string | null): string | undefined {
  if (!option || !SINCE_OPTIONS.includes(option as SinceOption)) return undefined;
  const now = new Date();
  if (option === "hoy") return new Date(now.getFullYear(), now.getMonth(), now.getDate()).toISOString();
  const days = { "7d": 7, "14d": 14, "30d": 30 }[option as "7d" | "14d" | "30d"];
  return new Date(now.getTime() - days * 86_400_000).toISOString();
}

/** Filtros del feed de Señales, sincronizados a la URL — panel-interaction-spec.md §3.3. */
export function useSignalsFilters() {
  const [searchParams, setSearchParams] = useSearchParams();

  const filters = useMemo<SignalsFilterState>(
    () => ({
      platform: searchParams.get("platform") ?? undefined,
      kind: searchParams.get("kind") ?? undefined,
      minStrength: searchParams.has("min_strength") ? Number(searchParams.get("min_strength")) : undefined,
      since: sinceToIso(searchParams.get("since")),
    }),
    [searchParams],
  );

  const sinceOption = (searchParams.get("since") as SinceOption | null) ?? undefined;

  const setFilter = useCallback(
    (key: "platform" | "kind" | "min_strength" | "since", value: string | undefined) => {
      setSearchParams(
        (prev) => {
          const next = new URLSearchParams(prev);
          if (value === undefined || value === "") next.delete(key);
          else next.set(key, value);
          return next;
        },
        { replace: true },
      );
    },
    [setSearchParams],
  );

  return { filters, sinceOption, setFilter };
}
